/**
 * Socle UX HTMX du panel admin (issue #16).
 *
 * Cinq responsabilités, volontairement sans framework ni build step :
 *  1. CSRF — pose le jeton sur TOUTES les requêtes HTMX (ceinture) en plus des
 *     champs cachés csrf_token des formulaires (bretelles) ;
 *  2. toasts — affiche les messages des réponses HTMX (événement « admin:toast »,
 *     déclenché par l'en-tête HX-Trigger) et fait disparaître automatiquement
 *     les flashs rendus côté serveur par base.html ;
 *  3. modale de confirmation générique — tout <form data-confirm="..."> passe
 *     par elle, qu'il soit natif ou piloté par htmx ;
 *  4. sélection groupée des annonces (#20) — compteur, barre d'actions et
 *     confirmation avec compte exact pour les checkboxes de la tab ANNONCES ;
 *  5. graphiques Chart.js — instanciés depuis les <script type="application/json"
 *     data-chart-cible="..."> embarqués dans la page (voir admin/_charts.html).
 *
 * Progressive enhancement : sans ce fichier, l'admin reste pleinement
 * fonctionnel (POST classiques, flashs statiques, pas de confirmations,
 * suppression groupée refusée poliment sur sélection vide).
 */
(function () {
    "use strict";

    /* ------------------------------------------------------------------
     * 1. CSRF global pour htmx
     * ------------------------------------------------------------------ */

    /** Lit le jeton CSRF exposé par le layout admin (<meta name="csrf-token">). */
    function lireJetonCsrf() {
        var balise = document.querySelector('meta[name="csrf-token"]');
        return balise ? balise.getAttribute("content") : null;
    }

    document.body.addEventListener("htmx:configRequest", function (evt) {
        var jeton = lireJetonCsrf();
        if (jeton) {
            evt.detail.headers["X-CSRFToken"] = jeton;
        }
    });

    /* ------------------------------------------------------------------
     * 2. Toasts
     * ------------------------------------------------------------------ */

    var DUREE_TOAST_MS = 5000;
    var DUREE_ANIMATION_MS = 300;

    /** Anime puis retire un toast du DOM. */
    function disparaitre(toast) {
        if (!toast || toast.classList.contains("toast-out")) return;
        toast.classList.add("toast-out");
        window.setTimeout(function () { toast.remove(); }, DUREE_ANIMATION_MS);
    }

    /** Crée un toast flottant dans #admin-toasts (catégories : success/error/warning/info). */
    function afficherToast(message, categorie) {
        var conteneur = document.getElementById("admin-toasts");
        if (!conteneur || !message) return;

        var toast = document.createElement("div");
        toast.className = "toast toast-" + (categorie || "info");
        toast.setAttribute("role", "status");

        var texte = document.createElement("span");
        texte.textContent = message;

        var fermer = document.createElement("button");
        fermer.className = "toast-close";
        fermer.setAttribute("aria-label", "Fermer la notification");
        fermer.textContent = "×";
        fermer.addEventListener("click", function () { disparaitre(toast); });

        toast.appendChild(texte);
        toast.appendChild(fermer);
        conteneur.appendChild(toast);
        window.setTimeout(function () { disparaitre(toast); }, DUREE_TOAST_MS);
    }

    /** Fait disparaître automatiquement les flashs rendus par base.html. */
    function animerFlashsExistants(racine) {
        racine.querySelectorAll(".toast:not(.toast-out)").forEach(function (toast) {
            window.setTimeout(function () { disparaitre(toast); }, DUREE_TOAST_MS);
        });
    }

    /* Les réponses d'actions HTMX portent :
       HX-Trigger: {"admin:toast": {"message": "...", "category": "success"}} */
    document.body.addEventListener("admin:toast", function (evt) {
        var detail = evt.detail || {};
        afficherToast(detail.message, detail.category);
    });

    /* ------------------------------------------------------------------
     * 3. Modale de confirmation générique
     * ------------------------------------------------------------------ */

    var fondModale = null;
    var formulaireEnAttente = null;
    var elementAyantLeFocus = null;

    function ouvrirModale(message, formulaire) {
        formulaireEnAttente = formulaire;
        elementAyantLeFocus = document.activeElement;
        document.getElementById("admin-modal-message").textContent = message;
        fondModale.hidden = false;
        var boutonConfirmer = fondModale.querySelector("[data-modal-confirmer]");
        boutonConfirmer.focus();
    }

    function fermerModale() {
        fondModale.hidden = true;
        formulaireEnAttente = null;
        if (elementAyantLeFocus && typeof elementAyantLeFocus.focus === "function") {
            elementAyantLeFocus.focus();
        }
    }

    /** Confirme : re-déclenche la soumission SANS repasser par la modale. */
    function confirmerSoumission() {
        var formulaire = formulaireEnAttente;
        fermerModale();
        if (!formulaire) return;
        formulaire.dataset.confirme = "true";
        if (typeof formulaire.requestSubmit === "function") {
            formulaire.requestSubmit();
        } else {
            formulaire.submit(); // très vieux navigateurs : soumission native directe
        }
        delete formulaire.dataset.confirme;
    }

    // Phase de capture : court AVANT le listener de htmx, pour intercepter
    // indifféremment les formulaires natifs et ceux pilotés par hx-post.
    document.addEventListener("submit", function (evt) {
        var formulaire = evt.target;
        if (!fondModale || !formulaire.matches("form[data-confirm]")) return;
        if (formulaire.dataset.confirme === "true") return; // déjà validé par la modale

        evt.preventDefault();
        evt.stopImmediatePropagation();
        ouvrirModale(formulaire.getAttribute("data-confirm"), formulaire);
    }, true);

    /* ------------------------------------------------------------------
     * 4. Sélection groupée des annonces (#20)
     * ------------------------------------------------------------------
     * Checkboxes de la tab ANNONCES : compteur dynamique, barre d'actions
     * visible dès qu'au moins une annonce est cochée, et modale de
     * confirmation portant LE NOMBRE exact d'annonces à supprimer.
     *
     * Progressive enhancement : sans ce fichier, les checkboxes partent
     * quand même en POST (le serveur refuse poliment une sélection vide) ;
     * la barre reste simplement affichée et le message de confirmation est
     * le libellé générique rendu côté serveur.
     */

    /** Met la barre en cohérence avec l'état courant des cases à cocher. */
    function majSelectionAnnonces(racine) {
        var barre = document.getElementById("annonces-bulk-bar");
        if (!barre) return;

        var cochees = Array.prototype.slice.call(
            racine.querySelectorAll(".annonce-check:checked")
        );
        var nombre = cochees.length;
        var pluriel = nombre > 1 ? "s" : "";

        var compteur = document.getElementById("bulk-compteur");
        if (compteur) {
            compteur.textContent = nombre + " annonce" + pluriel + " sélectionnée" + pluriel;
        }

        // La modale data-confirm lit l'attribut AU MOMENT de la soumission :
        // le mettre à jour ici donne un message avec le compte exact.
        var formulaire = document.getElementById("formulaire-bulk-suppression");
        if (formulaire && nombre > 0) {
            formulaire.setAttribute(
                "data-confirm",
                "Supprimer " + nombre + " annonce" + pluriel
                + " sélectionnée" + pluriel + " ? Cette action est définitive."
            );
        }

        barre.hidden = nombre === 0;
    }

    // Délégation : les lignes sont remplacées par chaque swap HTMX, on n'attache
    // donc JAMAIS de listener direct aux checkboxes elles-mêmes.
    document.addEventListener("change", function (evt) {
        var cible = evt.target;
        if (!cible || !estCaseSelection(cible)) return;
        if (cible.id === "annonces-select-all") {
            document.querySelectorAll(".annonce-check").forEach(function (case_) {
                case_.checked = cible.checked;
            });
        }
        majSelectionAnnonces(document);
    });

    /** Vrai si la case changée appartient au dispositif de sélection (#20). */
    function estCaseSelection(element) {
        return element.classList.contains("annonce-check")
            || element.id === "annonces-select-all";
    }

    /* ------------------------------------------------------------------
     * 5. Graphiques Chart.js
     * ------------------------------------------------------------------ */

    // Palette alignée sur le thème sombre de style.css.
    var COULEURS_SERIES = [
        "rgba(10, 132, 255, 0.75)",  // accent bleu
        "rgba(48, 209, 88, 0.75)",   // vert
        "rgba(191, 90, 242, 0.75)",  // violet
        "rgba(255, 159, 10, 0.75)",  // orange
        "rgba(255, 69, 58, 0.75)",   // rouge
        "rgba(100, 210, 255, 0.75)"  // cyan clair
    ];
    var COULEUR_TEXTE = "#a1a1aa";
    var COULEUR_GRILLE = "rgba(255, 255, 255, 0.08)";

    function optionsCommunes() {
        return {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: COULEUR_TEXTE } },
                tooltip: { rtl: false }
            },
            scales: {
                x: { ticks: { color: COULEUR_TEXTE }, grid: { color: COULEUR_GRILLE } },
                y: { ticks: { color: COULEUR_TEXTE }, grid: { color: COULEUR_GRILLE }, beginAtZero: true }
            }
        };
    }

    var CONFIGURATIONS = {
        bar: function (donnees) {
            var config = optionsCommunes();
            config.type = "bar";
            donnees.datasets.forEach(function (serie) {
                serie.backgroundColor = COULEURS_SERIES[0];
                serie.borderRadius = 4;
            });
            config.data = donnees;
            return config;
        },
        doughnut: function (donnees) {
            return {
                type: "doughnut",
                data: Object.assign({}, donnees, {
                    datasets: [{
                        data: donnees.datasets.length ? donnees.datasets[0].data : [],
                        backgroundColor: COULEURS_SERIES
                    }]
                }),
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: "right", labels: { color: COULEUR_TEXTE } } }
                }
            };
        }
    };

    /** Instancie tous les graphiques non encore initialisés sous `racine`. */
    function initialiserGraphiques(racine) {
        if (typeof window.Chart === "undefined") return;
        racine.querySelectorAll("script[type='application/json'][data-chart-cible]").forEach(function (noeud) {
            var canvas = document.querySelector(noeud.getAttribute("data-chart-cible"));
            if (!canvas || canvas.dataset.chartInitialise) return;

            var fabrique = CONFIGURATIONS[noeud.getAttribute("data-chart-type")] || CONFIGURATIONS.bar;
            try {
                var donnees = JSON.parse(noeud.textContent);
                new window.Chart(canvas, fabrique(donnees));
                canvas.dataset.chartInitialise = "true";
            } catch (erreur) {
                // Données malformées : on n'affiche pas le graphique mais on ne casse pas la page.
                console.error("Chart.js : données de graphique invalides", erreur);
            }
        });
    }

    /* ------------------------------------------------------------------
     * 5. Initialisation + intégration htmx
     * ------------------------------------------------------------------ */

    function demarrer() {
        fondModale = document.getElementById("admin-modal-backdrop");

        fondModale.addEventListener("click", function (evt) {
            if (evt.target === fondModale) fermerModale();
        });
        fondModale.querySelector("[data-modal-annuler]").addEventListener("click", fermerModale);
        fondModale.querySelector("[data-modal-confirmer]").addEventListener("click", confirmerSoumission);
        document.addEventListener("keydown", function (evt) {
            if (evt.key === "Escape" && !fondModale.hidden) fermerModale();
        });

        animerFlashsExistants(document);
        initialiserGraphiques(document);
        // État initial de la barre d'actions groupées (#20) : cachée tant
        // que rien n'est coché (enhancement — sans JS elle reste visible).
        majSelectionAnnonces(document);

        // Après un swap (navigation, action POST), le nouveau contenu peut porter
        // des flashs fraîchement rendus, de nouveaux canvas à instancier ou une
        // table d'annonces dont la sélection est à réinitialiser.
        document.body.addEventListener("htmx:afterSwap", function (evt) {
            animerFlashsExistants(evt.target);
            initialiserGraphiques(evt.target);
            majSelectionAnnonces(document);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", demarrer);
    } else {
        demarrer();
    }
})();
