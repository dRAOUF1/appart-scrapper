# Fixtures PAP — captures réelles de pap.fr

Captures du **2026-08-23**, destinées aux tests de `parsers/pap.py` et
`services/pap_geocode.py`. Octets originaux conservés (`resp.content`),
aucune retouche.

## Méthode & anti-bot

- Requêtes HTTP simples rejouées avec `curl_cffi` en
  `impersonate="chrome124"` — la même empreinte TLS que le parser (sans elle,
  Cloudflare répond un 403 « Just a moment » ; cf. docstring du module).
  Headers : `Accept: text/html, application/xhtml+xml`,
  `Accept-Language: fr-FR,fr;q=0.9`, `Referer: https://www.pap.fr/` ;
  pour `/json/ac-geo` : `X-Requested-With: XMLHttpRequest` +
  `Accept: application/json, text/javascript, */*; q=0.01`.
- ~42 requêtes GET au total, espacées de 2,5 s : **aucun blocage, aucun
  challenge** sur toute la session (200 partout).
- Le fichier `pap.html` qui traînait à la racine du repo était une page de
  challenge Cloudflare (« Just a moment... », 5,5 Ko, zéro carte) : capture
  ratée d'une session antérieure avec client non navigateur. Inutilisable
  comme fixture ; supprimé le 23/08/2026 (décision utilisateur).

## Structure DOM observée (vérifiée 2026-08-23)

Conforme à la carte décrite dans la docstring du parser :

```html
<div class="col-1-3">
  <div class="search-list-item-alt" data-piano-sp-click="{...tracking...}">
    <div class="owl-carousel" data-owl-carousel='{"items":1, "loop":true, ...}'>
      <div><img src="https://cdn.pap.fr/photos/pap/<hash>/x-p2.webp"
                alt="Rennes (35000)" style="height: 250px; ..."></div>
      <!-- plusieurs photos par annonce -->
    </div>
    <div class="item-thumb-dpe item-thumb-dpe-d"></div>   <!-- badge DPE a..f -->
    <div class="item-body">
      <a class="item-title" href="/annonces/colocation-rennes-35000-r454000447"
         name="454000447" data-piano-sp-click="{...}">
        <div class="item-price-container">
          <span class="item-price">510&nbsp;&euro;</span>
        </div>
        <span class="h1">Rennes (35000)</span>
        <ul class="item-tags">
          <li>Chambre en colocation</li><li>4&nbsp;pièces</li><li>68&nbsp;m²</li>
        </ul>
      </a>
      <p class="item-description" data-gaq="{...}">Colocation meublée ...</p>
    </div>
  </div>
  <script>_piq.push(['sendEvent', 'self_promotion.impression', {...}]);</script>
```

Points de repère pour les tests :

- **Sélecteur des cartes** : `a.item-title[href]` dont le href matche
  `/annonces/[\w-]*-r(\d+)`. Les bannières promo utilisent les MÊMES classes
  (`search-list-item-alt`, `item-title`, `h1`, parfois `item-price`) mais un
  href externe (`/pass-prioritaire`, domaine `acceslogement.fr`) sans fiche
  `-r{id}` → sortent naturellement via `_DETAIL_PATH_RE`.
- **Prix** : `span.item-price`, texte `510&nbsp;&euro;`,
  `1.527&nbsp;&euro;` (point = milliers). ⚠️ classe POSSIBLEMENT multi-valuée
  (`class="item-price txt-purple"`, ex. r464901570 dans Rennes p1) : seul un
  sélecteur par classe individuelle (BeautifulSoup `.select_one(".item-price")`)
  la trouve — un regex `class="item-price"` strict la rate.
- **Localisation** : `span.h1` du lien. Variantes réelles observées :
  - « Rennes (35000) » — CP complet dans la parenthèse finale ;
  - « Paris 15E (75015) » — idem ;
  - « Paris 15E », « Paris 19E », « Paris » — SANS CP (jusqu'à ~60 % des
    cartes d'une page Paris !) → `zip_code=""` → échec fermé `_location_ok` ;
  - « Appartement Paris 19e » — préfixe type, AUCUN tag (r33480, slug
    `location-paris-19e-appartement-logement-social`) ;
  - « Avec Grand Balcon » — ligne purement descriptive sans localisation
    (r443301635).
- **Tags** : `ul.item-tags li` — « Chambre en colocation », « 4&nbsp;pièces »,
  « 45,50&nbsp;m² » (décimale à virgule), « 105&nbsp;m² ». Absents sur certaines
  cartes.
- **Description** : `p.item-description`, APRÈS le lien (dans `.item-body`),
  déjà tronquée côté site (« ... »).
- **Photos** : URLs CDN ABSOLUES (`https://cdn.pap.fr/photos/pap/...webp`) —
  `_card_image` ne doit PAS les préfixer de BASE_URL.
- **Compteur machine** : attribut `infinite-scroll="{...}"` (encodé HTML,
  `&quot;`) contenant `"annonces_total":N,"annonces_page":M` — N = taille du
  pool (constant sur toute la série), M = cartes servies sur CETTE page
  (10-12). Plus fiable que tout grep de texte visible.
- **Titre `<title>`** : « Location appartement Rennes (35) jusqu'à 900 euros |
  Particulier à Particulier - PAP », suffixe « - page N » quand N ≥ 2.
- **Tracking** : `data-piano-sp-click` sur le conteneur et le lien ;
  `data-gaq` sur la description (« Consulter proximite Regions - 0km » chez
  Rennes) — du tracking analytics, PAS un marqueur géographique exploitable.

## Inventaire des captures

### Autocomplete `/json/ac-geo` (GET, header X-Requested-With requis)

| Fichier | URL exacte | Contenu / ce qu'il illustre |
|---|---|---|
| `ac-geo/ville-rennes.json` | `?q=Rennes` | 5 entrées. « Rennes (35) » id=43618 (parenthèse courte = département) devant « Rennes-sur-Loue (25440) », « Rennes-le-Château (11190) »... → cas `_pick_whole_city` (jamais de repli sur un homonyme). |
| `ac-geo/ville-paris-arrondissements.json` | `?q=Paris` | 20 entrées. « Paris (75) » id=439 PUIS les arrondissements SANS parenthèse (« Paris 1er » 37768, « Paris 4e » 37771...). Cas particulier 75 : aucune entrée « - 75 ». |
| `ac-geo/ville-cp-arrondissement-75015.json` | `?q=75015` | UNE seule entrée `{"id":37782,"name":"Paris 15e"}` — arrondissement sans parenthèse → `_pick_city` via retrait d'ordinal. |
| `ac-geo/departement-33.json` | `?q=33` | 20 entrées. Première = « Gironde - 33 » id=397 (`_DEPARTMENT_NAME_RE`), suivie de CP nus (« 33500 » id=2441...) qui ne doivent pas être pris pour le département. |

### Pages résultats (GET, pagination par suffixe `-N`)

| Fichier | URL exacte | Stats machine | Ce qu'il illustre |
|---|---|---|---|
| `results/locations-appartement-rennes-g43618-jusqu-a-900-euros_page01.html` | `https://www.pap.fr/annonce/locations-appartement-rennes-g43618-jusqu-a-900-euros` | annonces_total=12, annonces_page=10 | Page normale petite série : 10 cartes TOUTES avec CP (0 % sans CP), colocations (« Chambre en colocation »), prix multi-milliers absent mais classe `item-price txt-purple` présente (r464901570, 450 €), photos CDN absolues. |
| `results/...rennes-g43618-jusqu-a-900-euros_page02.html` | même URL + `-2` | annonces_total=12, annonces_page=0 | **Fin de série** : ZÉRO fiche `-r{id}`. Uniquement 2 bannières promo déguisées en cartes (`search-list-item-alt` + `a.item-title` href externe : `/pass-prioritaire`, `acceslogement.fr`). Test idéal du filtrage `_DETAIL_PATH_RE` + condition d'arrêt `nouvelles == 0`. |
| `results/...rennes-g43618-jusqu-a-900-euros_page03.html` | même URL + `-3` | annonces_total=12, annonces_page=10 | **Recyclage précoce** : contenu identique à la p1 (mêmes 10 uids, même taille 103574 o) — le site recycle dès qu'on dépasse la profondeur réelle de la série. Mini-modèle du plafond 25 pages. |
| `results/locations-appartement-paris-g439-entre-1000-et-2000-euros_page01.html` | `https://www.pap.fr/annonce/locations-appartement-paris-g439-entre-1000-et-2000-euros` | total=177, page=10 | Série FILTRÉE (segment `-entre-{a}-et-{b}-euros`). 14 fiches dont 2 doublons intra-page (r401201732 ×2, r464901267 ×2) ; 9/14 cartes SANS CP (« Paris 15E » nu) ; titres atypiques : « Appartement Paris 19e » (r33480), prix 1.527 € etc. |
| `results/...paris-g439-entre-1000-et-2000-euros_page02.html` | même URL + `-2` | total=177, page=9 | **Prix HORS bornes du filtre URL** : 890 €, 900 € (< min) et 2.050 € (> max) servis quand même → justification vivante de `_passes_filters` (rejeu des critères). Aussi « Avec Grand Balcon » (r443301635, h1 sans localisation). |
| `results/...paris-g439-entre-1000-et-2000-euros_page03.html` | même URL + `-3` | total=177, page=11 | Surface décimale « 45,50 m² » (r464501636) ; colocation « 7 pièces, 95 m² » (r463400612) et « 155 m² » (r461702747) ; CP variés 75006→75019 (pool hors-périmètre si le test prend 75015 comme périmètre). |
| `results/locations-appartement-paris-g439_page01.html` | `https://www.pap.fr/annonce/locations-appartement-paris-g439` | total=337, page=10 | Série profonde SANS filtre, p1 de référence. 14 fiches, 13 uids uniques, 4/14 sans CP. |
| `results/locations-appartement-paris-g439_page02.html` | même URL + `-2` | total=337, page=9 | Pages profondes normales (toutes nouvelles). |
| `results/locations-appartement-paris-g439_page03.html` | même URL + `-3` | total=337, page=10 | idem. |
| `results/locations-appartement-paris-g439_page24.html` | même URL + `-24` | total=337, page=12 | Page profonde AVANT le plafond : encore 12 nouveaux hors proches (le pool alimente jusqu'au bout). 8/15 sans CP. |
| `results/locations-appartement-paris-g439_page25.html` | même URL + `-25` | total=337, page=12 | Dernière page avant le plafond : 13 nouveaux. |
| `results/locations-appartement-paris-g439_page26_recyclee.html` | même URL + `-26` | total=337, page=10 | **PLAFOND DE PROFONDEUR SERVEUR CONFIRMÉ EN DIRECT** : 14 cartes TOUTES déjà vues dans les pages 1-25 (11/14 figurent déjà sur la p1 seule), 0 nouvelle annonce → c'est ici que `_collect_pages` doit s'arrêter et émettre `_DEPTH_CAP_SUSPECT` (pages_lues=26 ≥ 20). |

Faits transverses relevés pendant la pagination profonde (pages 1→26, séries
non toutes sauvegardées) :

- Chevauchement inter-pages réel : 2-6 uids déjà vus par page dès la p4-p6
  (le site re-sert des cartes) → la dédup `vues_ici` est nécessaire même hors
  recyclage.
- `annonces_total` constant par série (177 / 337 / 12), `annonces_page`
  variable (9-12) et toujours inférieur aux fiches détectées (doublons +
  élargissements).

## Limites honnêtes de ces captures

1. **Marqueur « à X km » JAMAIS OBSERVÉ** : zéro occurrence sur TOUTES les
   pages capturées, y compris sur un sondage d'arrondissement
   (`locations-appartement-paris-g37782`, p1-p2 consultées puis non
   conservées — faits notés ci-dessous). Le commentaire du parser (« d'abord
   marquées "à Xkm", puis non marquées ») date du 22/08/2026 ; soit il ne se
   déclenche que sur d'autres périmètres/densités, soit l'affichage a bougé.
   → Tester `_PROXIMITY_RE` avec une carte RECONSTRUITE (ligne h1 type
   « Vanves (92170) à 2km de Paris 15e »). À re-capter plus tard si besoin.
2. **Aucun prix décimal** (« 820,30 € ») dans ces captures → reconstruire
   aussi pour `_parse_price`.
3. Série arrondissement sondée mais non conservée : `GET
   https://www.pap.fr/annonce/locations-appartement-paris-g37782` → 200 final
   après réécriture automatique du slug vers
   `/annonce/locations-appartement-paris-15e-g37782` (le slug de lieu est
   bien ignoré puis renormalisé par le site, comme documenté) ;
   annonces_total=1404 ; doublons inter-pages dès la p2 (r464302467,
   r438000525 revus) ; une carte « Paris » nue (r405502206, hors
   arrondissement probable, sans CP ni distance).
4. La recherche principale demandée (Rennes ≤900 €) ne pagine pas
   naturellement (12 annonces) : les pages 1-2 exigées par la mission sont
   fournies par les séries Paris (177 et 337 annonces).

## Scénarios de test recommandés (pour test-engineer-devops)

1. `_parse_cards` sur `paris-g439-entre-1000-et-2000-euros_page01.html` :
   14 fiches attendues dont uid r401201732 présent 2× ; vérifier price_value
   (1.527 € → 1527.0), city/zip_code ('' pour « Paris 15E » nu), surface,
   property_type (« Appartement » depuis le slug, y compris pour r33480),
   image_url CDN absolue NON préfixée.
2. Filtrage pubs : `_parse_cards(rennes_..._page02.html)` → liste VIDE malgré
   2 blocs `search-list-item-alt`.
3. Arrêt fin de série : rennes p1 puis p3 (recycle p1) → `nouvelles == 0` à
   la 2e page, arrêt SANS warning (pages_lues=3 < 20).
4. Plafond de profondeur : g439 p1 puis p26_recyclee → arrêt + WARNING
   `_DEPTH_CAP_SUSPECT` (pages_lues ≥ 20), résultat possiblement partiel.
5. Échec fermé CP vide : sur A p1, ~9 cartes rejetées par `_location_ok`
   (zip_code=''), jamais comptées comme « nouvelles ».
6. Rejeu des critères : A p2 contient 890 €, 900 €, 2.050 € →
   `_passes_filters(priceMin=1000, priceMax=2000)` doit les rejeter tous.
7. `_parse_price` : « 510 € » → 510.0 ; « 1.527 € » → 1527.0 ; prix lu dans
   `class="item-price txt-purple"` (r464901570, Rennes p1) ; cas décimal à
   reconstruire.
8. Résolution géo (fixtures JSON) : `_pick_whole_city(q=Rennes)` → 43618
   (pas 16451) ; `_pick_department(q=33)` → 397 ; `_pick_city(q=75015,
   'Paris')` → 37782 (retrait ordinal) ; `_pick_whole_city(q=Paris)` → 439
   (parenthèse ≤ 3 caractères).
9. Doublon intra-page : uid répété compté UNE fois (`vues_ici`), la 2e
   occurrence sautée sans créer de Listing.
