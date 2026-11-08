"""Tests du pont LNbits -> backend et de l'annuaire LNURL regtest."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tontine_lightning import LightningLNbits, Reglages
from tontine_lightning.lnbits import ClientLNbits
from tontine_lightning.pont import annuaire_regtest
from tontine_lightning.pont.annuaire_regtest import ReglagesAnnuaire
from tontine_lightning.pont.app import creer_app
from tontine_lightning.pont.relais import Relais, ReglagesPont, StockRelais, extraire_payment_hash
from tontine_lightning.registre import RegistreVersements

from .faux_services import CLE_ADMIN, CLE_FACTURE, FauxLNbits, signer_facture

JETON = "j" * 40


class FauxBackend:
    """Imite POST /paiements/confirmation du moteur de Fatou."""

    def __init__(self) -> None:
        self.confirmations: list[dict] = []
        self.mode = "ok"  # ok | panne | refus

    def gestionnaire(self, req: httpx.Request) -> httpx.Response:
        if self.mode == "panne":
            raise httpx.ConnectError("backend arrêté")
        if self.mode == "refus":
            return httpx.Response(400, json={"detail": "Facture inconnue"})
        corps = json.loads(req.content)
        self.confirmations.append(corps)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def backend() -> FauxBackend:
    return FauxBackend()


@pytest.fixture
def relais(lnbits: FauxLNbits, backend: FauxBackend) -> Relais:
    def routeur(req: httpx.Request) -> httpx.Response:
        if req.url.host == "lnbits":
            return lnbits.gestionnaire(req)
        return backend.gestionnaire(req)

    r = ReglagesPont("http://lnbits:5000", CLE_FACTURE, "http://backend:8000", JETON, ":memory:", intervalle_s=0)
    return Relais(r, StockRelais(), http=httpx.AsyncClient(transport=httpx.MockTransport(routeur)))


def cotisation(lnbits: FauxLNbits, payee: bool = True, app: str = "tontine-lightning") -> str:
    bolt11, pre, ph = signer_facture(1520)
    from .faux_services import Paiement

    lnbits.paiements[ph] = Paiement(ph, pre, 1_520_000, "success" if payee else "pending", {"app": app})
    return ph


def corps_lnbits(ph: str) -> bytes:
    # Exactement ce que fait LNbits : json=payment.json() -> chaîne JSON ré-encodée.
    return json.dumps(json.dumps({"payment_hash": ph, "amount": 1520000, "status": "success"})).encode()


def test_extraction_payment_hash_formats() -> None:
    ph = "a" * 64
    assert extraire_payment_hash(corps_lnbits(ph)) == ph
    assert extraire_payment_hash(json.dumps({"payment_hash": ph})) == ph
    assert extraire_payment_hash(b"pas du json") is None
    assert extraire_payment_hash(json.dumps({"payment_hash": "court"})) is None


def test_webhook_bout_en_bout(relais: Relais, lnbits: FauxLNbits, backend: FauxBackend) -> None:
    ph = cotisation(lnbits)
    with TestClient(creer_app(relais.r, relais=relais)) as client:
        assert client.post("/webhook/lnbits?jeton=mauvais", content=corps_lnbits(ph)).status_code == 401
        rep = client.post(f"/webhook/lnbits?jeton={JETON}", content=corps_lnbits(ph))
        assert rep.status_code == 202
    conf = backend.confirmations[0]
    assert conf["payment_hash"] == ph
    assert hashlib.sha256(bytes.fromhex(conf["preimage"])).hexdigest() == ph


def test_webhook_mensonger_sans_effet(relais: Relais, lnbits: FauxLNbits, backend: FauxBackend) -> None:
    ph = cotisation(lnbits, payee=False)  # le webhook prétend « payé », LNbits dit « en attente »
    assert asyncio.run(relais.traiter(ph)) == "non_paye"
    assert backend.confirmations == []


@pytest.mark.parametrize("app", ["autre-appli", ""])
def test_paiement_etranger_rejete(relais: Relais, lnbits: FauxLNbits, backend: FauxBackend, app: str) -> None:
    ph = cotisation(lnbits, app=app)
    assert asyncio.run(relais.traiter(ph)) == "rejete"
    assert backend.confirmations == []


def test_backend_en_panne_puis_rattrapage(relais: Relais, lnbits: FauxLNbits, backend: FauxBackend) -> None:
    ph = cotisation(lnbits)
    backend.mode = "panne"
    assert asyncio.run(relais.traiter(ph)) == "a_reessayer"
    backend.mode = "ok"
    relais.stock._db.execute("UPDATE relais SET prochaine_tentative = 0")  # simule l'attente du délai
    asyncio.run(relais.reconcilier())
    assert len(backend.confirmations) == 1


def test_webhook_perdu_rattrape_par_reconciliation(relais: Relais, lnbits: FauxLNbits, backend: FauxBackend) -> None:
    ph = cotisation(lnbits)
    asyncio.run(relais.reconcilier())
    asyncio.run(relais.reconcilier())  # deuxième passage : pas de doublon
    assert [c["payment_hash"] for c in backend.confirmations] == [ph]


def test_refus_4xx_reessaye_puis_alerte(relais: Relais, lnbits: FauxLNbits, backend: FauxBackend) -> None:
    """Un 4xx peut être transitoire (mauvaise URL, redémarrage) : on réessaie, puis on alerte."""
    from tontine_lightning.pont.relais import TENTATIVES_MAX_4XX

    ph = cotisation(lnbits)
    backend.mode = "refus"
    assert asyncio.run(relais.traiter(ph)) == "a_reessayer"
    backend.mode = "ok"  # la configuration est corrigée
    relais.stock._db.execute("UPDATE relais SET prochaine_tentative = 0")
    asyncio.run(relais.reconcilier())
    assert len(backend.confirmations) == 1  # finalement créditée

    ph2 = cotisation(lnbits)
    backend.mode = "refus"
    for _ in range(TENTATIVES_MAX_4XX):
        relais.stock._db.execute("UPDATE relais SET prochaine_tentative = 0")
        resultat = asyncio.run(relais.traiter(ph2))
    assert resultat == "rejete" and relais.stock.compter().get("rejete") == 1


def test_jeton_envoye_au_backend(lnbits: FauxLNbits) -> None:
    vus: list = []

    def routeur(req: httpx.Request) -> httpx.Response:
        if req.url.host == "lnbits":
            return lnbits.gestionnaire(req)
        vus.append(req.headers.get("X-Jeton-Pont"))
        return httpx.Response(200, json={})

    r = ReglagesPont("http://lnbits:5000", CLE_FACTURE, "http://backend:8000", JETON, ":memory:", 0, jeton_backend="s3cret")
    relais = Relais(r, StockRelais(), http=httpx.AsyncClient(transport=httpx.MockTransport(routeur)))
    assert asyncio.run(relais.traiter(cotisation(lnbits))) == "livre"
    assert vus == ["s3cret"]


def test_livre_jamais_retrograde(relais: Relais) -> None:
    relais.stock.noter("a" * 64, "livre")
    relais.stock.noter("a" * 64, "rejete", "4xx tardif")
    assert relais.stock.statut("a" * 64) == "livre"


# ======================================================== annuaire regtest, bout en bout
def test_versement_via_annuaire_regtest(monkeypatch: pytest.MonkeyPatch, lnbits: FauxLNbits, tmp_path) -> None:
    """Adaptateur -> annuaire du pont (LUD-16) -> « LND membres » -> facture -> paiement LNbits."""
    macaroon = tmp_path / "invoice.macaroon"
    macaroon.write_bytes(b"\x02\x01")
    vus: dict = {}

    def faux_lnd(url, headers, json, verify, timeout):  # remplace l'appel REST à lnd-membres
        vus.update(url=url, macaroon=headers["Grpc-Metadata-macaroon"], verify=verify)
        dh = __import__("base64").b64decode(json["description_hash"]).hex()
        bolt11 = lnbits.facture_beneficiaire(int(json["value_msat"]) // 1000, description_hash=dh)
        return httpx.Response(200, json={"payment_request": bolt11}, request=httpx.Request("POST", url))

    monkeypatch.setattr(annuaire_regtest.httpx, "post", faux_lnd)
    r_pont = ReglagesPont("http://lnbits:5000", CLE_FACTURE, "http://backend:8000", JETON, ":memory:", 0)
    app = creer_app(
        r_pont,
        annuaire=ReglagesAnnuaire("http://testserver", "https://lnd-membres:8080", "/lnd/tls.cert", str(macaroon)),
    )
    reglages = Reglages(
        "http://lnbits.test", CLE_FACTURE, CLE_ADMIN, reseau="regtest",
        lnurl_autoriser_reseau_prive=True, lnurl_domaines_locaux={"regtest.local": "http://testserver"},
    )
    with TestClient(app) as client:
        ln = LightningLNbits(
            reglages,
            client=ClientLNbits(reglages, http=httpx.Client(base_url="http://lnbits.test", transport=httpx.MockTransport(lnbits.gestionnaire))),
            registre=RegistreVersements(),
            http_lnurl=client,
        )
        preimage = ln.payer("aminata@regtest.local", 3000, "Cagnotte t1 tour 1")
    assert len(preimage) == 64 and lnbits.appels_paiement == 1
    assert vus["url"] == "https://lnd-membres:8080/v1/invoices" and vus["macaroon"] == "0201"
