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

    /* --- Autocomplete de ville -------------------------------------------
     * Choisir une suggestion remplit le code postal ET le code INSEE caché.
     * C'est ce code INSEE qui permet ensuite à chaque source de retrouver son
     * propre identifiant de lieu (SeLoger en a besoin pour son placeId).
     */
    function attachAutocomplete(row) {
        const input = row.querySelector('[data-location-input]');
        const postal = row.querySelector('[data-location-postal]');
        const insee = row.querySelector('[data-location-insee]');
        const list = row.querySelector('[data-location-suggestions]');
        if (!input || !list) return;

        let timer = null;
        let controller = null;

        function hide() {
            list.hidden = true;
            list.innerHTML = '';
        }

        function pick(suggestion) {
            input.value = suggestion.city;
            if (postal) postal.value = suggestion.postalCode;
            if (insee) insee.value = suggestion.inseeCode || '';
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
                li.textContent = s.label;
                li.className = 'location-suggestion';
                li.addEventListener('mousedown', function (e) {
                    e.preventDefault();
                    pick(s);
                });
                list.appendChild(li);
            });
            list.hidden = false;
        }

        input.addEventListener('input', function () {
            // Une ville retapée à la main n'a plus de code INSEE valide : le
            // vider évite de garder celui de la ville précédemment choisie,
            // qui ferait chercher au mauvais endroit.
            if (insee) insee.value = '';

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
