/**
 * Suite node --test de static/search_form.js — écart n°1 de l'audit de tests.
 *
 * Le repo n'a ni runner JS ni build-step : cette suite tourne avec le
 * node:test natif (Node >= 18, zéro dépendance npm) et pilote le module au
 * travers d'un mini-DOM factice maison (tests/js/helpers/dom_stub.mjs). Le
 * module est chargé via createRequire APRÈS l'installation du document
 * factice : l'IIFE s'exécute alors sur un corps vide (aucun attachement
 * parasite), et chaque scénario construit son propre arbre frais qu'il passe
 * aux internes exposées par l'export conditionnel (voir la fin de
 * static/search_form.js — bloc inerte en navigateur).
 *
 * Scénarios de l'audit (#24) :
 *   (a) frappe d'un caractère alors que value == libellé choisi -> payload conservé ;
 *   (b) effacement de la lettre parasite -> payload restauré (bug corrigé,
 *       test dé-skipé ; b-bis/b-ter couvrent les angles morts de la décision) ;
 *   (c) modification réelle du texte -> payload vidé + ligne has-error ;
 *   (d) soumission bloquée tant qu'une ligne a perdu son payload, message français ;
 *   (e) purge des états d'erreur au clonage d'une nouvelle ligne.
 *
 * Lancement : `node --test tests/js/` (ou `make test-js`).
 */

import assert from 'node:assert/strict';
import { beforeEach, describe, it } from 'node:test';
import { createRequire } from 'node:module';

import { createFakeDocument, FakeElement } from './helpers/dom_stub.mjs';

// Le document factice doit exister AVANT le premier chargement du module :
// l'IIFE interroge document.querySelectorAll à l'exécution du require.
globalThis.document = createFakeDocument();
globalThis.window = {};

const require = createRequire(import.meta.url);
const searchForm = require('../../static/search_form.js');

// ---------------------------------------------------------------------------
// Environnement par scénario
// ---------------------------------------------------------------------------

const PARIS = {
    kind: 'city', city: 'Paris', postalCode: '75013', inseeCode: '75113',
    label: 'Paris (75013)',
};
const PARIS_JSON = JSON.stringify(PARIS);

/** Le délai d'anti-rebond de l'autocomplete (250 ms dans le code testé),
 * plus une marge : les timers sont réels, aucune dépendance à des fake timers. */
const DEBOUNCE_MS = 280;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Réponses servies par le fetch doublé (aucun réseau, jamais). */
let suggestionsServies = [];
let requetesFetch = [];
globalThis.fetch = (url) => {
    requetesFetch.push(String(url));
    return Promise.resolve({ ok: true, json: async () => suggestionsServies });
};

beforeEach(() => {
    suggestionsServies = [];
    requetesFetch = [];
});

describe('validation des bornes min/max', () => {
    it('bloque une borne maximale inférieure puis efface le message après correction', () => {
        const freshDocument = createFakeDocument();
        const form = new FakeElement('form');
        const minimum = new FakeElement('input', { attributes: { 'data-bound-min': 'price' } });
        const maximum = new FakeElement('input', { attributes: { 'data-bound-max': 'price' } });
        minimum.value = '1200';
        maximum.value = '800';
        form.appendChild(minimum);
        form.appendChild(maximum);
        freshDocument.body.appendChild(form);

        globalThis.document = freshDocument;
        delete require.cache[require.resolve('../../static/search_form.js')];
        require('../../static/search_form.js');

        assert.match(maximum.validationMessage, /supérieure ou égale/);
        maximum.value = '1500';
        maximum.emit('input');
        assert.equal(maximum.validationMessage, '');

        globalThis.document = createFakeDocument();
    });
});

function makeLocationRow() {
    const input = new FakeElement('input', { attributes: { 'data-location-input': '' } });
    const payload = new FakeElement('input', { attributes: { 'data-location-payload': '' } });
    const suggestions = new FakeElement('ul', { attributes: { 'data-location-suggestions': '' } });
    suggestions.hidden = true;
    const removeBtn = new FakeElement('button', { classes: ['location-remove-btn'] });
    const row = new FakeElement('div', { classes: ['location-row'] });
    for (const child of [input, payload, suggestions, removeBtn]) {
        row.appendChild(child);
    }
    return { row, input, payload, suggestions };
}

/** Un formulaire complet câblé comme le template : bouton « ajouter » frère
 * de la liste, lignes dedans. Retourne aussi le handler de soumission tel
 * que setupLocationList l'a posé. */
function makeLocationForm({ rows = 1 } = {}) {
    const form = new FakeElement('form');
    const container = new FakeElement('div');
    form.appendChild(container);
    const list = new FakeElement('ul', { attributes: { 'data-location-list': '' } });
    container.appendChild(list);
    const addBtn = new FakeElement('button', { attributes: { 'data-add-location': '' } });
    container.appendChild(addBtn);

    const built = [];
    for (let i = 0; i < rows; i++) {
        built.push(makeLocationRow());
        list.appendChild(built[i].row);
    }
    searchForm.setupLocationList(list);
    for (const piece of built) {
        searchForm.attachAutocomplete(piece.row);
    }
    return { form, container, list, addBtn, rows: built };
}

/** Le chemin réel d'un choix de suggestion : frappe (>= 2 caractères pour
 * franchir l'anti-rebond), attente du timer, mousedown sur la suggestion rendue. */
async function chooseSuggestion({ input, suggestions }, suggestion) {
    input.value = suggestion.label.slice(0, 3);
    input.emit('input');
    await sleep(DEBOUNCE_MS);
    const li = suggestions.children.find((child) => child.textContent.includes(suggestion.label));
    assert.ok(li, "la suggestion doit être rendue dans la liste avant d'être choisie");
    li.emit('mousedown');
}

// ---------------------------------------------------------------------------
// (a)(b)(c) — conservation / restauration / invalidation du payload choisi
// ---------------------------------------------------------------------------

describe("autocomplete d'une ligne de localisation", () => {
    it('(a) conserve le payload quand un événement input laisse le texte égal au libellé choisi', async () => {
        suggestionsServies = [PARIS];
        const line = makeLocationRow();
        searchForm.attachAutocomplete(line.row);
        await chooseSuggestion(line, PARIS);
        assert.equal(line.payload.value, PARIS_JSON, "précondition : la suggestion est choisie");

        // Coller exactement le même libellé (ou tout événement input sans
        // changement effectif) : c'est encore LA MÊME localisation.
        const requetesAvant = requetesFetch.length;
        line.input.value = PARIS.label;
        line.input.emit('input');

        assert.equal(line.payload.value, PARIS_JSON, 'le payload reste en place');
        assert.equal(
            line.row.classList.contains(searchForm.ROW_ERROR_CLASS), false,
            'la ligne ne bascule pas en erreur',
        );
        assert.equal(requetesFetch.length, requetesAvant, 'pas de requête réseau pour un texte inchangé');
    });

    it("(b) restaure le payload quand la lettre parasite est effacée (comportement documenté #24)", async () => {
        suggestionsServies = [PARIS];
        const line = makeLocationRow();
        searchForm.attachAutocomplete(line.row);
        await chooseSuggestion(line, PARIS);

        // Frappe parasite : invalidation immédiate (pré-condition du scénario).
        line.input.value = `${PARIS.label}x`;
        line.input.emit('input');
        assert.equal(line.payload.value, '', 'la frappe parasite vide bien le payload');
        assert.equal(line.row.classList.contains(searchForm.ROW_ERROR_CLASS), true);

        // Effacement de la lettre parasite : le texte redevient EXACTEMENT le choix.
        line.input.value = PARIS.label;
        line.input.emit('input');

        assert.equal(line.payload.value, PARIS_JSON, 'le payload documenté par #24 est restauré');
        assert.equal(line.row.classList.contains(searchForm.ROW_ERROR_CLASS), false);
    });

    it("(b-bis) suit l'alternance rapide modification -> retour au libellé -> re-modification", async () => {
        suggestionsServies = [PARIS];
        const line = makeLocationRow();
        searchForm.attachAutocomplete(line.row);
        await chooseSuggestion(line, PARIS);
        assert.equal(line.payload.value, PARIS_JSON, 'précondition');

        // 1re modification : payload suspendu, ligne en erreur.
        line.input.value = 'Paris modifié';
        line.input.emit('input');
        assert.equal(line.payload.value, '');
        assert.equal(line.row.classList.contains(searchForm.ROW_ERROR_CLASS), true);

        // Retour au libellé exact : restauration + erreur levée.
        line.input.value = PARIS.label;
        line.input.emit('input');
        assert.equal(line.payload.value, PARIS_JSON, 'retour au libellé exact restaure');
        assert.equal(line.row.classList.contains(searchForm.ROW_ERROR_CLASS), false);

        // Re-modification : le périmètre est à nouveau suspendu.
        line.input.value = 'Paris (75)';
        line.input.emit('input');
        assert.equal(line.payload.value, '', 're-modification suspend à nouveau');
        assert.equal(line.row.classList.contains(searchForm.ROW_ERROR_CLASS), true);
    });

    it("(b-ter) un effacement total oublie le choix : retaper le libellé à la main ne restaure rien", async () => {
        suggestionsServies = [PARIS];
        const line = makeLocationRow();
        searchForm.attachAutocomplete(line.row);
        await chooseSuggestion(line, PARIS);
        assert.equal(line.payload.value, PARIS_JSON, 'précondition');

        // Effacement total volontaire : abandon du choix, pas d'erreur affichée.
        line.input.value = '';
        line.input.emit('input');
        assert.equal(line.payload.value, '', 'le payload suit l\'effacement');
        assert.equal(
            line.row.classList.contains(searchForm.ROW_ERROR_CLASS), false,
            'un champ vide n\'est pas marqué en erreur',
        );

        // Retaper EXACTEMENT le même libellé à la main : saisie neuve, elle doit
        // repasser par les suggestions — aucune résurrection du payload oublié.
        line.input.value = PARIS.label;
        line.input.emit('input');
        assert.equal(line.payload.value, '', 'le choix oublié n\'est pas ressuscité');
        assert.equal(line.row.classList.contains(searchForm.ROW_ERROR_CLASS), true);
    });

    it('(c) vide le payload et marque la ligne has-error à toute modification réelle', async () => {
        suggestionsServies = [PARIS];
        const line = makeLocationRow();
        searchForm.attachAutocomplete(line.row);
        await chooseSuggestion(line, PARIS);

        line.input.value = 'Nantes (44000)';
        line.input.emit('input');

        assert.equal(line.payload.value, '', 'le périmètre choisi ne survit pas à une vraie modification');
        assert.equal(line.row.classList.contains(searchForm.ROW_ERROR_CLASS), true, 'contour rouge');
        const hint = line.row.querySelector('.location-row-error-message');
        assert.ok(hint, "le message sous la ligne existe");
        assert.equal(hint.hidden, false, 'le message est visible dès la frappe');
        assert.match(hint.textContent, /Choisissez la localisation dans les suggestions/);
    });
});

/** Les alertes de soumission rendues sous la liste. NB : on cherche PAR
 * CLASSE pour rester indépendant de l'attribut ; le scénario anti-doublon
 * vérifie en plus que la boîte créée porte bien [data-location-submit-error]
 * (sans lui, showListError recréait une div à chaque échec et clearListError
 * était un no-op — écart audit n°2, corrigé). */
function findAlerts(container) {
    return container.children.filter((child) => /location-submit-error/.test(child.className));
}

// ---------------------------------------------------------------------------
// (d) — garde de soumission
// ---------------------------------------------------------------------------

describe('garde de soumission du formulaire', () => {
    it("(d) bloque la soumission d'une ligne modifiée sans suggestion, message français", async () => {
        const ctx = makeLocationForm({ rows: 1 });
        const [line] = ctx.rows;
        line.input.value = 'Paris tapé à la main'; // texte SANS payload

        const event = ctx.form.emit('submit');
        assert.equal(event.prevented, true, 'la soumission est bloquée côté client');
        const alerts = findAlerts(ctx.container);
        assert.ok(alerts.length >= 1, "une boîte d'alerte est insérée au-dessus de la liste");
        const box = alerts[0];
        assert.equal(box.hidden, false);
        assert.match(
            box.textContent,
            /modifiées sans être choisies dans les suggestions/,
            'le message dit quoi faire',
        );
        assert.equal(
            line.row.classList.contains(searchForm.ROW_ERROR_CLASS), true,
            'la ligne fautive est (re)marquée has-error au moment de la soumission',
        );
        assert.match(box.getAttribute('role'), /alert/, "rôle d'alerte posé");
    });

    it('(d-bis) bloque tant que AU MOINS une ligne reste invalide, même si les autres sont valides', async () => {
        suggestionsServies = [PARIS];
        const ctx = makeLocationForm({ rows: 2 });
        await chooseSuggestion(ctx.rows[0], PARIS); // ligne 1 valide
        ctx.rows[1].input.value = 'Lyon saisie à la main'; // ligne 2 invalide

        const event = ctx.form.emit('submit');

        assert.equal(event.prevented, true, 'une seule ligne fautive suffit à bloquer');
        const alerts = findAlerts(ctx.container);
        assert.ok(alerts.length >= 1);
        assert.equal(alerts[0].hidden, false);
    });

    it('laisse partir une soumission dont toutes les lignes portent leur payload', async () => {
        suggestionsServies = [PARIS];
        const ctx = makeLocationForm({ rows: 1 });
        await chooseSuggestion(ctx.rows[0], PARIS);

        const event = ctx.form.emit('submit');

        assert.equal(event.prevented, false, 'rien ne bloque un formulaire sain');
        for (const alert of findAlerts(ctx.container)) {
            assert.equal(alert.hidden, true, 'aucune alerte résiduelle');
        }
    });

    it("refuse à part entière un formulaire sans aucune localisation (« Renseignez au moins une »)", () => {
        const ctx = makeLocationForm({ rows: 1 });

        const event = ctx.form.emit('submit');

        assert.equal(event.prevented, true);
        const alerts = findAlerts(ctx.container);
        assert.match(alerts[0].textContent, /Renseignez au moins une localisation\./);
    });

    it("(d-ter) réutilise la même boîte d'erreur (aucun empilement) et la masque à la soumission valide", async () => {
        suggestionsServies = [PARIS];
        const ctx = makeLocationForm({ rows: 1 });
        const [line] = ctx.rows;
        line.input.value = 'Paris tapé à la main'; // texte SANS payload

        // 1er échec : la boîte est créée, identifiable par son attribut.
        ctx.form.emit('submit');
        let alerts = findAlerts(ctx.container);
        assert.equal(alerts.length, 1, "exactement une boîte après le premier échec");
        assert.notEqual(
            alerts[0].getAttribute('data-location-submit-error'), null,
            "la boîte porte l'attribut qui permet de la retrouver",
        );

        // 2e échec : aucune nouvelle boîte — l'existante est réutilisée.
        ctx.form.emit('submit');
        alerts = findAlerts(ctx.container);
        assert.equal(alerts.length, 1, "pas de doublon au second échec");
        assert.equal(alerts[0].hidden, false);

        // La ligne est réparée : la soumission part et la boîte est masquée.
        await chooseSuggestion(line, PARIS);
        const event = ctx.form.emit('submit');
        assert.equal(event.prevented, false, 'la ligne réparée laisse partir le formulaire');
        alerts = findAlerts(ctx.container);
        assert.equal(alerts.length, 1, "toujours une seule boîte, jamais recréée");
        assert.equal(
            alerts[0].hidden, true,
            "clearListError masque la boîte existante (fini le no-op)",
        );
    });
});

// ---------------------------------------------------------------------------
// (e) — clonage d'une ligne supplémentaire
// ---------------------------------------------------------------------------

describe('ajout d’une ligne de localisation', () => {
    it('(e) démarre proprement : aucun état d’erreur hérité de la ligne modèle', () => {
        const ctx = makeLocationForm({ rows: 1 });
        const [model] = ctx.rows;
        // La ligne modèle est sale : frappe parasite (payload vidé + erreur +
        // message affiché) puis suggestions rouvertes avec du contenu résiduel.
        model.input.value = 'X'; // 1 caractère : erreur posée sans déclencher de requête
        model.input.emit('input');
        assert.equal(model.row.classList.contains(searchForm.ROW_ERROR_CLASS), true, 'précondition');
        model.suggestions.hidden = false;
        model.suggestions.appendChild(new FakeElement('li'));

        ctx.addBtn.emit('click');

        assert.equal(ctx.list.children.length, 2, 'une seconde ligne existe');
        const fresh = ctx.list.children[1];
        assert.notEqual(fresh, model.row);
        assert.equal(fresh.querySelector('[data-location-input]').value, '', 'champ visible vide');
        assert.equal(fresh.querySelector('[data-location-payload]').value, '', 'payload vide');
        const freshSuggestions = fresh.querySelector('[data-location-suggestions]');
        assert.equal(freshSuggestions.hidden, true, 'suggestions cachées');
        assert.equal(freshSuggestions.children.length, 0, 'suggestions vides');
        assert.equal(
            fresh.querySelector('.location-row-error-message'), null,
            "aucun message d'erreur cloné",
        );
        assert.equal(fresh.classList.contains(searchForm.ROW_ERROR_CLASS), false);
    });
});
