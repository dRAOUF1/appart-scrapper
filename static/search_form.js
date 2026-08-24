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

    /* --- Bloc Transports (issue #28) -------------------------------------
     * Une sélection de transport = une ligne ferrée francilienne, des
     * stations épinglées éventuelles (« toute la ligne » si aucune cochée),
     * un rayon à vol d'oiseau. L'état complet vit dans UN champ caché
     * transit_payload (JSON), même discipline que location_payload (#24) :
     * le JS le réécrit intégralement à chaque changement — il n'est jamais
     * vidé au premier caractère tapé ailleurs, et les lignes déjà choisies
     * ne peuvent pas être détruites par l'édition d'une autre.
     */
    const RAYONS_TRANSIT = [
        { valeur: 500, libelle: '500 m' },
        { valeur: 1000, libelle: '1 km' },
        { valeur: 2000, libelle: '2 km' },
    ];
    let transitRowSeq = 0;

    function setupTransitBlock(block) {
        const list = block.querySelector('[data-transit-list]');
        const payloadField = block.querySelector('[data-transit-payload]');
        const errorBox = block.querySelector('[data-transit-error]');
        const addBtn = block.querySelector('[data-add-transit]');
        if (!list || !payloadField) return;

        const cacheStations = {}; // line_id -> [{id, label}]

        function montrerErreur(message) {
            if (!errorBox) return;
            errorBox.textContent = message;
            errorBox.hidden = false;
        }

        function effacerErreur() {
            if (!errorBox) return;
            errorBox.hidden = true;
        }

        function lirePayloadInitial() {
            const brut = (payloadField.value || '').trim();
            if (!brut) return [];
            try {
                const parsed = JSON.parse(brut);
                if (!Array.isArray(parsed)) throw new Error('liste attendue');
                return parsed;
            } catch (e) {
                montrerErreur(
                    'Le bloc Transports contient des données illisibles : '
                    + 're-sélectionnez vos lignes avant d\'enregistrer.'
                );
                return [];
            }
        }

        function ecrirePayload() {
            const selections = lignes()
                .map(function (ligne) { return ligne.etat; })
                .filter(function (etat) { return Boolean(etat.lineId); })
                .map(function (etat) {
                    const selection = {
                        line_id: etat.lineId,
                        stop_ids: etat.stopIds.slice(),
                        radius_m: etat.radiusM,
                    };
                    if (etat.mode) selection.mode = etat.mode;
                    return selection;
                });
            payloadField.value = selections.length ? JSON.stringify(selections) : '';
        }

        function lignes() {
            return Array.from(list.querySelectorAll('[data-transit-row]'))
                .map(function (row) { return row.__transit; })
                .filter(Boolean);
        }

        function chargerStations(lineId, callback) {
            if (cacheStations[lineId]) {
                callback(cacheStations[lineId]);
                return;
            }
            fetch('/locations/transit/stops?line=' + encodeURIComponent(lineId))
                .then(function (r) { return r.ok ? r.json() : { items: [] }; })
                .then(function (data) {
                    cacheStations[lineId] = Array.isArray(data.items) ? data.items : [];
                    callback(cacheStations[lineId]);
                })
                .catch(function () { callback([]); });
        }

        function marquerLigneInvalide(row, invalide) {
            row.classList.toggle('has-error', invalide);
            let hint = row.querySelector('.transit-row-error-message');
            if (!hint) {
                hint = document.createElement('small');
                hint.className = 'transit-row-error-message';
                hint.textContent = 'Choisissez la ligne dans les suggestions';
                row.appendChild(hint);
            }
            hint.hidden = !invalide;
        }

        function rendreStations(ligne, stations) {
            const zone = ligne.row.querySelector('[data-transit-stations]');
            if (!zone) return;
            zone.innerHTML = '';
            if (!stations.length) {
                zone.hidden = true;
                const vide = ligne.row.querySelector('[data-transit-stations-vide]');
                if (vide) vide.hidden = false;
                return;
            }
            const groupe = document.createElement('div');
            groupe.className = 'pill-toggle-group transit-stations-group';
            stations.forEach(function (station) {
                const label = document.createElement('label');
                label.className = 'pill-toggle';
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.value = station.id;
                cb.checked = ligne.etat.stopIds.indexOf(station.id) !== -1;
                cb.addEventListener('change', function () {
                    if (cb.checked) {
                        if (ligne.etat.stopIds.indexOf(station.id) === -1) {
                            ligne.etat.stopIds.push(station.id);
                        }
                    } else {
                        ligne.etat.stopIds = ligne.etat.stopIds.filter(function (id) {
                            return id !== station.id;
                        });
                    }
                    majCompteur(ligne, stations.length);
                    ecrirePayload();
                });
                const texte = document.createElement('span');
                texte.className = 'pill-toggle-label';
                texte.textContent = station.label;
                label.appendChild(cb);
                label.appendChild(texte);
                groupe.appendChild(label);
            });
            zone.appendChild(groupe);
            zone.hidden = false;
            const vide = ligne.row.querySelector('[data-transit-stations-vide]');
            if (vide) vide.hidden = true;
            majCompteur(ligne, stations.length);
        }

        function majCompteur(ligne, total) {
            const compteur = ligne.row.querySelector('[data-transit-compteur]');
            if (!compteur) return;
            compteur.textContent = ligne.etat.stopIds.length
                ? ligne.etat.stopIds.length + ' station(s) épinglée(s) sur ' + total
                : 'Toute la ligne (' + total + ' stations)';
        }

        function attacherAutocompleteLigne(ligne) {
            const input = ligne.row.querySelector('[data-transit-line-input]');
            const listeSuggestions = ligne.row.querySelector('[data-transit-line-suggestions]');
            let timer = null;
            let controller = null;

            function cacher() {
                listeSuggestions.hidden = true;
                listeSuggestions.innerHTML = '';
            }

            function choisir(suggestion) {
                input.value = suggestion.label;
                ligne.etat.lineId = suggestion.id;
                ligne.etat.lineLabel = suggestion.label;
                marquerLigneInvalide(ligne.row, false);
                cacher();
                ecrirePayload();
                chargerStations(suggestion.id, function (stations) {
                    rendreStations(ligne, stations);
                });
            }

            input.addEventListener('input', function () {
                // Retomber exactement sur le libellé choisi restaure la
                // sélection : encore LA MÊME ligne, pas une saisie neuve (#24).
                if (ligne.etat.lineLabel && input.value === ligne.etat.lineLabel) {
                    marquerLigneInvalide(ligne.row, false);
                    cacher();
                    return;
                }
                // Une vraie modification désélectionne CETTE ligne seulement :
                // les autres sélections du payload restent intactes.
                ligne.etat.lineId = '';
                ligne.etat.mode = '';
                ligne.etat.stopIds = [];
                marquerLigneInvalide(ligne.row, input.value.trim().length > 0);
                const zone = ligne.row.querySelector('[data-transit-stations]');
                if (zone) { zone.innerHTML = ''; zone.hidden = true; }

                const query = input.value.trim();
                clearTimeout(timer);
                if (query.length < 1) { cacher(); ecrirePayload(); return; }
                timer = setTimeout(function () {
                    if (controller) controller.abort();
                    controller = new AbortController();
                    fetch('/locations/transit/lines?q=' + encodeURIComponent(query),
                        { signal: controller.signal })
                        .then(function (r) { return r.ok ? r.json() : { items: [] }; })
                        .then(function (data) {
                            listeSuggestions.innerHTML = '';
                            (data.items || []).forEach(function (item) {
                                const li = document.createElement('li');
                                li.className = 'location-suggestion';
                                li.textContent = item.label;
                                li.addEventListener('mousedown', function (e) {
                                    e.preventDefault();
                                    choisir(item);
                                });
                                listeSuggestions.appendChild(li);
                            });
                            listeSuggestions.hidden = !(data.items || []).length;
                        })
                        .catch(function () { /* requête annulée ou réseau indisponible */ });
                }, 250);
            });

            input.addEventListener('blur', function () {
                setTimeout(cacher, 150);
            });
        }

        function creerLigne(initial) {
            const row = document.createElement('div');
            row.className = 'transit-row';
            row.setAttribute('data-transit-row', '');
            const uid = ++transitRowSeq;
            const etat = {
                lineId: initial.line_id || '',
                lineLabel: initial.line_label || initial.line_id || '',
                mode: initial.mode || '',
                stopIds: Array.isArray(initial.stop_ids)
                    ? initial.stop_ids.map(String).slice()
                    : [],
                radiusM: RAYONS_TRANSIT.some(function (r) {
                    return Number(initial.radius_m) === r.valeur;
                }) ? Number(initial.radius_m) : 1000,
            };

            const champLigne = document.createElement('div');
            champLigne.className = 'location-autocomplete';
            const input = document.createElement('input');
            input.type = 'text';
            input.placeholder = 'Chercher une ligne (ex. Métro 14)';
            input.setAttribute('aria-label', 'Ligne de transports');
            input.autocomplete = 'off';
            input.setAttribute('data-transit-line-input', '');
            if (etat.lineLabel) input.value = etat.lineLabel;
            const suggestions = document.createElement('ul');
            suggestions.className = 'location-suggestions';
            suggestions.setAttribute('data-transit-line-suggestions', '');
            suggestions.hidden = true;
            champLigne.appendChild(input);
            champLigne.appendChild(suggestions);

            const stationsZone = document.createElement('div');
            stationsZone.className = 'transit-stations';
            stationsZone.setAttribute('data-transit-stations', '');
            stationsZone.hidden = true;

            const videNote = document.createElement('small');
            videNote.className = 'form-help';
            videNote.setAttribute('data-transit-stations-vide', '');
            videNote.textContent = 'Choisissez d\'abord une ligne.';
            videNote.hidden = Boolean(etat.lineId);

            const compteur = document.createElement('small');
            compteur.className = 'form-help';
            compteur.setAttribute('data-transit-compteur', '');

            const rayonsGroupe = document.createElement('div');
            rayonsGroupe.className = 'pill-toggle-group';
            RAYONS_TRANSIT.forEach(function (rayon) {
                const label = document.createElement('label');
                label.className = 'pill-toggle';
                const radio = document.createElement('input');
                radio.type = 'radio';
                radio.name = 'transit_radius_' + uid;
                radio.value = String(rayon.valeur);
                radio.checked = etat.radiusM === rayon.valeur;
                radio.addEventListener('change', function () {
                    if (radio.checked) {
                        etat.radiusM = rayon.valeur;
                        ecrirePayload();
                    }
                });
                const texte = document.createElement('span');
                texte.className = 'pill-toggle-label';
                texte.textContent = rayon.libelle;
                label.appendChild(radio);
                label.appendChild(texte);
                rayonsGroupe.appendChild(label);
            });

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'location-remove-btn';
            removeBtn.setAttribute('aria-label', 'Retirer cette ligne de transports');
            removeBtn.textContent = '✕';
            removeBtn.addEventListener('click', function () {
                row.remove();
                ecrirePayload();
                effacerErreur();
            });

            row.appendChild(champLigne);
            row.appendChild(stationsZone);
            row.appendChild(videNote);
            row.appendChild(compteur);
            rayonsGroupe.style.marginTop = '0.5rem';
            row.appendChild(rayonsGroupe);
            row.appendChild(removeBtn);
            list.appendChild(row);

            const ligne = { row: row, etat: etat };
            row.__transit = ligne;
            attacherAutocompleteLigne(ligne);

            // Hydratation : une ligne déjà enregistrée recharge ses stations.
            if (etat.lineId) {
                chargerStations(etat.lineId, function (stations) {
                    rendreStations(ligne, stations);
                });
            }
            return ligne;
        }

        // Garde de soumission : un texte saisi sans suggestion choisie n'a
        // plus de ligne — bloquer avant le départ, comme les localisations.
        const form = block.closest('form');
        if (form) {
            form.addEventListener('submit', function (e) {
                const invalides = lignes().filter(function (ligne) {
                    const input = ligne.row.querySelector('[data-transit-line-input]');
                    return Boolean(input && input.value.trim()) && !ligne.etat.lineId;
                });
                invalides.forEach(function (ligne) {
                    marquerLigneInvalide(ligne.row, true);
                });
                if (invalides.length) {
                    montrerErreur(
                        'Une ou plusieurs lignes ont été modifiées sans être choisies dans les '
                        + 'suggestions : sélectionnez-les puis enregistrez à nouveau.'
                    );
                    e.preventDefault();
                    return;
                }
                effacerErreur();
            });
        }

        if (addBtn) {
            addBtn.addEventListener('click', function () {
                creerLigne({});
                effacerErreur();
            });
        }

        lirePayloadInitial().forEach(function (selection) {
            creerLigne(selection || {});
        });
        ecrirePayload();
    }

    document.querySelectorAll('[data-transit-block]').forEach(setupTransitBlock);

    /* --- Exposition pour la suite de tests (node --test) ------------------
     * Sous Node, `module` existe : on expose les internes que les scénarios
     * de tests/js pilotent (l'état frais vient du DOM factice construit par
     * chaque test, ces fonctions restent pures côté effets navigateur).
     * En navigateur, `module` n'existe pas : ce bloc est inerte et rien de
     * ce qui précède n'est changé d'un octet de comportement.
     */
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            attachAutocomplete: attachAutocomplete,
            setupLocationList: setupLocationList,
            validateBeforeSubmit: validateBeforeSubmit,
            ROW_ERROR_CLASS: ROW_ERROR_CLASS,
        };
    }
})();
