"""Logique de la tontine : creation, inscription, ordre de passage, tours, retards, versement."""

import hashlib
import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from .conversion import fcfa_vers_sats
from .lightning import Lightning, preimage_valide

SECONDES_PAR_JOUR = 86_400


class ErreurTontine(Exception):
    """Action refusee par les regles de la tontine."""


class Statut(str, Enum):
    INSCRIPTION = "inscription"
    EN_COURS = "en_cours"
    TERMINEE = "terminee"


class StatutCotisation(str, Enum):
    EN_ATTENTE = "en_attente"
    PAYE = "paye"


class StatutMembre(str, Enum):
    ACTIF = "actif"
    EXCLU = "exclu"


class StatutVersement(str, Enum):
    AUCUN = "aucun"
    EN_COURS = "en_cours"   # si le serveur s'arrete ici, il faut verifier a la main
    FAIT = "fait"
    ECHEC = "echec"


@dataclass
class Membre:
    id: str
    nom: str
    adresse_lightning: str
    statut: StatutMembre = StatutMembre.ACTIF
    amendes_dues_fcfa: int = 0


@dataclass
class Cotisation:
    membre_id: str
    statut: StatutCotisation = StatutCotisation.EN_ATTENTE
    en_retard: bool = False
    amende_fcfa: int = 0    # amende incluse dans la facture de cette cotisation
    montant_sats: int | None = None
    payment_request: str | None = None
    payment_hash: str | None = None
    preimage: str | None = None


@dataclass
class Tour:
    numero: int
    beneficiaire_id: str
    date_limite: float
    cotisations: dict[str, Cotisation]
    versement_statut: StatutVersement = StatutVersement.AUCUN
    versement_sats: int | None = None
    versement_preimage: str | None = None

    @property
    def complet(self) -> bool:
        return all(c.statut == StatutCotisation.PAYE for c in self.cotisations.values())


@dataclass
class Tontine:
    id: str
    nom: str
    montant_fcfa: int
    frequence: str
    nombre_membres: int
    organisateur_id: str
    jeton_organisateur: str   # secret : donne uniquement a l'organisateur
    code_invitation: str
    delai_jours: int = 7
    amende_fcfa: int = 0
    membres: list[Membre] = field(default_factory=list)
    ordre: list[str] = field(default_factory=list)
    graine_tirage: str | None = None   # publiee pour que chacun puisse refaire le tirage
    tours: list[Tour] = field(default_factory=list)
    statut: Statut = Statut.INSCRIPTION

    def membre(self, membre_id: str) -> Membre:
        for m in self.membres:
            if m.id == membre_id:
                return m
        raise ErreurTontine("Membre inconnu")

    @property
    def tour_actuel(self) -> Tour | None:
        return self.tours[-1] if self.tours else None


def tirer_ordre(membre_ids: list[str], graine: str) -> list[str]:
    """Tirage au sort reproductible : la meme graine donne toujours le meme ordre."""
    ordre = sorted(membre_ids)
    random.Random(graine).shuffle(ordre)
    return ordre


class Moteur:
    """Applique les regles et enregistre chaque changement."""

    def __init__(self, lightning: Lightning, source_cours, stockage=None, horloge=time.time):
        self.lightning = lightning
        self.source_cours = source_cours
        self.stockage = stockage
        self.horloge = horloge
        self.tontines: dict[str, Tontine] = {}
        self._factures: dict[str, tuple[str, int, str]] = {}  # hash -> (tontine, tour, membre)
        self._verrou = threading.RLock()
        if stockage is not None:
            for t in stockage.charger():
                self.tontines[t.id] = t
                for tour in t.tours:
                    for c in tour.cotisations.values():
                        if c.payment_hash:
                            self._factures[c.payment_hash] = (t.id, tour.numero, c.membre_id)

    def _sauver(self, t: Tontine):
        if self.stockage is not None:
            self.stockage.sauver(t)

    # --- Creation et inscription ---

    def creer_tontine(self, nom: str, montant_fcfa: int, frequence: str, nombre_membres: int,
                      nom_organisateur: str, adresse_organisateur: str,
                      delai_jours: int = 7, amende_fcfa: int = 0) -> Tontine:
        if nombre_membres < 2:
            raise ErreurTontine("Il faut au moins 2 membres")
        if montant_fcfa <= 0:
            raise ErreurTontine("Le montant doit etre positif")
        if delai_jours < 1 or amende_fcfa < 0:
            raise ErreurTontine("Delai ou amende invalide")
        with self._verrou:
            organisateur = Membre(secrets.token_hex(8), nom_organisateur, adresse_organisateur)
            t = Tontine(id=secrets.token_hex(8), nom=nom, montant_fcfa=montant_fcfa,
                        frequence=frequence, nombre_membres=nombre_membres,
                        organisateur_id=organisateur.id,
                        jeton_organisateur=secrets.token_urlsafe(24),
                        code_invitation=secrets.token_urlsafe(6),
                        delai_jours=delai_jours, amende_fcfa=amende_fcfa,
                        membres=[organisateur])
            self.tontines[t.id] = t
            self._sauver(t)
            return t

    def tontine(self, tontine_id: str) -> Tontine:
        t = self.tontines.get(tontine_id)
        if t is None:
            raise ErreurTontine("Tontine introuvable")
        return t

    def etat(self, tontine_id: str) -> Tontine:
        """Renvoie la tontine apres avoir pris en compte les retards."""
        with self._verrou:
            t = self.tontine(tontine_id)
            self._actualiser_retards(t)
            return t

    def rejoindre(self, code_invitation: str, nom: str, adresse_lightning: str) -> tuple[Tontine, Membre]:
        with self._verrou:
            t = next((t for t in self.tontines.values() if t.code_invitation == code_invitation), None)
            if t is None:
                raise ErreurTontine("Lien d'invitation invalide")
            if t.statut != Statut.INSCRIPTION:
                raise ErreurTontine("Les inscriptions sont fermees")
            if len(t.membres) >= t.nombre_membres:
                raise ErreurTontine("La tontine est complete")
            m = Membre(secrets.token_hex(8), nom, adresse_lightning)
            t.membres.append(m)
            self._sauver(t)
            return t, m

    # --- Ordre de passage ---

    def _verifier_jeton(self, t: Tontine, jeton: str):
        if not secrets.compare_digest(jeton.encode(), t.jeton_organisateur.encode()):
            raise ErreurTontine("Seul l'organisateur peut faire cette action")

    def _verifier_inscription(self, t: Tontine, jeton: str):
        self._verifier_jeton(t, jeton)
        if t.statut != Statut.INSCRIPTION:
            raise ErreurTontine("La tontine a deja commence")

    def fixer_ordre(self, tontine_id: str, jeton: str, ordre: list[str]) -> Tontine:
        with self._verrou:
            t = self.tontine(tontine_id)
            self._verifier_inscription(t, jeton)
            if sorted(ordre) != sorted(m.id for m in t.membres):
                raise ErreurTontine("L'ordre doit contenir chaque membre une seule fois")
            t.ordre, t.graine_tirage = list(ordre), None
            self._sauver(t)
            return t

    def tirer_au_sort(self, tontine_id: str, jeton: str) -> Tontine:
        with self._verrou:
            t = self.tontine(tontine_id)
            self._verifier_inscription(t, jeton)
            t.graine_tirage = secrets.token_hex(16)
            t.ordre = tirer_ordre([m.id for m in t.membres], t.graine_tirage)
            self._sauver(t)
            return t

    # --- Deroulement des tours ---

    def demarrer(self, tontine_id: str, jeton: str) -> Tontine:
        with self._verrou:
            t = self.tontine(tontine_id)
            self._verifier_inscription(t, jeton)
            if len(t.membres) != t.nombre_membres:
                raise ErreurTontine(f"Il manque {t.nombre_membres - len(t.membres)} membre(s)")
            if sorted(t.ordre) != sorted(m.id for m in t.membres):
                raise ErreurTontine("L'ordre de passage n'est pas fixe pour tous les membres")
            t.statut = Statut.EN_COURS
            self._nouveau_tour(t)
            self._sauver(t)
            return t

    def _nouveau_tour(self, t: Tontine):
        numero = len(t.tours) + 1
        actifs = [m for m in t.membres if m.statut == StatutMembre.ACTIF]
        t.tours.append(Tour(numero=numero, beneficiaire_id=t.ordre[numero - 1],
                            date_limite=self.horloge() + t.delai_jours * SECONDES_PAR_JOUR,
                            cotisations={m.id: Cotisation(m.id) for m in actifs}))

    def _actualiser_retards(self, t: Tontine):
        """Apres la date limite, marque les retards et ajoute l'amende au prochain montant du."""
        if t.statut != Statut.EN_COURS or self.horloge() <= t.tour_actuel.date_limite:
            return
        change = False
        for c in t.tour_actuel.cotisations.values():
            if c.statut == StatutCotisation.EN_ATTENTE and not c.en_retard:
                c.en_retard = True
                t.membre(c.membre_id).amendes_dues_fcfa += t.amende_fcfa
                change = True
        if change:
            self._sauver(t)

    def situation(self, t: Tontine) -> list[dict]:
        """Ce que tous les membres voient : qui est en defaut et combien chacun doit."""
        tour = t.tour_actuel
        lignes = []
        for m in t.membres:
            c = tour.cotisations.get(m.id) if tour else None
            en_attente = c is not None and c.statut == StatutCotisation.EN_ATTENTE
            lignes.append({
                "membre_id": m.id,
                "nom": m.nom,
                "statut": m.statut.value,
                "en_defaut": en_attente and c.en_retard,
                "montant_du_fcfa": t.montant_fcfa + m.amendes_dues_fcfa if en_attente else 0,
            })
        return lignes

    def facture_cotisation(self, tontine_id: str, membre_id: str) -> Cotisation:
        """Cree (ou renvoie) la facture Lightning du membre pour le tour en cours."""
        with self._verrou:
            t = self.tontine(tontine_id)
            if t.statut != Statut.EN_COURS:
                raise ErreurTontine("La tontine n'est pas en cours")
            self._actualiser_retards(t)
            tour = t.tour_actuel
            c = tour.cotisations.get(membre_id)
            if c is None:
                raise ErreurTontine("Membre inconnu")
            if c.statut == StatutCotisation.PAYE:
                raise ErreurTontine("Cotisation deja payee")
            if c.payment_hash is None:
                amende = t.membre(membre_id).amendes_dues_fcfa
                sats = fcfa_vers_sats(t.montant_fcfa + amende, self.source_cours.cours_btc_eur())
                f = self.lightning.creer_facture(sats, f"{t.nom} - tour {tour.numero}")
                c.amende_fcfa = amende
                c.montant_sats, c.payment_request, c.payment_hash = sats, f.payment_request, f.payment_hash
                self._factures[f.payment_hash] = (t.id, tour.numero, membre_id)
                self._sauver(t)
            return c

    def confirmer_paiement(self, payment_hash: str, preimage: str) -> Tontine:
        """Appelee quand Lightning signale un paiement. Un paiement n'est compte qu'une fois."""
        with self._verrou:
            ref = self._factures.get(payment_hash)
            if ref is None:
                raise ErreurTontine("Facture inconnue")
            if not preimage_valide(preimage, payment_hash):
                raise ErreurTontine("Preimage invalide")
            tontine_id, numero, membre_id = ref
            t = self.tontine(tontine_id)
            tour = t.tours[numero - 1]
            c = tour.cotisations[membre_id]
            if c.statut == StatutCotisation.PAYE:
                return t  # notification repetee : on l'ignore
            c.statut, c.preimage = StatutCotisation.PAYE, preimage
            m = t.membre(membre_id)
            m.amendes_dues_fcfa = max(0, m.amendes_dues_fcfa - c.amende_fcfa)
            self._sauver(t)
            if tour.complet and tour.versement_statut == StatutVersement.AUCUN:
                self._verser(t)
            return t

    def exclure(self, tontine_id: str, jeton: str, membre_id: str) -> Tontine:
        """Exclut un membre en retard ; le tour peut alors se terminer sans lui."""
        with self._verrou:
            t = self.tontine(tontine_id)
            self._verifier_jeton(t, jeton)
            if t.statut != Statut.EN_COURS:
                raise ErreurTontine("La tontine n'est pas en cours")
            self._actualiser_retards(t)
            tour = t.tour_actuel
            m = t.membre(membre_id)
            c = tour.cotisations.get(membre_id)
            if c is None or c.statut != StatutCotisation.EN_ATTENTE or not c.en_retard:
                raise ErreurTontine("Seul un membre en retard sur ce tour peut etre exclu")
            if membre_id == tour.beneficiaire_id:
                raise ErreurTontine("Le beneficiaire du tour en cours ne peut pas etre exclu")
            m.statut = StatutMembre.EXCLU
            if membre_id in t.ordre[tour.numero:]:
                t.ordre.remove(membre_id)   # il ne recevra pas de cagnotte
            del tour.cotisations[membre_id]
            if c.payment_hash:
                self._factures.pop(c.payment_hash, None)
            self._sauver(t)
            if tour.complet and tour.versement_statut == StatutVersement.AUCUN:
                self._verser(t)
            return t

    def relancer_versement(self, tontine_id: str, jeton: str) -> Tontine:
        """Reessaie un versement dont l'echec a ete signale par Lightning."""
        with self._verrou:
            t = self.tontine(tontine_id)
            self._verifier_jeton(t, jeton)
            if t.tour_actuel is None or t.tour_actuel.versement_statut != StatutVersement.ECHEC:
                raise ErreurTontine("Aucun versement a relancer")
            self._verser(t)
            return t

    def _verser(self, t: Tontine):
        tour = t.tour_actuel
        beneficiaire = t.membre(tour.beneficiaire_id)
        total = sum(c.montant_sats for c in tour.cotisations.values())
        # Enregistre avant de payer : apres une panne, on ne verse pas une seconde fois.
        tour.versement_statut = StatutVersement.EN_COURS
        self._sauver(t)
        try:
            preimage = self.lightning.payer(
                beneficiaire.adresse_lightning, total, f"{t.nom} - cagnotte du tour {tour.numero}")
        except Exception:
            tour.versement_statut = StatutVersement.ECHEC
            self._sauver(t)
            return
        tour.versement_preimage, tour.versement_sats = preimage, total
        tour.versement_statut = StatutVersement.FAIT
        if tour.numero == len(t.ordre):
            t.statut = Statut.TERMINEE
        else:
            self._nouveau_tour(t)
        self._sauver(t)


def empreinte_anonyme(membre_id: str, sel: str) -> str:
    """Identifiant public d'un membre pour le registre Nostr (ni nom ni telephone)."""
    return hashlib.sha256(f"{sel}:{membre_id}".encode()).hexdigest()[:16]