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
        // Le périmètre choisi (libellé + JSON), mémorisé à part du champ
        // caché : tant que le champ visible affiche exactement ce libellé,
        // le payload reste valide — même si l'utilisateur entre dans le champ
        // et tape un caractère sans choisir de suggestion (#24).
        let chosen = null;

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
            chosen = { label: suggestion.label, json: JSON.stringify(suggestion) };
            setRowError(row, false);
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
            // Retomber exactement sur le libellé choisi (effacer la lettre
            // parasite qui avait invalidé la ligne) restaure le périmètre :
            // c'est encore LA MÊME localisation, pas une saisie neuve.
            if (chosen && input.value === chosen.label) {
                if (payload) payload.value = chosen.json;
                setRowError(row, false);
                hide();
                return;
            }
            // Une vraie modification invalide le périmètre choisi : le vider
            // et le dire tout de suite (contour rouge), au lieu d'un refus
            // incompréhensible à l'enregistrement.
            chosen = null;
            if (payload) payload.value = '';
            setRowError(row, input.value.trim().length > 0);

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

    /* --- Marquage visuel d'une ligne sans périmètre -----------------------
     * Une ligne dont le texte a été modifié sans re-choisir de suggestion n'a
     * plus de payload : contour rouge + message sous la ligne, dès la frappe.
     */
    const ROW_ERROR_CLASS = 'has-error';

    function setRowError(row, hasError) {
        row.classList.toggle(ROW_ERROR_CLASS, hasError);
        let hint = row.querySelector('.location-row-error-message');
        if (!hint) {
            hint = document.createElement('small');
            hint.className = 'location-row-error-message';
            hint.textContent = 'Choisissez la localisation dans les suggestions';
            row.appendChild(hint);
        }
        hint.hidden = !hasError;
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
            clearListError(list);
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
                // Le clone hériterait de l'état d'erreur du modèle : une
                // nouvelle ligne démarre proprement.
                newRow.classList.remove(ROW_ERROR_CLASS);
                newRow.querySelectorAll('.location-row-error-message').forEach(function (el) {
                    el.remove();
                });
                list.appendChild(newRow);
                attachAutocomplete(newRow);
                refreshRemoveButtons();
            });
        }

        // Garde de soumission : bloquer un départ qui échouerait côté serveur.
        const form = list.closest('form');
        if (form) {
            form.addEventListener('submit', function (e) {
                if (!validateBeforeSubmit(list)) e.preventDefault();
            });
        }

        refreshRemoveButtons();
    }

    /* --- Garde de soumission ----------------------------------------------
     * Une ligne modifiée sans re-choisir sa suggestion n'a plus de payload :
     * le serveur refuserait la recherche. On bloque avant le départ, avec un
     * message qui dit quoi faire — plutôt qu'un toast par source (#24).
     */
    function validateBeforeSubmit(list) {
        const rows = Array.from(list.querySelectorAll('.location-row'));
        let firstInvalidInput = null;

        rows.forEach(function (row) {
            const input = row.querySelector('[data-location-input]');
            const payload = row.querySelector('[data-location-payload]');
            const hasText = Boolean(input && input.value.trim());
            const hasPayload = Boolean(payload && payload.value.trim());
            setRowError(row, hasText && !hasPayload);
            if (hasText && !hasPayload && !firstInvalidInput) firstInvalidInput = input;
        });

        if (firstInvalidInput) {
            showListError(
                list,
                'Une ou plusieurs localisations ont été modifiées sans être choisies dans les'
                + ' suggestions : sélectionnez-les dans la liste puis enregistrez à nouveau.'
            );
            firstInvalidInput.focus();
            return false;
        }

        const anyFilled = rows.some(function (row) {
            const input = row.querySelector('[data-location-input]');
            const payload = row.querySelector('[data-location-payload]');
            return Boolean((input && input.value.trim()) || (payload && payload.value.trim()));
        });
        if (!anyFilled) {
            showListError(list, 'Renseignez au moins une localisation.');
            return false;
        }

        clearListError(list);
        return true;
    }

    function showListError(list, message) {
        let box = list.parentElement.querySelector('[data-location-submit-error]');
        if (!box) {
            box = document.createElement('div');
            box.className = 'location-submit-error';
            box.setAttribute('role', 'alert');
            list.parentElement.insertBefore(box, list);
        }
        box.textContent = message;
        box.hidden = false;
    }

    function clearListError(list) {
        const box = list.parentElement.querySelector('[data-location-submit-error]');
        if (box) box.hidden = true;
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
