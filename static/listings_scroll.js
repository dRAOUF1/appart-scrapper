/*
 * Scroll infini de la page des annonces (#14).
 *
 * Remplace la pagination « ← Précédent / Suivant → » : quand l'utilisateur
 * approche du bas de la grille, la sentinelle croise le viewport et la
 * tranche suivante (/listings/<id>/page) est chargée en fetch puis insérée
 * dans la grille — sans rechargement de page.
 *
 * Contrat avec le serveur :
 * - chaque tranche est un fragment `.listings-slice` portant `data-page`,
 *   `data-total-pages` et `data-end-of-list="true"` en fin de liste ;
 * - les filtres et le tri sont repris tels quels depuis la querystring
 *   courante (seul `page` est réécrit) — le fragment montre alors exactement
 *   ce que la vue complète aurait montré ;
 * - une réponse sans tranche (redirection vers /login, HTML inattendu) ou
 *   une erreur réseau laisse le verrou relâché : le prochain croisement de
 *   la sentinelle retente. En fin de liste l'observateur est débranché :
 *   plus AUCUNE requête n'est émise.
 *
 * Vanilla JS volontaire : aucune dépendance réseau supplémentaire.
 */
(function () {
  'use strict';

  var ROOT_MARGIN = '600px 0px'; // on anticipe l'approche du bas de liste

  var grid = document.getElementById('listing-grid');
  var sentinel = document.getElementById('listings-sentinel');
  var statusBox = document.getElementById('listings-scroll-status');
  if (!grid || !sentinel || !statusBox || !statusBox.dataset.searchId) return;

  var searchId = statusBox.dataset.searchId;
  var spinner = document.getElementById('listings-spinner');
  var endMessage = document.getElementById('listings-end');
  var errorMessage = document.getElementById('listings-error');

  // État initial lu depuis la première tranche, rendue côté serveur.
  var initialSlice = grid.querySelector('.listings-slice');
  if (!initialSlice) return;

  var nextPage = parseInt(initialSlice.dataset.page, 10) + 1;
  var hasMore = initialSlice.dataset.endOfList !== 'true';
  var loading = false;
  var observer = null;

  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }

  /* Les filtres/tri courants sont conservés ; seul `page` change. */
  function fetchNextSlice() {
    var params = new URLSearchParams(window.location.search);
    params.set('page', String(nextPage));
    return fetch('/listings/' + searchId + '/page?' + params.toString(), {
      headers: { 'Accept': 'text/html' },
    });
  }

  function markEnd() {
    hasMore = false;
    if (observer) observer.disconnect(); // plus aucune requête n'est émise
    show(endMessage);
  }

  function reflectPageInUrl(page) {
    var params = new URLSearchParams(window.location.search);
    params.set('page', String(page));
    var qs = params.toString();
    history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
  }

  function loadNextPage() {
    if (loading || !hasMore) return;
    loading = true; // verrou anti-double-fetch
    hide(errorMessage);
    show(spinner);

    var requestedPage = nextPage;
    fetchNextSlice()
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.text();
      })
      .then(function (text) {
        // Parsing hors du document : le fragment n'est inséré que s'il est
        // bien une tranche (une redirection vers /login renverrait du HTML
        // sans `.listings-slice`).
        var tpl = document.createElement('template');
        tpl.innerHTML = text;
        var slice = tpl.content.querySelector('.listings-slice');
        if (!slice) throw new Error('fragment inattendu');

        grid.appendChild(tpl.content);
        hide(spinner);

        if (slice.dataset.endOfList === 'true') {
          markEnd();
        } else {
          nextPage = parseInt(slice.dataset.page, 10) + 1;
        }
        reflectPageInUrl(requestedPage);

        // Contenu encore trop court pour repousser la sentinelle hors du
        // viewport : on enchaîne (le verrou, relâché ci-dessous, resérialise).
        loading = false;
        checkSentinel();
      })
      .catch(function () {
        // Réseau ou réponse inattendue : message discret, pas d'état « fin »,
        // une nouvelle traversée de la sentinelle relancera la tentative.
        hide(spinner);
        show(errorMessage);
        loading = false;
      });
  }

  function checkSentinel() {
    if (!hasMore || loading || !observer) return;
    var rect = sentinel.getBoundingClientRect();
    if (rect.top < window.innerHeight + parseInt(ROOT_MARGIN, 10)) {
      loadNextPage();
    }
  }

  if (!hasMore) {
    // Tout est déjà affiché (liste courte) : état « fin », aucun fetch.
    markEnd();
    return;
  }

  observer = new IntersectionObserver(function (entries) {
    if (entries.some(function (entry) { return entry.isIntersecting; })) {
      loadNextPage();
    }
  }, { rootMargin: ROOT_MARGIN });

  observer.observe(sentinel);
})();
