# Intégration avec le moteur (backend/) — à voir avec Fatou

J'ai branché le **vrai** `backend/app/tontine.py` sur `LightningLNbits` dans les tests (`tests/test_integration_moteur.py`). Un tour complet fonctionne : 3 cotisations, confirmation, versement, démarrage du tour 2.

Le croisement des deux codes, puis une relecture de sécurité indépendante, font apparaître **4 corrections nécessaires dans `backend/`** (des petits changements). Chaque problème est **reproduit par un test**, et chaque correction est **prouvée** sur une copie de `tontine.py` (`tests/backend_fatou/tontine_corrige.py`).

---

## 0. 🔴 Une cotisation peut être confirmée sans avoir été payée (faille critique)

**Problème.** Pour une facture *entrante*, c'est LNbits qui tire la préimage au hasard, **à la création** de la facture, et qui la stocke (vérifié dans le code source de LNbits). Notre serveur connaît donc la préimage **avant** tout paiement. `SHA-256(préimage) == hash` prouve le paiement au *payeur*, mais ne prouve rien à *nous*.

Aujourd'hui, `confirmer_paiement` se contente d'une préimage valide, et `/paiements/confirmation` est public. Quelqu'un qui a la clé « invoice » (elle est dans `.env` et visible dans l'interface LNbits) lit les préimages des factures impayées. Il les poste au backend, le tour devient « complet » et **la cagnotte est versée avec l'argent des autres tontines**.

Test : `test_PROBLEME_C2_moteur_actuel_accepte_une_fausse_confirmation`.

**Correction, en deux couches :**

*a) Dans le moteur (`confirmer_paiement`)* : demander à LNbits si l'argent est réellement arrivé.
```python
            if not preimage_valide(preimage, payment_hash):
                raise ErreurTontine("Preimage invalide")
            # La preimage d'une facture ENTRANTE est generee par LNbits a la creation : le serveur la
            # connait avant tout paiement. Seule la reception constatee chez LNbits prouve le paiement.
            verifier = getattr(self.lightning, "cotisation_recue", None)
            if verifier is not None and not verifier(payment_hash):
                raise ErreurTontine("Paiement non recu par le noeud Lightning")
```
`LightningFactice` n'a pas cette méthode : les tests de Fatou ne changent pas.

*b) Dans la route (`main.py`)* : seul le pont peut appeler la confirmation.
```python
import secrets
from fastapi import Header

@app.post("/paiements/confirmation")
def confirmation(d: Confirmation, x_jeton_pont: str = Header(default="")):
    attendu = os.environ.get("PONT_JETON_BACKEND", "")
    if attendu and not secrets.compare_digest(x_jeton_pont.encode(), attendu.encode()):
        raise HTTPException(status_code=401, detail="Reserve au pont Lightning")
    return vue(executer(lambda: moteur.confirmer_paiement(d.payment_hash, d.preimage)))
```
`PONT_JETON_BACKEND` est généré par `scripts/regtest.py init` dans `lightning/.env`, que le backend charge.

Test de la correction : `test_CORRIGE_C2_fausse_confirmation_refusee`.

---

## 1. 🔴 Deux tontines homonymes partagent le même versement

**Problème.** `payer()` est idempotent sur `(adresse, montant, description)` : c'est ce qui empêche de verser deux fois. Or la description vaut `f"{t.nom} - cagnotte du tour {tour.numero}"`. Deux tontines nommées « Test », avec le même bénéficiaire, le même montant et le même numéro de tour, produisent donc la même clé. **La 2ᵉ cagnotte est marquée FAIT sans qu'aucun sat ne parte.** En démo, quand on recrée « Test » avec les mêmes adresses, c'est presque certain.

Test : `test_PROBLEME_deux_tontines_homonymes_partagent_la_cle_de_versement`.

**Correction (1 ligne, `_verser`)** : mettre l'identifiant unique de la tontine dans la description.
```python
preimage = self.lightning.payer(
    beneficiaire.adresse_lightning, total, f"{t.nom} - cagnotte du tour {tour.numero} ({t.id})")
```
⚠️ La description doit rester **stable** pour un même tour (pas d'horodatage) : c'est ce qui rend la relance sans danger.

---

## 2. 🟠 « Issue inconnue » affichée comme « échec », et blocage après une panne

**Problème A.** `_verser` fait `except Exception` et passe en `ECHEC`. Mais `payer()` peut lever `VersementEnAttente` : le paiement est **parti** et son issue n'est pas encore connue. Le membre voit « échec » alors que les fonds sont en route. La relance est sans danger (l'adaptateur reprend le suivi au lieu de repayer, voir `test_relance_apres_issue_incertaine_ne_paie_pas_deux_fois`), mais l'affichage est faux.

**Problème B.** Après un redémarrage, un tour resté `EN_COURS` est bloqué : `relancer_versement` n'accepte que `ECHEC` (le commentaire « il faut vérifier à la main » le dit).

**Correction** : grâce à l'idempotence, on peut relancer depuis `EN_COURS` sans risque.
```python
from tontine_lightning import VersementEnAttente   # en haut du fichier (ou un import protégé)

    def _verser(self, t: Tontine):
        ...
        try:
            preimage = self.lightning.payer(
                beneficiaire.adresse_lightning, total, f"{t.nom} - cagnotte du tour {tour.numero} ({t.id})")
        except VersementEnAttente:
            return                      # reste EN_COURS : fonds en vol, pas un échec
        except Exception:
            tour.versement_statut = StatutVersement.ECHEC
            self._sauver(t)
            return
        ...

    def relancer_versement(self, tontine_id: str, jeton: str) -> Tontine:
        with self._verrou:
            t = self.tontine(tontine_id)
            self._verifier_jeton(t, jeton)
            if t.tour_actuel is None or t.tour_actuel.versement_statut not in (
                    StatutVersement.ECHEC, StatutVersement.EN_COURS):
                raise ErreurTontine("Aucun versement a relancer")
            self._verser(t)             # idempotent : ne repaie jamais un versement parti
            return t

    def reprendre_versements(self):
        """Au démarrage : termine les versements interrompus par une panne."""
        with self._verrou:
            for t in self.tontines.values():
                if t.statut == Statut.EN_COURS and t.tour_actuel.versement_statut == StatutVersement.EN_COURS:
                    self._verser(t)
```
Pour garder les tests de Fatou indépendants de mon paquet, l'import peut être protégé :
```python
try:
    from tontine_lightning import VersementEnAttente
except ImportError:                     # LightningFactice seul
    class VersementEnAttente(Exception): ...
```

---

## 3. 🟠 La cagnotte ne peut pas payer ses propres frais

**Problème.** Le portefeuille reçoit exactement la somme des cotisations, puis doit envoyer cette somme **plus** les frais de routage Lightning (et LNbits exige une réserve de frais). Sans marge, le versement est refusé avant tout envoi. Il n'y a aucun risque, mais il n'y a pas de versement non plus.

Test : `test_PROBLEME_sans_reserve_de_frais_le_versement_echoue`.

**Décision prise** : le bénéficiaire reçoit **exactement** ce que les membres ont payé (c'est la promesse de la tontine). Le portefeuille garde une **réserve de frais** :
- en regtest, `scripts/regtest.py init` la crée automatiquement (20 000 sats) ;
- en mainnet, l'équipe dépose une fois quelques milliers de sats. Les frais réels sont de l'ordre de quelques sats par versement.

Rien à changer dans le moteur. Le message d'erreur l'indique explicitement si la réserve est vide.

---

## 4. Branchement dans `main.py`

Voir le [README](README.md#brancher-le-backend-à-faire-avec-fatou). Ajouter au démarrage, après la création du moteur :
```python
moteur.reprendre_versements()   # après la correction n° 2
```

---

## 5. Limites à connaître (pas de correction pour la démo)

| Point | Effet | Piste |
|---|---|---|
| `_verser` appelle `payer()` en tenant le verrou global du moteur | pendant un versement (≤ 10 s de suivi), les autres requêtes de l'API attendent | verser hors verrou, via une tâche de fond. Pour la démo, le délai a été réduit à 10 s et le pont attend jusqu'à 45 s |
| Un membre **exclu** paie quand même son ancienne facture | `confirmer_paiement` répond « Facture inconnue » et les fonds restent sur le portefeuille | rembourser à la main, ou ne pas retirer le hash de `_factures` et le rembourser automatiquement |
| La facture est réutilisée jusqu'au paiement | elle doit vivre au moins le délai du tour : l'expiration est maintenant de **7 jours** par défaut (`LIGHTNING_EXPIRATION_FACTURE_S`). Si une tontine a `delai_jours > 7`, augmenter cette valeur ou régénérer les factures expirées | régénérer si expirée, en gardant l'ancien hash connu |
| Plafond de versement à 500 000 sats par défaut | une grosse tontine (10 × 50 000 FCFA ≈ 850 000 sats) serait refusée | ajuster `LIGHTNING_MAX_VERSEMENT_SATS` au besoin réel ; le garder bas pour la démo mainnet (20 000) |
| Backend lancé hors Docker sous **Linux** | le pont le joint via `host.docker.internal` : uvicorn doit écouter sur `0.0.0.0`, donc il est visible sur le réseau local | grâce à la correction n° 0, une requête venue du réseau ne peut plus confirmer de faux paiement. Sous Windows/macOS (Docker Desktop), `127.0.0.1` suffit |

---

## Message à envoyer à Fatou

> Salut Fatou, j'ai branché ton moteur sur LNbits et un tour complet marche (testé avec ton vrai `tontine.py`). Avant de fusionner, il y a 4 petites modifs dans `backend/`, toutes détaillées dans `lightning/INTEGRATION.md`, avec un test pour chacune :
> 0. **faille critique** : LNbits connaît la préimage d'une facture dès sa création, donc une préimage valide ne prouve pas qu'on a été payé. Dans `confirmer_paiement`, il faut appeler `self.lightning.cotisation_recue(hash)`, et protéger `/paiements/confirmation` par le jeton du pont. Sinon, on peut faire verser une cagnotte sans qu'aucune cotisation ne soit payée ;
> 1. **urgent** : ajouter `({t.id})` dans la description du versement, sinon deux tontines du même nom partagent le même versement et la 2ᵉ n'est jamais payée ;
> 2. traiter `VersementEnAttente` comme « en cours » au lieu d'« échec », et permettre de relancer depuis « en cours » (sans danger, mon module ne repaie jamais) ;
> 3. ajouter `reprendre_versements()` au démarrage.
>
> Et dans `main.py`, la fonction `service_lightning()` du README. Tu veux que je les fasse dans ton dossier, ou tu préfères les faire toi-même ?
