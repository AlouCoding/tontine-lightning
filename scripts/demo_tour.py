"""Démo de bout en bout : un tour complet de tontine via l'API du backend, payé en Lightning (regtest).

Prérequis : réseau démarré (scripts/regtest.py init) et backend lancé en mode LNbits :
    set LIGHTNING_MODE=lnbits
    set LIGHTNING_ENV_FILE=..\\lightning\\.env
    python -m uvicorn app.main:app

Usage : python scripts/demo_tour.py [--fcfa 5000] [--api http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regtest import attendre, etape, lncli, ok  # noqa: E402  (mêmes outils que regtest.py)

# API locale : on ignore tout proxy système (sinon 127.0.0.1 peut partir vers le proxy et bloquer).
_OUVREUR = urllib.request.build_opener(urllib.request.ProxyHandler({}))

MEMBRES = [("Awa", "awa@regtest.local"), ("Bineta", "bineta@regtest.local"), ("Coumba", "coumba@regtest.local")]


def api(methode: str, url: str, corps: dict | None = None) -> dict:
    requete = urllib.request.Request(url, method=methode, data=json.dumps(corps).encode() if corps is not None else None)
    requete.add_header("Content-Type", "application/json")
    try:
        with _OUVREUR.open(requete, timeout=60) as rep:
            return json.loads(rep.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{methode} {url} -> {exc.code} : {exc.read().decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Backend injoignable ({url}) : lancez-le en mode LNbits") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Tour complet de tontine via l'API, payé en Lightning regtest")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--fcfa", type=int, default=5000)
    args = parser.parse_args()
    base = args.api.rstrip("/")

    etape(f"1. Awa crée une tontine de {len(MEMBRES)} membres à {args.fcfa} FCFA")
    r = api("POST", f"{base}/tontines", {
        "nom": f"Démo {time.strftime('%H:%M:%S')}", "montant_fcfa": args.fcfa, "nombre_membres": len(MEMBRES),
        "nom_organisateur": MEMBRES[0][0], "adresse_lightning": MEMBRES[0][1],
    })
    tid, jeton = r["tontine"]["id"], r["jeton_organisateur"]
    code = r["lien_invitation"].rsplit("/", 1)[-1]
    ok(f"tontine {tid}")

    etape("2. Les autres membres rejoignent avec le lien d'invitation")
    for nom, adresse in MEMBRES[1:]:
        api("POST", f"{base}/rejoindre/{code}", {"nom": nom, "adresse_lightning": adresse})
        ok(f"{nom} inscrite ({adresse})")

    etape("3. Tirage au sort de l'ordre de passage, puis démarrage")
    api("POST", f"{base}/tontines/{tid}/tirage", {"jeton_organisateur": jeton})
    t = api("POST", f"{base}/tontines/{tid}/demarrer", {"jeton_organisateur": jeton})
    noms = {m["id"]: m["nom"] for m in t["membres"]}
    beneficiaire = noms[t["tours"][0]["beneficiaire_id"]]
    ok(f"ordre : {' -> '.join(noms[i] for i in t['ordre'])} ; bénéficiaire du tour 1 : {beneficiaire}")

    etape("4. Chaque membre scanne son QR code et paie en Lightning")
    total = 0
    for membre_id, nom in noms.items():
        c = api("POST", f"{base}/tontines/{tid}/membres/{membre_id}/facture")
        total += c["montant_sats"]
        lncli("lnd-membres", "payinvoice", "--force", c["payment_request"])
        ok(f"{nom} a payé {c['montant_sats']} sats")

    etape("5. Le pont confirme chaque paiement au backend, qui verse la cagnotte")

    def tour_verse():
        tour = api("GET", f"{base}/tontines/{tid}")["tours"][0]
        statuts = [c["statut"] for c in tour["cotisations"].values()]
        print(f"    … cotisations : {statuts.count('paye')}/{len(statuts)} payées, versement : {tour['versement_statut']}", flush=True)
        return tour if tour["versement_statut"] == "fait" else None

    tour = attendre(tour_verse, "versement de la cagnotte", 90)
    ok(f"{beneficiaire} a reçu {tour['versement_sats']} sats (preuve : préimage {tour['versement_preimage'][:16]}…)")
    assert tour["versement_sats"] == total

    etape("6. Journal public de la tontine (ce que le registre Nostr publiera)")
    for e in api("GET", f"{base}/tontines/{tid}/evenements"):
        print(f"    #{e['numero']:>2} {e['type']}")

    print("\nTour complet réussi : création, inscriptions, tirage, 3 paiements Lightning, confirmation par le pont, versement.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, TimeoutError) as exc:
        print(f"\n[ERREUR] {exc}", file=sys.stderr)
        sys.exit(1)
