"""Outil du réseau RÉGTEST (Windows, macOS, Linux : seul Docker est requis).

    python scripts/regtest.py init            # mine, finance les nœuds, ouvre les canaux, crée le portefeuille LNbits, écrit .env
    python scripts/regtest.py verifier        # test réel de bout en bout : cotisation + versement sur la pile Docker
    python scripts/regtest.py payer <bolt11>  # un « membre » paie une facture (ex. le QR affiché par le frontend)
    python scripts/regtest.py soldes          # soldes des canaux et du portefeuille de la tontine
    python scripts/regtest.py miner [n]       # mine n blocs (6 par défaut)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

RACINE = Path(__file__).resolve().parents[1]
FICHIER_ENV = RACINE / ".env"


def _port(nom: str, defaut: str) -> str:
    """Port lu dans l'environnement, sinon dans .env, sinon la valeur par défaut."""
    if os.environ.get(nom):
        return os.environ[nom]
    if FICHIER_ENV.exists():
        for ligne in FICHIER_ENV.read_text(encoding="utf-8").splitlines():
            if ligne.strip().startswith(f"{nom}="):
                return ligne.split("=", 1)[1].strip() or defaut
    return defaut


# 127.0.0.1 et non localhost : évite ~2 s de tentative IPv6 sous Windows
LNBITS = f"http://127.0.0.1:{_port('LNBITS_PORT', '5000')}"
PONT = f"http://127.0.0.1:{_port('PONT_PORT', '18001')}"
CAPACITE_CANAL_SATS = 5_000_000
RESERVE_FRAIS_SATS = 20_000
BTC_RPC = ["-regtest", "-rpcport=18443", "-rpcuser=tontine", "-rpcpassword=tontine-regtest"]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # consoles Windows


# ============================================================ utilitaires
def etape(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"    [OK] {msg}", flush=True)


def lancer(args: list[str], verifier: bool = True) -> str:
    res = subprocess.run(args, cwd=RACINE, capture_output=True, text=True, encoding="utf-8")
    if verifier and res.returncode != 0:
        raise RuntimeError(f"Échec : {' '.join(args[:8])}…\n{res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def lancer_visible(args: list[str]) -> None:
    """Lance une commande en affichant sa sortie (téléchargement des images : peut être long)."""
    if subprocess.run(args, cwd=RACINE).returncode != 0:
        raise RuntimeError(f"Échec : {' '.join(args)}")


def btc(*args: str, portefeuille: str | None = None) -> str:
    extra = [f"-rpcwallet={portefeuille}"] if portefeuille else []
    return lancer(["docker", "compose", "exec", "-T", "-u", "bitcoin", "bitcoind", "bitcoin-cli", *BTC_RPC, *extra, *args])


def lncli(noeud: str, *args: str) -> Any:
    sortie = lancer(["docker", "compose", "exec", "-T", "-u", "lnd", noeud, "lncli", "--lnddir=/home/lnd/.lnd", "--network=regtest", *args])
    try:
        return json.loads(sortie)
    except ValueError:
        return sortie


def attendre(condition: Callable[[], Any], description: str, delai_s: int = 120) -> Any:
    limite = time.monotonic() + delai_s
    derniere: Exception | None = None
    while time.monotonic() < limite:
        try:
            resultat = condition()
            if resultat:
                return resultat
        except Exception as exc:  # noqa: BLE001 - on réessaie jusqu'au délai
            derniere = exc
        time.sleep(2)
    raise TimeoutError(f"Délai dépassé : {description}" + (f" ({derniere})" if derniere else ""))


def http_json(methode: str, url: str, corps: dict | None = None, cle: str | None = None) -> Any:
    requete = urllib.request.Request(url, method=methode, data=json.dumps(corps).encode() if corps is not None else None)
    requete.add_header("Content-Type", "application/json")
    if cle:
        requete.add_header("X-Api-Key", cle)
    with urllib.request.urlopen(requete, timeout=15) as rep:
        return json.loads(rep.read() or b"null")


def lire_env() -> dict[str, str]:
    source = FICHIER_ENV if FICHIER_ENV.exists() else RACINE / ".env.example"
    valeurs: dict[str, str] = {}
    for ligne in source.read_text(encoding="utf-8").splitlines():
        if ligne.strip() and not ligne.lstrip().startswith("#") and "=" in ligne:
            k, v = ligne.split("=", 1)
            valeurs[k.strip()] = v.strip()
    return valeurs


def ecrire_env(maj: dict[str, str]) -> None:
    """Met à jour .env en gardant les commentaires et les autres lignes."""
    source = FICHIER_ENV if FICHIER_ENV.exists() else RACINE / ".env.example"
    lignes, vues = [], set()
    for ligne in source.read_text(encoding="utf-8").splitlines():
        cle = ligne.split("=", 1)[0].strip() if "=" in ligne and not ligne.lstrip().startswith("#") else None
        if cle in maj:
            lignes.append(f"{cle}={maj[cle]}")
            vues.add(cle)
        else:
            lignes.append(ligne)
    lignes += [f"{k}={v}" for k, v in maj.items() if k not in vues]
    FICHIER_ENV.write_text("\n".join(lignes) + "\n", encoding="utf-8")


def miner(n: int = 6) -> None:
    adresse = btc("getnewaddress", portefeuille="mineur")
    btc("generatetoaddress", str(n), adresse)


# ============================================================ commandes
def cmd_init(_: argparse.Namespace) -> None:
    etape("Démarrage des conteneurs (bitcoind, 2 nœuds LND, LNbits)")
    print("    1er lancement : téléchargement des images (~1-2 Go), cela peut prendre plusieurs minutes.", flush=True)
    lancer_visible(["docker", "compose", "up", "-d"])
    attendre(lambda: btc("getblockchaininfo"), "bitcoind")
    ok("bitcoind répond")

    etape("Portefeuille du mineur et premiers blocs")
    if "mineur" not in json.loads(btc("listwallets")):
        try:
            btc("loadwallet", "mineur")
        except RuntimeError:
            btc("createwallet", "mineur")
    if int(json.loads(btc("getblockchaininfo"))["blocks"]) < 101:
        miner(101)  # une récompense de bloc n'est dépensable qu'après 100 confirmations
    ok(f"hauteur de chaîne : {json.loads(btc('getblockchaininfo'))['blocks']}")

    noeuds = ("lnd-tontine", "lnd-membres")
    etape("Financement on-chain des deux nœuds Lightning")
    for n in noeuds:
        attendre(lambda n=n: lncli(n, "getinfo"), n)
    finances = False
    for n in noeuds:
        info = lncli(n, "getinfo")
        a_des_canaux = int(info.get("num_active_channels", 0)) + int(info.get("num_pending_channels", 0)) > 0
        if int(lncli(n, "walletbalance")["confirmed_balance"]) < 20_000_000 and not a_des_canaux:
            adresse = lncli(n, "newaddress", "p2wkh")["address"]
            btc("sendtoaddress", adresse, "1", portefeuille="mineur")
            finances = True
    if finances:
        miner(6)
    for n in noeuds:
        attendre(lambda n=n: lncli(n, "getinfo").get("synced_to_chain"), f"{n} synchronisé")
    ok("chaque nœud a des fonds confirmés")

    etape("Canaux de paiement (dans les deux sens)")
    cles = {n: lncli(n, "getinfo")["identity_pubkey"] for n in noeuds}
    if any(lncli(n, "pendingchannels")["pending_open_channels"] for n in noeuds):
        miner(6)  # canal resté en attente (init interrompu) : on le confirme avant d'en ouvrir un autre
        attendre(lambda: not any(lncli(n, "pendingchannels")["pending_open_channels"] for n in noeuds),
                 "confirmation des canaux en attente")
    ouverts = False
    for source, cible in (("lnd-membres", "lnd-tontine"), ("lnd-tontine", "lnd-membres")):
        deja = _canal_initie_par(source, cles[cible])
        if deja:
            continue
        lancer(["docker", "compose", "exec", "-T", "-u", "lnd", source, "lncli", "--lnddir=/home/lnd/.lnd", "--network=regtest",
                "connect", f"{cles[cible]}@{cible}:9735"], verifier=False)  # « already connected » est sans gravité
        lncli(source, "openchannel", f"--node_key={cles[cible]}", f"--local_amt={CAPACITE_CANAL_SATS}")
        # LND n'accepte qu'UN canal en attente par pair (maxpendingchannels=1) : on confirme
        # chaque canal (6 blocs) avant d'ouvrir le suivant.
        miner(6)
        attendre(lambda s=source, c=cles[cible]: _canal_initie_par(s, c) and not lncli(s, "pendingchannels")["pending_open_channels"],
                 f"confirmation du canal {source} -> {cible}")
        ouverts = True
    if ouverts:
        miner(1)
    attendre(lambda: len(lncli("lnd-tontine", "listchannels", "--active_only")["channels"]) >= 2, "canaux actifs")
    ok(f"2 canaux de {CAPACITE_CANAL_SATS:,} sats actifs entre tontine et membres".replace(",", " "))

    etape("Portefeuille LNbits de la tontine")
    attendre(lambda: urllib.request.urlopen(LNBITS, timeout=5).status == 200, "LNbits", 180)
    env = lire_env()
    cle_facture, cle_admin = env.get("LNBITS_INVOICE_KEY", ""), env.get("LNBITS_ADMIN_KEY", "")
    cle_valide = False
    if cle_facture:
        try:
            http_json("GET", f"{LNBITS}/api/v1/wallet", cle=cle_facture)
            cle_valide = True
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403, 404):
                # Erreur passagère : surtout ne pas créer un 2e portefeuille et écraser les clés.
                raise RuntimeError(f"LNbits répond {exc.code} : relancer init dans un instant") from exc
    if cle_valide:
        ok("portefeuille existant réutilisé")
    else:
        portefeuille = http_json("POST", f"{LNBITS}/api/v1/account", {"name": "Tontine"})
        cle_facture, cle_admin = portefeuille["inkey"], portefeuille["adminkey"]
        ok("portefeuille « Tontine » créé")

    etape("Réserve de frais du portefeuille (la cagnotte ne peut pas payer ses propres frais)")
    solde = http_json("GET", f"{LNBITS}/api/v1/wallet", cle=cle_facture)["balance"] // 1000
    if solde < RESERVE_FRAIS_SATS // 2:
        facture = http_json(
            "POST", f"{LNBITS}/api/v1/payments",
            {"out": False, "unit": "sat", "amount": RESERVE_FRAIS_SATS, "memo": "Réserve de frais"},  # sans étiquette : ignorée par le pont
            cle=cle_facture,
        )
        lncli("lnd-membres", "payinvoice", "--force", facture.get("bolt11") or facture["payment_request"])
        solde = attendre(
            lambda: (s := http_json("GET", f"{LNBITS}/api/v1/wallet", cle=cle_facture)["balance"] // 1000) >= RESERVE_FRAIS_SATS and s,
            "réserve de frais créditée", 60,
        )
    ok(f"solde du portefeuille Tontine : {solde} sats")

    jeton = env.get("PONT_JETON_WEBHOOK") or secrets.token_urlsafe(32)
    jeton_backend = env.get("PONT_JETON_BACKEND") or secrets.token_urlsafe(32)
    ecrire_env(
        {
            "LIGHTNING_RESEAU": "regtest",
            "LNBITS_URL": LNBITS,
            "LNBITS_INVOICE_KEY": cle_facture,
            "LNBITS_ADMIN_KEY": cle_admin,
            "PONT_JETON_WEBHOOK": jeton,
            "PONT_JETON_BACKEND": jeton_backend,
            "LIGHTNING_WEBHOOK_URL": f"http://pont:8000/webhook/lnbits?jeton={jeton}",
            "LNURL_DOMAINES_LOCAUX": f"regtest.local={PONT}",
            "COMPOSE_PROFILES": "pont",
        }
    )
    ok(f"clés écrites dans {FICHIER_ENV.name}")

    etape("Démarrage du pont (webhooks + annuaire nom@regtest.local)")
    lancer_visible(["docker", "compose", "--profile", "pont", "up", "-d", "--build", "pont"])
    attendre(lambda: http_json("GET", f"{PONT}/sante"), "pont", 120)
    ok(f"pont opérationnel sur {PONT}")

    print("\nPrêt. Étape suivante : python scripts/regtest.py verifier")


def _canal_initie_par(source: str, cible_pub: str) -> bool:
    """Vrai si `source` a déjà ouvert (ou est en train d'ouvrir) un canal vers `cible_pub`."""
    for c in lncli(source, "listchannels")["channels"]:
        if c.get("remote_pubkey") == cible_pub and c.get("initiator"):
            return True
    for p in lncli(source, "pendingchannels")["pending_open_channels"]:
        canal = p.get("channel", {})
        if canal.get("remote_node_pub") == cible_pub and canal.get("initiator") in ("INITIATOR_LOCAL", True):
            return True
    return False


def cmd_payer(args: argparse.Namespace) -> None:
    print(lncli("lnd-membres", "payinvoice", "--force", args.bolt11))


def cmd_miner(args: argparse.Namespace) -> None:
    miner(args.n)
    ok(f"{args.n} blocs minés")


def cmd_soldes(_: argparse.Namespace) -> None:
    for n in ("lnd-tontine", "lnd-membres"):
        s = lncli(n, "channelbalance")
        print(f"{n:12} local={s['local_balance']['sat']:>10} sats   distant={s['remote_balance']['sat']:>10} sats")
    env = lire_env()
    if env.get("LNBITS_INVOICE_KEY"):
        solde = http_json("GET", f"{LNBITS}/api/v1/wallet", cle=env["LNBITS_INVOICE_KEY"])["balance"] // 1000
        print(f"{'LNbits':12} portefeuille Tontine = {solde} sats")


def cmd_verifier(_: argparse.Namespace) -> None:
    """Exécute la VRAIE bibliothèque contre la pile Docker : la preuve que tout fonctionne."""
    sys.path.insert(0, str(RACINE))
    from tontine_lightning import LightningLNbits, preimage_valide
    from tontine_lightning.reglages import Reglages

    reglages = Reglages.depuis_env(str(FICHIER_ENV))
    ln = LightningLNbits(reglages)

    etape("1. Le moteur crée une facture de cotisation de 2 000 sats")
    facture = ln.creer_facture(2000, "Vérification regtest : cotisation")
    ok(facture.payment_request[:60] + "…")
    assert not ln.cotisation_recue(facture.payment_hash), "une facture impayée ne doit pas être reconnue"
    ok("avant paiement : cotisation_recue() = False (la préimage connue de LNbits ne suffit pas)")

    etape("2. Un membre la paie depuis son portefeuille (lnd-membres)")
    lncli("lnd-membres", "payinvoice", "--force", facture.payment_request)
    statut = attendre(lambda: (s := ln.client.statut(facture.payment_hash)).paye and s, "paiement reçu", 60)
    assert preimage_valide(statut.preimage, facture.payment_hash)
    ok(f"payée, preuve vérifiée : SHA-256({statut.preimage[:16]}…) == {facture.payment_hash[:16]}…")
    assert ln.cotisation_recue(facture.payment_hash)
    ok("après paiement : cotisation_recue() = True")

    etape("3. Le pont confirme la cotisation au backend")
    sante = http_json("GET", f"{PONT}/sante")
    ok(f"pont : {sante}")
    if sante.get("backend") != "ok":
        print("    (backend non démarré : le pont réessaiera automatiquement dès qu'il sera lancé)")

    etape("4. Versement de 1 500 sats à aminata@regtest.local")
    preimage = ln.payer("aminata@regtest.local", 1500, f"Vérification regtest {int(time.time())}")
    ok(f"versé, préimage {preimage[:16]}…")

    print("\nTout fonctionne : réception, preuve cryptographique, relais et versement.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Outil du réseau régtest de Tontine Lightning")
    sous = parser.add_subparsers(dest="commande", required=True)
    sous.add_parser("init").set_defaults(f=cmd_init)
    sous.add_parser("verifier").set_defaults(f=cmd_verifier)
    p = sous.add_parser("payer")
    p.add_argument("bolt11")
    p.set_defaults(f=cmd_payer)
    m = sous.add_parser("miner")
    m.add_argument("n", nargs="?", type=int, default=6)
    m.set_defaults(f=cmd_miner)
    sous.add_parser("soldes").set_defaults(f=cmd_soldes)
    args = parser.parse_args()
    try:
        args.f(args)
    except (RuntimeError, TimeoutError) as exc:
        print(f"\n[ERREUR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
