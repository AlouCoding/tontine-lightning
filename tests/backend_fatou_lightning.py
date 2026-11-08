"""Copie conforme de backend/app/lightning.py (Ndèye Fatou Sow), pour tester la compatibilité."""

import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Facture:
    payment_request: str  # la facture BOLT11, affichee en QR code
    payment_hash: str     # empreinte SHA-256 de la preimage, en hexadecimal


class Lightning(Protocol):
    def creer_facture(self, montant_sats: int, description: str) -> Facture: ...

    def payer(self, adresse_lightning: str, montant_sats: int, description: str) -> str:
        """Envoie le paiement et renvoie la preimage (hexadecimal) en cas de succes."""
        ...


def preimage_valide(preimage_hex: str, payment_hash_hex: str) -> bool:
    """Verifie que SHA-256(preimage) correspond a l'empreinte de la facture."""
    try:
        empreinte = hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest()
    except ValueError:
        return False
    return secrets.compare_digest(empreinte, payment_hash_hex.lower())
