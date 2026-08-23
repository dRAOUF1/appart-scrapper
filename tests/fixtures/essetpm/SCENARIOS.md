# Esset PM — matière pour les tests (destinée à test-engineer-devops)

Captures réelles du 2026-08-23, prises avec en-têtes navigateur complets
(User-Agent Firefox, `Origin: https://www.locations.esset-pm.com`).
Source : API publique `https://bo-back.esset-pm.com/v1/public/api`
(découverte via `/conf.json` chargé au runtime par la SPA).

## Fichiers

| Fichier | Requête | Contenu |
|---|---|---|
| `offre_national_sans_filtre.json` | `POST /offre`, tous drapeaux `false`, `typeLieu/codeLieu` vides | 92 offres (parc complet) |
| `offre_departement_92.json` | idem + `typeLieu:"d", codeLieu:"92"` | 55 offres |
| `offre_ville_75005.json` | idem + `typeLieu:"v", codeLieu:"75005"` | 3 offres |
| `offre_code_inconnu.json` | idem + `codeLieu:"99999"` | `[]` — HTTP **200** (vide légitime) |
| `detail_29383-23.json` | `GET /offre/29383-23` | fiche complète enrichie |

## Faits vérifiés en live (à couvrir par les tests)

1. **`budget` est ignoré par l'API** : `budget:[100000,200000]` renvoie les
   mêmes offres que `[0,15000]` (loyers dès ~754 €). → le parser DOIT rejouer
   priceMin/priceMax localement sur `loyerCc`.
2. **Tous les drapeaux à `false` = « pas de filtre »** : renvoie tout (92 vs
   90 avec drapeaux actifs). C'est ce que le parser envoie ; tous les filtres
   (types, prix, surface, pièces, chambres, localisation) sont appliqués côté
   parser.
3. **`codeDepartement` arrive bourré d'espaces** (« 75 », « 92 ») dans les
   réponses : stripper avant comparaison.
4. **`photoCouverture` contient espaces/apostrophes** (ex. « Capture
   d'écran 2026-05-14 181739.png ») : encodage URL requis
   (`img.prod.fonciatech.net/esset` + chemin quoté).
5. Le portail ne référence que de la **location** ; types UI :
   appartement/maison/parking (+ studio/f2..f5 = pièces). Aucun terrain.
6. Fiche publique d'une offre : `https://www.locations.esset-pm.com/location/{codeAnnonce}`.

## Scénarios attendus du parser (`parsers/essetpm.py`)

- `listing_id` = `essetpm_{codeAnnonce}` (ex. `essetpm_29383-23`) ;
- `url` = fiche publique ci-dessus (HTTP 200 vérifié) ;
- périmètres : `city`→1 POST (`v`+CP), `whole_city`→1 POST **par CP** de
  `postalCodes[]`, `department`→(`d`+code), `region`→(`r`+code) — codes INSEE
  officiels acceptés tels quels ;
- filtres locaux : `matches_locations(codePostal)` (échec fermé si CP vide),
  types via premier mot de `typeBien` (Appartement/Maison/Parking), loyer,
  surface, `nbPieces` vs `rooms` (« 5 » = 5 et plus), `nbChambres` (fiche)
  vs `bedrooms` (même règle) ;
- erreurs = `ValueError` : transaction ≠ location demandée, terrain seul,
  aucune localisation, tous périmètres en échec réseau ;
- déduplication globale sur `codeAnnonce` entre périmètres qui se recouvrent ;
- détail indisponible → dégradation gracieuse (annonce conservée sans
  enrichment, warning), pas d'échec du scrape.

Vérifié manuellement le 23/08/2026 : scrape ÎdF (région) + rooms [2,3] +
surfaceMin 30 → 43 annonces toutes conformes, enrichies (description, DPE,
photos multiples, loyer HC/charges/honoraires/dépôt).
