"""Client synchrone pour l'API LNbits v1 (vérifié sur le code source de LNbits 1.x).

Endpoints utilisés :
- POST /api/v1/payments {"out": false, ...}  créer une facture        (clé invoice)
- POST /api/v1/payments {"out": true, ...}   payer une facture         (clé admin)
- GET  /api/v1/payments/{payment_hash}        statut + préimage si payé (clé invoice)
- GET  /api/v1/payments?limit=N               derniers paiements        (clé invoice)
- GET  /api/v1/wallet                         solde en millisats        (clé invoice)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .erreurs import ErreurLNbits
from .reglages import Reglages


@dataclass(frozen=True)
class FactureLNbits:
    payment_hash: str
    bolt11: str


@dataclass(frozen=True)
class StatutPaiement:
    payment_hash: str
    statut: str  # "success" | "pending" | "failed" | "inconnu"
    preimage: str | None
    montant_msat: int | None  # positif = reçu, négatif = envoyé
    frais_msat: int | None
    extra: dict[str, Any]

    @property
    def paye(self) -> bool:
        return self.statut == "success"

    @property
    def echoue(self) -> bool:
        return self.statut in ("failed", "inconnu")


def analyser_statut(payment_hash: str, donnees: dict[str, Any]) -> StatutPaiement:
    """Transforme la réponse de GET /api/v1/payments/{hash} en StatutPaiement."""
    details = donnees.get("details") or {}
    if donnees.get("paid"):
        statut = "success"
    else:
        statut = str(donnees.get("status") or details.get("status") or "pending").lower()
        if statut not in ("pending", "failed", "success"):
            statut = "pending"
    montant = details.get("amount")
    frais = details.get("fee")
    return StatutPaiement(
        payment_hash=payment_hash,
        statut=statut,
        # LNbits n'expose la préimage qu'une fois le paiement réussi.
        preimage=donnees.get("preimage") if statut == "success" else None,
        montant_msat=int(montant) if montant is not None else None,
        frais_msat=abs(int(frais)) if frais is not None else None,
        extra=details.get("extra") or {},
    )


class ClientLNbits:
    """Enveloppe httpx autour de LNbits, avec des erreurs explicites et sans fuite de clé."""

    def __init__(self, reglages: Reglages, http: httpx.Client | None = None) -> None:
        self._r = reglages
        self._http = http or httpx.Client(base_url=reglages.lnbits_url, timeout=reglages.timeout_s)

    def _appel(self, methode: str, chemin: str, cle: str, **kwargs: Any) -> Any:
        try:
            rep = self._http.request(methode, chemin, headers={"X-Api-Key": cle}, **kwargs)
        except httpx.HTTPError as exc:
            raise ErreurLNbits(f"LNbits injoignable ({methode} {chemin}) : {exc.__class__.__name__}") from exc
        if rep.status_code >= 400:
            try:
                detail = rep.json().get("detail", rep.text)
            except ValueError:
                detail = rep.text
            raise ErreurLNbits(f"LNbits {methode} {chemin} -> {rep.status_code} : {detail}", rep.status_code)
        try:
            return rep.json()
        except ValueError as exc:
            raise ErreurLNbits(f"Réponse non JSON de LNbits ({chemin})") from exc

    # ---------------------------------------------------------------- réception
    def creer_facture(
        self,
        montant_sats: int,
        memo: str,
        expiration_s: int,
        webhook: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> FactureLNbits:
        corps: dict[str, Any] = {
            "out": False,  # obligatoire : la valeur par défaut côté LNbits est True
            "unit": "sat",
            "amount": montant_sats,
            "memo": memo,
            "expiry": expiration_s,
        }
        if webhook:
            corps["webhook"] = webhook
        if extra:
            corps["extra"] = extra
        d = self._appel("POST", "/api/v1/payments", self._r.cle_facture, json=corps)
        bolt11 = d.get("bolt11") or d.get("payment_request")  # selon la version de LNbits
        if not bolt11 or not d.get("payment_hash"):
            raise ErreurLNbits("Réponse de création de facture incomplète")
        return FactureLNbits(payment_hash=d["payment_hash"], bolt11=bolt11)

    def statut(self, payment_hash: str) -> StatutPaiement:
        """Statut d'un paiement. Un paiement inconnu de LNbits a le statut « inconnu »."""
        try:
            d = self._appel("GET", f"/api/v1/payments/{payment_hash}", self._r.cle_facture)
        except ErreurLNbits as exc:
            if exc.code_http == 404:
                return StatutPaiement(payment_hash, "inconnu", None, None, None, {})
            raise
        return analyser_statut(payment_hash, d)

    def lister_paiements(self, limite: int = 100) -> list[dict[str, Any]]:
        d = self._appel("GET", "/api/v1/payments", self._r.cle_facture, params={"limit": limite})
        return d if isinstance(d, list) else d.get("data", [])

    def chercher_versement(self, cle: str, limite: int = 200) -> StatutPaiement | None:
        """Retrouve chez LNbits un versement sortant (en cours ou réussi) portant extra.versement == cle.

        Seconde mémoire de l'idempotence : si le registre local est perdu (autre dossier,
        conteneur recréé), LNbits se souvient encore de ce qui a été envoyé.
        """
        for p in self.lister_paiements(limite):
            if (
                isinstance(p, dict)
                and (p.get("extra") or {}).get("versement") == cle
                and int(p.get("amount") or 0) < 0
                and p.get("status") in ("success", "pending")
                and isinstance(p.get("payment_hash"), str)
            ):
                return self.statut(p["payment_hash"])
        return None

    def solde_sats(self) -> int:
        d = self._appel("GET", "/api/v1/wallet", self._r.cle_facture)
        return int(d.get("balance", 0)) // 1000

    # ---------------------------------------------------------------- envoi
    def payer_facture(self, bolt11: str, extra: dict[str, Any] | None = None) -> StatutPaiement | None:
        """Paie une facture (clé ADMIN). Retourne le statut si LNbits le donne directement."""
        corps: dict[str, Any] = {"out": True, "bolt11": bolt11}
        if extra:
            corps["extra"] = extra
        d = self._appel("POST", "/api/v1/payments", self._r.cle_admin, json=corps)
        if not isinstance(d, dict) or not d.get("payment_hash"):
            return None
        # LNbits renvoie l'objet Payment : on le présente comme une réponse de statut.
        return analyser_statut(
            d["payment_hash"],
            {"paid": d.get("status") == "success", "status": d.get("status"), "preimage": d.get("preimage"), "details": d},
        )

    def fermer(self) -> None:
        self._http.close()
