"""Tests de la remise en état du réseau régtest après un redémarrage de Docker ou du PC.

Docker n'est pas lancé ici : un faux réseau (FauxReseau) remplace bitcoind, les deux nœuds LND et LNbits,
et reproduit les pannes vues en vrai (portefeuille déchargé, nœud non synchronisé, nœuds déconnectés,
LNbits qui ne joint plus lnd-tontine, chaîne de bitcoind remise à zéro).
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

_CHEMIN = Path(__file__).resolve().parents[1] / "scripts" / "regtest.py"
_spec = importlib.util.spec_from_file_location("regtest_script", _CHEMIN)
regtest = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(regtest)


class FauxReseau:
    """État minimal d'un réseau régtest, modifié par les commandes comme le ferait Docker."""

    def __init__(self) -> None:
        self.portefeuilles: list[str] = ["mineur"]
        self.hauteur = 300
        self.hauteur_lnd = {"lnd-tontine": 300, "lnd-membres": 300}
        self.synchro = {"lnd-tontine": True, "lnd-membres": True}
        self.connectes = True
        self.nb_canaux = 2
        self.lnbits_joint_lnd = True
        self.cle_lnbits = "cle-facture"
        self.journal: list[str] = []  # actions « coûteuses » faites, pour les vérifier

    # ---- ce que le script appelle
    def btc(self, *args: str, portefeuille: str | None = None) -> str:
        if args[0] == "getblockchaininfo":
            return json.dumps({"blocks": self.hauteur})
        if args[0] == "listwallets":
            return json.dumps(self.portefeuilles)
        if args[0] in ("loadwallet", "createwallet"):
            self.portefeuilles.append(args[1])
            self.journal.append(args[0])
            return "{}"
        raise AssertionError(f"commande bitcoind inattendue : {args}")

    def lncli(self, noeud: str, *args: str) -> Any:
        if args[0] == "getinfo":
            return {"block_height": self.hauteur_lnd[noeud], "synced_to_chain": self.synchro[noeud],
                    "identity_pubkey": f"{noeud}-pub"}
        if args[0] == "listchannels":
            actifs = "--active_only" in args
            n = self.nb_canaux if (self.connectes or not actifs) else 0
            return {"channels": [{"chan_id": i} for i in range(n)]}
        if args[0] == "listpeers":
            return {"peers": [{"pub_key": "lnd-membres-pub"}] if self.connectes else []}
        raise AssertionError(f"commande lncli inattendue : {args}")

    def miner(self, n: int = 6) -> None:
        self.hauteur += n
        self.hauteur_lnd = {k: self.hauteur for k in self.hauteur_lnd}
        self.synchro = {k: True for k in self.synchro}
        self.journal.append(f"miner {n}")

    def lancer(self, args: list[str], verifier: bool = True) -> str:
        if "connect" in args:
            self.connectes = True
            self.journal.append("connect")
        return ""

    def lancer_visible(self, args: list[str]) -> None:
        if "restart" in args and "lnbits" in args:
            self.lnbits_joint_lnd = True
            self.journal.append("restart lnbits")

    def http_json(self, methode: str, url: str, corps: dict | None = None, cle: str | None = None) -> Any:
        assert methode == "POST" and url.endswith("/api/v1/payments")
        assert corps and corps["out"] is False and corps["amount"] == 1, "la sonde ne doit JAMAIS envoyer d'argent"
        if cle != self.cle_lnbits:
            raise urllib.error.HTTPError(url, 401, "clé invalide", None, None)  # type: ignore[arg-type]
        if not self.lnbits_joint_lnd:
            raise urllib.error.HTTPError(url, 520, "Unable to connect to https://lnd-tontine:8080", None, None)  # type: ignore[arg-type]
        return {"payment_hash": "x"}


class _Ouvreur:
    @staticmethod
    def open(url: str, timeout: int = 5) -> Any:
        return type("Rep", (), {"status": 200})()


@pytest.fixture
def reseau(monkeypatch: pytest.MonkeyPatch) -> FauxReseau:
    r = FauxReseau()
    for nom in ("btc", "lncli", "miner", "lancer", "lancer_visible", "http_json"):
        monkeypatch.setattr(regtest, nom, getattr(r, nom))
    monkeypatch.setattr(regtest, "_OUVREUR", _Ouvreur())
    monkeypatch.setattr(regtest, "lire_env", lambda: {"LNBITS_INVOICE_KEY": r.cle_lnbits})
    monkeypatch.setattr(regtest.time, "sleep", lambda s: None)
    return r


def test_rien_a_faire_si_tout_va_bien(reseau: FauxReseau) -> None:
    assert regtest.reparer_apres_redemarrage() == []
    assert reseau.journal == [], "aucune action ne doit être faite sur un réseau sain"


def test_redemarrage_complet_les_quatre_reparations(reseau: FauxReseau) -> None:
    """Ce que Fatou a vu après un redémarrage de Docker : tout est cassé en même temps."""
    reseau.portefeuilles = []
    reseau.synchro = {"lnd-tontine": False, "lnd-membres": False}
    reseau.connectes = False
    reseau.lnbits_joint_lnd = False

    faits = regtest.reparer_apres_redemarrage()

    assert len(faits) == 4
    assert reseau.journal == ["loadwallet", "miner 1", "connect", "connect", "restart lnbits"]
    assert reseau.portefeuilles == ["mineur"] and all(reseau.synchro.values())
    assert reseau.connectes and reseau.lnbits_joint_lnd


def test_relancer_une_deuxieme_fois_ne_fait_rien(reseau: FauxReseau) -> None:
    reseau.portefeuilles, reseau.connectes, reseau.lnbits_joint_lnd = [], False, False
    regtest.reparer_apres_redemarrage()
    reseau.journal.clear()
    assert regtest.reparer_apres_redemarrage() == [] and reseau.journal == []


def test_seulement_lnbits_deconnecte(reseau: FauxReseau) -> None:
    """La panne exacte du 17 novembre : « Unable to connect to https://lnd-tontine:8080 »."""
    reseau.lnbits_joint_lnd = False
    assert regtest.reparer_apres_redemarrage() == ["LNbits redémarré et reconnecté à lnd-tontine"]
    assert reseau.journal == ["restart lnbits"]


def test_chaine_perdue_message_clair_et_aucune_action(reseau: FauxReseau) -> None:
    """« Block height out of range » : bitcoind a perdu sa chaîne, LND s'en souvient. Pas réparable."""
    reseau.hauteur = 0
    with pytest.raises(RuntimeError, match="docker compose down -v"):
        regtest.reparer_apres_redemarrage()
    assert "miner 1" not in reseau.journal, "ne jamais miner par-dessus une chaîne perdue"


def test_premier_lancement_ne_touche_a_rien(reseau: FauxReseau) -> None:
    """Réseau neuf : pas encore de blocs, de canaux ni de portefeuille LNbits. init s'en charge ensuite."""
    reseau.portefeuilles = []
    reseau.hauteur = 0
    reseau.hauteur_lnd = {"lnd-tontine": 0, "lnd-membres": 0}
    reseau.synchro = {"lnd-tontine": False, "lnd-membres": False}
    reseau.nb_canaux = 0
    reseau.cle_lnbits = ""

    regtest.reparer_apres_redemarrage()

    assert "miner 1" not in reseau.journal and "connect" not in reseau.journal
    assert "restart lnbits" not in reseau.journal


def test_cle_lnbits_perimee_n_est_pas_une_panne_lnd(reseau: FauxReseau, monkeypatch: pytest.MonkeyPatch) -> None:
    """Après down -v, l'ancienne clé du .env est refusée (401) : c'est à init de recréer le portefeuille."""
    monkeypatch.setattr(regtest, "lire_env", lambda: {"LNBITS_INVOICE_KEY": "ancienne-cle"})
    assert regtest.reparer_apres_redemarrage() == []
    assert "restart lnbits" not in reseau.journal
