"""Le VRAI moteur de Fatou (backend/app du dépôt) branché sur LightningLNbits.

Aucune copie : le test charge directement ../backend/app. Il teste donc toujours la version
actuelle du moteur. Hors du dépôt (dossier backend/ absent), ces tests sont ignorés.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest

from tontine_lightning import LightningLNbits, Reglages
from tontine_lightning.lnbits import ClientLNbits
from tontine_lightning.registre import RegistreVersements

from .faux_services import CLE_ADMIN, CLE_FACTURE, FauxLNbits, FauxPortefeuille

DOSSIER_BACKEND = Path(__file__).resolve().parents[2] / "backend"

if not (DOSSIER_BACKEND / "app" / "tontine.py").is_file():
    pytest.skip("backend/ absent : test d'intégration possible seulement dans le dépôt", allow_module_level=True)

sys.path.insert(0, str(DOSSIER_BACKEND))
tontine = importlib.import_module("app.tontine")
lightning_backend = importlib.import_module("app.lightning")
conversion = importlib.import_module("app.conversion")

ADRESSE = "benef@wallet.test"
COURS = 90_000.0


@pytest.fixture
def lnbits() -> FauxLNbits:
    return FauxLNbits(solde_msat=1_000_000)  # réserve de frais : 1 000 sats


@pytest.fixture
def ln(lnbits: FauxLNbits) -> LightningLNbits:
    r = Reglages("http://lnbits.test", CLE_FACTURE, CLE_ADMIN, reseau="regtest",
                 delai_suivi_versement_s=0, lnurl_autoriser_reseau_prive=True)
    return LightningLNbits(
        r,
        client=ClientLNbits(r, http=httpx.Client(base_url=r.lnbits_url, transport=httpx.MockTransport(lnbits.gestionnaire))),
        registre=RegistreVersements(),
        http_lnurl=httpx.Client(transport=httpx.MockTransport(FauxPortefeuille(lnbits).gestionnaire)),
        fabrique_facture=lightning_backend.Facture,  # exactement comme backend/app/main.py
        dormir=lambda s: None,
    )


@pytest.fixture
def moteur(ln: LightningLNbits):
    return tontine.Moteur(lightning=ln, source_cours=conversion.CoursFixe(COURS))


def creer_tour(moteur, nom: str = "Tontine du marché"):
    t = moteur.creer_tontine(nom, 5000, "mensuelle", 3, "Awa", ADRESSE)
    for prenom in ("Bineta", "Coumba"):
        moteur.rejoindre(t.code_invitation, prenom, ADRESSE)
    moteur.tirer_au_sort(t.id, t.jeton_organisateur)
    moteur.demarrer(t.id, t.jeton_organisateur)
    return t


def tout_payer(moteur, lnbits: FauxLNbits, t) -> None:
    for m in t.membres:
        c = moteur.facture_cotisation(t.id, m.id)
        lnbits.regler(c.payment_hash)  # le membre scanne et paie
        lnbits.solde_msat += c.montant_sats * 1000
        moteur.confirmer_paiement(c.payment_hash, lnbits.paiements[c.payment_hash].preimage)  # ce que fait le pont


def test_le_moteur_reconnait_l_adaptateur_comme_service_lightning(ln: LightningLNbits) -> None:
    for methode in ("creer_facture", "cotisation_recue", "payer"):  # interface Lightning de lightning.py
        assert callable(getattr(ln, methode))
    assert isinstance(ln.creer_facture(1000, "x"), lightning_backend.Facture)


def test_tour_complet(moteur, lnbits: FauxLNbits) -> None:
    t = creer_tour(moteur)
    tout_payer(moteur, lnbits, t)
    sats = conversion.fcfa_vers_sats(5000, COURS)
    assert t.tours[0].versement_statut == tontine.StatutVersement.FAIT
    assert t.tours[0].versement_sats == 3 * sats
    assert lightning_backend.preimage_valide(t.tours[0].versement_preimage, next(
        p.payment_hash for p in lnbits.paiements.values() if p.montant_msat < 0))
    assert len(t.tours) == 2 and lnbits.appels_paiement == 1
    types = [e.type for e in t.evenements]
    assert types.count("cotisation_payee") == 3 and "versement_fait" in types


def test_fausse_confirmation_refusee(moteur, lnbits: FauxLNbits) -> None:
    """Faille n° 0 : la préimage d'une facture impayée (connue de LNbits) ne doit rien déclencher."""
    lnbits.solde_msat = 100_000_000  # l'argent d'autres tontines
    t = creer_tour(moteur)
    for m in t.membres:
        c = moteur.facture_cotisation(t.id, m.id)
        with pytest.raises(tontine.ErreurTontine, match="non recu"):
            moteur.confirmer_paiement(c.payment_hash, lnbits.paiements[c.payment_hash].preimage)
    assert lnbits.appels_paiement == 0


def test_tontines_homonymes_versees_separement(moteur, lnbits: FauxLNbits) -> None:
    for _ in range(2):
        tout_payer(moteur, lnbits, creer_tour(moteur, "Test"))
    assert lnbits.appels_paiement == 2


def test_issue_inconnue_reste_en_cours_puis_reprise(moteur, lnbits: FauxLNbits) -> None:
    lnbits.mode_paiement = "en_attente"
    t = creer_tour(moteur)
    tout_payer(moteur, lnbits, t)
    assert t.tours[0].versement_statut == tontine.StatutVersement.EN_COURS
    for p in lnbits.paiements.values():
        if p.montant_msat < 0:
            p.statut = "success"
    moteur.reprendre_versements()
    assert t.tours[0].versement_statut == tontine.StatutVersement.FAIT
    assert lnbits.appels_paiement == 1


def test_relances_repetees_sans_double_paiement(moteur, lnbits: FauxLNbits) -> None:
    lnbits.mode_paiement = "en_attente"
    t = creer_tour(moteur)
    tout_payer(moteur, lnbits, t)
    for _ in range(3):
        moteur.relancer_versement(t.id, t.jeton_organisateur)
    assert lnbits.appels_paiement == 1


def test_sans_reserve_de_frais_echec_sans_envoi(moteur, lnbits: FauxLNbits) -> None:
    lnbits.solde_msat = 0
    t = creer_tour(moteur)
    tout_payer(moteur, lnbits, t)
    assert t.tours[0].versement_statut == tontine.StatutVersement.ECHEC
    assert lnbits.appels_paiement == 0
