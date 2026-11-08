"""Registre des versements : garantit qu'une cagnotte n'est jamais versée deux fois.

Principe (journal d'intention, « write-ahead ») : AVANT d'envoyer le moindre sat, on écrit
dans SQLite « je vais payer la facture B dont l'empreinte est H ». Ensuite seulement on paie.
- Si le serveur plante juste après l'envoi, au redémarrage on trouve H en « en_cours » :
  on interroge LNbits sur H au lieu de repayer.
- Si deux requêtes arrivent en même temps, la transaction IMMEDIATE de SQLite n'en laisse
  passer qu'une.
- On garde la facture B : une relance repaie la MÊME facture tant qu'elle n'a pas expiré.
  LND refuse de payer deux fois le même hash : seconde barrière contre le doublon.
- Toute mise à jour est conditionnée au payment_hash : le suivi périmé d'une ancienne
  tentative ne peut pas écraser la tentative en cours.

Cycle de vie : en_cours -> paye | echoue (un versement « echoue » peut être retenté).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATUTS = ("en_cours", "paye", "echoue")
_COLONNES = "cle, statut, payment_hash, montant_sats, destination, preimage, tentatives, maj, bolt11"


@dataclass(frozen=True)
class Versement:
    cle: str
    statut: str
    payment_hash: str
    montant_sats: int
    destination: str
    preimage: str | None
    tentatives: int
    maj: str = ""
    bolt11: str | None = None

    @property
    def age_s(self) -> float:
        """Secondes écoulées depuis la dernière mise à jour (réservation comprise)."""
        if not self.maj:
            return float("inf")
        return (datetime.now(timezone.utc) - datetime.fromisoformat(self.maj)).total_seconds()


class RegistreVersements:
    def __init__(self, chemin: str = ":memory:") -> None:
        if chemin != ":memory:":
            Path(chemin).parent.mkdir(parents=True, exist_ok=True)
        self._verrou = threading.RLock()  # réentrant : reserver() appelle lire()
        # isolation_level=None : on pilote les transactions nous-mêmes (BEGIN IMMEDIATE).
        self._db = sqlite3.connect(chemin, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS versements (
                   cle          TEXT PRIMARY KEY,
                   statut       TEXT NOT NULL CHECK (statut IN ('en_cours', 'paye', 'echoue')),
                   payment_hash TEXT NOT NULL,
                   montant_sats INTEGER NOT NULL CHECK (montant_sats > 0),
                   destination  TEXT NOT NULL,
                   preimage     TEXT,
                   tentatives   INTEGER NOT NULL DEFAULT 1,
                   maj          TEXT NOT NULL,
                   bolt11       TEXT
               )"""
        )

    @staticmethod
    def _maintenant() -> str:
        return datetime.now(timezone.utc).isoformat()

    def lire(self, cle: str) -> Versement | None:
        with self._verrou:
            ligne = self._db.execute(f"SELECT {_COLONNES} FROM versements WHERE cle = ?", (cle,)).fetchone()
        return Versement(*ligne) if ligne else None

    def reserver(
        self, cle: str, payment_hash: str, montant_sats: int, destination: str, bolt11: str | None = None
    ) -> Versement | None:
        """Inscrit l'intention de payer. Retourne le versement existant s'il est déjà en cours ou payé.

        Retourne None si la réservation a réussi (l'appelant peut alors payer).
        """
        with self._verrou:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existant = self.lire(cle)
                if existant and existant.statut in ("en_cours", "paye"):
                    self._db.execute("COMMIT")
                    return existant
                tentatives = (existant.tentatives + 1) if existant else 1
                self._db.execute(
                    f"""INSERT INTO versements ({_COLONNES})
                       VALUES (?, 'en_cours', ?, ?, ?, NULL, ?, ?, ?)
                       ON CONFLICT(cle) DO UPDATE SET statut='en_cours', payment_hash=excluded.payment_hash,
                           montant_sats=excluded.montant_sats, destination=excluded.destination,
                           preimage=NULL, tentatives=excluded.tentatives, maj=excluded.maj,
                           bolt11=excluded.bolt11""",
                    (cle, payment_hash, montant_sats, destination, tentatives, self._maintenant(), bolt11),
                )
                self._db.execute("COMMIT")
                return None
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def marquer(self, cle: str, payment_hash: str, statut: str, preimage: str | None = None) -> bool:
        """Change le statut de la tentative `payment_hash`. False si ce n'est plus la tentative courante.

        « echoue » n'est accepté que depuis « en_cours » ; « paye » est accepté pour la bonne
        tentative même si elle avait été crue échouée (un succès réel prime sur tout).
        """
        if statut not in STATUTS:
            raise ValueError(f"Statut inconnu : {statut}")
        depuis = ("en_cours", "echoue") if statut == "paye" else ("en_cours",)
        with self._verrou:
            curseur = self._db.execute(
                f"""UPDATE versements SET statut = ?, preimage = COALESCE(?, preimage), maj = ?
                    WHERE cle = ? AND payment_hash = ? AND statut IN ({",".join("?" * len(depuis))})""",
                (statut, preimage, self._maintenant(), cle, payment_hash, *depuis),
            )
            return curseur.rowcount == 1

    def en_cours(self) -> list[Versement]:
        with self._verrou:
            lignes = self._db.execute(f"SELECT {_COLONNES} FROM versements WHERE statut = 'en_cours'").fetchall()
        return [Versement(*l) for l in lignes]
