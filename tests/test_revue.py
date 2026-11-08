"""Non-régression des défauts trouvés par la relecture indépendante (C2, I1, I2, I3, I5, I6, mineurs)."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from tontine_lightning import LightningLNbits, Reglages, VersementEchoue, VersementEnAttente, VersementRefuse
from tontine_lightning.adaptateur import cle_versement
from tontine_lightning.lnbits import ClientLNbits
from tontine_lightning.lnurl import demander_facture
from tontine_lightning.pont.relais import Relais, ReglagesPont, StockRelais
from tontine_lightning.registre import RegistreVersements

from .faux_services import CLE_ADMIN, CLE_FACTURE, METADATA, FauxLNbits, FauxPortefeuille, Paiement, signer_facture

ARGS = ("benef@wallet.test", 4000, "Cagnotte t1 tour 1 (abc123)")


def _ln(lnbits: FauxLNbits, registre: RegistreVersements | None = None, portefeuille=None, **kw) -> LightningLNbits:
    base = dict(lnbits_url="http://lnbits.test", cle_facture=CLE_FACTURE, cle_admin=CLE_ADMIN, reseau="regtest",
                delai_suivi_versement_s=0, lnurl_autoriser_reseau_prive=True)
    base.update(kw)
    r = Reglages(**base)
    http = httpx.Client(base_url=r.lnbits_url, transport=httpx.MockTransport(lnbits.gestionnaire))
    lnurl = httpx.Client(transport=httpx.MockTransport((portefeuille or FauxPortefeuille(lnbits)).gestionnaire))
    return LightningLNbits(r, client=ClientLNbits(r, http=http), registre=registre or RegistreVersements(),
                           http_lnurl=lnurl, dormir=lambda s: None)


# ===================================================================== C2
def test_C2_preimage_connue_mais_cotisation_non_payee_refusee() -> None:
    """LNbits connaît la préimage dès la création : seule compte la réception constatée chez LNbits."""
    lnbits = FauxLNbits()
    ln = _ln(lnbits)
    f = ln.creer_facture(1500, "Cotisation")
    assert lnbits.paiements[f.payment_hash].preimage  # préimage connue du serveur AVANT paiement
    assert ln.cotisation_recue(f.payment_hash) is False
    lnbits.regler(f.payment_hash)
    assert ln.cotisation_recue(f.payment_hash) is True


def test_C2_paiement_sortant_ou_etranger_n_est_pas_une_cotisation() -> None:
    lnbits = FauxLNbits()
    ln = _ln(lnbits)
    _, pre, ph = signer_facture(1000)
    lnbits.paiements[ph] = Paiement(ph, pre, -1_000_000, "success", {"app": "tontine-lightning"})  # sortant
    _, pre2, ph2 = signer_facture(1000)
    lnbits.paiements[ph2] = Paiement(ph2, pre2, 1_000_000, "success", {"app": "autre"})  # autre appli
    assert not ln.cotisation_recue(ph) and not ln.cotisation_recue(ph2) and not ln.cotisation_recue("ab" * 32)


# ===================================================================== I1
def test_I1_suivi_perime_n_ecrase_pas_la_tentative_courante() -> None:
    lnbits = FauxLNbits(mode_paiement="en_attente")
    reg = RegistreVersements()
    ln = _ln(lnbits, reg)
    cle = cle_versement(*ARGS)
    with pytest.raises(VersementEnAttente):
        ln.payer(*ARGS)  # H1 en vol
    h1 = reg.lire(cle).payment_hash
    lnbits.paiements[h1].statut = "failed"
    with pytest.raises(VersementEchoue):
        ln.payer(*ARGS)  # constat d'échec de H1
    with pytest.raises(VersementEnAttente):
        ln.payer(*ARGS)  # relance : H2 en vol
    h2 = reg.lire(cle).payment_hash
    assert h2 != h1
    with pytest.raises(VersementEnAttente):
        ln._suivre(cle, h1, 0)  # le vieux suivi de H1 voit « failed »… mais n'écrase plus H2
    assert reg.lire(cle).statut == "en_cours" and reg.lire(cle).payment_hash == h2
    with pytest.raises(VersementEnAttente):
        ln.payer(*ARGS)  # nouvelle relance : suit H2, ne crée pas H3
    assert lnbits.appels_paiement == 2


# ===================================================================== I2
def test_I2_registre_perdu_lnbits_se_souvient() -> None:
    lnbits = FauxLNbits(mode_paiement="en_attente")
    with pytest.raises(VersementEnAttente):
        _ln(lnbits).payer(*ARGS)
    with pytest.raises(VersementEnAttente):
        _ln(lnbits).payer(*ARGS)  # nouveau registre vide (autre dossier, conteneur recréé…)
    assert lnbits.appels_paiement == 1
    for p in lnbits.paiements.values():
        p.statut = "success"
    assert _ln(lnbits).payer(*ARGS)  # 3e registre vide : retrouve le succès chez LNbits
    assert lnbits.appels_paiement == 1


def test_I2_mainnet_exige_un_registre_absolu() -> None:
    with pytest.raises(ValueError, match="absolu"):
        Reglages("http://x", "a", "b", reseau="mainnet", chemin_registre="versements.sqlite3")


# ===================================================================== I3
def test_I3_relance_reutilise_la_meme_facture_si_jamais_transmise() -> None:
    lnbits = FauxLNbits()
    reg = RegistreVersements()
    ln = _ln(lnbits, reg)
    cle = cle_versement(*ARGS)
    bolt11 = lnbits.facture_beneficiaire(4000, description_hash=__import__("hashlib").sha256(METADATA.encode()).hexdigest())
    decodee_hash = lnbits.factures_externes[bolt11.lower()].payment_hash
    reg.reserver(cle, decodee_hash, 4000, ARGS[0], bolt11.lower())  # tentative jamais transmise à LNbits
    reg._db.execute("UPDATE versements SET maj = '2020-01-01T00:00:00+00:00'")
    with pytest.raises(VersementEchoue):
        ln.payer(*ARGS)  # périmée + inconnue de LNbits => échec certain
    assert ln.payer(*ARGS)
    assert reg.lire(cle).payment_hash == decodee_hash  # même facture repayée, pas une nouvelle


# ===================================================================== auto-paiement, LUD-06
def test_refuse_de_payer_une_facture_de_la_tontine() -> None:
    lnbits = FauxLNbits()
    ln = _ln(lnbits)
    propre = ln.creer_facture(4000, "Cotisation")
    with pytest.raises(VersementRefuse, match="tontine lui-même"):
        ln.payer(propre.payment_request, 4000, "Cagnotte")
    assert lnbits.appels_paiement == 0


def test_lnurl_sans_metadata_refuse() -> None:
    class SansMetadata(FauxPortefeuille):
        def gestionnaire(self, req: httpx.Request) -> httpx.Response:
            rep = super().gestionnaire(req)
            if req.url.path.startswith("/.well-known"):
                d = rep.json()
                d.pop("metadata")
                return httpx.Response(200, json=d)
            return rep

    lnbits = FauxLNbits()
    with pytest.raises(VersementRefuse, match="LUD-06"):
        _ln(lnbits, portefeuille=SansMetadata(lnbits)).payer(*ARGS)


# ===================================================================== I5 / I6
def test_I5_connexion_epinglee_sur_l_ip_verifiee(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Rebinding : la 2e résolution DNS rendrait 127.0.0.1 ; on ne refait pas de résolution."""
    appels = {"n": 0}
    vrai = socket.getaddrinfo

    def faux_dns(hote, port, *a, **k):
        if hote == "evil.example":
            appels["n"] += 1
            ip = "93.184.216.34" if appels["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 443))]
        return vrai(hote, port, *a, **k)

    vues: list[tuple[str, str | None, str | None]] = []

    def transport(req: httpx.Request) -> httpx.Response:
        vues.append((req.url.host, req.headers.get("Host"), req.extensions.get("sni_hostname")))
        return httpx.Response(404, json={"status": "ERROR", "reason": "fin du test"})

    monkeypatch.setattr(socket, "getaddrinfo", faux_dns)
    # tmp_path : chemin absolu sur tous les systèmes (« /tmp/... » n'est pas absolu sous Windows)
    r = Reglages("http://x", "a", "b", reseau="mainnet", chemin_registre=str(tmp_path / "r.sqlite3"))
    with pytest.raises(Exception):
        demander_facture("x@evil.example", 1000, r, http=httpx.Client(transport=httpx.MockTransport(transport)))
    assert vues == [("93.184.216.34", "evil.example", "evil.example")]


def test_I6_parametres_du_callback_conserves() -> None:
    vues: list[str] = []
    lnbits = FauxLNbits()
    dh = __import__("hashlib").sha256(METADATA.encode()).hexdigest()

    def portefeuille(req: httpx.Request) -> httpx.Response:
        vues.append(str(req.url))
        if req.url.path == "/.well-known/lnurlp/benef":
            return httpx.Response(200, json={"tag": "payRequest", "callback": "https://wallet.test/cb?id=42",
                                             "minSendable": 1000, "maxSendable": 10**10, "metadata": METADATA})
        return httpx.Response(200, json={"pr": lnbits.facture_beneficiaire(int(req.url.params["amount"]) // 1000, description_hash=dh)})

    r = Reglages("http://x", "a", "b", lnurl_autoriser_reseau_prive=True)
    demander_facture("benef@wallet.test", 5, r, http=httpx.Client(transport=httpx.MockTransport(portefeuille)))
    assert "id=42" in vues[1] and "amount=5000" in vues[1]


# ===================================================================== pont : traitements simultanés
def test_pont_webhook_et_reconciliation_simultanes_une_seule_livraison() -> None:
    lnbits = FauxLNbits()
    recus: list[str] = []

    async def routeur(req: httpx.Request) -> httpx.Response:
        if req.url.host == "lnbits":
            return lnbits.gestionnaire(req)
        recus.append(json.loads(req.content)["payment_hash"])
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    _, pre, ph = signer_facture(1000)
    lnbits.paiements[ph] = Paiement(ph, pre, 1_000_000, "success", {"app": "tontine-lightning"})
    r = ReglagesPont("http://lnbits:5000", CLE_FACTURE, "http://backend:8000", "j" * 40, ":memory:", 0)
    relais = Relais(r, StockRelais(), http=httpx.AsyncClient(transport=httpx.MockTransport(routeur)))

    async def scenario() -> None:
        await asyncio.gather(relais.traiter(ph), relais.reconcilier())

    asyncio.run(scenario())
    assert recus == [ph]
