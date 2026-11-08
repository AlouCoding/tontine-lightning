"""Service « pont » : reçoit les webhooks LNbits et confirme les cotisations au backend.

Lancer : uvicorn tontine_lightning.pont.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request

from .annuaire_regtest import ReglagesAnnuaire, creer_routeur
from .relais import Relais, ReglagesPont, StockRelais, extraire_payment_hash

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
journal = logging.getLogger("tontine_lightning.pont")


def reglages_depuis_env() -> ReglagesPont:
    jeton = os.environ.get("PONT_JETON_WEBHOOK", "")
    if len(jeton) < 32:
        raise RuntimeError("PONT_JETON_WEBHOOK doit faire au moins 32 caractères (lancer scripts/regtest.py init)")
    return ReglagesPont(
        lnbits_url=os.environ.get("LNBITS_URL", "http://lnbits:5000").rstrip("/"),
        cle_facture=os.environ["LNBITS_INVOICE_KEY"],
        backend_url=os.environ.get("BACKEND_URL", "http://host.docker.internal:8000").rstrip("/"),
        jeton_webhook=jeton,
        jeton_backend=os.environ.get("PONT_JETON_BACKEND", ""),
        chemin_registre=os.environ.get("PONT_REGISTRE", "/donnees/pont.sqlite3"),
        intervalle_s=float(os.environ.get("PONT_INTERVALLE_S", "10")),
    )


def creer_app(reglages: ReglagesPont, relais: Relais | None = None, annuaire: ReglagesAnnuaire | None = None) -> FastAPI:
    relais = relais or Relais(reglages, StockRelais(reglages.chemin_registre))

    async def boucle_reconciliation() -> None:
        while True:
            try:
                await relais.reconcilier()
            except Exception:  # la boucle ne doit jamais mourir
                journal.exception("Erreur dans la réconciliation")
            await asyncio.sleep(reglages.intervalle_s)

    @asynccontextmanager
    async def cycle_de_vie(_: FastAPI) -> AsyncIterator[None]:
        tache = asyncio.create_task(boucle_reconciliation()) if reglages.intervalle_s > 0 else None
        yield
        if tache:
            tache.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tache
        await relais.fermer()

    app = FastAPI(title="Tontine Lightning — pont LNbits", lifespan=cycle_de_vie, docs_url=None, redoc_url=None)
    app.state.relais = relais

    @app.post("/webhook/lnbits", status_code=202)
    async def webhook(request: Request, taches: BackgroundTasks, jeton: str = Query("")) -> dict:
        # Le jeton empêche n'importe qui de déclencher des traitements (le contrôle cryptographique reste la vraie garantie).
        if not hmac.compare_digest(jeton.encode(), reglages.jeton_webhook.encode()):
            raise HTTPException(401, "Jeton invalide")
        payment_hash = extraire_payment_hash(await request.body())
        if payment_hash is None:
            raise HTTPException(400, "payment_hash introuvable")
        taches.add_task(relais.traiter, payment_hash)
        return {"recu": True}

    @app.get("/vivant")
    async def vivant() -> dict:
        """Vérification légère pour Docker : ne contacte ni LNbits ni le backend (pas de bruit dans leurs logs)."""
        return {"vivant": True}

    @app.get("/sante")
    async def sante() -> dict:
        compte = relais.stock.compter()
        etat: dict[str, object] = {"relais": compte, "alertes": compte.get("rejete", 0)}
        async with httpx.AsyncClient(timeout=5) as http:
            for nom, url in (("lnbits", f"{reglages.lnbits_url}/api/v1/wallet"), ("backend", f"{reglages.backend_url}/docs")):
                try:
                    rep = await http.get(url, headers={"X-Api-Key": reglages.cle_facture} if nom == "lnbits" else None)
                    etat[nom] = "ok" if rep.status_code < 500 else f"erreur {rep.status_code}"
                except httpx.HTTPError as exc:
                    etat[nom] = f"injoignable ({exc.__class__.__name__})"
        return etat

    if annuaire is not None:
        app.include_router(creer_routeur(annuaire))
        journal.warning("Annuaire LNURL regtest ACTIVÉ (ne jamais activer en mainnet)")
    return app


def _app_depuis_env() -> FastAPI:
    annuaire = None
    if os.environ.get("ANNUAIRE_REGTEST", "").lower() in {"1", "true", "oui"}:
        annuaire = ReglagesAnnuaire(
            url_publique=os.environ.get("ANNUAIRE_URL_PUBLIQUE", "http://127.0.0.1:18001").rstrip("/"),
            lnd_rest=os.environ.get("LND_MEMBRES_REST", "https://lnd-membres:8080"),
            lnd_cert=os.environ.get("LND_MEMBRES_CERT", "/lnd-membres/tls.cert"),
            lnd_macaroon=os.environ.get(
                "LND_MEMBRES_MACAROON", "/lnd-membres/data/chain/bitcoin/regtest/invoice.macaroon"
            ),
        )
    return creer_app(reglages_depuis_env(), annuaire=annuaire)


_APP: FastAPI | None = None


def __getattr__(nom: str):  # « app » n'est construite qu'à la demande d'uvicorn, et une seule fois
    global _APP
    if nom == "app":
        if _APP is None:
            _APP = _app_depuis_env()
        return _APP
    raise AttributeError(nom)
