"""Adresse Lightning (LUD-16) -> facture BOLT11, avec protection SSRF.

Flux (https://github.com/lnurl/luds) :
1. GET https://<domaine>/.well-known/lnurlp/<nom>
   -> {"tag": "payRequest", "callback", "minSendable", "maxSendable", "metadata"}   (millisats)
2. GET <callback>?amount=<millisats>  -> {"pr": "<facture BOLT11>"}
3. LUD-06 : la facture DOIT avoir description_hash == SHA-256(metadata), sinon on refuse.

SSRF : l'adresse est fournie par un utilisateur et notre serveur va appeler son domaine.
Sans contrôle, « x@127.0.0.1 » ou « x@169.254.169.254 » ferait appeler nos services internes.
On exige donc HTTPS et une IP publique (sauf en regtest, où tout est local).
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .erreurs import AdresseLightningInvalide
from .reglages import Reglages

_FORMAT_ADRESSE = re.compile(r"^[a-z0-9._+-]{1,64}@([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


@dataclass(frozen=True)
class DemandeLNURL:
    bolt11: str
    metadata: str | None  # chaîne brute, pour vérifier le description_hash


def analyser_adresse(adresse: str) -> tuple[str, str]:
    """Valide le format « nom@domaine » et retourne (nom, domaine) en minuscules."""
    adresse = adresse.strip().lower()
    if not _FORMAT_ADRESSE.match(adresse):
        raise AdresseLightningInvalide(f"Adresse Lightning invalide : {adresse!r}")
    nom, domaine = adresse.split("@", 1)
    return nom, domaine


TAILLE_MAX_REPONSE = 64 * 1024  # une réponse LNURL fait quelques centaines d'octets


def _ips_publiques(hote: str, port: int) -> list[str]:
    """Résout `hote` et exige que TOUTES ses adresses soient publiques."""
    try:
        ip_litterale = ipaddress.ip_address(hote)
        adresses = [ip_litterale]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hote, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise AdresseLightningInvalide(f"Domaine introuvable : {hote}") from exc
        adresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not adresses or any(not ip.is_global for ip in adresses):
        raise AdresseLightningInvalide(f"Le domaine {hote} pointe vers une adresse non publique")
    return [str(ip) for ip in adresses]


def verifier_url_sortante(url: str, autoriser_prive: bool) -> str | None:
    """Contrôle une URL avant de l'appeler. Retourne l'IP vérifiée à utiliser (None si réseau privé autorisé)."""
    parties = urlparse(url)
    if not parties.hostname:
        raise AdresseLightningInvalide(f"URL sans hôte : {url!r}")
    if parties.username or parties.password:
        raise AdresseLightningInvalide("URL avec identifiants interdite")
    if autoriser_prive:
        if parties.scheme not in ("http", "https"):
            raise AdresseLightningInvalide(f"Schéma interdit : {parties.scheme}")
        return None
    if parties.scheme != "https":
        raise AdresseLightningInvalide("Seules les URL HTTPS sont acceptées")
    return _ips_publiques(parties.hostname, parties.port or 443)[0]


def _get_json(http: httpx.Client, url: httpx.URL, autoriser_prive: bool) -> dict[str, Any]:
    ip = verifier_url_sortante(str(url), autoriser_prive)
    hote = url.host
    if ip is not None:
        # Anti « DNS rebinding » : on se connecte à l'IP QU'ON VIENT DE VÉRIFIER, au lieu de laisser
        # httpx refaire une résolution DNS (qui pourrait cette fois renvoyer 127.0.0.1).
        # Le nom reste utilisé pour l'en-tête Host et la vérification TLS (SNI).
        requete = http.build_request(
            "GET", str(url.copy_with(host=ip)), headers={"Host": url.netloc.decode()}, extensions={"sni_hostname": hote}
        )
    else:
        requete = http.build_request("GET", str(url))
    try:
        rep = http.send(requete, stream=True)
        try:
            rep.raise_for_status()
            corps = b""
            for morceau in rep.iter_bytes():
                corps += morceau
                if len(corps) > TAILLE_MAX_REPONSE:
                    raise AdresseLightningInvalide("Réponse LNURL trop volumineuse")
        finally:
            rep.close()
        donnees = json.loads(corps)
    except (httpx.HTTPError, ValueError) as exc:
        raise AdresseLightningInvalide(f"Serveur LNURL injoignable ou réponse invalide : {exc.__class__.__name__}") from exc
    if not isinstance(donnees, dict):
        raise AdresseLightningInvalide("Réponse LNURL inattendue")
    if str(donnees.get("status", "")).upper() == "ERROR":
        raise AdresseLightningInvalide(f"Erreur LNURL : {donnees.get('reason', 'inconnue')}")
    return donnees


def demander_facture(
    adresse: str,
    montant_sats: int,
    reglages: Reglages,
    commentaire: str | None = None,
    http: httpx.Client | None = None,
) -> DemandeLNURL:
    """Demande au portefeuille du bénéficiaire une facture de `montant_sats` sats."""
    nom, domaine = analyser_adresse(adresse)
    base = reglages.lnurl_domaines_locaux.get(domaine, f"https://{domaine}")
    proprietaire = http is None
    # Pas de redirection (contournerait le contrôle SSRF), pas de proxy hérité de l'environnement.
    http = http or httpx.Client(timeout=httpx.Timeout(10.0), follow_redirects=False, trust_env=False)
    prive = reglages.lnurl_autoriser_reseau_prive
    try:
        meta = _get_json(http, httpx.URL(f"{base}/.well-known/lnurlp/{nom}"), prive)
        if meta.get("tag") != "payRequest" or not meta.get("callback"):
            raise AdresseLightningInvalide("Cette adresse n'accepte pas les paiements LNURL-pay")
        montant_msat = montant_sats * 1000
        minimum, maximum = int(meta.get("minSendable", 1000)), int(meta.get("maxSendable", 0))
        if montant_msat < minimum or (maximum and montant_msat > maximum):
            raise AdresseLightningInvalide(
                f"Montant refusé par le portefeuille (accepte {minimum // 1000} à {maximum // 1000} sats)"
            )
        params: dict[str, Any] = {"amount": montant_msat}
        autorise = int(meta.get("commentAllowed", 0) or 0)
        if commentaire and autorise > 0:
            params["comment"] = commentaire[:autorise]
        # LUD-06 : le callback peut déjà contenir des paramètres ; on les complète au lieu de les remplacer.
        try:
            url_callback = httpx.URL(str(meta["callback"])).copy_merge_params(params)
        except (httpx.InvalidURL, TypeError) as exc:
            raise AdresseLightningInvalide("URL de callback LNURL invalide") from exc
        reponse = _get_json(http, url_callback, prive)
        bolt11 = reponse.get("pr")
        if not isinstance(bolt11, str) or not bolt11:
            raise AdresseLightningInvalide("Le portefeuille n'a pas renvoyé de facture")
        metadata = meta.get("metadata")
        return DemandeLNURL(bolt11=bolt11, metadata=metadata if isinstance(metadata, str) else None)
    finally:
        if proprietaire:
            http.close()
