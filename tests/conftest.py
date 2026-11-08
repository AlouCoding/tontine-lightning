from __future__ import annotations

import httpx
import pytest

from tontine_lightning import LightningLNbits, Reglages
from tontine_lightning.lnbits import ClientLNbits
from tontine_lightning.registre import RegistreVersements

from .backend_fatou_lightning import Facture as FactureFatou
from .faux_services import CLE_ADMIN, CLE_FACTURE, FauxLNbits, FauxPortefeuille


class Horloge:
    """Horloge simulée : dormir() avance le temps instantanément."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def dormir(self, s: float) -> None:
        self.t += s


@pytest.fixture
def reglages() -> Reglages:
    return Reglages(
        lnbits_url="http://lnbits.test",
        cle_facture=CLE_FACTURE,
        cle_admin=CLE_ADMIN,
        reseau="regtest",
        url_webhook="http://pont:8000/webhook/lnbits?jeton=x",
        max_versement_sats=100_000,
        delai_suivi_versement_s=5,
        lnurl_autoriser_reseau_prive=True,  # le faux portefeuille n'a pas de vrai DNS ; SSRF testé à part
    )


@pytest.fixture
def lnbits() -> FauxLNbits:
    return FauxLNbits()


@pytest.fixture
def portefeuille(lnbits: FauxLNbits) -> FauxPortefeuille:
    return FauxPortefeuille(lnbits)


@pytest.fixture
def horloge() -> Horloge:
    return Horloge()


@pytest.fixture
def ln(reglages: Reglages, lnbits: FauxLNbits, portefeuille: FauxPortefeuille, horloge: Horloge) -> LightningLNbits:
    http = httpx.Client(base_url=reglages.lnbits_url, transport=httpx.MockTransport(lnbits.gestionnaire))
    http_lnurl = httpx.Client(transport=httpx.MockTransport(portefeuille.gestionnaire))
    return LightningLNbits(
        reglages,
        client=ClientLNbits(reglages, http=http),
        registre=RegistreVersements(),
        http_lnurl=http_lnurl,
        fabrique_facture=FactureFatou,
        horloge=horloge,
        dormir=horloge.dormir,
    )
