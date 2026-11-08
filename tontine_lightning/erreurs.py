"""Exceptions de la couche Lightning.

Le moteur peut tout attraper avec `ErreurLightning`, ou distinguer :
- VersementRefuse   : bloqué AVANT tout envoi (plafond, solde, facture suspecte) -> rien n'est parti ;
- VersementEchoue   : envoyé mais échoué (pas de route…) -> on peut relancer sans risque ;
- VersementEnAttente: issue encore inconnue -> NE PAS considérer comme échoué ; rappeler payer()
                      avec les mêmes arguments plus tard : il reprendra le suivi sans repayer.
"""

from __future__ import annotations


class ErreurLightning(Exception):
    """Base de toutes les erreurs de la couche Lightning."""


class ErreurLNbits(ErreurLightning):
    """LNbits injoignable ou réponse invalide."""

    def __init__(self, message: str, code_http: int | None = None) -> None:
        super().__init__(message)
        self.code_http = code_http


class FactureInvalide(ErreurLightning):
    """Facture BOLT11 illisible, expirée, du mauvais réseau ou au mauvais montant."""


class AdresseLightningInvalide(ErreurLightning):
    """Adresse Lightning mal formée, serveur LNURL injoignable ou réponse non conforme."""


class VersementRefuse(ErreurLightning):
    """Versement bloqué par un garde-fou avant tout envoi."""


class VersementEchoue(ErreurLightning):
    """Le paiement a été tenté et a échoué : aucun fonds n'est parti, on peut relancer."""


class VersementEnAttente(ErreurLightning):
    """Le paiement est parti mais son issue n'est pas encore connue."""

    def __init__(self, message: str, payment_hash: str) -> None:
        super().__init__(message)
        self.payment_hash = payment_hash
