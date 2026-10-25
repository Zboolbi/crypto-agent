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

CAL_BINS = 10        # calibrage : tranches de probabilité annoncée
CAL_PRIOR = 20       # poids du taux de base dans chaque tranche (lissage)
CAL_DECAY = 0.998    # oubli progressif : le calibrage suit le modèle qui évolue
NOVELTY_MAX = 4.0    # au-delà (en écarts-types), le contexte est jugé inconnu

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
        self.base_rate = 0.5   # part de trades gagnants observée (moyenne mobile)
        self.cal_n = [0.0] * CAL_BINS
        self.cal_w = [0.0] * CAL_BINS
        self.cal_net = [0.0] * CAL_BINS  # résultat net réel cumulé par tranche (pb)
        self.avg_net = 0.0               # résultat net moyen observé (moyenne mobile, pb)

    def predict(self, x):
        z = self.scaler.transform(x)
        return sigmoid(self.b + sum(w * v for w, v in zip(self.w, z)))

    def estimate(self, x):
        """Estimation calibrée sur les résultats réels : probabilité de gain, résultat net moyen (pb)
        et nombre de cas observés dans la tranche. Le résultat net inclut tout ce que la formule
        théorique ignore : glissement du stop, sorties à la durée max ou sur flux inversé."""
        z = self.scaler.transform(x)
        raw = sigmoid(self.b + sum(w * v for w, v in zip(self.w, z)))
        if max(abs(v) for v in z) > NOVELTY_MAX or self.seen < 50:
            return {"p": self.base_rate, "net": self.avg_net, "n": 0.0, "brut": raw}
        i = min(int(raw * CAL_BINS), CAL_BINS - 1)
        n = self.cal_n[i]
        return {"p": (self.cal_w[i] + CAL_PRIOR * self.base_rate) / (n + CAL_PRIOR),
                "net": (self.cal_net[i] + CAL_PRIOR * self.avg_net) / (n + CAL_PRIOR), "n": n, "brut": raw}

    def calibration(self, threshold=0.3):
        """(probabilité brute moyenne annoncée, taux réel) sur les tranches ≥ threshold."""
        n = w = mid = 0.0
        for i in range(int(threshold * CAL_BINS), CAL_BINS):
            n += self.cal_n[i]
            w += self.cal_w[i]
            mid += self.cal_n[i] * (i + 0.5) / CAL_BINS
        return (mid / n, w / n) if n >= 5 else (None, None)

    def update(self, x, y, net=None):
        p_before = self.predict(x) if self.seen else 0.5  # évaluation honnête : avant d'apprendre
        i = min(int(p_before * CAL_BINS), CAL_BINS - 1)
        self.cal_n = [n * CAL_DECAY for n in self.cal_n]
        self.cal_w = [v * CAL_DECAY for v in self.cal_w]
        self.cal_net = [v * CAL_DECAY for v in self.cal_net]
        self.cal_n[i] += 1
        self.cal_w[i] += y
        if net is not None:
            self.cal_net[i] += net
            self.avg_net += 0.02 * (net - self.avg_net)
        self.base_rate += 0.02 * (y - self.base_rate)

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
        return {"w": self.w, "b": self.b, "seen": self.seen,
                "base_rate": self.base_rate, "cal_n": self.cal_n, "cal_w": self.cal_w,
                "cal_net": self.cal_net, "avg_net": self.avg_net,
                "scaler": {"count": self.scaler.count, "mean": self.scaler.mean, "m2": self.scaler.m2}}

    @classmethod
    def from_dict(cls, d):
        m = cls()
        if not d or len(d.get("w", [])) != len(FEATURES):
            return m  # variables modifiées : on repart de zéro
        m.w, m.b, m.seen = d["w"], d["b"], d["seen"]
        m.base_rate = d["base_rate"]
        if d.get("cal_net"):
            m.cal_n, m.cal_w, m.cal_net = d["cal_n"], d["cal_w"], d["cal_net"]
            m.avg_net = d.get("avg_net", 0.0)
        # sinon : ancien format sans résultat net, le calibrage repart de zéro (le modèle est conservé)
        s = d["scaler"]
        m.scaler.count, m.scaler.mean, m.scaler.m2 = s["count"], s["mean"], s["m2"]
        return m
