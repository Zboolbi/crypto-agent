"""
Apprentissage en ligne pour le scalping.

Une régression logistique par profil de trade estime la probabilité qu'un short atteigne
son objectif avant son stop, frais compris, à partir de l'état du marché au moment du signal.
Elle est mise à jour après chaque trade réel et après chaque trade « fantôme » : un signal
suivi virtuellement, sans capital engagé, pour apprendre beaucoup plus vite des schémas
qui mènent aux pertes.

Bibliothèque standard uniquement ; l'état est sérialisable en JSON.
"""

import math

FEATURES = [
    "flux_vendeur",        # part nette des ventes agressives sur la fenêtre
    "baisse_5s",           # baisse récente (en 10 pb)
    "baisse_30s",
    "carnet_vendeur",      # déséquilibre des 5 meilleurs niveaux (plus d'offres que de demandes)
    "ecart_bps",           # écart achat / vente
    "volatilite_bps",
    "volume_log",          # volume échangé dans la fenêtre (log10 $)
    "intensite_log",       # nombre de transactions dans la fenêtre (log10)
    "tendance_baissiere",  # score 15 min inversé, ramené à [-1, 1]
    "financement",         # taux de financement (pb) : positif = les longs paient les shorts
    "historique_crypto",   # résultat moyen récent sur cette crypto (en 10 pb)
]
# L'heure n'est pas utilisée : sur quelques heures d'historique, elle se comporte comme une
# constante et le modèle lui attribue à tort les résultats de la séance.

LABELS = {
    "flux_vendeur": "pression vendeuse",
    "baisse_5s": "baisse sur 5 s",
    "baisse_30s": "baisse sur 30 s",
    "carnet_vendeur": "carnet chargé côté vente",
    "ecart_bps": "écart achat/vente large",
    "volatilite_bps": "volatilité élevée",
    "volume_log": "volume élevé",
    "intensite_log": "transactions nombreuses",
    "tendance_baissiere": "tendance 15 min baissière",
    "financement": "financement favorable au short",
    "historique_crypto": "crypto gagnante récemment",
}


def sigmoid(s):
    s = max(-30.0, min(30.0, s))
    return 1.0 / (1.0 + math.exp(-s))


class OnlineScaler:
    """Moyenne et variance glissantes (Welford) pour standardiser les variables."""

    def __init__(self, n):
        self.count, self.mean, self.m2 = 0, [0.0] * n, [0.0] * n

    def update(self, x):
        self.count += 1
        for i, v in enumerate(x):
            d = v - self.mean[i]
            self.mean[i] += d / self.count
            self.m2[i] += d * (v - self.mean[i])

    def transform(self, x):
        out = []
        for i, v in enumerate(x):
            sd = math.sqrt(self.m2[i] / (self.count - 1)) if self.count > 1 else 1.0
            z = (v - self.mean[i]) / sd if sd > 1e-12 else 0.0
            out.append(max(-5.0, min(5.0, z)))
        return out


class OnlineLogit:
    """Régression logistique à descente de gradient stochastique, régularisée (L2)."""

    def __init__(self, n=None, lr=0.05, l2=1e-3):
        n = n or len(FEATURES)
        self.lr, self.l2 = lr, l2
        self.w, self.b = [0.0] * n, 0.0
        self.scaler = OnlineScaler(n)
        self.seen = 0
        self.accuracy = 0.5    # taux de bonnes prédictions (moyenne mobile)
        self.brier = 0.25      # erreur quadratique des probabilités (moyenne mobile)
        self.base_rate = 0.5   # part de trades gagnants observée (moyenne mobile)

    def predict(self, x):
        z = self.scaler.transform(x)
        return sigmoid(self.b + sum(w * v for w, v in zip(self.w, z)))

    def update(self, x, y):
        p_before = self.predict(x) if self.seen else 0.5  # évaluation honnête : avant d'apprendre
        a = 0.02
        self.accuracy += a * (float((p_before > 0.5) == bool(y)) - self.accuracy)
        self.brier += a * ((p_before - y) ** 2 - self.brier)
        self.base_rate += a * (y - self.base_rate)

        self.scaler.update(x)
        z = self.scaler.transform(x)
        p = sigmoid(self.b + sum(w * v for w, v in zip(self.w, z)))
        lr = self.lr / math.sqrt(1 + self.seen / 500)
        g = p - y
        self.w = [w - lr * (g * v + self.l2 * w) for w, v in zip(self.w, z)]
        self.b -= lr * g
        self.seen += 1

    def explain(self, k=3):
        """Variables les plus influentes : (libellé, poids), favorables puis défavorables."""
        pairs = sorted(zip(FEATURES, self.w), key=lambda p: p[1])
        good = [(LABELS[n], w) for n, w in reversed(pairs[-k:]) if w > 0]
        bad = [(LABELS[n], w) for n, w in pairs[:k] if w < 0]
        return good, bad

    def to_dict(self):
        return {"w": self.w, "b": self.b, "seen": self.seen, "accuracy": self.accuracy, "brier": self.brier,
                "base_rate": self.base_rate,
                "scaler": {"count": self.scaler.count, "mean": self.scaler.mean, "m2": self.scaler.m2}}

    @classmethod
    def from_dict(cls, d):
        m = cls()
        if not d or len(d.get("w", [])) != len(FEATURES):
            return m  # variables modifiées : on repart de zéro
        m.w, m.b, m.seen = d["w"], d["b"], d["seen"]
        m.accuracy, m.brier, m.base_rate = d["accuracy"], d["brier"], d["base_rate"]
        s = d["scaler"]
        m.scaler.count, m.scaler.mean, m.scaler.m2 = s["count"], s["mean"], s["m2"]
        return m
