# Récapitulatif du projet crypto-agent

**État au 25/10/2026, 10:08 (heure de Paris, heure d'hiver depuis cette nuit).**
Ce document est la référence de travail. Le mettre à jour (date et heure en tête) après chaque étape importante, plutôt que de relire tout le code ou tout l'historique.

---

## 1. En une phrase

Un agent de trading crypto **en mode papier (argent fictif)**. Il fait tourner trois stratégies en parallèle sur Binance Futures : scalping (secondes), tendance (heures) et arbitrage de financement (jours). Il a un tableau de bord local et des services macOS. **Aucune stratégie n'est rentable à ce jour.**

## 2. Emplacements

| Quoi | Où |
|---|---|
| Code et données (version active) | `~/crypto-agent` (dépôt git local) |
| Ancienne copie, à ne plus utiliser | `~/Desktop/Projet vibe code` (bloquée par macOS pour les services) |
| Dépôt GitHub (public) | https://github.com/Zboolbi/crypto-agent. Seul le 1er commit `34528ea` est poussé. **L'utilisateur veut garder les commits suivants en local.** |
| Python | `~/crypto-agent/.venv` = Python 3.12.15, installé avec uv (`~/.local/share/uv-bootstrap/bin/uv`). Python 3.9 système uniquement en secours. |
| Dépendances | `requirements.txt` (ccxt 4.5.85), `requirements-dev.txt` (+ ruff 0.17.0) |
| CLI GitHub | `~/.local/bin/gh` 2.102.0, connecté au compte **Zboolbi**. Identité des commits : `ZBooBli <102283985+Zboolbi@users.noreply.github.com>`. |
| Tableau de bord | http://localhost:8050 |

## 3. Fichiers du code (~5 200 lignes, 39 tests)

| Fichier | Rôle |
|---|---|
| `crypto_agent.py` | Cours du top 100 CoinGecko, prévisions statistiques, affichage. C'est la base historique. |
| `sources.py` | Données multi-API (CoinGecko, Coinpaprika, Coinlore, Binance, Bybit, DefiLlama, Kraken), client WebSocket maison, horloge Binance robuste (5 mesures façon NTP), flux répartis par paquets de 50 contrats. |
| `trader.py` | Stratégie **tendance** (score sur bougies de 15 min, long/short, stops suiveurs, optimiseur), courtiers papier et ccxt, `DEFAULT_CONFIG` (**tous les réglages**), commande `service`. |
| `scalper.py` | Stratégie **scalping**. Voir § 5. |
| `learner.py` | Régression logistique en ligne, calibrage par tranches, garde-fou face à l'inconnu (plus de 4 écarts-types). |
| `funding.py` | **Arbitrage de financement** : achat au comptant et vente du perpétuel, backtest sur l'historique réel des taux, moteur papier. |
| `dashboard.py` / `dashboard.html` | Tableau de bord temps réel façon Grafana (bibliothèque standard uniquement). |
| `analyse.py` | Comparaison des 3 stratégies depuis un point de départ (`point-de-depart`, puis `analyse`). |
| `installer_service.sh` / `desinstaller_service.sh` | Services launchd. |
| `tests/` | `test_core.py`, `test_dashboard.py`, `test_funding.py`. |

**Réglages** : les valeurs par défaut sont dans `trader.py` (`DEFAULT_CONFIG`) et les valeurs effectives dans `config_trading.json` (non versionné). Attention : `load_config()` réécrit ce fichier, et une clé déjà présente garde son ancienne valeur. Pour appliquer un nouveau défaut, il faut supprimer ou mettre à jour la clé dans le fichier.

## 4. Services en cours (launchd, mode papier)

| Service | Commande | Journal |
|---|---|---|
| `com.cryptoagent.papier` | `trader.py --mode papier service` → scalping (`strategie = scalping`), via `caffeinate` | `logs/agent_papier.log` |
| `com.cryptoagent.dashboard` | `dashboard.py` | `logs/dashboard.log` |
| `com.cryptoagent.funding` | `funding.py lancer` | `logs/funding_papier.log` |
| `com.cryptoagent.tendance` | `trader.py --mode papier auto` | `logs/tendance_papier.log` |

- Redémarrer un service : `launchctl kickstart -k gui/$(id -u)/com.cryptoagent.<nom>`
- Tout installer : `./installer_service.sh papier`. Tout arrêter : `./desinstaller_service.sh papier`.
- Un verrou (`.scalper_papier.lock`) empêche de lancer deux scalpers à la fois.
- Tester : `.venv/bin/python -m unittest discover -s tests`. Analyse statique : `.venv/bin/ruff check --exclude .venv .`

## 5. Scalping : fonctionnement actuel

1. **Univers** : tous les perpétuels USDT de Binance à plus de 10 M$ de volume quotidien, soit 107 à 130 contrats. Sont exclus les stablecoins et les contrats dont le pas de prix dépasse 3 pb.
2. **Flux** : transactions sur `/market` (aggTrade) et carnet sur `/public` (depth5@100ms). Reconnexion par flux.
3. **Signaux candidats (short uniquement)** : famille « élan » (pression vendeuse et baisse) et famille « rebond » (poussée acheteuse rapide). Profils : précis 8/20 pb sur 60 s, éclair 10/8 pb sur 20 s, rapide 20/15 pb sur 60 s, large 35/25 pb sur 180 s. Entrée par ordre limite ou au marché, soit **16 variantes**.
4. **Trades fantômes** pour chaque candidat. Ils entraînent un modèle par variante.
5. **Simulation réaliste depuis le 25/10 04:20** : aucun ordre n'est exécuté avant la latence aller-retour (au moins 300 ms, ou 2 fois le retard mesuré). Un ordre au marché est exécuté au moins bon prix entre le carnet et la dernière transaction. Un ordre post-only est rejeté si le prix a franchi sa limite. L'objectif n'est actif qu'après confirmation de l'entrée ; le stop est immédiat.
5 bis. **Deux stratégies en parallèle (depuis le 25/10 10:05)**, positions indexées par (contrat, stratégie) :
   - **court** : les 4 profils ci-dessus, familles élan et rebond, **15 % du capital engagés au plus** ;
   - **longue** : profils trente_s (30/25 pb, 30 s), une_min (45/35 pb, 1 min), cinq_min (90/60 pb, 5 min) et dix_min (140/90 pb, 10 min) ; familles élan, rebond et **tendance** (score 15 min ≤ −40 et pression vendeuse) ; **10 % engagés au plus** ; fantômes au plus toutes les 10 s par crypto. Réglages `longue_*` et `scalp_profils_longs`.
6. **Trade réel décidé par le modèle** seulement si :
   - le modèle est prêt (200 exemples) ;
   - **la variante est validée hors échantillon** : ses 50 derniers trades fantômes « que le modèle aurait pris » gagnent plus de 1 pb net en moyenne ;
   - le gain espéré calibré dépasse 1 pb.
   Taille : **2 à 10 % du capital** selon la confiance (Kelly fractionné). Plafond total : **25 % du capital engagés** (`scalp_exposition_max_pct`).
7. **Exploration** : trades de **10 % du capital**, sans plafond horaire, 10 s minimum entre deux trades sur une même crypto et une même stratégie, **budget de pertes de 25 % du capital par jour (2 500 $)**, réglé le 25/10 à 09:57 à la demande de l'utilisateur. Désactivée en mode réel par défaut.
8. **Garde-fous** : la variante doit avoir un stop d'au moins 3 fois l'écart achat/vente ; quarantaine d'1 h d'une crypto après 5 trades réels perdants ; coupe-circuits (−15 % par jour, −50 % depuis le plus haut).
9. **Solde en temps réel** : `.venv/bin/python scalper.py solde [--suivre]`. Bilan : `scalper.py bilan`.

## 6. Historique des résultats (papier, frais inclus)

| Date | Événement | Résultat |
|---|---|---|
| 24/10 soir | Première version du scalping (modèle surconfiant) | 11 trades, 9 % gagnants, −49 $ |
| 25/10 00:36–00:58 | Exploration illimitée, 107 cryptos | ≈ −66 $ ; élan +0,2 pb brut seulement, rebond −8,8 pb brut |
| 25/10 01:16–05:16 | **Cycle 1 (4 h)** | Scalping **+2 420 $ = ARTEFACT** (exécution sans latence, carnet en retard, 187 objectifs en moins de 0,3 s, surtout sur ALT). Corrigé, données archivées en `*.sans-latence`. |
| 25/10 04:21–08:21 | **Cycle 2 (4 h, simulation réaliste)** | Scalping **−557 $** (modèle −456 $ sur 213 trades, 36 % gagnants ; exploration −101 $). Tendance −11 $. Financement −0,19 $. |
| 25/10 08:22–09:57 | Cycle 3, avec la validation hors échantillon | **0 trade de scalping** : aucune variante validée, budget d'exploration du jour épuisé. Cycle interrompu. |
| 25/10 09:57 | **Nouvelle session** (`scalper.py nouvelle-session`) : portefeuille neuf à 10 000 $, modèles et validation conservés. Plafonds : 10 % par trade, 25 % engagés, budget d'exploration 25 %/jour. | Ancien journal archivé en `*.session-20261025-0957`. |
| 25/10 10:05 | **Stratégie longue ajoutée** (30 s à 10 min) en parallèle du scalping court. | À 10:07 : 77 trades d'exploration, 48 % gagnants, −45,16 $. |

**Soldes à 10:07** : scalping 9 954,84 $ (nouvelle session à 10 000 $ depuis 09:57) ; tendance 9 699,53 $ (3 shorts ouverts depuis 01:15 : DOT, AAVE, JUP) ; financement 9 995,08 $ (1 position INIT à environ 11 %/an, +0,20 $ encaissés).

## 7. Ce que les données ont établi (ne pas re-démontrer)

- **Le scalping n'a pas d'avantage après frais.** Avant frais, l'élan fait environ 0 pb et le rebond est négatif. Les frais coûtent 4 à 10 pb par aller-retour.
- **Un taux de gain élevé ne suffit pas.** Le profil « précis » gagne 59 à 63 % du temps mais perd de l'argent : environ +4 pb par gain contre −27 pb par perte. Il faudrait 87 % de gains.
- **Le calibrage est maintenant juste** (annoncé ≈ réel). Le défaut restant était le « meilleur choix parmi 16 », corrigé par la validation hors échantillon.
- **Latence** : 150 à 300 ms entre Binance et le Mac. Le traitement prend 3 à 4 µs par message (Python 3.12), donc le calcul n'est jamais le frein.
- **Financement** : sur 11 mois, le meilleur taux moyen est d'environ 5 %/an (PUMP, HYPE), BTC 3,2 %, médiane négative. Le meilleur backtest donne environ +1,2 %/an ; détenir toute l'année la meilleure crypto rapporterait au plus environ 3 % sur le capital. Aller-retour : 0,50 % de frais.
- **Tendance** : backtests d'octobre entre −1 et +10 % selon la crypto, échantillons petits. Aucun trade fermé pendant les cycles de 4 h.

## 8. Consignes et préférences de l'utilisateur

- **Objectif déclaré** : gagner de l'argent le plus vite possible, beaucoup de trades, au moins 55 % de gagnants, solde en hausse. Shorts privilégiés ; positions courtes ou longues acceptées.
- **Il veut une analyse franche**, avec des chiffres vérifiés. Ne rien promettre, et toujours vérifier un gain spectaculaire avant de l'annoncer (cf. l'artefact du cycle 1).
- Mode papier uniquement pour l'instant. **Le mode réel n'a jamais été activé** et le chemin testnet/réel du scalper n'a pas été testé sur un compte.
- **Ne pas pousser sur GitHub** sans demande explicite.
- Refusé : se connecter à son compte ChatGPT (identifiants d'un autre service). Hors périmètre : modifier les réglages de sécurité macOS (accès complet au disque).
- Il demande des cycles de test de 4 h, puis analyse, optimisation et nouveau cycle, jusqu'à de nouvelles consignes.
- 25/10 09:50 : il veut que chaque position soit au moins gagnante, frais compris. Fait établi : un objectif atteint l'est toujours ; seuls le stop, la durée maximale et le flux inversé peuvent fermer en perte. **Question en attente** : bloquer les sorties en perte autres que le stop (durée maximale et flux inversé ne fermeraient plus en perte) ? Le stop doit rester.
- 25/10 10:00 : il veut deux stratégies en parallèle, le scalping actuel (« très bien ») et des positions plus longues (30 s à 10 min) avec de plus gros gains. C'est fait.

## 9. Points ouverts et pistes

1. **Les trades actuels sont presque tous d'exploration** (aucune variante validée). Avec un budget de 2 500 $/jour, l'exploration peut faire baisser le solde ; à surveiller. Observer si des variantes longues (cinq_min, dix_min, tendance) passent la validation.
1 bis. La ligne « Profils » du journal au démarrage n'affiche que les profils courts (cosmétique).
2. **Tendance** : 3 shorts ouverts depuis 01:15 sans clôture ; la vérifier dans le prochain bilan.
3. **Financement** : stable mais lent ; il faut des jours pour juger.
4. Le tableau de bord n'affiche que le scalping ; la tendance et le financement n'y figurent pas.
5. Le mode réel nécessite clés API (`trader.py cles`), ccxt, et une validation sur le testnet.

## 10. Commits locaux (du plus récent au plus ancien)

`(dernier)` stratégie longue en parallèle · `(avant-dernier)` plafonds 10 %/25 % + nouvelle session · `297baa5` RECAP · `4e777a2` validation hors échantillon · `dff2d40` affichage du prix des ordres au marché · `da6f2b3` simulation avec latence · `4f9ff94` financement + budget d'exploration + analyse.py · `611e34e` univers élargi + limites levées + stop/écart · `89aa797` correction des shorts rebond + espacement · `1b52265` exploration · `ab8f813` tableau de bord + calibrage + Python 3.12 · `34528ea` version initiale (seul commit poussé)
