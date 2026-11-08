"""Faux LNbits et faux portefeuille LNURL, fidèles aux formats réels, avec de vraies factures signées."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field

import httpx
from bolt11 import Bolt11, MilliSatoshi, encode
from bolt11.models.tags import Tags

CLE_FACTURE = "cle-facture-test"
CLE_ADMIN = "cle-admin-test"
# Clé privée de test (nœud fictif). Ne jamais réutiliser en dehors des tests.
CLE_NOEUD = "e126f68f7eafcc8b74f54d269fe206be715000f94dac067d1c04a8ca3b2db734"
METADATA = json.dumps([["text/plain", "Portefeuille de benef"]])


def signer_facture(
    montant_sats: int | None,
    reseau: str = "bcrt",
    description_hash: str | None = None,
    expiration_s: int = 3600,
    date: int | None = None,
) -> tuple[str, str, str]:
    """Fabrique une facture BOLT11 réellement signée. Retourne (bolt11, preimage, payment_hash)."""
    preimage = os.urandom(32).hex()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    tags: dict = {
        "payment_hash": payment_hash,
        "payment_secret": os.urandom(32).hex(),
        "expire_time": expiration_s,
        "features": {"var_onion_optin": "required", "payment_secret": "required"},
    }
    if description_hash:
        tags["description_hash"] = description_hash
    else:
        tags["description"] = "test"
    facture = Bolt11(
        currency=reseau,
        amount_msat=MilliSatoshi(montant_sats * 1000) if montant_sats else None,
        date=date or int(time.time()),
        tags=Tags.from_dict(tags),
    )
    return encode(facture, CLE_NOEUD), preimage, payment_hash


@dataclass
class Paiement:
    payment_hash: str
    preimage: str
    montant_msat: int  # positif = reçu, négatif = envoyé
    statut: str = "pending"
    extra: dict = field(default_factory=dict)
    webhook: str | None = None


@dataclass
class FauxLNbits:
    solde_msat: int = 10_000_000_000
    paiements: dict[str, Paiement] = field(default_factory=dict)
    factures_externes: dict[str, Paiement] = field(default_factory=dict)  # bolt11 -> paiement (bénéficiaires)
    appels_paiement: int = 0
    mode_paiement: str = "succes"  # succes | echec | en_attente | erreur_reseau_apres_envoi
    trafiquer_montant: bool = False
    derniere_creation: dict | None = None
    verrou: threading.Lock = field(default_factory=threading.Lock)

    # ---------------------------------------------------------------- utilitaires de test
    def facture_beneficiaire(self, montant_sats: int, **kwargs) -> str:
        bolt11, pre, ph = signer_facture(montant_sats, **kwargs)
        self.factures_externes[bolt11.lower()] = Paiement(ph, pre, -montant_sats * 1000)
        return bolt11

    def regler(self, payment_hash: str) -> None:
        self.paiements[payment_hash].statut = "success"

    def _payment_json(self, p: Paiement) -> dict:
        return {
            "payment_hash": p.payment_hash,
            "amount": p.montant_msat,
            "fee": -2000 if p.montant_msat < 0 and p.statut == "success" else 0,
            "status": p.statut,
            "extra": p.extra,
            "preimage": p.preimage if p.statut == "success" else None,
        }

    # ---------------------------------------------------------------- API
    def gestionnaire(self, req: httpx.Request) -> httpx.Response:
        cle = req.headers.get("X-Api-Key")
        if cle not in (CLE_FACTURE, CLE_ADMIN):
            return httpx.Response(401, json={"detail": "Invalid key"})
        chemin = req.url.path
        corps = json.loads(req.content) if req.content else {}

        if req.method == "POST" and chemin == "/api/v1/payments":
            if not corps.get("out", True):
                self.derniere_creation = corps
                montant = corps["amount"] + (1 if self.trafiquer_montant else 0)
                bolt11, pre, ph = signer_facture(montant)
                self.paiements[ph] = Paiement(ph, pre, corps["amount"] * 1000, extra=corps.get("extra") or {}, webhook=corps.get("webhook"))
                return httpx.Response(201, json={"payment_hash": ph, "bolt11": bolt11, "status": "pending"})
            if cle != CLE_ADMIN:
                return httpx.Response(403, json={"detail": "Invoice (or Admin) key required."})
            with self.verrou:
                self.appels_paiement += 1
            p = self.factures_externes.get(corps["bolt11"].lower())
            if p is None:
                return httpx.Response(400, json={"detail": "Invalid bolt11"})
            p.extra = corps.get("extra") or {}
            self.paiements[p.payment_hash] = p
            if self.mode_paiement == "echec":
                p.statut = "failed"
                return httpx.Response(520, json={"detail": "Payment failed: no route"})
            if self.mode_paiement == "en_attente":
                return httpx.Response(201, json=self._payment_json(p))
            p.statut = "success"
            self.solde_msat += p.montant_msat - 2000
            if self.mode_paiement == "erreur_reseau_apres_envoi":
                raise httpx.ReadTimeout("connexion coupée après l'envoi")
            return httpx.Response(201, json=self._payment_json(p))

        if req.method == "GET" and chemin == "/api/v1/payments":
            return httpx.Response(200, json=[self._payment_json(p) for p in self.paiements.values()])

        if req.method == "GET" and chemin.startswith("/api/v1/payments/"):
            p = self.paiements.get(chemin.rsplit("/", 1)[-1])
            if p is None:
                return httpx.Response(404, json={"detail": "Payment does not exist."})
            details = self._payment_json(p)
            if p.statut == "success":
                return httpx.Response(200, json={"paid": True, "preimage": p.preimage, "details": details})
            if p.statut == "failed":
                return httpx.Response(200, json={"paid": False, "status": "failed", "details": details})
            return httpx.Response(200, json={"paid": False, "status": "pending", "preimage": None, "details": details})

        if req.method == "GET" and chemin == "/api/v1/wallet":
            return httpx.Response(200, json={"name": "Tontine", "balance": self.solde_msat})

        return httpx.Response(404, json={"detail": "Not found"})


@dataclass
class FauxPortefeuille:
    """Serveur LNURL du bénéficiaire (benef@wallet.test)."""

    lnbits: FauxLNbits
    mode: str = "conforme"  # conforme | mauvais_montant | sans_description_hash | mainnet

    def gestionnaire(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/.well-known/lnurlp/benef":
            return httpx.Response(
                200,
                json={
                    "tag": "payRequest",
                    "callback": "https://wallet.test/lnurlp/benef/callback",
                    "minSendable": 1000,
                    "maxSendable": 10_000_000_000,
                    "metadata": METADATA,
                    "commentAllowed": 32,
                },
            )
        if req.url.path == "/lnurlp/benef/callback":
            sats = int(req.url.params["amount"]) // 1000
            dh = hashlib.sha256(METADATA.encode()).hexdigest()
            if self.mode == "mauvais_montant":
                sats += 1
            if self.mode == "sans_description_hash":
                dh = None
            reseau = "bc" if self.mode == "mainnet" else "bcrt"
            return httpx.Response(200, json={"pr": self.lnbits.facture_beneficiaire(sats, reseau=reseau, description_hash=dh)})
        return httpx.Response(404, json={"status": "ERROR", "reason": "Utilisateur inconnu"})
