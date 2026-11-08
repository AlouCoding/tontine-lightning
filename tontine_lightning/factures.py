"""Décodage et contrôle LOCAL des factures BOLT11.

On ne fait confiance ni à LNbits ni au portefeuille du bénéficiaire pour nous dire ce que
contient une facture : on la décode nous-mêmes. Le décodage vérifie la somme de contrôle
bech32 et retrouve, à partir de la signature, la clé publique du nœud qui l'a émise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import bolt11

from .erreurs import FactureInvalide


@dataclass(frozen=True)
class FactureDecodee:
    bolt11: str
    reseau: str  # préfixe : bc, tb, tbs, bcrt
    montant_msat: int | None
    payment_hash: str
    description: str | None
    description_hash: str | None
    noeud_emetteur: str | None  # clé publique retrouvée depuis la signature
    expire_a: int  # timestamp unix

    @property
    def montant_sats(self) -> int | None:
        return None if self.montant_msat is None else self.montant_msat // 1000


def normaliser(facture: str) -> str:
    """Retire espaces et préfixe « lightning: » (QR codes), met en minuscules."""
    facture = facture.strip()
    if facture.lower().startswith("lightning:"):
        facture = facture[len("lightning:") :]
    return facture.lower()


def decoder_facture(facture: str, prefixe_attendu: str, maintenant: float | None = None) -> FactureDecodee:
    """Décode une facture et vérifie qu'elle est du bon réseau et pas expirée."""
    texte = normaliser(facture)
    try:
        brut = bolt11.decode(texte)
    except Exception as exc:  # la bibliothèque lève plusieurs types d'exceptions
        raise FactureInvalide(f"Facture illisible : {exc}") from exc

    if brut.currency != prefixe_attendu:
        # Empêche par exemple de payer une facture mainnet avec une config de test (ou l'inverse).
        raise FactureInvalide(f"Facture du réseau « {brut.currency} », attendu « {prefixe_attendu} »")
    if not brut.has_payment_hash:
        raise FactureInvalide("Facture sans payment_hash")

    expire_a = int(brut.date) + int(brut.expiry or 3600)
    if (maintenant if maintenant is not None else time.time()) >= expire_a:
        raise FactureInvalide("Facture expirée")

    montant = int(brut.amount_msat) if brut.amount_msat else None
    return FactureDecodee(
        bolt11=texte,
        reseau=brut.currency,
        montant_msat=montant,
        payment_hash=brut.payment_hash,
        description=brut.description,
        description_hash=brut.description_hash,
        noeud_emetteur=brut.payee,
        expire_a=expire_a,
    )


def exiger_montant(facture: FactureDecodee, montant_sats: int) -> None:
    """Refuse une facture sans montant ou dont le montant n'est pas exactement celui prévu."""
    if facture.montant_msat is None:
        raise FactureInvalide("Facture sans montant : refusée (le montant doit être fixé par la facture)")
    if facture.montant_msat != montant_sats * 1000:
        raise FactureInvalide(
            f"Montant de la facture ({facture.montant_msat / 1000:g} sats) ≠ montant prévu ({montant_sats} sats)"
        )
