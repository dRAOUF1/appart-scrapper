/**
 * Mini-DOM factice pour la suite node:test de static/search_form.js.
 *
 * Zéro dépendance npm (le repo refuse le build-step) : on n'implémente que
 * la surface réellement touchée par le code testé — éléments avec attributs,
 * classes, enfants, listeners, valeurs de formulaire, plus un moteur de
 * sélecteurs limité aux formes utilisées (`tag`, `.classe`, `[attr]`,
 * `tag.classe`, `tag[attr]`). Tout le reste lève ou renvoie vide, à dessein :
 * si un futur changement du JS sort de cette surface, le test doit échouer
 * bruyamment plutôt que valider un fantôme.
 *
 * Sémantique respectée là où elle compte pour les scénarios :
 * - `cloneNode(true)` copie classes, attributs, valeur, visibilité ET enfants,
 *   mais PAS les listeners (comme le vrai DOM) — c'est ce qui rend le test du
 *   nettoyage au clonage significatif : sans purge explicite, le clone reste sale ;
 * - `innerHTML = ''` vide les enfants (seule écriture faite par le code) ;
 * - `classList.toggle(cls, force)` suit la spec (force=true ajoute, false retire).
 */

/** Analyse un sélecteur simple en prédicat. */
function compileSelector(selector) {
    const attrWithValue = selector.match(/^\[([^=\]]+)=["']([^"']*)["']\]$/);
    if (attrWithValue) {
        const [, attr, value] = attrWithValue;
        return (node) => node.getAttribute(attr) === value;
    }
    const tagWithClass = selector.match(/^([a-zA-Z][a-zA-Z0-9]*)\.([\w-]+)$/);
    if (tagWithClass) {
        const [, tag, cls] = tagWithClass;
        return (node) => node.tagName.toLowerCase() === tag && node.classList.contains(cls);
    }
    const tagWithAttr = selector.match(/^([a-zA-Z][a-zA-Z0-9]*)\[([^\]]+)\]$/);
    if (tagWithAttr) {
        const [, tag, attr] = tagWithAttr;
        return (node) => (
            node.tagName.toLowerCase() === tag && Object.prototype.hasOwnProperty.call(node.attributes, attr)
        );
    }
    if (/^[a-zA-Z][a-zA-Z0-9]*$/.test(selector)) {
        return (node) => node.tagName.toLowerCase() === selector;
    }
    if (/^\.[\w-]+$/.test(selector)) {
        const cls = selector.slice(1);
        return (node) => node.classList.contains(cls);
    }
    if (/^\[[^\]]+\]$/.test(selector)) {
        const attr = selector.slice(1, -1);
        return (node) => Object.prototype.hasOwnProperty.call(node.attributes, attr);
    }
    throw new Error(`Sélecteur non supporté par le mini-DOM : « ${selector} »`);
}

export class FakeElement {
    constructor(tagName, { attributes = {}, classes = [], parent = null } = {}) {
        this.tagName = String(tagName).toUpperCase();
        this.attributes = { ...attributes };
        this.classes = new Set(classes);
        this.parent = parent;
        this.children = [];
        this.listeners = {};
        // Champs de formulaire / état visible, comme sur un vrai élément.
        this.value = '';
        this.hidden = false;
        this.style = {};
        this._textContent = '';
        this.validationMessage = '';
        this.setCustomValidity = (message) => {
            this.validationMessage = String(message);
        };
        // Le focus réel n'a pas d'effet observable ici : no-op suffisant.
        this.focus = () => {};
    }

    /** Comme le vrai DOM : le texte propre + celui de toute la descendance. */
    get textContent() {
        return this._textContent + this.children.map((child) => child.textContent).join('');
    }

    set textContent(value) {
        this._textContent = String(value);
    }

    get className() {
        return [...this.classes].join(' ');
    }

    set className(value) {
        this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
    }

    /** API DOM réelle, déléguée au Set interne. */
    get classList() {
        const classes = this.classes;
        return {
            contains: (cls) => classes.has(cls),
            add: (...cls) => cls.forEach((c) => classes.add(c)),
            remove: (...cls) => cls.forEach((c) => classes.delete(c)),
            toggle: (cls, force) => {
                const wanted = force === undefined ? !classes.has(cls) : Boolean(force);
                if (wanted) {
                    classes.add(cls);
                } else {
                    classes.delete(cls);
                }
                return wanted;
            },
        };
    }

    get parentElement() {
        return this.parent;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    /* --- Arbre ------------------------------------------------------------ */

    appendChild(child) {
        if (child.parent) {
            child.parent.removeChild(child);
        }
        child.parent = this;
        this.children.push(child);
        return child;
    }

    insertBefore(node, reference) {
        if (node.parent) {
            node.parent.removeChild(node);
        }
        const index = reference ? this.children.indexOf(reference) : -1;
        node.parent = this;
        if (index === -1) {
            this.children.push(node);
        } else {
            this.children.splice(index, 0, node);
        }
        return node;
    }

    removeChild(child) {
        const index = this.children.indexOf(child);
        if (index !== -1) {
            this.children.splice(index, 1);
            child.parent = null;
        }
        return child;
    }

    remove() {
        if (this.parent) {
            this.parent.removeChild(this);
        }
    }

    /** Copie fidèle SANS listeners (les vrais nodes ne les clonent pas). */
    cloneNode(deep = false) {
        const clone = new FakeElement(this.tagName);
        clone.attributes = { ...this.attributes };
        clone.classes = new Set(this.classes);
        clone.value = this.value;
        clone.hidden = this.hidden;
        clone.textContent = this.textContent;
        clone.style = { ...this.style };
        if (deep) {
            for (const child of this.children) {
                clone.appendChild(child.cloneNode(true));
            }
        }
        return clone;
    }

    /* --- innerHTML : seule écriture « '' » faite par le code testé -------- */

    set innerHTML(markup) {
        if (markup !== '') {
            throw new Error(`innerHTML non parsé par le mini-DOM (reçu : « ${markup} »)`);
        }
        for (const child of [...this.children]) {
            this.removeChild(child);
        }
    }

    /* --- Recherche --------------------------------------------------------- */

    matches(selector) {
        return compileSelector(selector)(this);
    }

    querySelectorAll(selector) {
        const found = [];
        for (const child of this.children) {
            if (child.matches(selector)) {
                found.push(child);
            }
            found.push(...child.querySelectorAll(selector));
        }
        return found;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    closest(selector) {
        let node = this;
        while (node) {
            if (node.matches(selector)) {
                return node;
            }
            node = node.parent;
        }
        return null;
    }

    /* --- Événements (dispatch synchrone, comme un test piloterait) --------- */

    addEventListener(type, handler) {
        (this.listeners[type] ||= []).push(handler);
    }

    emit(type, event = { prevented: false }) {
        if (!('preventDefault' in event)) {
            // Trace preventDefault comme un vrai Event : la garde de
            // soumission du formulaire est testée via ce marqueur.
            event.preventDefault = function () {
                this.prevented = true;
            };
        }
        for (const handler of this.listeners[type] || []) {
            handler(event);
        }
        return event;
    }
}

/** Racine document minimale : createElement + requêtes depuis le corps. */
export function createFakeDocument() {
    const body = new FakeElement('body');
    return {
        body,
        createElement: (tagName) => new FakeElement(tagName),
        querySelector: (selector) => body.querySelector(selector),
        querySelectorAll: (selector) => body.querySelectorAll(selector),
        getElementById: () => null,
    };
}
