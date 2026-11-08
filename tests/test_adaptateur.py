"""Tests de LightningLNbits contre l'interface du moteur de Fatou."""

from __future__ import annotations

import threading

import pytest

from tontine_lightning import (
    LightningLNbits,
    VersementEchoue,
    VersementEnAttente,
    VersementRefuse,
)
from tontine_lightning.erreurs import ErreurLNbits

from .backend_fatou_lightning import Facture as FactureFatou
from .backend_fatou_lightning import preimage_valide as preimage_valide_fatou
from .faux_services import FauxLNbits, FauxPortefeuille

BENEF = "benef@wallet.test"
DESC = "Cagnotte tontine t42 tour 1"


# =========================================================== creer_facture
def test_creer_facture_respecte_interface_fatou(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    f = ln.creer_facture(1520, "Cotisation t42 tour 1 - Aminata")
    assert isinstance(f, FactureFatou)  # le moteur peut faire asdict() dessus
    assert f.payment_request.startswith("lnbcrt15200n")  # regtest, 1520 sats
    corps = lnbits.derniere_creation
    assert corps["out"] is False and corps["unit"] == "sat" and corps["amount"] == 1520
    assert corps["extra"] == {"app": "tontine-lightning"}
    assert corps["webhook"].startswith("http://pont:8000/webhook/lnbits")


def test_creer_facture_detecte_lnbits_incoherent(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    lnbits.trafiquer_montant = True  # LNbits rend une facture d'un autre montant
    with pytest.raises(ErreurLNbits, match="incohérente"):
        ln.creer_facture(1000, "x")


@pytest.mark.parametrize("montant", [0, -5, 2_000_000, 10.5, True])
def test_creer_facture_montants_invalides(ln: LightningLNbits, montant) -> None:
    with pytest.raises(VersementRefuse):
        ln.creer_facture(montant, "x")


# =========================================================== payer : cas nominal
def test_payer_vers_adresse_lightning(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    preimage = ln.payer(BENEF, 4560, DESC)
    versement = next(iter(ln.registre._db.execute("SELECT payment_hash FROM versements")))
    assert preimage_valide_fatou(preimage, versement[0])  # vérifiable par le moteur de Fatou
    assert lnbits.appels_paiement == 1


def test_payer_facture_bolt11_directe(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    bolt11 = lnbits.facture_beneficiaire(800)
    assert ln.payer("lightning:" + bolt11.upper(), 800, DESC)  # préfixe QR et majuscules acceptés


# =========================================================== idempotence et concurrence
def test_jamais_deux_fois(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    p1 = ln.payer(BENEF, 4560, DESC)
    p2 = ln.payer(BENEF, 4560, DESC)
    assert p1 == p2 and lnbits.appels_paiement == 1


def test_tours_differents_payes_separement(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    ln.payer(BENEF, 4560, "Cagnotte tontine t42 tour 1")
    ln.payer(BENEF, 4560, "Cagnotte tontine t42 tour 2")
    assert lnbits.appels_paiement == 2


def test_appels_simultanes_un_seul_paiement(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    resultats: list[str] = []
    erreurs: list[Exception] = []

    def tache() -> None:
        try:
            resultats.append(ln.payer(BENEF, 4560, DESC))
        except Exception as exc:  # noqa: BLE001
            erreurs.append(exc)

    fils = [threading.Thread(target=tache) for _ in range(8)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert lnbits.appels_paiement == 1
    assert not erreurs and len(set(resultats)) == 1


# =========================================================== issues incertaines
def test_paiement_en_attente_puis_reussi(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    lnbits.mode_paiement = "en_attente"
    with pytest.raises(VersementEnAttente) as info:
        ln.payer(BENEF, 4560, DESC)
    lnbits.regler(info.value.payment_hash)  # le paiement aboutit plus tard sur le réseau
    assert ln.payer(BENEF, 4560, DESC)
    assert lnbits.appels_paiement == 1  # repris, pas repayé


def test_echec_puis_relance(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    lnbits.mode_paiement = "echec"
    with pytest.raises(VersementEchoue):
        ln.payer(BENEF, 4560, DESC)
    lnbits.mode_paiement = "succes"
    assert ln.payer(BENEF, 4560, DESC)
    assert lnbits.appels_paiement == 2


def test_coupure_reseau_apres_envoi(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    lnbits.mode_paiement = "erreur_reseau_apres_envoi"
    assert ln.payer(BENEF, 4560, DESC)  # on vérifie à la source au lieu de conclure à l'échec
    assert lnbits.appels_paiement == 1


def test_reprise_apres_panne(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    lnbits.mode_paiement = "en_attente"
    with pytest.raises(VersementEnAttente) as info:
        ln.payer(BENEF, 4560, DESC)
    lnbits.regler(info.value.payment_hash)
    assert list(ln.reprendre_versements_en_cours().values()) == ["paye"]


# =========================================================== garde-fous
def test_plafond(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    with pytest.raises(VersementRefuse, match="plafond"):
        ln.payer(BENEF, 100_001, DESC)
    assert lnbits.appels_paiement == 0


def test_solde_insuffisant(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    lnbits.solde_msat = 4_000_000
    with pytest.raises(VersementRefuse, match="Solde"):
        ln.payer(BENEF, 4560, DESC)


@pytest.mark.parametrize(
    "mode, message",
    [("mauvais_montant", "Montant"), ("sans_description_hash", "LUD-06"), ("mainnet", "réseau")],
)
def test_facture_beneficiaire_suspecte(ln: LightningLNbits, portefeuille: FauxPortefeuille, lnbits: FauxLNbits, mode, message) -> None:
    portefeuille.mode = mode
    with pytest.raises(VersementRefuse, match=message):
        ln.payer(BENEF, 4560, DESC)
    assert lnbits.appels_paiement == 0  # rien n'est parti


def test_facture_expiree(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    bolt11 = lnbits.facture_beneficiaire(800, date=1_700_000_000, expiration_s=60)
    with pytest.raises(VersementRefuse, match="expirée"):
        ln.payer(bolt11, 800, DESC)


def test_facture_sans_montant(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    from .faux_services import signer_facture

    bolt11, _, _ = signer_facture(None)
    with pytest.raises(VersementRefuse, match="sans montant"):
        ln.payer(bolt11, 800, DESC)


def test_adresse_inconnue(ln: LightningLNbits) -> None:
    from tontine_lightning import AdresseLightningInvalide

    with pytest.raises(AdresseLightningInvalide):
        ln.payer("personne@wallet.test", 1000, DESC)


# =========================================================== non-régression : condition de course
def test_inconnu_recent_n_est_pas_un_echec(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    """Un versement réservé par un autre fil, pas encore transmis à LNbits (404), reste « en attente »."""
    from tontine_lightning.adaptateur import cle_versement

    cle = cle_versement(BENEF, 4560, DESC)
    ln.registre.reserver(cle, "ab" * 32, 4560, BENEF)  # réservé, jamais envoyé
    with pytest.raises(VersementEnAttente):
        ln.payer(BENEF, 4560, DESC)
    assert lnbits.appels_paiement == 0 and ln.registre.lire(cle).statut == "en_cours"


def test_inconnu_perime_devient_echec_puis_relance(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    from tontine_lightning.adaptateur import cle_versement

    cle = cle_versement(BENEF, 4560, DESC)
    ln.registre.reserver(cle, "ab" * 32, 4560, BENEF)
    ln.registre._db.execute("UPDATE versements SET maj = '2020-01-01T00:00:00+00:00'")  # vieille réservation
    with pytest.raises(VersementEchoue):
        ln.payer(BENEF, 4560, DESC)
    assert ln.payer(BENEF, 4560, DESC)  # relance propre
    assert lnbits.appels_paiement == 1


def test_refus_4xx_lnbits_est_un_echec_certain(ln: LightningLNbits, lnbits: FauxLNbits) -> None:
    lnbits.factures_externes.clear()  # LNbits répondra 400 « Invalid bolt11 »
    original = lnbits.facture_beneficiaire

    def facture_non_enregistree(sats, **kw):
        bolt11 = original(sats, **kw)
        lnbits.factures_externes.clear()
        return bolt11

    lnbits.facture_beneficiaire = facture_non_enregistree  # type: ignore[method-assign]
    with pytest.raises(VersementEchoue, match="refusé"):
        ln.payer(BENEF, 4560, DESC)
