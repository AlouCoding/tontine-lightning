"""Relais des paiements reçus : LNbits -> POST /paiements/confirmation du backend.

Garanties :
- Zéro confiance dans le webhook : son contenu ne sert qu'à connaître le payment_hash.
  On relit le paiement chez LNbits (avec notre clé) et on vérifie SHA-256(preimage) == hash.
- Livraison « au moins une fois » : si le backend est arrêté, on réessaie avec un délai
  croissant ; une boucle de réconciliation rattrape aussi les webhooks perdus.
- Dédoublonnage : chaque payment_hash livré est noté dans SQLite et n'est plus renvoyé.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..crypto import est_hex32, preimage_valide
from ..lnbits import analyser_statut
from ..reglages import ETIQUETTE_APP

journal = logging.getLogger("tontine_lightning.pont")

DELAI_MAX_S = 300
# Un refus 4xx du backend est souvent transitoire (mauvaise URL, redémarrage, 401, 429…) :
# on réessaie ~1 h avant d'abandonner, avec une alerte dans les logs et sur /sante.
TENTATIVES_MAX_4XX = 20


@dataclass(frozen=True)
class ReglagesPont:
    lnbits_url: str
    cle_facture: str
    backend_url: str
    jeton_webhook: str
    chemin_registre: str = "pont.sqlite3"
    intervalle_s: float = 10.0
    # confirmer_paiement() peut déclencher le versement (jusqu'à ~10 s de suivi) : marge large.
    timeout_s: float = 45.0
    jeton_backend: str = ""  # envoyé au backend dans X-Jeton-Pont : seul le pont peut confirmer


class StockRelais:
    """Mémoire des livraisons : livre | rejete | a_reessayer."""

    def __init__(self, chemin: str = ":memory:") -> None:
        self._verrou = threading.RLock()
        self._db = sqlite3.connect(chemin, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS relais (
                   payment_hash TEXT PRIMARY KEY,
                   statut TEXT NOT NULL CHECK (statut IN ('livre', 'rejete', 'a_reessayer')),
                   tentatives INTEGER NOT NULL DEFAULT 0,
                   prochaine_tentative REAL NOT NULL DEFAULT 0,
                   derniere_erreur TEXT,
                   maj REAL NOT NULL
               )"""
        )

    def statut(self, payment_hash: str) -> str | None:
        with self._verrou:
            ligne = self._db.execute("SELECT statut FROM relais WHERE payment_hash = ?", (payment_hash,)).fetchone()
        return ligne[0] if ligne else None

    def noter(self, payment_hash: str, statut: str, erreur: str | None = None) -> None:
        maintenant = time.time()
        with self._verrou:
            ligne = self._db.execute(
                "SELECT tentatives, statut FROM relais WHERE payment_hash = ?", (payment_hash,)
            ).fetchone()
            if ligne and ligne[1] == "livre":
                return  # une livraison réussie n'est jamais écrasée
            tentatives = (ligne[0] if ligne else 0) + 1
            prochaine = maintenant + min(DELAI_MAX_S, 2**tentatives) if statut == "a_reessayer" else 0
            self._db.execute(
                """INSERT INTO relais (payment_hash, statut, tentatives, prochaine_tentative, derniere_erreur, maj)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(payment_hash) DO UPDATE SET statut=excluded.statut, tentatives=excluded.tentatives,
                       prochaine_tentative=excluded.prochaine_tentative, derniere_erreur=excluded.derniere_erreur,
                       maj=excluded.maj""",
                (payment_hash, statut, tentatives, prochaine, erreur, maintenant),
            )

    def peut_tenter(self, payment_hash: str) -> bool:
        with self._verrou:
            ligne = self._db.execute(
                "SELECT statut, prochaine_tentative FROM relais WHERE payment_hash = ?", (payment_hash,)
            ).fetchone()
        if ligne is None:
            return True
        statut, prochaine = ligne
        return statut == "a_reessayer" and time.time() >= prochaine

    def tentatives(self, payment_hash: str) -> int:
        with self._verrou:
            ligne = self._db.execute("SELECT tentatives FROM relais WHERE payment_hash = ?", (payment_hash,)).fetchone()
        return ligne[0] if ligne else 0

    def a_reessayer(self) -> list[str]:
        with self._verrou:
            lignes = self._db.execute(
                "SELECT payment_hash FROM relais WHERE statut = 'a_reessayer' AND prochaine_tentative <= ?", (time.time(),)
            ).fetchall()
        return [l[0] for l in lignes]

    def compter(self) -> dict[str, int]:
        with self._verrou:
            return dict(self._db.execute("SELECT statut, COUNT(*) FROM relais GROUP BY statut").fetchall())


def extraire_payment_hash(corps: Any) -> str | None:
    """Récupère le payment_hash d'un webhook LNbits.

    LNbits envoie `json=payment.json()` : le corps est une CHAÎNE JSON encodée une 2e fois.
    On accepte les deux formes (dict ou chaîne), et on ne garde que le hash.
    """
    import json

    if isinstance(corps, (str, bytes)):
        try:
            corps = json.loads(corps)
        except ValueError:
            return None
        if isinstance(corps, str):  # double encodage
            try:
                corps = json.loads(corps)
            except ValueError:
                return None
    if not isinstance(corps, dict):
        return None
    valeur = corps.get("payment_hash")
    return valeur.lower() if isinstance(valeur, str) and est_hex32(valeur) else None


class Relais:
    def __init__(self, reglages: ReglagesPont, stock: StockRelais, http: httpx.AsyncClient | None = None) -> None:
        self.r = reglages
        self.stock = stock
        self._http = http or httpx.AsyncClient(timeout=reglages.timeout_s, trust_env=False)
        self._en_traitement: set[str] = set()  # webhook et réconciliation ne traitent pas le même hash en même temps

    async def _lnbits(self, chemin: str, **kwargs: Any) -> httpx.Response:
        return await self._http.get(
            f"{self.r.lnbits_url}{chemin}", headers={"X-Api-Key": self.r.cle_facture}, **kwargs
        )

    async def traiter(self, payment_hash: str) -> str:
        """Vérifie un paiement à la source et le livre au backend. Retourne le statut du relais."""
        if payment_hash in self._en_traitement:
            return "en_traitement"
        self._en_traitement.add(payment_hash)
        try:
            return await self._traiter(payment_hash)
        finally:
            self._en_traitement.discard(payment_hash)

    async def _traiter(self, payment_hash: str) -> str:
        if not self.stock.peut_tenter(payment_hash):
            return self.stock.statut(payment_hash) or "ignore"

        try:
            rep = await self._lnbits(f"/api/v1/payments/{payment_hash}")
        except httpx.HTTPError as exc:
            self.stock.noter(payment_hash, "a_reessayer", f"LNbits injoignable : {exc.__class__.__name__}")
            return "a_reessayer"
        if rep.status_code == 404:
            self.stock.noter(payment_hash, "rejete", "Paiement inconnu de LNbits")
            return "rejete"
        if rep.status_code >= 400:
            self.stock.noter(payment_hash, "a_reessayer", f"LNbits {rep.status_code}")
            return "a_reessayer"

        statut = analyser_statut(payment_hash, rep.json())
        if not statut.paye:
            return "non_paye"  # pas encore payé : rien à noter, un futur webhook ou la réconciliation repassera
        if statut.montant_msat is None or statut.montant_msat <= 0:
            self.stock.noter(payment_hash, "rejete", "Paiement sortant, pas une cotisation")
            return "rejete"
        if statut.extra.get("app") != ETIQUETTE_APP:
            self.stock.noter(payment_hash, "rejete", "Paiement sans l'étiquette de l'application")
            return "rejete"
        if not statut.preimage or not preimage_valide(statut.preimage, payment_hash):
            self.stock.noter(payment_hash, "rejete", "Préimage absente ou invalide")
            journal.error("Préimage invalide pour %s : anomalie à examiner", payment_hash)
            return "rejete"

        try:
            rep_backend = await self._http.post(
                f"{self.r.backend_url}/paiements/confirmation",
                json={"payment_hash": payment_hash, "preimage": statut.preimage},
                headers={"X-Jeton-Pont": self.r.jeton_backend} if self.r.jeton_backend else None,
            )
        except httpx.HTTPError as exc:
            self.stock.noter(payment_hash, "a_reessayer", f"Backend injoignable : {exc.__class__.__name__}")
            return "a_reessayer"

        if 200 <= rep_backend.status_code < 300:
            self.stock.noter(payment_hash, "livre")
            journal.info("Cotisation %s… confirmée au backend", payment_hash[:12])
            return "livre"
        if 400 <= rep_backend.status_code < 500:
            detail = f"Backend {rep_backend.status_code} : {rep_backend.text[:300]}"
            if self.stock.tentatives(payment_hash) + 1 >= TENTATIVES_MAX_4XX:
                # Cotisation PAYÉE mais jamais créditée (ex. membre exclu) : à traiter à la main.
                self.stock.noter(payment_hash, "rejete", detail)
                journal.error("ALERTE : cotisation %s… payée mais refusée par le backend (%s) : à rembourser ou créditer", payment_hash[:12], detail)
                return "rejete"
            self.stock.noter(payment_hash, "a_reessayer", detail)
            journal.warning("Backend a refusé %s… (%s) : nouvel essai plus tard", payment_hash[:12], detail)
            return "a_reessayer"
        self.stock.noter(payment_hash, "a_reessayer", f"Backend {rep_backend.status_code}")
        return "a_reessayer"

    async def reconcilier(self, limite: int = 100) -> int:
        """Rattrape les paiements reçus dont le webhook a été perdu, et les livraisons à réessayer."""
        candidats: set[str] = set(self.stock.a_reessayer())
        try:
            rep = await self._lnbits("/api/v1/payments", params={"limit": limite})
            rep.raise_for_status()
            for p in rep.json():
                ph = p.get("payment_hash")
                if (
                    isinstance(ph, str)
                    and p.get("status") == "success"
                    and int(p.get("amount", 0)) > 0
                    and (p.get("extra") or {}).get("app") == ETIQUETTE_APP
                    and self.stock.statut(ph) is None
                ):
                    candidats.add(ph)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            journal.warning("Réconciliation : liste LNbits indisponible (%s)", exc.__class__.__name__)
        for ph in candidats:
            await self.traiter(ph)
        return len(candidats)

    async def fermer(self) -> None:
        await self._http.aclose()
