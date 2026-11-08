"""Tests des briques de sécurité : cryptographie, factures, SSRF, réglages."""

from __future__ import annotations

import hashlib

import pytest

from tontine_lightning import AdresseLightningInvalide, FactureInvalide, Reglages, preimage_valide
from tontine_lightning.factures import decoder_facture
from tontine_lightning.lnurl import analyser_adresse, verifier_url_sortante

from .faux_services import signer_facture


def test_preimage() -> None:
    pre = "ab" * 32
    ph = hashlib.sha256(bytes.fromhex(pre)).hexdigest()
    assert preimage_valide(pre, ph) and preimage_valide(pre, ph.upper())
    assert not preimage_valide("cd" * 32, ph)
    assert not preimage_valide("zz" * 32, ph)
    assert not preimage_valide(pre, ph[:-2])


def test_decodage_local_signature_et_noeud() -> None:
    bolt11, _, ph = signer_facture(1520)
    f = decoder_facture(bolt11, "bcrt")
    assert f.payment_hash == ph and f.montant_sats == 1520
    assert f.noeud_emetteur and len(f.noeud_emetteur) == 66  # clé publique compressée retrouvée


def test_facture_alteree_refusee() -> None:
    bolt11, _, _ = signer_facture(1520)
    with pytest.raises(FactureInvalide):
        decoder_facture(bolt11[:-6] + "qqqqqq", "bcrt")


def test_mauvais_reseau_refuse() -> None:
    bolt11, _, _ = signer_facture(1520, reseau="bc")
    with pytest.raises(FactureInvalide, match="réseau"):
        decoder_facture(bolt11, "bcrt")


@pytest.mark.parametrize(
    "url",
    [
        "http://exemple.com/x",  # pas HTTPS
        "https://localhost/.well-known/lnurlp/a",  # boucle locale
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",  # métadonnées cloud
        "https://10.0.0.5/x",
    ],
)
def test_ssrf_bloque(url: str) -> None:
    with pytest.raises(AdresseLightningInvalide):
        verifier_url_sortante(url, autoriser_prive=False)


def test_regtest_autorise_reseau_local() -> None:
    verifier_url_sortante("http://localhost:8001/.well-known/lnurlp/a", autoriser_prive=True)
    with pytest.raises(AdresseLightningInvalide):
        verifier_url_sortante("file:///etc/passwd", autoriser_prive=True)


@pytest.mark.parametrize("adresse", ["sans-arobase", "a@b", "a@127.0.0.1", "a b@x.com", "a@exemple.com/../x"])
def test_format_adresse(adresse: str) -> None:
    with pytest.raises(AdresseLightningInvalide):
        analyser_adresse(adresse)


def test_reglages_masquent_les_cles() -> None:
    r = Reglages("http://x", "secret-facture", "secret-admin")
    assert "secret" not in repr(r)


def test_reglages_reseau_inconnu() -> None:
    with pytest.raises(ValueError):
        Reglages("http://x", "a", "b", reseau="litecoin")


def test_reglages_depuis_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LNBITS_URL", "http://localhost:5000/")
    monkeypatch.setenv("LNBITS_INVOICE_KEY", "i")
    monkeypatch.setenv("LNBITS_ADMIN_KEY", "a")
    monkeypatch.setenv("LIGHTNING_RESEAU", "mainnet")
    monkeypatch.setenv("LNURL_DOMAINES_LOCAUX", "regtest.local=http://localhost:8001/")
    r = Reglages.depuis_env()
    assert r.lnbits_url == "http://localhost:5000" and r.prefixe_bolt11 == "bc"
    assert r.lnurl_autoriser_reseau_prive is False  # jamais de réseau privé par défaut en mainnet
    assert r.lnurl_domaines_locaux == {"regtest.local": "http://localhost:8001"}
