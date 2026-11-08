"""Primitives cryptographiques d'un paiement Lightning.

Une facture contient payment_hash = SHA-256(preimage). Le secret `preimage` n'est connu
que du destinataire, qui ne le révèle qu'en encaissant le paiement : posséder une
préimage qui correspond à l'empreinte prouve donc que la facture a été payée.
"""

from __future__ import annotations

import hashlib
import hmac


def est_hex32(valeur: str) -> bool:
    """Vrai si `valeur` représente exactement 32 octets en hexadécimal."""
    if not isinstance(valeur, str) or len(valeur) != 64:
        return False
    try:
        bytes.fromhex(valeur)
    except ValueError:
        return False
    return True


def preimage_valide(preimage_hex: str, payment_hash_hex: str) -> bool:
    """Vérifie que SHA-256(preimage) == payment_hash (comparaison à temps constant)."""
    if not (est_hex32(preimage_hex) and est_hex32(payment_hash_hex)):
        return False
    empreinte = hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest()
    return hmac.compare_digest(empreinte, payment_hash_hex.lower())


def sha256_hex(texte: str) -> str:
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()
