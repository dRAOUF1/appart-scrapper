# Citya — matière pour les tests (destinée à test-engineer-devops)

Captures réelles du 2026-08-23. Source : citya.com, Symfony/Twig
server-rendered (Tailwind/Stimulus), SANS API JSON publique exploitable
(`/api/*` répond 401 « Full authentication is required » hors provenance
interne). L'autocomplete géo `/api/search_place_results` fait exception :
il suffit d'un `Referer: https://www.citya.com/...` pour qu'il réponde sans
session ni cookie (vérifié dans les deux sens le même jour).

## Fichiers

### Autocomplete (`GET /api/search_place_results?q=...`, header Referer requis)

| Fichier | Requête | Contenu |
|---|---|---|
| `autocomplete_ville_toulouse.json` | `q=toulouse` | 1 ville : id **31555** (= INSEE), slug `toulouse-31555`, codesPostaux[6] |
| `autocomplete_ville_lyon.json` | `q=lyon` | entrée principale `lyon-69000` (**id 69123** = INSEE réel, suffixe slug = CP !) + arrondissements séparés (`lyon-2e-arrondissement-69002`…) |
| `autocomplete_homonymes_saint-maur.json` | `q=saint-maur` | 5 communes homonymes → levés par `codesPostaux` |
| `autocomplete_departement_31.json` | `q=31` | la requête accepte le CODE : `{"code":"31","slug":"haute-garonne-31"}` |
| `autocomplete_departement_2A.json` | `q=2A` | Corse : code `"2A"`, slug `corse-du-sud-2a` (minuscule dans le slug) |
| `autocomplete_region_occitanie.json` | `q=Occitanie` | régions requêtées par NOM officiel : slug `occitanie-76`, suffixe = INSEE région ; l'API ne publie pas de code région (seul `libelle` + `slug`) |
| `autocomplete_region_absente_corse.json` | `q=Corse` | `regions: []` — la région Corse n'existe PAS côté autocomplete (seulement ses départements) |
| `autocomplete_region_absente_reunion.json` | `q=Réunion` | réponse vide — régions DROM non référencées |

### Pages de résultats (`GET /annonces/location/{type}/{slug}?...`)

| Fichier | Requête | Contenu |
|---|---|---|
| `search_bordeaux_appartement_prixMax900.html` | appartement, bordeaux-33063, `?prixMax=900` | 16 cartes, filtre natif appliqué (33 sans filtre) |
| `search_quiberon_parking_elargissement.html` | parking, quiberon-56170 | titre scopé « Quiberon (56) » mais cartes à Rennes/Brest/Saint-Brieuc/Lorient → **élargissement secteur** |
| `search_toulouse_maison.html` | maison, toulouse-31555 | 12 cartes, aucun compteur « résultats » sur cette page |
| `search_toulouse_cp_seul_repli_national.html` | appartement, `toulouse-31000` (**CP au lieu du slug résolu**) | 200 OK, ~5 676 annonces NATIONALES, titre sans ville → repli silencieux |
| `search_bordeaux_page99_hors_fin.html` | bordeaux-33063 `?page=99` | 200 gracieux, **0 carte** (fin de pagination) |
| `search_compose_toulouse_bordeaux_union_exacte.html` | appartement, `toulouse-31555,bordeaux-33063` | 52... 46 annonces annoncées = **somme exacte** 13+33 ; titre « Toulouse (31) ou Bordeaux (33) » |
| `search_compose_ville_departement.html` | appartement, `toulouse-31555,haute-garonne-31` | 29 = haute-garonne seul (toulouse ⊂ département, inclusion gérée) |
| `search_compose_lyon_agregat_faux.html` | appartement, `lyon-69000,toulouse-31555` | 32 au lieu de 55 : Lyon ne contribue que 19 biens **sans AUCUN itemId commun** avec sa recherche seule (42) |
| `search_compose_paris_perdu.html` | appartement, `paris-75,lyon-69000` | titre « Paris (75) ou Lyon (69) » mais 19 biens tous lyonnais : Paris contribue ZÉRO |

## Faits vérifiés en live (à couvrir par les tests)

1. **Le suffixe des slugs ville est opaque** : tantôt l'INSEE (toulouse-31555,
   nice-06088), tantôt le CP (lyon-69000, marseille-13000, paris-75000), et le
   libellé compte autant que le code.
2. **Repli national silencieux** : un slug mal formé (`foo-bar-31555`) ou un
   simple CP (`toulouse-31000`) répond 200 avec le stock FRANCE ENTIÈRE et un
   titre sans nom de ville. On ne construit donc jamais un slug localement.
3. **Composition par VIRGULES = mécanisme officiel multi-localisations** :
   le frontend du site remplit son champ `realLocalisation` avec
   `slugs.join(",")` (search_property.js). L'union est EXACTE pour villes
   normales (toulouse+bordeaux = 46 = 13+33 ; triple t,n,b = 72 = somme ;
   nice+bordeaux = 59), départements et régions, inclusions comprises
   (toulouse+haute-garonne = 29 = HG seul ; bordeaux+gironde = 105 = gironde ;
   occitanie+toulouse = région seule).
4. **⚠️ Les agrégats « toute la ville » à arrondissements cassent l'union** :
   `paris-75`, `paris-75000` et `marseille-13000` y contribuent ZÉRO annonce ;
   `lyon-69000` y contribue un ensemble DIFFÉRENT (19 vs 42 en solo, zéro
   chevauchement d'itemId). La seule PRÉSENCE de paris-75 dans la liste suffit
   à casser lyon-69000 (`ile-de-france-11,lyon-69000` = 351 = 309+42 exact,
   mais `ile-de-france-11,paris-75,lyon-69000` = 328 : lyon retombe à 19).
   Ces slugs ne doivent JAMAIS voyager dans une liste composée — requête solo
   obligatoire. Les arrondissements précis composent normalement
   (`paris-15e-arrondissement-75015` + lyon = ses 3 biens présents).
4b. **Solos redondants à économiser** : un solo dont le périmètre postal est
   couvert par une autre localisation ne ramène que des annonces déjà
   obtenues — les 13 itemIds de paris-75 sont TOUS présents dans la recherche
   ile-de-france-11 (309 biens, vérifié itemId par itemId sur 13 pages).
   `paris-75` ⊂ `ile-de-france-11` → pas de requête Paris.
5. **Un seul type par requête** : `appartement-maison` (tiret) replie sur TOUS
   les types (31 vs 13), `appartement/maison` (slash) répond 404, aucun
   paramètre GET de type n'est lu (`propertyType[]=`, `types=` ignorés).
6. **Filtres natifs courts combinables** : `prixMax` (33→4 à ≤600),
   `surfaceMin` (33→16 à ≥60), `nbrePiecesMin` (33→13 à ≥3). Les variantes
   préfixées `search_adverts_full[prixMax]=…` sont IGNORÉES. Pas de prixMin /
   surfaceMax / pièces max côté site.
7. **Élargissement sectoriel** : Quiberon parking → biens hors commune sous un
   titre pourtant scopé. `matches_locations(zip_code)` est OBLIGATOIRE en aval.
8. **Couverture ville entière** : lyon-69000 rend des biens dans tous les CP
   69001..69009 ; paris-75 couvre les 20 arrondissements (8 CP distincts
   observés sur 13 biens).
9. **Pagination** : ~24 cartes/page ; `?page=99` au-delà de la fin = 200 avec
   zéro carte (fin de boucle propre).
10. **Régions non résolubles** : Corse (94) et DROM (01/02/03/04/06) absentes
    de `regions[]` — le niveau département existe, pas le niveau région.
11. Pas d'anti-bot constaté : rafale de 12 requêtes autocomplete + dizaines de
    pages HTML au curl nu, aucun 403/429.

## Scénarios attendus du parser (`parsers/citya.py`)

- `listing_id` = `citya_{data-itemid}` (ex. `citya_GES12040005-198`) ;
  BeautifulSoup normalisant les attributs HTML en minuscules, lire
  `data-itemid`/`data-itemname` (camelCase du site) ;
- carte → Listing : prix machine depuis `data-price` (numérique, ex. "486"),
  pas du libellé affiché (« 486 € ») ; ville+CP via motif `Ville (33000)` ;
  pièces/surface depuis `data-itemname` (« Appartement 1 pièce 18m² ») ;
  photos `img[src*="/media/images/"]` absolutisées ; badge « Meublé » →
  `headline` ; `description` et DPE absents des cartes → vides ;
- résolution (services/citya_geocode.py, cache `citya_geo_ids`) :
  - `city` → priorité id==INSEE & CP∈codesPostaux, puis id==INSEE seul
    (arrondissements : « Paris 15e Arrondissement » id 75114 →
    `paris-15e-arrondissement-75015`, scope précis vérifié), puis
    libellé+CP (fusions de communes) ;
  - `whole_city` → même entrée principale (Lyon id 69123 → `lyon-69000`) ;
  - `department` → `q=<code>` direct, comparaison insensible à la casse (2A) ;
  - `region` → requête par nom officiel (geo.api.gouv.fr si absent des
    critères), slug suffixé du code attendu ; Corse/DROM → None + warning ;
- scrape : cibles de requêtes = slugs composables (city/department/region)
  joints par virgules en UNE URL, plus une URL solo par agrégat whole_city
  (Paris/Lyon/Marseille) — doublon de slug jamais interrogé deux fois ;
  boucle types × pages, dédup globale sur `listing_id` ; erreurs =
  `ValueError` (transaction achat explicite, aucune localisation, type non
  référencé seul, aucun périmètre résolu, échec HTTP) ;
- recadrage local `_passes_filters` : matches_locations d'abord, puis bornes
  prixMin/prixMax, surfaceMin/surfaceMax, et pièces EXACTES (le natif
  nbrePiecesMin ne sait que couper en dessous) — pièce illisible gardée,
  pièce absente (0) non exclue ;
- URLs (`build_search_urls`) : miroir exact du scrape — UNE URL composée par
  type pour le groupe composable + une URL solo par agrégat whole_city,
  mêmes filtres natifs, page 1 sans paramètre `page`.

Vérifié manuellement le 23/08/2026 : Toulouse+Bordeaux+Rennes composés →
site rend l'union exacte (52 cartes), parser retient les 27 biens aux CP
exactement demandés (city = code postal précis) ; Paris (whole_city) +
Toulouse (city) → 2 requêtes (paris-75 solo + groupe), 16 annonces 75xxx/
31000 uniquement ; rooms [2,3] → 3 annonces toutes 2 ou 3 pièces ; Quiberon
parking → 0 annonce (élargissement filtré) ; dédup slug dans les deux ordres.
