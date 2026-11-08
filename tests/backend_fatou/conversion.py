"""Substitut de backend/app/conversion.py (non fourni) : même signature, cours fixe pour les tests."""


def fcfa_vers_sats(fcfa: int, cours_btc_eur: float) -> int:
    # 1 € = 655,957 FCFA (parité fixe), 1 BTC = 100 000 000 sats
    return round(fcfa / 655.957 / cours_btc_eur * 100_000_000)


class SourceCours:
    def cours_btc_eur(self) -> float:
        return 90_000.0
