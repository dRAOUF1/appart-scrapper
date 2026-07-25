/* Formulaire de recherche — comportements partagés entre la page de création
 * (searches.html) et la page d'édition (search_edit.html).
 *
 * Un seul formulaire sert toutes les sources : l'utilisateur décrit ce qu'il
 * cherche, et c'est le backend qui traduit ces critères au format de chaque
 * source. Ce script ne connaît donc le vocabulaire d'aucune source — les
 * capacités affichées viennent de ce que les parsers déclarent, exposé dans
 * window.SOURCE_CAPABILITIES par le template.
 */

(function () {
    'use strict';

    const PROPERTY_TYPE_LABELS = {
        apartment: 'Appartement',
        house: 'Maison',
        parking: 'Parking',
        land: 'Terrain',
    };

    /* --- Autocomplete de localisation ------------------------------------
     * Les suggestions couvrent quatre niveaux : région, département, ville
     * entière, code postal. Choisir une suggestion stocke le périmètre complet
     * dans un champ caché, en JSON : c'est ce périmètre que le backend
     * enregistre, et chaque source le traduit ensuite vers son propre
     * identifiant de lieu.
     */
    const KIND_HINTS = {
        region: 'Région',
        department: 'Département',
        whole_city: 'Ville entière',
        city: 'Code postal',
    };

    function attachAutocomplete(row) {
        const input = row.querySelector('[data-location-input]');
        const payload = row.querySelector('[data-location-payload]');
        const list = row.querySelector('[data-location-suggestions]');
        if (!input || !list) return;

        let timer = null;
        let controller = null;

        function hide() {
            list.hidden = true;
            list.innerHTML = '';
        }

        function pick(suggestion) {
            // Le libellé porte déjà le code postal quand il en a un
            // (« Paris (75015) ») : un champ séparé n'aurait rien à afficher
            // pour un département ou une région.
            input.value = suggestion.label;
            if (payload) payload.value = JSON.stringify(suggestion);
            hide();
        }

        function render(suggestions) {
            list.innerHTML = '';
            if (!Array.isArray(suggestions) || !suggestions.length) {
                hide();
                return;
            }
            suggestions.forEach(function (s) {
                const li = document.createElement('li');
                li.className = 'location-suggestion';

                const label = document.createElement('span');
                label.textContent = s.label;
                li.appendChild(label);

                // Le niveau est affiché explicitement : « Gironde » peut être
                // un département comme une commune, il faut pouvoir choisir.
                const hint = KIND_HINTS[s.kind];
                if (hint) {
                    const badge = document.createElement('span');
                    badge.className = 'location-suggestion-kind';
                    badge.textContent = hint;
                    li.appendChild(badge);
                }

                li.addEventListener('mousedown', function (e) {
                    e.preventDefault();
                    pick(s);
                });
                list.appendChild(li);
            });
            list.hidden = false;
        }

        input.addEventListener('input', function () {
            // Une saisie retapée à la main invalide le périmètre choisi
            // précédemment : le vider évite de conserver celui de l'ancienne
            // sélection, qui ferait chercher au mauvais endroit.
            if (payload) payload.value = '';

            const query = input.value.trim();
            clearTimeout(timer);
            if (query.length < 2) {
                hide();
                return;
            }

            timer = setTimeout(function () {
                if (controller) controller.abort();
                controller = new AbortController();
                fetch('/api/locations?q=' + encodeURIComponent(query), { signal: controller.signal })
                    .then(function (r) { return r.ok ? r.json() : []; })
                    .then(render)
                    .catch(function () { /* requête annulée ou réseau indisponible */ });
            }, 250);
        });

        input.addEventListener('blur', function () {
            // Laisse le temps au mousedown d'une suggestion de se déclencher.
            setTimeout(hide, 150);
        });
    }

    /* --- Lignes de localisation répétables ------------------------------- */
    function setupLocationList(list) {
        const addBtn = list.parentElement.querySelector('[data-add-location]');

        function refreshRemoveButtons() {
            const rows = list.querySelectorAll('.location-row');
            rows.forEach(function (row) {
                const btn = row.querySelector('.location-remove-btn');
                // Au moins une localisation est requise : pas de bouton
                // "retirer" quand il ne reste qu'une ligne.
                if (btn) btn.style.visibility = rows.length > 1 ? 'visible' : 'hidden';
            });
        }

        list.addEventListener('click', function (e) {
            if (!e.target.classList.contains('location-remove-btn')) return;
            if (list.querySelectorAll('.location-row').length <= 1) return;
            e.target.closest('.location-row').remove();
            refreshRemoveButtons();
        });

        if (addBtn) {
            addBtn.addEventListener('click', function () {
                const newRow = list.querySelector('.location-row').cloneNode(true);
                newRow.querySelectorAll('input').forEach(function (input) { input.value = ''; });
                newRow.querySelectorAll('[data-location-suggestions]').forEach(function (ul) {
                    ul.innerHTML = '';
                    ul.hidden = true;
                });
                newRow.querySelectorAll('[data-location-payload]').forEach(function (el) {
                    el.value = '';
                });
                list.appendChild(newRow);
                attachAutocomplete(newRow);
                refreshRemoveButtons();
            });
        }

        refreshRemoveButtons();
    }

    /* --- Avertissement de capacités --------------------------------------
     * Prévient tout de suite qu'un type de bien coché ne sera pas honoré par
     * une des sources sélectionnées, au lieu de le découvrir dans les logs
     * après le premier scrape.
     */
    function setupCapabilityWarning() {
        const warning = document.querySelector('[data-property-type-warning]');
        const capabilities = window.SOURCE_CAPABILITIES;
        if (!warning || !Array.isArray(capabilities)) return;

        function sync() {
            const checkedSources = Array.from(
                document.querySelectorAll('.source-checkbox:checked')
            ).map(function (cb) { return cb.value; });

            const checkedTypes = Array.from(
                document.querySelectorAll('[data-property-type]:checked')
            ).map(function (cb) { return cb.value; });

            const problems = [];
            capabilities
                .filter(function (src) { return checkedSources.indexOf(src.id) !== -1; })
                .forEach(function (src) {
                    const supported = src.supported_property_types || [];
                    const missing = checkedTypes.filter(function (t) {
                        return supported.indexOf(t) === -1;
                    });
                    if (missing.length) {
                        const labels = missing.map(function (t) {
                            return PROPERTY_TYPE_LABELS[t] || t;
                        });
                        problems.push('⚠️ ' + src.name + ' ne référence pas : ' + labels.join(', '));
                    }
                });

            warning.textContent = problems.join(' · ');
            warning.hidden = problems.length === 0;
        }

        document.querySelectorAll('.source-checkbox, [data-property-type]').forEach(function (cb) {
            cb.addEventListener('change', sync);
        });
        sync();
    }

    document.querySelectorAll('[data-location-list]').forEach(setupLocationList);
    document.querySelectorAll('[data-location-row]').forEach(attachAutocomplete);
    setupCapabilityWarning();
})();
