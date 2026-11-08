"""Implémentation LNbits de l'interface `Lightning` du moteur (backend/app/lightning.py).

    class Lightning(Protocol):
        def creer_facture(self, montant_sats: int, description: str) -> Facture: ...
        def payer(self, adresse_lightning: str, montant_sats: int, description: str) -> str: ...

Branchement dans backend/app/main.py :
    from tontine_lightning import LightningLNbits
    from .lightning import Facture
    lightning = LightningLNbits.depuis_env(fabrique_facture=Facture)
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .crypto import preimage_valide, sha256_hex
from .erreurs import (
    ErreurLNbits,
    FactureInvalide,
    VersementEchoue,
    VersementEnAttente,
    VersementRefuse,
)
from .factures import FactureDecodee, decoder_facture, exiger_montant, normaliser
from .lnbits import ClientLNbits, StatutPaiement
from .lnurl import demander_facture
from .registre import RegistreVersements
from .reglages import ETIQUETTE_APP, Reglages

journal = logging.getLogger("tontine_lightning")


@dataclass
class Facture:
    """Même forme que backend/app/lightning.py:Facture (utilisée si aucune fabrique n'est fournie)."""

    payment_request: str
    payment_hash: str


def cle_versement(adresse: str, montant_sats: int, description: str) -> str:
    """Clé d'idempotence d'un versement : mêmes arguments => même versement, jamais payé deux fois.

    Le moteur doit donc mettre dans `description` de quoi distinguer chaque tour
    (ex. « Cagnotte tontine t42 tour 3 »).
    """
    brut = f"{adresse.strip().lower()}|{montant_sats}|{description.strip()}"
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


class LightningLNbits:
    """Service Lightning réel, adossé à LNbits."""

    def __init__(
        self,
        reglages: Reglages,
        client: ClientLNbits | None = None,
        registre: RegistreVersements | None = None,
        http_lnurl: httpx.Client | None = None,
        fabrique_facture: Callable[..., Any] = Facture,
        horloge: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self.reglages = reglages
        self.client = client or ClientLNbits(reglages)
        self.registre = registre or RegistreVersements(reglages.chemin_registre)
        self._http_lnurl = http_lnurl
        self._fabrique = fabrique_facture
        self._horloge = horloge
        self._dormir = dormir

    @classmethod
    def depuis_env(cls, fabrique_facture: Callable[..., Any] = Facture, fichier_env: str | None = None) -> "LightningLNbits":
        return cls(Reglages.depuis_env(fichier_env), fabrique_facture=fabrique_facture)

    # ================================================================== réception
    def creer_facture(self, montant_sats: int, description: str) -> Any:
        """Crée la facture de cotisation. Le moteur l'affiche en QR code et garde le payment_hash."""
        self._verifier_montant(montant_sats, self.reglages.max_facture_sats, "facture")
        facture = self.client.creer_facture(
            montant_sats=montant_sats,
            memo=description[:640],
            expiration_s=self.reglages.expiration_facture_s,
            webhook=self.reglages.url_webhook,
            extra={"app": ETIQUETTE_APP},
        )
        # Défense en profondeur : on vérifie ce que LNbits nous a rendu avant de le montrer à un membre.
        decodee = decoder_facture(facture.bolt11, self.reglages.prefixe_bolt11)
        if decodee.payment_hash != facture.payment_hash:
            raise ErreurLNbits("Facture incohérente : payment_hash différent de celui annoncé par LNbits")
        try:
            exiger_montant(decodee, montant_sats)
        except FactureInvalide as exc:
            raise ErreurLNbits(f"Facture incohérente renvoyée par LNbits : {exc}") from exc
        return self._fabrique(payment_request=decodee.bolt11, payment_hash=decodee.payment_hash)

    # ================================================================== vérification d'une cotisation
    def cotisation_recue(self, payment_hash: str) -> bool:
        """Vrai seulement si LNbits a RÉELLEMENT reçu ce paiement sur le portefeuille de la tontine.

        IMPORTANT : pour une facture entrante, la préimage n'est PAS une preuve de paiement envers
        nous. LNbits la génère lui-même à la création de la facture (vérifié dans son code) :
        notre serveur la connaît donc avant tout paiement. La seule vérité est le statut chez LNbits.
        Le backend doit appeler cette méthode avant de confirmer une cotisation.
        """
        try:
            statut = self.client.statut(payment_hash.lower())
        except ErreurLNbits:
            return False
        return bool(
            statut.paye
            and (statut.montant_msat or 0) > 0  # entrant
            and statut.extra.get("app") == ETIQUETTE_APP
            and statut.preimage
            and preimage_valide(statut.preimage, payment_hash)
        )

    # ================================================================== versement
    def payer(self, adresse_lightning: str, montant_sats: int, description: str) -> str:
        """Verse `montant_sats` au bénéficiaire et retourne la préimage (preuve du versement).

        Idempotent : rappeler payer() avec les mêmes arguments ne repaie jamais un versement
        réussi ou en cours ; cela reprend son suivi. Deux mémoires : le registre local, et
        LNbits lui-même (extra.versement), au cas où le registre aurait été perdu.
        """
        self._verifier_montant(montant_sats, self.reglages.max_versement_sats, "versement")
        cle = cle_versement(adresse_lightning, montant_sats, description)
        delai = self.reglages.delai_suivi_versement_s

        existant = self.registre.lire(cle)
        if existant and existant.statut == "paye" and existant.preimage:
            return existant.preimage
        if existant and existant.statut == "en_cours":
            return self._suivre(cle, existant.payment_hash, delai)

        # Registre perdu ? LNbits se souvient des versements déjà envoyés pour cette clé.
        trace = self.client.chercher_versement(cle)
        if trace is not None:
            journal.warning("Versement %s… retrouvé chez LNbits (registre local incomplet) : suivi sans repayer", cle[:12])
            deja = self.registre.reserver(cle, trace.payment_hash, montant_sats, adresse_lightning)
            return self._suivre(cle, deja.payment_hash if deja else trace.payment_hash, delai)

        self._verifier_solde(montant_sats)
        decodee = self._facture_de_la_tentative(existant, adresse_lightning, montant_sats, description)

        connu = self.client.statut(decodee.payment_hash)
        if (connu.montant_msat or 0) > 0:
            # Payer une facture de notre propre portefeuille = paiement interne : rien ne sort, mais
            # une préimage valide serait renvoyée et le versement compté comme fait.
            raise VersementRefuse("Facture refusée : elle a été émise par le portefeuille de la tontine lui-même")

        # Journal d'intention : écrit AVANT l'envoi (voir registre.py).
        deja = self.registre.reserver(cle, decodee.payment_hash, montant_sats, adresse_lightning, decodee.bolt11)
        if deja is not None:
            if deja.statut == "paye" and deja.preimage:
                return deja.preimage
            return self._suivre(cle, deja.payment_hash, delai)
        if connu.paye:  # facture réutilisée déjà payée lors d'une tentative précédente
            return self._conclure_paye(cle, connu)

        journal.info("Versement %s… : %d sats vers %s", cle[:12], montant_sats, adresse_lightning)
        try:
            reponse = self.client.payer_facture(decodee.bolt11, extra={"app": ETIQUETTE_APP, "versement": cle})
        except ErreurLNbits as exc:
            # Jamais de conclusion sur la seule erreur HTTP : on demande à la source de vérité.
            try:
                apres = self.client.statut(decodee.payment_hash)
            except ErreurLNbits:
                apres = None
            refus_net = exc.code_http is not None and 400 <= exc.code_http < 500
            if refus_net and apres is not None and apres.statut == "inconnu":
                # LNbits a refusé AVANT d'enregistrer le paiement (facture rejetée…) : rien n'est parti.
                self.registre.marquer(cle, decodee.payment_hash, "echoue")
                raise VersementEchoue(f"LNbits a refusé le paiement : {exc}") from exc
            journal.warning("Issue de l'envoi incertaine (%s), vérification auprès de LNbits", exc)
            return self._suivre(cle, decodee.payment_hash, min(10.0, delai))

        if reponse is not None and reponse.paye:
            return self._conclure_paye(cle, reponse)
        return self._suivre(cle, decodee.payment_hash, delai)

    def reprendre_versements_en_cours(self) -> dict[str, str]:
        """À appeler au démarrage : tranche les versements interrompus par une panne. {cle: statut}."""
        bilan: dict[str, str] = {}
        for v in self.registre.en_cours():
            try:
                self._suivre(v.cle, v.payment_hash, 0)
                bilan[v.cle] = "paye"
            except VersementEchoue:
                bilan[v.cle] = "echoue"
            except (VersementEnAttente, ErreurLNbits):
                bilan[v.cle] = "en_cours"
        return bilan

    # ================================================================== interne
    @staticmethod
    def _verifier_montant(montant_sats: int, plafond: int, quoi: str) -> None:
        if not isinstance(montant_sats, int) or isinstance(montant_sats, bool) or montant_sats <= 0:
            raise VersementRefuse(f"Montant de {quoi} invalide : {montant_sats!r}")
        if montant_sats > plafond:
            raise VersementRefuse(f"Montant de {quoi} ({montant_sats} sats) au-dessus du plafond ({plafond} sats)")

    def _verifier_solde(self, montant_sats: int) -> None:
        frais_max = max(self.reglages.frais_min_sats, int(montant_sats * self.reglages.frais_max_pourcent / 100))
        solde = self.client.solde_sats()
        if solde < montant_sats + frais_max:
            raise VersementRefuse(
                f"Solde insuffisant : {solde} sats pour {montant_sats} sats + {frais_max} sats de frais max. "
                "La cagnotte ne couvre pas les frais de routage : alimenter la réserve de frais du portefeuille."
            )

    def _facture_de_la_tentative(self, precedent, destination: str, montant_sats: int, description: str) -> FactureDecodee:
        """Réutilise la facture d'une tentative abandonnée si LNbits ne l'a jamais vue et qu'elle est valide.

        Repayer le MÊME hash est sûr : si une requête « fantôme » finissait par passer, LND
        refuserait le second paiement du même hash. Une nouvelle facture (nouveau hash) n'a
        pas cette protection.
        """
        if precedent is not None and precedent.statut == "echoue" and precedent.bolt11:
            try:
                ancienne = decoder_facture(precedent.bolt11, self.reglages.prefixe_bolt11, time.time() + 60)
                exiger_montant(ancienne, montant_sats)
                if self.client.statut(ancienne.payment_hash).statut == "inconnu":
                    return ancienne
            except (FactureInvalide, ErreurLNbits):
                pass  # expirée ou douteuse : on en demande une nouvelle
        return self._obtenir_facture_beneficiaire(destination, montant_sats, description)

    def _obtenir_facture_beneficiaire(self, destination: str, montant_sats: int, description: str) -> FactureDecodee:
        """Adresse Lightning -> facture (LNURL), ou facture BOLT11 fournie directement. Contrôles stricts."""
        metadata: str | None = None
        if "@" in destination:
            demande = demander_facture(destination, montant_sats, self.reglages, description, self._http_lnurl)
            bolt11, metadata = demande.bolt11, demande.metadata
        else:
            bolt11 = normaliser(destination)

        try:
            decodee = decoder_facture(bolt11, self.reglages.prefixe_bolt11)
            exiger_montant(decodee, montant_sats)
        except FactureInvalide as exc:
            raise VersementRefuse(f"Facture du bénéficiaire refusée : {exc}") from exc

        if "@" in destination and self.reglages.lnurl_verifier_description_hash:
            # LUD-06 : prouve que la facture a bien été émise pour CETTE demande LNURL.
            if metadata is None or decodee.description_hash != sha256_hex(metadata):
                raise VersementRefuse("Facture du bénéficiaire refusée : description_hash ≠ SHA-256(metadata) (LUD-06)")
        return decodee

    def _etat_courant(self, cle: str) -> str:
        """Résultat à rendre quand notre suivi concernait une tentative qui n'est plus la courante."""
        courant = self.registre.lire(cle)
        if courant is not None and courant.statut == "paye" and courant.preimage:
            return courant.preimage
        if courant is not None and courant.statut == "en_cours":
            raise VersementEnAttente("Une autre tentative de ce versement est en cours", courant.payment_hash)
        raise VersementEchoue("Versement échoué : vous pouvez relancer")

    def _reservation_perimee(self, cle: str) -> bool:
        versement = self.registre.lire(cle)
        return versement is None or versement.age_s >= self.reglages.seuil_abandon_s

    def _conclure_paye(self, cle: str, statut: StatutPaiement) -> str:
        if not statut.preimage or not preimage_valide(statut.preimage, statut.payment_hash):
            # Ne devrait jamais arriver : on laisse « en_cours » pour une vérification humaine.
            raise ErreurLNbits("Paiement annoncé réussi mais préimage invalide : versement laissé en vérification")
        if not self.registre.marquer(cle, statut.payment_hash, "paye", statut.preimage):
            courant = self.registre.lire(cle)
            if courant is not None and courant.payment_hash != statut.payment_hash:
                journal.critical(
                    "Versement %s… : l'ancienne tentative %s a réussi alors que la tentative %s existe : à vérifier",
                    cle[:12], statut.payment_hash[:12], courant.payment_hash[:12],
                )
        journal.info("Versement %s… payé (frais : %s msat)", cle[:12], statut.frais_msat)
        return statut.preimage

    def _suivre(self, cle: str, payment_hash: str, delai_s: float) -> str:
        """Interroge LNbits jusqu'à succès, échec ou fin du délai.

        « failed » chez LNbits = échec certain. Mais « inconnu » (404) n'est PAS un échec :
        le paiement peut être réservé par un autre fil qui n'a pas encore appelé LNbits, ou
        encore en cours de traitement après un timeout. Conclure trop tôt à l'échec
        provoquerait un second paiement. On ne tranche qu'au-delà de `seuil_abandon_s`.
        """
        limite = self._horloge() + delai_s
        while True:
            statut = self.client.statut(payment_hash)
            if statut.paye:
                return self._conclure_paye(cle, statut)
            if statut.statut == "failed" or (statut.statut == "inconnu" and self._reservation_perimee(cle)):
                if not self.registre.marquer(cle, payment_hash, "echoue"):
                    return self._etat_courant(cle)  # une autre tentative a pris le relais : on ne l'écrase pas
                raise VersementEchoue(
                    f"Versement échoué ({statut.statut}) : aucun fonds n'est parti, vous pouvez relancer"
                )
            if self._horloge() >= limite:
                raise VersementEnAttente(
                    "Versement en cours sur le réseau Lightning : rappeler payer() plus tard (aucun double paiement)",
                    payment_hash,
                )
            self._dormir(1.0)
