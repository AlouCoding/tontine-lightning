# ⚡ lightning/ — couche Lightning de Tontine Lightning

**Responsable :** Alassane Ndour · Infrastructure répartie
**Rôle :** nœud Lightning et LNbits, réception des cotisations, versement de la cagnotte, sécurité des fonds.

> Concepts Bitcoin/Lightning, flux détaillés et modèle de menaces : **[ARCHITECTURE.md](ARCHITECTURE.md)**
> Branchement avec le moteur et 4 corrections à faire dans `backend/`, dont une faille critique : **[INTEGRATION.md](INTEGRATION.md)**

## Ce qu'il y a dedans

| Élément | Ce que ça fait |
|---|---|
| `tontine_lightning/adaptateur.py` | `LightningLNbits` : implémente `creer_facture()` et `payer()` de l'interface du moteur (`backend/app/lightning.py`) |
| `tontine_lightning/pont/` | service qui reçoit les webhooks LNbits, **revérifie chaque paiement** et appelle `POST /paiements/confirmation` |
| `docker-compose.yml` | vrai réseau Bitcoin + Lightning en local (regtest) : bitcoind, 2 nœuds LND, LNbits, pont |
| `docker-compose.mainnet.yml` | démo avec de vrais sats (LNbits + phoenixd) |
| `scripts/regtest.py` | initialise le réseau, simule les membres, vérifie tout de bout en bout |
| `tests/` | 76 tests : vraies factures BOLT11 signées, et le vrai moteur de Fatou branché sur l'adaptateur |

## Démarrage (regtest, ~5 min)

Prérequis : Docker Desktop lancé, Python ≥ 3.10.

```bash
cd lightning
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux : source .venv/bin/activate)
pip install -e ".[dev]"
pytest                             # 76 tests, sans réseau

python scripts/regtest.py init     # conteneurs, minage, canaux, portefeuille LNbits, .env, pont
python scripts/regtest.py verifier # VRAIE cotisation + VRAI versement sur la pile Docker
```

`init` est idempotent : on peut le relancer sans risque. Il écrit `lightning/.env` (ignoré par Git).

| Commande | Usage |
|---|---|
| `python scripts/regtest.py payer <bolt11>` | un « membre » paie le QR affiché par le frontend |
| `python scripts/regtest.py soldes` | soldes des canaux et du portefeuille de la tontine |
| `python scripts/regtest.py miner 6` | mine 6 blocs |
| `docker compose logs -f pont` | voir les cotisations relayées au backend |
| `docker compose down -v` | tout effacer (chaîne, canaux, portefeuilles) et repartir de zéro |

En regtest, **toute adresse `nom@regtest.local`** est une adresse Lightning valide : le pont la sert et le nœud `lnd-membres` reçoit les fonds. Utilisez-les comme `adresse_lightning` lors de l'inscription des membres.

## Brancher le backend (à faire avec Fatou)

**1.** Dans `backend/requirements.txt`, ajouter :
```
-e ../lightning
```

**2.** Dans `backend/app/main.py`, remplacer la création du moteur :
```python
import os
from .lightning import Facture, LightningFactice


def service_lightning():
    """LNbits si LIGHTNING_MODE=lnbits, sinon le faux service (tests, dev sans Docker)."""
    if os.environ.get("LIGHTNING_MODE") == "lnbits":
        from tontine_lightning import LightningLNbits
        ln = LightningLNbits.depuis_env(fabrique_facture=Facture)  # renvoie SA classe Facture
        ln.reprendre_versements_en_cours()  # règle les versements interrompus par une panne
        return ln
    return LightningFactice()


moteur = Moteur(lightning=service_lightning(), source_cours=SourceCours(),
                stockage=Stockage(os.environ.get("TONTINE_DB", "tontine.db")))
```

**3.** Appliquer les 4 corrections de [INTEGRATION.md](INTEGRATION.md), dont la n° 0 (faille critique) : vérification `cotisation_recue()` dans `confirmer_paiement`, et jeton du pont sur `/paiements/confirmation`.

**4.** Lancer le backend avec la config Lightning :
```bash
# Windows PowerShell
$env:LIGHTNING_MODE="lnbits"; $env:LIGHTNING_ENV_FILE="..\lightning\.env"
python -m uvicorn app.main:app --reload
```

Sans `LIGHTNING_MODE`, rien ne change : les tests de Fatou continuent d'utiliser `LightningFactice`.

### Contrat avec le moteur (important)

| Situation dans `payer()` | Exception | Ce que le moteur doit faire |
|---|---|---|
| Succès | (retourne la préimage) | marquer versé, publier la preuve |
| Bloqué avant envoi (plafond, solde, facture suspecte, adresse invalide) | `VersementRefuse`, `AdresseLightningInvalide` | afficher l'erreur ; rien n'est parti |
| Envoyé mais échoué (pas de route) | `VersementEchoue` | on peut relancer |
| **Issue inconnue** (paiement en vol) | `VersementEnAttente` | **ne pas** marquer échoué. Rappeler `payer()` **avec exactement les mêmes arguments** plus tard |

Toutes héritent de `ErreurLightning`. **L'idempotence repose sur `(adresse, montant, description)`** : la description d'un versement doit être stable et unique par tour (ex. `"Cagnotte tontine t42 tour 3"`), et ne doit **pas** contenir d'horodatage.

## Démo mainnet (vrais sats)

1. Installer et lancer [phoenixd](https://phoenix.acinq.co/server) sur la machine (port 9740).
2. `cp .env.mainnet.example .env.mainnet`, puis remplir le mot de passe phoenixd.
3. `docker compose -f docker-compose.mainnet.yml --env-file .env.mainnet up -d lnbits`
4. Créer le portefeuille (commande dans `.env.mainnet.example`), remplir les clés et le jeton.
5. `docker compose -f docker-compose.mainnet.yml --env-file .env.mainnet up -d --build`
6. Backend : `LIGHTNING_ENV_FILE=..\lightning\.env.mainnet`.

Plafonds bas par défaut (20 000 sats). Les membres paient avec Phoenix, Wallet of Satoshi, Breez, etc.

## Sécurité, en une liste
- La clé **admin** LNbits ne quitte jamais le serveur ; `.env` est ignoré par Git.
- LNbits et le pont n'écoutent que sur `127.0.0.1` ; bitcoind et LND ne sont pas exposés.
- Le pont tourne en utilisateur non root et ne lit que `invoice.macaroon` de `lnd-membres` (il ne peut pas dépenser).
- Une cotisation n'est confirmée que si LNbits a **réellement reçu** l'argent (`cotisation_recue()`), et seul le pont peut appeler `/paiements/confirmation` (jeton). Une préimage seule ne suffit jamais.
- Détail des garde-fous : [ARCHITECTURE.md § 4](ARCHITECTURE.md#4-flux-dun-versement-le-moment-risqué).

## Structure
```
lightning/
├── tontine_lightning/
│   ├── adaptateur.py      LightningLNbits (interface du moteur)
│   ├── lnbits.py          client API LNbits
│   ├── factures.py        décodage local BOLT11 (signature, réseau, expiration)
│   ├── lnurl.py           adresse Lightning -> facture, anti-SSRF, LUD-06
│   ├── registre.py        journal d'intention des versements (SQLite)
│   ├── crypto.py          SHA-256(préimage) == payment_hash
│   ├── reglages.py        configuration et plafonds
│   ├── erreurs.py         exceptions
│   └── pont/              webhooks -> backend, annuaire regtest
├── scripts/regtest.py
├── tests/
├── docker-compose.yml  docker-compose.mainnet.yml  Dockerfile  pyproject.toml
└── ARCHITECTURE.md
```
