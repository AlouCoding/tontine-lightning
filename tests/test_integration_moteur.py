"""Le VRAI moteur de Fatou (copie de backend/app/tontine.py) branché sur LightningLNbits.

Ces tests documentent aussi les points à corriger côté moteur (voir INTEGRATION.md).
"""

from __future__ import annotations

import pytest

from tontine_lightning import LightningLNbits

from .backend_fatou.conversion import SourceCours
from .backend_fatou.tontine import Moteur, Statut, StatutVersement
from .faux_services import FauxLNbits

ADRESSE = "benef@wallet.test"


def creer_tour_complet(moteur: Moteur, nom: str = "Tontine du marché") -> tuple:
    t = moteur.creer_tontine(nom, 5000, "mensuelle", 3, "Awa", ADRESSE)
    for prenom in ("Bineta", "Coumba"):
        moteur.rejoindre(t.code_invitation, prenom, ADRESSE)
    moteur.tirer_au_sort(t.id, t.jeton_organisateur)
    moteur.demarrer(t.id, t.jeton_organisateur)
    return t


def payer_toutes_les_cotisations(moteur: Moteur, lnbits: FauxLNbits, t) -> None:
    for m in t.membres:
        c = moteur.facture_cotisation(t.id, m.id)
        lnbits.regler(c.payment_hash)  # le membre scanne et paie
        lnbits.solde_msat += c.montant_sats * 1000  # les fonds arrivent sur le portefeuille de la tontine
        p = lnbits.paiements[c.payment_hash]
        moteur.confirmer_paiement(c.payment_hash, p.preimage)  # ce que fait le pont


@pytest.fixture
def moteur(ln: LightningLNbits) -> Moteur:
    return Moteur(lightning=ln, source_cours=SourceCours())


def test_tour_complet_avec_le_vrai_moteur(moteur: Moteur, lnbits: FauxLNbits) -> None:
    lnbits.solde_msat = 1_000_000  # réserve de frais : 1 000 sats
    t = creer_tour_complet(moteur)
    payer_toutes_les_cotisations(moteur, lnbits, t)
    tour1 = t.tours[0]
    assert tour1.versement_statut == StatutVersement.FAIT
    assert tour1.versement_sats == 3 * 8469
    assert len(t.tours) == 2 and t.statut == Statut.EN_COURS  # le tour 2 a démarré
    assert lnbits.appels_paiement == 1


def test_PROBLEME_sans_reserve_de_frais_le_versement_echoue(moteur: Moteur, lnbits: FauxLNbits) -> None:
    """La cagnotte = exactement la somme des cotisations : rien pour payer les frais de routage."""
    lnbits.solde_msat = 0
    t = creer_tour_complet(moteur)
    payer_toutes_les_cotisations(moteur, lnbits, t)
    assert t.tours[0].versement_statut == StatutVersement.ECHEC
    assert lnbits.appels_paiement == 0  # refusé avant tout envoi : aucun risque, mais pas de versement


def test_PROBLEME_deux_tontines_homonymes_partagent_la_cle_de_versement(moteur: Moteur, lnbits: FauxLNbits) -> None:
    """Même nom + même bénéficiaire + même montant + même numéro de tour => même description.

    La 2e cagnotte est considérée comme déjà versée : le moteur la marque FAIT sans qu'aucun
    sat ne parte. Scénario très probable en démo (on recrée « Test » avec les mêmes adresses).
    """
    lnbits.solde_msat = 1_000_000
    t1 = creer_tour_complet(moteur, "Test")
    payer_toutes_les_cotisations(moteur, lnbits, t1)
    t2 = creer_tour_complet(moteur, "Test")
    payer_toutes_les_cotisations(moteur, lnbits, t2)
    assert t2.tours[0].versement_statut == StatutVersement.FAIT
    assert lnbits.appels_paiement == 1  # <- un seul versement réel pour deux cagnottes


def test_relance_apres_issue_incertaine_ne_paie_pas_deux_fois(moteur: Moteur, lnbits: FauxLNbits) -> None:
    """VersementEnAttente est vu comme un échec par le moteur ; la relance reste sans danger."""
    lnbits.solde_msat = 1_000_000
    lnbits.mode_paiement = "en_attente"
    t = creer_tour_complet(moteur)
    payer_toutes_les_cotisations(moteur, lnbits, t)
    assert t.tours[0].versement_statut == StatutVersement.ECHEC  # affiché « échec » alors que les fonds sont en vol
    for p in list(lnbits.paiements.values()):
        if p.montant_msat < 0:
            p.statut = "success"  # le paiement aboutit sur le réseau
    moteur.relancer_versement(t.id, t.jeton_organisateur)
    assert t.tours[0].versement_statut == StatutVersement.FAIT
    assert lnbits.appels_paiement == 1


# ====================================================== les correctifs proposés fonctionnent
from .backend_fatou import tontine_corrige as corrige  # noqa: E402


@pytest.fixture
def moteur_corrige(ln: LightningLNbits) -> corrige.Moteur:
    return corrige.Moteur(lightning=ln, source_cours=SourceCours())


def test_CORRIGE_tontines_homonymes_versees_separement(moteur_corrige, lnbits: FauxLNbits) -> None:
    lnbits.solde_msat = 1_000_000
    t1 = creer_tour_complet(moteur_corrige, "Test")
    payer_toutes_les_cotisations(moteur_corrige, lnbits, t1)
    t2 = creer_tour_complet(moteur_corrige, "Test")
    payer_toutes_les_cotisations(moteur_corrige, lnbits, t2)
    assert lnbits.appels_paiement == 2


def test_CORRIGE_en_attente_reste_en_cours_puis_reprise(moteur_corrige, lnbits: FauxLNbits) -> None:
    lnbits.solde_msat = 1_000_000
    lnbits.mode_paiement = "en_attente"
    t = creer_tour_complet(moteur_corrige)
    payer_toutes_les_cotisations(moteur_corrige, lnbits, t)
    assert t.tours[0].versement_statut == corrige.StatutVersement.EN_COURS  # plus de faux « échec »
    for p in list(lnbits.paiements.values()):
        if p.montant_msat < 0:
            p.statut = "success"
    moteur_corrige.reprendre_versements()  # ce que fait main.py au démarrage
    assert t.tours[0].versement_statut == corrige.StatutVersement.FAIT
    assert lnbits.appels_paiement == 1


def test_CORRIGE_relance_depuis_en_cours_sans_double_paiement(moteur_corrige, lnbits: FauxLNbits) -> None:
    lnbits.solde_msat = 1_000_000
    lnbits.mode_paiement = "en_attente"
    t = creer_tour_complet(moteur_corrige)
    payer_toutes_les_cotisations(moteur_corrige, lnbits, t)
    moteur_corrige.relancer_versement(t.id, t.jeton_organisateur)  # l'organisateur s'impatiente
    moteur_corrige.relancer_versement(t.id, t.jeton_organisateur)
    assert lnbits.appels_paiement == 1


def test_CORRIGE_C2_fausse_confirmation_refusee(moteur_corrige, lnbits: FauxLNbits) -> None:
    """Attaque : récupérer la préimage d'une facture impayée (connue de LNbits) et la poster."""
    lnbits.solde_msat = 1_000_000
    t = creer_tour_complet(moteur_corrige)
    for m in t.membres:
        c = moteur_corrige.facture_cotisation(t.id, m.id)
        preimage_volee = lnbits.paiements[c.payment_hash].preimage  # lisible avec la clé « invoice »
        with pytest.raises(corrige.ErreurTontine, match="non recu"):
            moteur_corrige.confirmer_paiement(c.payment_hash, preimage_volee)
    assert t.tours[0].versement_statut == corrige.StatutVersement.AUCUN
    assert lnbits.appels_paiement == 0  # aucune cagnotte versée


def test_PROBLEME_C2_moteur_actuel_accepte_une_fausse_confirmation(moteur: Moteur, lnbits: FauxLNbits) -> None:
    lnbits.solde_msat = 100_000_000  # le portefeuille contient l'argent d'AUTRES tontines
    t = creer_tour_complet(moteur)
    for m in t.membres:
        c = moteur.facture_cotisation(t.id, m.id)
        moteur.confirmer_paiement(c.payment_hash, lnbits.paiements[c.payment_hash].preimage)  # rien n'a été payé
    assert t.tours[0].versement_statut == StatutVersement.FAIT
    assert lnbits.appels_paiement == 1  # <- la cagnotte est partie sans qu'aucune cotisation ne soit reçue
