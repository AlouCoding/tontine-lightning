"""Annuaire d'adresses Lightning pour le RÉGTEST uniquement (désactivé en mainnet).

En regtest, aucun Wallet of Satoshi ni Phoenix n'existe. Pour que le versement suive
exactement le même chemin qu'en production (adresse Lightning -> LNURL -> facture), le pont
sert des adresses « nom@regtest.local » dont les factures sont émises par le nœud LND des
membres (lnd-membres). Conforme à LUD-06/LUD-16, description_hash inclus.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query

_NOM = re.compile(r"^[a-z0-9._+-]{1,64}$")


@dataclass(frozen=True)
class ReglagesAnnuaire:
    url_publique: str  # base vue par le backend, ex. http://127.0.0.1:18001
    lnd_rest: str  # ex. https://lnd-membres:8080
    lnd_cert: str  # chemin du tls.cert
    lnd_macaroon: str  # chemin d'invoice.macaroon (ne permet que de créer des factures)


def metadata_pour(nom: str) -> str:
    return json.dumps([["text/plain", f"Portefeuille regtest de {nom}"], ["text/identifier", f"{nom}@regtest.local"]])


def creer_routeur(r: ReglagesAnnuaire) -> APIRouter:
    routeur = APIRouter(tags=["annuaire regtest"])

    def verifier_nom(nom: str) -> str:
        nom = nom.lower()
        if not _NOM.match(nom):
            raise HTTPException(404, detail={"status": "ERROR", "reason": "Nom invalide"})
        return nom

    @routeur.get("/.well-known/lnurlp/{nom}")
    def lnurlp(nom: str) -> dict:
        nom = verifier_nom(nom)
        return {
            "tag": "payRequest",
            "callback": f"{r.url_publique}/lnurlp/{nom}/callback",
            "minSendable": 1_000,
            "maxSendable": 1_000_000_000,
            "metadata": metadata_pour(nom),
            "commentAllowed": 0,
        }

    @routeur.get("/lnurlp/{nom}/callback")
    def callback(nom: str, amount: int = Query(gt=0)) -> dict:
        nom = verifier_nom(nom)
        description_hash = hashlib.sha256(metadata_pour(nom).encode()).digest()
        macaroon = Path(r.lnd_macaroon).read_bytes().hex()
        try:
            rep = httpx.post(
                f"{r.lnd_rest}/v1/invoices",
                headers={"Grpc-Metadata-macaroon": macaroon},
                # L'API REST de LND encode les champs « bytes » en base64.
                json={"value_msat": str(amount), "description_hash": base64.b64encode(description_hash).decode()},
                verify=r.lnd_cert,
                timeout=15,
            )
            rep.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, detail={"status": "ERROR", "reason": f"LND indisponible : {exc.__class__.__name__}"})
        return {"pr": rep.json()["payment_request"], "routes": []}

    return routeur
