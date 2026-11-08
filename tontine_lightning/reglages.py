"""Réglages de la couche Lightning, lus depuis l'environnement (ou un fichier .env).

Règle de sécurité : la clé ADMIN de LNbits permet de dépenser les fonds de la tontine.
Elle ne vit que côté serveur (backend + pont), jamais dans le frontend ni dans Git.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Préfixe BOLT11 attendu pour chaque réseau (« lnbc… », « lntb… », « lntbs… », « lnbcrt… »).
PREFIXES_RESEAU: dict[str, str] = {
    "mainnet": "bc",
    "testnet": "tb",
    "signet": "tbs",
    "regtest": "bcrt",
}

# Étiquette posée sur chaque facture : le pont ne relaie que les paiements portant cette marque.
ETIQUETTE_APP = "tontine-lightning"


def charger_fichier_env(chemin: str | os.PathLike[str]) -> None:
    """Charge un fichier .env simple (CLE=valeur) sans écraser l'environnement existant."""
    fichier = Path(chemin)
    if not fichier.is_file():
        return
    for ligne in fichier.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, valeur = ligne.split("=", 1)
        os.environ.setdefault(cle.strip(), valeur.strip().strip('"').strip("'"))


def _bool(nom: str, defaut: bool) -> bool:
    valeur = os.environ.get(nom)
    if valeur is None or valeur.strip() == "":
        return defaut
    return valeur.strip().lower() in {"1", "true", "oui", "yes", "on"}


def _domaines_locaux(brut: str) -> dict[str, str]:
    """« regtest.local=http://localhost:8001,autre=http://x » -> {domaine: url_de_base}."""
    resultat: dict[str, str] = {}
    for paire in filter(None, (p.strip() for p in brut.split(","))):
        domaine, _, url = paire.partition("=")
        if domaine and url:
            resultat[domaine.strip().lower()] = url.strip().rstrip("/")
    return resultat


@dataclass(frozen=True)
class Reglages:
    """Paramètres de connexion et garde-fous de la couche Lightning."""

    lnbits_url: str
    cle_facture: str  # « Invoice/read key » : créer des factures, lire les statuts
    cle_admin: str  # « Admin key » : envoyer des paiements
    reseau: str = "regtest"
    url_webhook: str | None = None  # URL du pont appelée par LNbits à chaque paiement reçu
    max_facture_sats: int = 1_000_000
    max_versement_sats: int = 500_000
    frais_max_pourcent: float = 1.0
    frais_min_sats: int = 10
    # Le moteur réutilise la même facture jusqu'au paiement : elle doit vivre au moins le délai du tour.
    expiration_facture_s: int = 7 * 86_400
    # Le moteur appelle payer() en tenant son verrou global : on n'attend pas longtemps.
    delai_suivi_versement_s: float = 10.0
    # Un versement réservé mais inconnu de LNbits depuis plus longtemps que ce seuil n'est jamais parti.
    seuil_abandon_s: float = 120.0
    chemin_registre: str = "versements.sqlite3"
    lnurl_autoriser_reseau_prive: bool = False
    lnurl_verifier_description_hash: bool = True
    lnurl_domaines_locaux: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if self.reseau not in PREFIXES_RESEAU:
            raise ValueError(f"Réseau inconnu : {self.reseau!r} (attendu : {', '.join(PREFIXES_RESEAU)})")
        if self.max_versement_sats <= 0 or self.max_facture_sats <= 0:
            raise ValueError("Les plafonds doivent être strictement positifs")
        if self.reseau == "mainnet" and not Path(self.chemin_registre).is_absolute():
            # Un registre relatif « perdu » en changeant de dossier = risque de double versement.
            raise ValueError("En mainnet, LIGHTNING_REGISTRE doit être un chemin absolu")

    @property
    def prefixe_bolt11(self) -> str:
        return PREFIXES_RESEAU[self.reseau]

    @classmethod
    def depuis_env(cls, fichier_env: str | None = None) -> "Reglages":
        """Construit les réglages depuis l'environnement (voir .env.example)."""
        if fichier_env or os.environ.get("LIGHTNING_ENV_FILE"):
            charger_fichier_env(fichier_env or os.environ["LIGHTNING_ENV_FILE"])

        def requis(nom: str) -> str:
            valeur = os.environ.get(nom, "").strip()
            if not valeur:
                raise RuntimeError(f"Variable d'environnement manquante : {nom}")
            return valeur

        reseau = os.environ.get("LIGHTNING_RESEAU", "regtest").strip().lower()
        return cls(
            lnbits_url=requis("LNBITS_URL").rstrip("/"),
            cle_facture=requis("LNBITS_INVOICE_KEY"),
            cle_admin=requis("LNBITS_ADMIN_KEY"),
            reseau=reseau,
            url_webhook=os.environ.get("LIGHTNING_WEBHOOK_URL") or None,
            max_facture_sats=int(os.environ.get("LIGHTNING_MAX_FACTURE_SATS", "1000000")),
            max_versement_sats=int(os.environ.get("LIGHTNING_MAX_VERSEMENT_SATS", "500000")),
            frais_max_pourcent=float(os.environ.get("LIGHTNING_FRAIS_MAX_POURCENT", "1.0")),
            frais_min_sats=int(os.environ.get("LIGHTNING_FRAIS_MIN_SATS", "10")),
            expiration_facture_s=int(os.environ.get("LIGHTNING_EXPIRATION_FACTURE_S", str(7 * 86_400))),
            delai_suivi_versement_s=float(os.environ.get("LIGHTNING_DELAI_SUIVI_S", "10")),
            seuil_abandon_s=float(os.environ.get("LIGHTNING_SEUIL_ABANDON_S", "120")),
            # Chemin absolu stable : le registre ne doit pas dépendre du dossier de lancement.
            chemin_registre=os.environ.get("LIGHTNING_REGISTRE")
            or str(Path.home() / ".tontine_lightning" / f"versements-{reseau}.sqlite3"),
            # En regtest, les services tournent sur des adresses privées (Docker, localhost).
            lnurl_autoriser_reseau_prive=_bool("LNURL_AUTORISER_RESEAU_PRIVE", reseau == "regtest"),
            lnurl_verifier_description_hash=_bool("LNURL_VERIFIER_DESCRIPTION_HASH", True),
            lnurl_domaines_locaux=_domaines_locaux(os.environ.get("LNURL_DOMAINES_LOCAUX", "")),
        )

    def __repr__(self) -> str:  # Ne jamais afficher les clés dans un log ou une trace.
        return (
            f"Reglages(lnbits_url={self.lnbits_url!r}, reseau={self.reseau!r}, "
            f"max_versement_sats={self.max_versement_sats}, cles=***)"
        )
