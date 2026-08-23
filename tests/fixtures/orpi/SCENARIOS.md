# Fixtures Orpi — captures réelles du 2026-08-23

Captures brutes (octets originaux, aucune retouche) de l'API publique d'orpi.com,
rejouant les requêtes EXACTES du code de production (`parsers/orpi.py`,
`services/orpi_geocode.py`) : mêmes en-têtes (`User-Agent` Chrome/122 desktop,
`Accept: application/json`), même encodage des paramètres tableau que `requests`,
sans cookie ni session, délais de 2,5 s entre requêtes (8 requêtes au total).

Chaque fichier est du JSON valide (vérifié après écriture).

---

## 1. `search_union_2_villes_location.json` — recherche UNION multi-villes (fixture principale)

- **Requête** : `GET https://www.orpi.com/recherche/ajax?transactions%5B%5D=rent&realEstateTypes%5B%5D=appartement&locations%5B%5D%5Bvalue%5D=rosny-sous-bois&locations%5B%5D%5Bvalue%5D=montreuil`
- **Ordre des paramètres** (miroir exact de `_search_params()`) : `transactions[]` d'abord,
  puis `realEstateTypes[]`, puis un `locations[][value]` par slug.
- **Ce que ça illustre** : l'union multi-slugs en UNE requête — la réponse contient
  5 items de Rosny-sous-Bois ET 2 items de Montreuil (7 au total, `count: 7`).
  Marché locatif mince ce jour-là : c'est le vrai résultat du site, pas une anomalie
  (l'union est prouvée par la présence des deux villes dans une seule réponse).

### Structure vérifiée en live

- Clés top-level : `items`, `count`, `totalCount`, `polygons`, `similars`, `agency`.
  - `count` : nombre d'annonces du périmètre (= `len(items)` ici, jamais > 500 observé).
  - `totalCount` : **toujours 0, même quand `count: 7`** — champ apparemment mort du
    site, ne pas s'en servir.
  - `similars` : liste d'annonces HORS périmètre suggérées par le site (jusqu'à 30
    dans la fixture « vide ») — même forme que les items mais tronquée (~20 clés).
    Le parser doit les IGNORER ; un test qui nourrirait tout le payload à
    `_dict_to_listing` les traiterait à tort.
  - `agency` : `null` au niveau racine.
  - `polygons` : géométries de contour, ignorées par le parser.
- Items : objets très riches (**143 clés vues en union sur les 18 items capturés**).
- **`zipCode` : la clé EXISTE sur chaque item mais vaut `null` partout**
  (18/18 items + tous les similars). La docstring du parser (« l'API ne renvoie PAS
  de champ CP ») est donc fonctionnellement exacte — le champ existe mais n'est
  jamais renseigné : l'extraction du CP depuis le slug (`_zip_from_slug`) reste
  indispensable. Vérifié sur chaque item : le CP extrait du slug (93110 / 93100)
  correspond bien à la ville de l'item.
- Types des champs lus : `price` entier, `surface` flottant (`60.0`), `nbRooms`
  entier, `onMarketSince` ISO 8601 TZ (`"2026-08-06T00:00:00+02:00"`),
  `estatePhotos` liste de dicts portant `fullUrl` ET `url`,
  `city`/`district`/`agency` dicts imbriqués avec `name`.

### Cas nominal (point 5 de la mission)

**TOUS les items des deux recherches capturées sont nominaux** : les 16 champs lus
par `_dict_to_listing` (`reference`, `id`, `slug`, `city.name`, `district.name`,
`locationDescription`, `price`, `surface`, `nbRooms`, `type`, `transaction`,
`estatePhotos[].fullUrl|url`, `agency.name`, `isExclusive`, `longAd`,
`onMarketSince`) sont présents et non vides sur 18/18 items.

Référence pour un cas nominal déterministe : **index 0 de ce fichier** —

- `reference`: `73544025-bb3a-40d0-aa33-93d44e5bbc34` → `listing_id = orpi_73544025-bb3a-40d0-aa33-93d44e5bbc34`
- `slug`: `appartement-t3-rosny-sous-bois-93110-73544025-bb3a-40d0-aa33-93d44e5bbc34`
  → URL attendue `https://www.orpi.com/annonce-location-appartement-t3-rosny-sous-bois-93110-73544025-bb3a-40d0-aa33-93d44e5bbc34/`
  → CP extrait du slug : `93110`
- `transaction: rent`, `type: appartement`, `nbRooms: 3`, `surface: 60.0`, `price: 1190`
- `district.name`: `Rosny Sud - Gare RER A`, `locationDescription`: `Rosny-sous-Bois`
- `agency.name`: `Agence De La Mairie`, `isExclusive: true`, 6 photos avec `fullUrl`

---

## 2. `search_sans_resultat.json` — recherche volontairement vide

- **Requête** : `GET https://www.orpi.com/recherche/ajax?transactions%5B%5D=rent&realEstateTypes%5B%5D=appartement&locations%5B%5D%5Bvalue%5D=rosny-sous-bois&minPrice=99999999`
- **Ce que ça illustre** : filtre prix natif (`minPrice`) rendant zéro résultat.

### Comportement SURPRENANT vérifié

- Une recherche vide renvoie **`items: []` + `count: 0` — la clé `items` est
  TOUJOURS présente**. Le chemin `ValueError("JSON sans items")` de `scrape()`
  ne peut donc PAS être déclenché par une vraie requête vide : il n'arrive que si
  le site répond autre chose (page d'erreur HTML, JSON inattendu). **Pour tester ce
  chemin, le mock doit fabriquer un payload sans `items` (ou non-dict) — cas
  synthétique, pas capturable en live.**
- Même à zéro résultat, la réponse pèse **480 Ko** (30 `similars` nationaux +
  `polygons`). Ne pas s'étonner de la taille de la fixture.
- Conséquence attendue côté parser : `scrape()` ne lève PAS ici, il retourne une
  liste vide (échec ≠ résultat vide sont bien distingués par le pipeline).

---

## 3. `search_achat_pieces_filtrees.json` — transaction ACHAT + filtre pièces

- **Requête** : `GET https://www.orpi.com/recherche/ajax?transactions%5B%5D=buy&realEstateTypes%5B%5D=appartement&locations%5B%5D%5Bvalue%5D=rosny-sous-bois&numbersOfRooms%5B%5D=2&numbersOfRooms%5B%5D=3`
- **Ce que ça illustre** :
  - `transactions[]=buy` → les items portent `transaction: "buy"` : c'est ce champ
    qui fait basculer `_detail_url` vers le préfixe `/annonce-vente-{slug}/`
    (contre `/annonce-location-{slug}/` pour `rent`).
  - `numbersOfRooms[]` répété = égalité multiple : les 11 items renvoyés ont bien
    `nbRooms` ∈ {2, 3} (vérifié sur les 11 items) — confirme le comportement natif
    documenté du site.
  - Seconde ville possible dans un même fichier pour varier les jeux de test.

---

## 4. Autocomplete géo — `GET https://www.orpi.com/recherche/autocompletion/<texte ou code>`

Le terme est un **segment de CHEMIN** (pas `?q=`). La réponse est un **dict de
groupes par type de périmètre**, dont seuls les groupes pertinents sont présents
(schéma variable selon la requête — `_group()` gère les absents).

### 4a. `autocomplete_ville_93110.json` — ville par code postal

- **Requête** : `GET https://www.orpi.com/recherche/autocompletion/93110`
- Groupes présents : `zipcode` (1), `city` (1), `district` (5).
- Ville : `{"name": "Rosny-sous-Bois (93110)", "slug": "rosny-sous-bois",
  "value": "rosny-sous-bois", "area": "city", "zipcode": ["93110"], ...}` —
  le groupe `city` porte la liste des CP (`zipcode`), base du choix de
  `_pick_city` (nom + CP).
- Piège visible : l'entrée du groupe `zipcode` vaut
  `{"value": "cp-93110", "label": "93110"}` — sa `value` est PRÉFIXÉE `cp-`, ce
  n'est pas le CP nu. C'est le groupe `city` qu'il faut lire.
- Les districts (`area: "district"`, slugs composés type
  `rosny-sous-bois-centre-ville`) partagent la réponse : ne pas les prendre pour
  des villes.

### 4b. `autocomplete_departement_33.json` — département par code

- **Requête** : `GET https://www.orpi.com/recherche/autocompletion/33`
- Groupes : `district` (1), `department` (1).
- Piège documenté rendu tangible : le groupe `district` contient
  « 33 Hectares » (`value: "neuilly-sur-marne-33-hectares"`, `area: "district"`)
  alors que la requête était le CODE 33 — sans le filtre `area == "department"`
  de `_pick_department`, on résoudrait Neuilly-sur-Marne. L'entrée correcte :
  `{"name": "Gironde", "value": "gironde", "area": "department"}`.

### 4c. `autocomplete_region_par_nom.json` — région par NOM (slug natif)

- **Requête** : `GET https://www.orpi.com/recherche/autocompletion/ile-de-france`
  (nom slugifié — aucune requête par code région possible : « 11 » renvoie l'Aude)
- Groupes : `region` (3), `city` (11), `district` (32), `province` (4).
- Piège majeur : la correspondance floue du site renvoie **trois régions** —
  `Ile-de-France` (exacte), `Hauts-de-France`, `France d'outre-mer` — plus des
  dizaines de villes/districts contenant « france ». `_pick_region` doit ne
  retenir QUE la correspondance exacte du nom normalisé, sans repli flou.
- Pas de clé `department` du tout dans cette réponse.

### 4d. `autocomplete_departement_2A.json` — département corse par CODE (réponse dégénérée)

- **Requête** : `GET https://www.orpi.com/recherche/autocompletion/2A`
- **Réponse : `[]` — un TABLEAU JSON vide de 2 octets, PAS un objet.**
  C'est le seul contenu non-dict de ces captures (JSON valide quand même).
  `_query_autocomplete` le réduit en `{}` via son check `isinstance(data, dict)`
  → résolution `None`. Fixture idéale du chemin « réponse inattendue traitée
  comme vide, sans lever ».

### 4e. `autocomplete_departement_corse_du_sud_par_nom.json` — département par NOM (repli Corse)

- **Requête** : `GET https://www.orpi.com/recherche/autocompletion/corse-du-sud`
- Groupes : `department` (2), `region` (1), `district` (42), `province` (2), `city` (3).
- Illustre le repli par nom des départements corses (`_CORSE_DEPARTMENT_NAMES`)
  ET la résolution d'une région SANS identifiant natif élargie à ses départements
  (chemin « région par sélection départements » du parser : chaque département est
  requêté individuellement puis tous partent dans UNE requête `locations[][value]`).
- Piège : le groupe `department` contient **à la fois** `Corse-du-Sud` et
  `Haute-Corse` (flou du site) + la région `corse` — le choix doit être fait par
  correspondance exacte du nom, pas « première entrée ».
- Format de `parents` vérifié : `[["Corse", "corse", "region", ""]]`
  (libellé, slug, type, champ vide).

---

## Faits NON observables en live (cas synthétiques pour les tests)

1. **Aucun item `sold: true` ni `enabled: false`** dans les 3 recherches capturées
   (18 items + 66 similars, tous `sold: false, enabled: true`). Ces chemins
   d'écartement du parser doivent être couverts en injectant des items synthétiques
   dans le tableau `items` d'une fixture.
2. **Troncature `count > len(items)`** (warning du cap 500) : toutes les recherches
   capturées ont `count == len(items)` ; une fixture de troncature réelle serait un
   blob de ~15 Mo (les items font ~29 Ko pièce). Recommandation : modifier
   synthétiquement `count` dans une copie de fixture plutôt que capturer un
   périmètre national.
3. **Payload sans clé `items`** : impossible à obtenir du site vivant (cf. §2) —
   mock synthétique requis pour le chemin `ValueError`.

## Écarts parser vs live détectés (aucun bug bloquant)

- `zipCode` : la CLÉ existe sur les items (contrairement à l'intitulé exact de la
  docstring) mais vaut toujours `null` → le comportement du parser (extraction
  slug) reste correct. Nuance de documentation seulement.
- `totalCount` toujours 0 : sans effet, le parser lit `count`.
- Tout le reste (union, égalité multiple `numbersOfRooms[]`, groupes autocomplete,
  pièges « 33 Hectares » / régions floues / Corse 2A, formats d'URL de détail
  `annonce-location-`/`annonce-vente-`) est CONFIRMÉ conforme aux hypothèses du
  parser datées du 22/08/2026.

## Récapitulatif

| Fichier | Taille | Requête | Point clé |
|---|---|---|---|
| `autocomplete_ville_93110.json` | 2,4 Ko | `/recherche/autocompletion/93110` | ville par CP, valeur `cp-93110` du groupe zipcode |
| `autocomplete_departement_33.json` | 0,6 Ko | `/recherche/autocompletion/33` | filtre `area == "department"` (piège « 33 Hectares ») |
| `autocomplete_region_par_nom.json` | 18,2 Ko | `/recherche/autocompletion/ile-de-france` | 3 régions floues, correspondance exacte exigée |
| `autocomplete_departement_2A.json` | 2 o | `/recherche/autocompletion/2A` | tableau `[]` (non-dict) → échec traité comme vide |
| `autocomplete_departement_corse_du_sud_par_nom.json` | 16,3 Ko | `/recherche/autocompletion/corse-du-sud` | repli par nom, 2 départements floués |
| `search_union_2_villes_location.json` | 198 Ko | `/recherche/ajax` rent ×2 slugs | union multi-villes, cas nominal index 0 |
| `search_sans_resultat.json` | 469 Ko | `/recherche/ajax` minPrice 99999999 | `items: []` présent, jamais absent |
| `search_achat_pieces_filtrees.json` | 683 Ko | `/recherche/ajax` buy + rooms 2,3 | préfixe `annonce-vente-`, égalité multiple |
