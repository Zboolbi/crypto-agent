# Agent Crypto

Suivi en temps réel des **100 cryptomonnaies les plus capitalisées** avec **option de prévision**.
Données : API publique [CoinGecko](https://www.coingecko.com/). Python 3.8+ uniquement, aucune dépendance à installer.

## Lancer l'agent (mode interactif)

```bash
python3 crypto_agent.py
```

```
crypto> top 20            # 20 premières cryptos
crypto> top 10 gain       # plus fortes hausses 24h (tri : rank, gain, loss, volume, 7d)
crypto> info sol          # détail d'une crypto (symbole, nom ou id)
crypto> prevision btc 14  # prévision sur 14 jours
crypto> modeles eth 7     # prévision + détail de chaque modèle
crypto> surveiller btc eth sol   # suivi en continu avec alertes
crypto> export top100.csv # export .csv ou .json
crypto> devise usd        # changer de devise
crypto> quitter
```

## Commandes directes

```bash
python3 crypto_agent.py top                       # top 100 complet
python3 crypto_agent.py top -n 10 --tri gain      # top 10 des hausses
python3 crypto_agent.py top --export top100.json  # export
python3 crypto_agent.py info ethereum
python3 crypto_agent.py prevision btc --jours 30 --modeles
python3 crypto_agent.py surveiller btc eth --intervalle 60 --seuil 1.5
python3 crypto_agent.py --devise usd top -n 5
```

## Comment fonctionne la prévision

Elle part de l'historique quotidien (180 jours par défaut) et combine trois modèles, calculés sur le logarithme des prix :

| Modèle | Ce qu'il capte |
|---|---|
| Régression linéaire (30 j) | la tendance récente |
| Holt avec tendance amortie | le niveau et la tendance lissés, paramètres optimisés |
| Marche aléatoire avec dérive | le rendement moyen sur 90 j |

L'estimation finale est la moyenne des trois. La **fourchette à 90 %** vient de la volatilité historique : elle s'élargit avec √(horizon).

L'agent affiche aussi :
- des **indicateurs** : moyennes mobiles 20/50 j, RSI 14 j, volatilité ;
- un **backtest** : l'erreur moyenne du modèle sur des périodes passées, comparée au modèle naïf « le prix ne bouge pas », et le taux de bonne direction.

> ⚠️ Les prix des cryptomonnaies sont très difficiles à prévoir : le modèle fait souvent à peine mieux, voire moins bien, que le modèle naïf. Le backtest est là pour le montrer. **Ce n'est pas un conseil financier.**

## Limites de requêtes

L'API gratuite limite le nombre d'appels par minute. Les réponses sont gardées en cache dans `~/.cache/crypto_agent/` (60 s pour les cours, 10 min pour l'historique). Quand la limite est atteinte, l'agent attend puis réessaie, ou utilise les dernières données en cache.
Pour un quota plus large, créez une clé « Demo » gratuite sur CoinGecko :

```bash
export COINGECKO_API_KEY="votre_cle"
```

---

# Agent de trading autonome (`trader.py`)

L'agent analyse les tendances du top 100, ouvre des **shorts** (en priorité) et des longs, gère ses stops, **s'optimise seul** et peut tourner **en permanence comme service**, en se connectant et en se reconnectant seul.

## Sources de données (liste [public-apis › Cryptocurrency](https://github.com/public-apis/public-apis#cryptocurrency))

La liste compte une centaine d'API. L'agent utilise celles qui sont gratuites, sans clé, et utiles pour trader des contrats perpétuels (`sources.py`) :

| API | Rôle | Repli |
|---|---|---|
| **Binance Futures** | contrats, bougies 15 min, prix mark, taux de financement | Bybit |
| **Bybit** | mêmes données (source principale si l'exchange est Bybit) | Binance |
| **CoinGecko** | classement du top 100 par capitalisation | Coinpaprika, puis Coinlore |
| **Coinpaprika**, **Coinlore** | classement de secours | |
| **DefiLlama**, **Kraken** | contrôle croisé du prix avant chaque ouverture | |

Avant d'ouvrir une position, l'agent compare le prix avec une autre source. Si l'écart dépasse 1,5 %, il n'ouvre pas : cela évite de trader sur une donnée erronée. CryptoCompare et CoinCap, aussi dans la liste, demandent désormais une clé et ne sont pas utilisés.

## Les trois modes

| Mode | Ordres | Argent |
|---|---|---|
| `papier` (défaut) | simulés au prix mark, frais et glissement inclus | fictif (10 000 $) |
| `testnet` | envoyés au réseau de test de l'exchange | fictif |
| `reel` | envoyés à l'exchange | **réel**, confirmation « JE CONFIRME » exigée |

## Connexion à l'exchange (une seule fois)

```bash
pip3 install ccxt
python3 trader.py cles
```

La commande demande la clé et le secret (saisie masquée), puis les enregistre dans `.env`, lisible par vous seul. L'agent s'y connectera ensuite seul. Créez la clé avec le droit « trading Futures » **et sans droit de retrait**.

## Lancer l'agent en autonomie

```bash
./installer_service.sh papier     # service permanent en simulation
./installer_service.sh testnet    # puis sur le testnet
./installer_service.sh reel       # puis en réel (confirmation demandée)
./desinstaller_service.sh reel    # arrêt
tail -f logs/agent_reel.log       # suivre son activité
```

Le service démarre à l'ouverture de session, redémarre s'il plante, empêche la mise en veille (`caffeinate`) et se reconnecte seul après une coupure réseau ou exchange, en espaçant de plus en plus ses tentatives. Au démarrage, il reprend les positions déjà ouvertes sur l'exchange et repère celles que l'exchange a fermées par stop pendant son absence. Toutes les 24 h, il se ré-optimise.

Le Mac doit rester allumé avec la session ouverte. Pour un fonctionnement vraiment 24h/24, un petit serveur (VPS) est plus fiable.

## Commandes

```bash
python3 trader.py                         # mode interactif
python3 trader.py tendances -n 30         # scan : score, financement, signaux
python3 trader.py short eth               # short, taille calculée selon le risque
python3 trader.py long btc 1000           # long de 1000 $
python3 trader.py fermer tout
python3 trader.py portefeuille
python3 trader.py auto                    # boucle autonome dans le terminal
python3 trader.py service                 # service autonome (utilisé par l'installateur)
python3 trader.py optimiser --appliquer   # recherche des meilleurs réglages
python3 trader.py backtest sol            # teste les réglages actuels
python3 trader.py profil agressif         # ou prudent
python3 trader.py --mode reel portefeuille
```

## Comment il décide

**Score de tendance** (-100 à +100), calculé à chaque cycle (toutes les 2 min) sur des bougies de 15 min : croisement des moyennes mobiles 6h/24h, pente, momentum, prévision, filtre RSI. Le **taux de financement** s'y ajoute : quand les longs paient les shorts, le score penche vers le short, puisque le short encaisse alors le financement.

**Biais short** : il faut un score ≤ −45 pour ouvrir un short, et ≥ +101 pour un long, autrement dit **jamais de long** avec les réglages optimisés actuels. Les shorts sont toujours servis en premier.

**Auto-optimisation** : 384 combinaisons de réglages (seuils long et short, distance du stop, objectif, sortie) sont testées sur environ 30 jours de bougies des 8 plus grosses cryptos. Le calibrage se fait sur les 70 % les plus anciens et la validation sur les 30 % récents. Un nouveau réglage n'est adopté que s'il fait mieux **en validation**.

## Profils de risque

| Réglage | `agressif` (actif) | `prudent` |
|---|---|---|
| Risque par trade | 3 % | 1 % |
| Levier total max | 5× | 2× |
| Nombre de positions | illimité | 5 |
| Coupe-circuit journalier | −15 % | −5 % |
| Arrêt sur drawdown | −50 % | −20 % |
| Bougies / cycle | 15 min / 2 min | 1 h / 15 min |

Tous les réglages se modifient dans `config_trading.json`, ou avec `config <clé> <valeur>`. Mettre `0` à `perte_journaliere_max_pct` ou `drawdown_max_pct` désactive le coupe-circuit correspondant.

## Ce qu'il faut savoir

- **Aucune garantie de gain.** Sur les ~9 derniers jours de validation, le meilleur réglage fait +1,6 % en moyenne par crypto, et les réglages agressifs de départ −3,8 %. Ces résultats viennent d'un marché baissier : une stratégie 100 % short peut perdre vite quand le marché se retourne.
- **Le levier limite les pertes, il ne les supprime pas.** Avec 5× et des shorts corrélés, une hausse générale de 10 % peut coûter environ 50 % du capital. Les stops sont là pour l'éviter, mais un « gap » peut les dépasser.
- Les frais de financement sont pris en compte dans le score, mais pas dans la simulation papier ni dans le backtest.
- Le short avec levier peut faire perdre plus que la mise. Selon votre pays, les dérivés crypto peuvent être restreints.
- Faites tourner le service en `papier` puis en `testnet` pendant plusieurs semaines avant de passer en réel avec un petit montant. **Ceci n'est pas un conseil financier.**

---

# Scalping qui apprend (`scalper.py`, stratégie active du service)

Beaucoup de petits shorts de quelques secondes à 3 minutes, déclenchés par le flux temps réel de Binance Futures. Ce flux passe par deux connexions WebSocket, conformément à la [séparation /market et /public](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice), et chacune se reconnecte seule.

**Comment il décide :**
1. **Signal candidat** : les ventes agressives dominent sur les 10 dernières secondes et le prix baisse.
2. **Trades fantômes** : chaque candidat est suivi virtuellement selon 3 profils (éclair 10/8 pb sur 20 s, rapide 20/15 pb sur 60 s, large 35/25 pb sur 180 s) et 2 modes d'entrée (ordre limite ou au marché). Ces 6 variantes alimentent 6 modèles (`learner.py`), qui apprennent quels contextes mènent aux pertes, frais compris, sans engager de capital.
3. **Trade réel** seulement après 200 exemples, et si le gain espéré après frais est positif. Pour un ordre limite, ce gain est pondéré par son taux réel d'exécution. L'agent retient la meilleure variante.
4. **Taille** : de 5 à 60 % du capital selon la confiance du modèle (Kelly fractionné). Elle est réduite après une série de pertes.
5. **Aucune pause** : l'agent peut reshorter la même crypto quelques millisecondes après une sortie, si le modèle le justifie.
6. **Quarantaine** : une crypto dont les trades perdent en moyenne plus de 8 pb (sur au moins 120 exemples) n'est plus tradée en réel, mais l'agent continue d'apprendre dessus.

```bash
python3 scalper.py bilan     # résultats, ce que le modèle a appris, cryptos piégeuses
python3 scalper.py reset     # repart de zéro
python3 -m unittest discover -s tests   # tests
```

**Limites :**
- La latence est de 150 à 300 ms entre Binance et ce Mac. Le trading à la milliseconde demande un serveur hébergé à côté de l'exchange.
- Au premier apprentissage (5 min), ces signaux ne gagnaient que 13 à 26 % du temps après frais : l'agent n'a donc pris aucun trade réel. C'est voulu, il ne trade que s'il a une espérance positive.
- En mode testnet ou réel, le suivi des ordres limite se fait en interrogeant l'exchange chaque seconde. Ce chemin n'a encore été testé ni sur un compte ni avec un faux exchange : validez-le sur le testnet avant tout.
