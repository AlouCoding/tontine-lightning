# Lightning – Alassane Ndour

Ce dossier contient le portefeuille et le nœud Lightning, le réseau de test, la réception des paiements, les versements et la sécurité des fonds.

## À faire

- [ ] Installer LNbits et choisir le réseau de la démo
- [ ] Réussir un premier vrai paiement (c'est la priorité : tout le projet en dépend)
- [ ] Écrire la classe qui relie LNbits au moteur
- [ ] Prévenir le moteur à chaque paiement reçu

## Ce que le moteur attend

Le moteur (dans `backend/`) passe par l'interface `Lightning` de `backend/app/lightning.py`. Ta classe doit fournir ces deux méthodes :

| Méthode | Ce qu'elle doit faire | Ce qu'elle renvoie |
|---|---|---|
| `creer_facture(montant_sats, description)` | Créer une facture | Un objet `Facture` avec `payment_request` (BOLT11) et `payment_hash` |
| `payer(adresse_lightning, montant_sats, description)` | Verser la cagnotte au bénéficiaire | La preimage en hexadécimal, ou une exception si le paiement échoue |

Pour l'utiliser, remplace `LightningFactice()` par ta classe dans `backend/app/main.py`.

## Prévenir le moteur d'un paiement

Quand un paiement arrive, appelle l'API :

```
POST /paiements/confirmation
{ "payment_hash": "...", "preimage": "..." }
```

Le moteur vérifie que l'empreinte SHA-256 de la preimage correspond au `payment_hash`. Si la même notification arrive deux fois, le paiement n'est compté qu'une seule fois.

## À vérifier avec Fatou

- Est-ce que LNbits donne bien la preimage quand il signale un paiement reçu ?
- Combien de temps une facture reste-t-elle valable ? Le moteur ne sait pas encore remplacer une facture expirée.
- Que faire si `payer` échoue après un délai dépassé ? L'argent est peut-être parti, et relancer le versement risquerait de payer deux fois.
