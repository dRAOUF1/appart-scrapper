/*
 * Carte des biens d'une recherche + repères personnels (issue #26).
 *
 * Vanilla JS volontaire : Leaflet est la seule dépendance (vendored), aucun
 * bundler, aucun plugin de cluster (interdit par le périmètre).
 *
 * Comportements clés :
 * - INIT PARESSEUSE : rien n'est chargé tant que l'utilisateur n'ouvre pas
 *   l'onglet « Carte » ; ensuite tout vit ici (tuiles OSM, données, CRUD).
 * - Une annonce SANS coordonnées n'est jamais envoyée par le serveur ; une
 *   carte sans aucune annonce géolocalisée affiche un état dédié et reste
 *   utilisable pour les repères personnels.
 * - Les repères perso sont GLOBAUX (par utilisateur) : ils apparaissent sur
 *   toutes les cartes de toutes les recherches.
 * - Toute insertion de texte utilisateur passe par textContent (jamais
 *   innerHTML) : un libellé de repère ne peut pas injecter du HTML.
 * - Un JSON malformé ou une erreur réseau ne casse rien : état d'erreur
 *   affiché, pas d'exception non capturée.
 *
 * Contrat serveur (routes de session web_bp) :
 * - GET  /listings/<id>/map-data -> { points: [...], pins: [...] }
 * - POST /pins                    (form-encoded, champ csrf_token)
 * - PATCH /pins/<id>              (form-encoded, champ csrf_token)
 * - DELETE /pins/<id>             (jeton CSRF via en-tête X-CSRFToken)
 * - GET  /listings/poi            -> catalogue des couches POI (#27)
 * - GET  /listings/poi/<couche>?bbox=S,W,N,E -> { points: [...], degrade }
 */
(function () {
  'use strict';

  var view = document.getElementById('carte-view');
  var tabListe = document.getElementById('tab-liste');
  var tabCarte = document.getElementById('tab-carte');
  var viewListe = document.getElementById('liste-view');
  if (!view || !tabListe || !tabCarte || !viewListe) return;

  var searchId = view.dataset.searchId;
  var csrfToken = '';
  var pinForm = document.getElementById('pin-form');
  if (pinForm) {
    var csrfInput = pinForm.querySelector('input[name="csrf_token"]');
    csrfToken = csrfInput ? csrfInput.value : '';
  }

  var map = null;
  var loaded = false;
  var addingPin = false;
  var bienMarkers = [];
  var pinMarkers = {}; // id -> { marker: Marker, row: HTMLLIElement }
  var tileLayer = null;

  // ------------------------------------------------------------------
  // Petits utilitaires DOM
  // ------------------------------------------------------------------

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function setHint(message, isError) {
    var hint = $('pin-hint');
    if (!hint) return;
    hint.textContent = message || '';
    hint.hidden = !message;
    hint.classList.toggle('map-hint-error', Boolean(isError));
  }

  // ------------------------------------------------------------------
  // Onglets Liste / Carte
  // ------------------------------------------------------------------

  function showListe() {
    viewListe.hidden = false;
    view.hidden = true;
    tabListe.classList.add('active');
    tabCarte.classList.remove('active');
    tabListe.setAttribute('aria-selected', 'true');
    tabCarte.setAttribute('aria-selected', 'false');
  }

  function showCarte() {
    viewListe.hidden = true;
    view.hidden = false;
    tabListe.classList.remove('active');
    tabCarte.classList.add('active');
    tabListe.setAttribute('aria-selected', 'false');
    tabCarte.setAttribute('aria-selected', 'true');
    initOnce();
    // La carte peut avoir été construite alors que le conteneur était
    // masqué (taille nulle) : Leaflet doit recalculer ses dimensions.
    if (map) map.invalidateSize();
  }

  tabListe.addEventListener('click', showListe);
  tabCarte.addEventListener('click', showCarte);

  // ------------------------------------------------------------------
  // Construction de la carte et chargement des données
  // ------------------------------------------------------------------

  function buildMap() {
    map = L.map('map-canvas', { scrollWheelZoom: true }).setView([46.6, 2.4], 6);
    tileLayer = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    });
    tileLayer.addTo(map);
    map.on('click', onMapClick);
    map.on('moveend', onMapMoveEnd);
  }

  function initOnce() {
    if (loaded) return;
    loaded = true;
    try {
      buildMap();
    } catch (err) {
      // Leaflet absent ou conteneur inattendu : la page reste utilisable.
      setHint("Impossible d'initialiser la carte.", true);
      return;
    }
    fetch('/listings/' + encodeURIComponent(searchId) + '/map-data')
      .then(function (resp) { return resp.ok ? resp : Promise.reject(new Error(String(resp.status))); })
      .then(function (resp) { return resp.json(); })
      .then(function (data) { renderData(data); })
      .catch(function () {
        // Réponse malformée ou réseau indisponible : jamais d'exception
        // non capturée, un message suffit — les repères restent ajoutables.
        setHint('Chargement des données impossible — rouvrez l\u2019onglet pour réessayer.', true);
      });
  }

  function renderData(data) {
    var points = data && Array.isArray(data.points) ? data.points : [];
    var pins = data && Array.isArray(data.pins) ? data.pins : [];

    points.forEach(addBienMarker);
    pins.forEach(addPinMarker);
    refreshCounts(points.length);

    var emptyBox = $('map-empty');
    if (emptyBox) emptyBox.hidden = points.length > 0;

    if (points.length) {
      var bounds = L.latLngBounds(bienMarkers.map(function (m) { return m.getLatLng(); }));
      map.fitBounds(bounds.pad(0.25), { maxZoom: 16 });
    }
  }

  function refreshCounts(pointCount) {
    var box = $('map-count');
    var pinCount = Object.keys(pinMarkers).length;
    if (!box) return;
    if (!pointCount) {
      box.textContent = 'Aucune annonce géolocalisée · ' + pinCount + ' repère' + (pinCount > 1 ? 's' : '');
    } else {
      box.textContent =
        pointCount + ' annonce' + (pointCount > 1 ? 's' : '') +
        ' géolocalisée' + (pointCount > 1 ? 's' : '') +
        ' · ' + pinCount + ' repère' + (pinCount > 1 ? 's' : '') + ' perso';
    }
    var counter = $('pin-count');
    if (counter) counter.textContent = String(pinCount);
    var none = $('pin-none');
    if (none) none.hidden = pinCount > 0;
  }

  // ------------------------------------------------------------------
  // Annonces géolocalisées : marqueurs + popups
  //
  // divIcon PARTOUT, jamais L.Icon.Default : les images PNG des marqueurs
  // Leaflet ne sont pas vendored (exception réseau limitée au js/css), et des
  // icônes CSS évitent toute 404. Le style distingue la précision :
  // exacte = point plein ; approximative = anneau translucide + cercle 50 m ;
  // commune = anneau large + cercle ~400 m (centre de commune).
  // ------------------------------------------------------------------

  function precisionLabel(precision) {
    if (precision === 'approximative') return 'Position approximative';
    if (precision === 'commune') return 'Position à la commune près';
    return '';
  }

  function bienIconClass(precision) {
    if (precision === 'approximative') return 'bien-marker bien-approx';
    if (precision === 'commune') return 'bien-marker bien-commune';
    return 'bien-marker bien-exact';
  }

  function addBienMarker(point) {
    var lat = Number(point.latitude);
    var lon = Number(point.longitude);
    // Filet client : le serveur n'envoie que des points valides — si un JSON
    // dégradé glisse une valeur inutilisable, on l'ignore silencieusement.
    if (!isFinite(lat) || !isFinite(lon) || (lat === 0 && lon === 0)) return;

    var icon = L.divIcon({ className: '', html: '<span class="' + bienIconClass(point.location_precision) + '"></span>', iconSize: [16, 16], iconAnchor: [8, 8] });
    var marker = L.marker([lat, lon], { icon: icon }).addTo(map);

    if (point.location_precision === 'approximative') {
      L.circle([lat, lon], { radius: 50, className: 'bien-circle-approx' }).addTo(map);
    } else if (point.location_precision === 'commune') {
      L.circle([lat, lon], { radius: 400, className: 'bien-circle-commune' }).addTo(map);
    }

    marker.bindPopup(buildBienPopup(point));
    bienMarkers.push(marker);
  }

  function buildBienPopup(point) {
    var box = el('div', 'bien-popup');

    var title = point.title || 'Annonce sans titre';
    box.appendChild(el('strong', 'bien-popup-title', String(title)));

    if (point.price) {
      box.appendChild(el('div', 'bien-popup-price', String(point.price)));
    }

    var metaParts = [];
    if (point.surface) metaParts.push(String(point.surface) + ' m²');
    if (point.rooms) {
      var roomsNum = parseInt(point.rooms, 10);
      if (!isNaN(roomsNum)) metaParts.push(roomsNum + ' pièce' + (roomsNum > 1 ? 's' : ''));
    }
    if (point.city) metaParts.push(String(point.city));
    if (metaParts.length) box.appendChild(el('div', 'bien-popup-meta', metaParts.join(' · ')));

    var hintLabel = precisionLabel(point.location_precision);
    if (hintLabel) box.appendChild(el('div', 'bien-popup-precision', hintLabel));

    if (point.url) {
      var link = el('a', 'btn btn-ghost btn-sm bien-popup-link', "Voir l'annonce");
      link.href = String(point.url);
      link.target = '_blank';
      link.rel = 'noopener';
      box.appendChild(link);
    }
    return box;
  }

  // ------------------------------------------------------------------
  // Repères personnels : marqueurs + panneau latéral
  // ------------------------------------------------------------------

  function pinIcon(icon) {
    return L.divIcon({
      className: '',
      html: '<span class="pin-marker">' + (icon || '\uD83D\uDCCD') + '</span>',
      iconSize: [28, 28],
      iconAnchor: [14, 26],
      popupAnchor: [0, -24],
    });
  }

  function addPinMarker(pin) {
    var lat = Number(pin.latitude);
    var lon = Number(pin.longitude);
    if (!isFinite(lat) || !isFinite(lon) || (lat === 0 && lon === 0)) return;

    var marker = L.marker([lat, lon], { icon: pinIcon(pin.icon) }).addTo(map);
    marker.bindPopup(buildPinPopup(pin));

    var row = el('li', 'pin-row');
    row.dataset.pinId = String(pin.id);

    var label = el('span', 'pin-row-label', pin.icon + ' ' + (pin.label || 'Repère'));
    if (pin.note) {
      label.title = pin.note;
    }
    row.appendChild(label);

    var renameBtn = el('button', 'btn btn-ghost btn-sm', '\u270F\uFE0F');
    renameBtn.type = 'button';
    renameBtn.title = 'Renommer ce repère';
    renameBtn.addEventListener('click', function () { promptRenamePin(pin); });
    row.appendChild(renameBtn);

    var deleteBtn = el('button', 'btn btn-ghost btn-sm', '\uD83D\uDDD1\uFE0F');
    deleteBtn.type = 'button';
    deleteBtn.title = 'Supprimer ce repère';
    deleteBtn.addEventListener('click', function () { confirmDeletePin(pin, marker); });
    row.appendChild(deleteBtn);

    var list = $('pin-list');
    if (list) list.prepend(row);

    pinMarkers[pin.id] = { marker: marker, row: row };
    refreshCounts(bienMarkers.length);
  }

  function buildPinPopup(pin) {
    var box = el('div', 'pin-popup');
    box.appendChild(el('strong', 'pin-popup-label', (pin.icon || '') + ' ' + (pin.label || 'Repère')));
    if (pin.note) box.appendChild(el('p', 'pin-popup-note', String(pin.note)));
    return box;
  }

  function promptRenamePin(pin) {
    var nouveau = window.prompt('Nouveau libellé du repère :', pin.label || '');
    if (nouveau === null) return; // annulé
    nouveau = nouveau.trim();
    if (!nouveau) {
      setHint('Le libellé ne peut pas être vide.', true);
      return;
    }

    var body = new FormData();
    body.append('csrf_token', csrfToken);
    body.append('label', nouveau);

    fetch('/pins/' + encodeURIComponent(pin.id), { method: 'PATCH', body: body })
      .then(function (resp) {
        return resp.json().catch(function () { throw new Error(String(resp.status)); })
          .then(function (payload) {
            if (!resp.ok) throw new Error(payload && payload.error ? payload.error : String(resp.status));
            return payload;
          });
      })
      .then(function (updated) {
        pin.label = updated.label !== undefined ? updated.label : nouveau;
        pin.note = updated.note !== undefined ? updated.note : pin.note;
        pin.icon = updated.icon !== undefined ? updated.icon : pin.icon;
        applyPinUpdate(pin);
        setHint('Repère renommé.', false);
      })
      .catch(function (err) { setHint('Renommage impossible : ' + err.message, true); });
  }

  function applyPinUpdate(pin) {
    var entry = pinMarkers[pin.id];
    if (!entry) return;
    entry.marker.setIcon(pinIcon(pin.icon));
    entry.marker.setPopupContent(buildPinPopup(pin));
    if (entry.row) {
      var labelSpan = entry.row.querySelector('.pin-row-label');
      if (labelSpan) {
        labelSpan.textContent = pin.icon + ' ' + (pin.label || 'Repère');
        labelSpan.title = pin.note || '';
      }
    }
  }

  function confirmDeletePin(pin, marker) {
    if (!window.confirm('Supprimer le repère « ' + (pin.label || 'sans nom') + ' » ?')) return;

    fetch('/pins/' + encodeURIComponent(pin.id), {
      method: 'DELETE',
      headers: { 'X-CSRFToken': csrfToken },
    })
      .then(function (resp) {
        return resp.json().catch(function () { throw new Error(String(resp.status)); })
          .then(function (payload) {
            if (!resp.ok) throw new Error(payload && payload.error ? payload.error : String(resp.status));
            return payload;
          });
      })
      .then(function () {
        map.removeLayer(marker);
        var entry = pinMarkers[pin.id];
        if (entry && entry.row && entry.row.parentElement) {
          entry.row.parentElement.removeChild(entry.row);
        }
        delete pinMarkers[pin.id];
        refreshCounts(bienMarkers.length);
        setHint('Repère supprimé.', false);
      })
      .catch(function (err) { setHint('Suppression impossible : ' + err.message, true); });
  }

  // ------------------------------------------------------------------
  // Mode d'ajout d'un repère : bouton -> clic carte -> mini-formulaire
  // ------------------------------------------------------------------

  function onMapClick(event) {
    if (!addingPin) return;
    exitAddMode();
    openPinForm(event.latlng.lat, event.latlng.lng);
  }

  function enterAddMode() {
    addingPin = true;
    var canvas = $('map-canvas');
    if (canvas) canvas.classList.add('map-adding');
    setHint("Cliquez sur la carte à l'endroit de votre repère…", false);
    var btn = $('pin-add-btn');
    if (btn) {
      btn.textContent = 'Annuler l\u2019ajout';
      btn.classList.add('btn-ghost');
    }
  }

  function exitAddMode() {
    addingPin = false;
    var canvas = $('map-canvas');
    if (canvas) canvas.classList.remove('map-adding');
    var btn = $('pin-add-btn');
    if (btn) {
      btn.textContent = '\uD83D\uDCCD Ajouter un repère';
      btn.classList.remove('btn-ghost');
    }
  }

  function openPinForm(lat, lon) {
    if (!pinForm) return;
    pinForm.elements.latitude.value = String(lat);
    pinForm.elements.longitude.value = String(lon);
    var coordsLine = $('pin-form-coords');
    if (coordsLine) {
      coordsLine.textContent = 'Position choisie : ' + lat.toFixed(5) + ', ' + lon.toFixed(5);
    }
    pinForm.hidden = false;
    var labelInput = pinForm.elements.label;
    if (labelInput) labelInput.focus();
  }

  function closePinForm() {
    if (!pinForm) return;
    pinForm.hidden = true;
    pinForm.reset();
  }

  var addBtn = $('pin-add-btn');
  if (addBtn) {
    addBtn.addEventListener('click', function () {
      if (addingPin) {
        exitAddMode();
        setHint('', false);
        closePinForm();
      } else {
        closePinForm();
        enterAddMode();
      }
    });
  }

  var cancelBtn = $('pin-cancel');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', function () {
      closePinForm();
      setHint('', false);
    });
  }

  if (pinForm) {
    pinForm.addEventListener('submit', function (event) {
      event.preventDefault();
      var data = new FormData(pinForm);
      fetch('/pins', { method: 'POST', body: data })
        .then(function (resp) {
          return resp.json().catch(function () { throw new Error(String(resp.status)); })
            .then(function (payload) {
              if (!resp.ok) throw new Error(payload && payload.error ? payload.error : String(resp.status));
              return payload;
            });
        })
        .then(function (pin) {
          addPinMarker(pin);
          closePinForm();
          setHint('Repère « ' + (pin.label || 'sans nom') + ' » ajouté.', false);
        })
        .catch(function (err) { setHint('Ajout impossible : ' + err.message, true); });
    });
  }

  // ------------------------------------------------------------------
  // Couches POI contextuelles (#27)
  //
  // Une couche = un LayerGroup dédié, activable depuis le panneau Calques.
  // RÈGLE ABSOLUE : rien ne se charge au rendu initial des biens — pas même
  // le catalogue des couches, qui n'est fetché qu'au PREMIER clic sur le
  // bouton. Les données d'une couche ne partent qu'à son activation.
  //
  // - Toggle ON  : lazy fetch du proxy POUR LA BBOX VISIBLE -> markers dans
  //   le groupe ; toggle OFF : removeLayer SANS détruire le groupe (la
  //   réactivation est instantanée, sans refetch).
  // - moveend throttlé : re-fetch seulement si une couche est active ET que
  //   la bbox visible a changé SIGNIFICATIVEMENT — la clé client est la bbox
  //   arrondie comme côté serveur (~3 décimales), un petit pan ne re-fetch
  //   donc rien et maximise les hits du cache proxy.
  // - Échec réseau/serveur : toast discret, couche vide, carte intacte ;
  //   `degrade: true` (Overpass indisponible) est traité pareil, sans erreur
  //   bloquante.
  //
  // Le rendu est GÉNÉRIQUE par layer-id/type : ajouter « écoles » demain
  // = une entrée dans COUCHES_POI côté serveur, zéro JS à changer (les types
  // inconnus retombent sur l'icône de la couche).
  // ------------------------------------------------------------------

  var POI_BBOX_ARRONDI = 3;
  var POI_MOVEEND_DELAI_MS = 700;
  var poiState = {};          // id -> { group, active, loading, fetchedKey, icone, label }
  var poiCatalogueCharge = false;
  var poiMoveTimer = null;
  var toastTimer = null;

  var POI_TYPE_ICONES = { bus: '\uD83D\uDE8C', tram: '\uD83D\uDE8A', metro: '\uD83D\uDE87', gare: '\uD83D\uDE89', ferry: '\u26F4\uFE0F' };
  var POI_TYPE_LIBELLES = {
    bus: 'Arrêt de bus',
    tram: 'Arrêt de tram',
    metro: 'Station de m\u00E9tro',
    gare: 'Gare',
    ferry: 'Terminal ferry',
  };

  function showToast(message) {
    var toast = $('map-toast');
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 4000);
  }

  function bboxVisible() {
    if (!map) return '';
    var b = map.getBounds();
    return [
      b.getSouth().toFixed(POI_BBOX_ARRONDI),
      b.getWest().toFixed(POI_BBOX_ARRONDI),
      b.getNorth().toFixed(POI_BBOX_ARRONDI),
      b.getEast().toFixed(POI_BBOX_ARRONDI),
    ].join(',');
  }

  function chargerCataloguePoi() {
    fetch('/listings/poi')
      .then(function (resp) {
        return resp.json().catch(function () { throw new Error(String(resp.status)); })
          .then(function (payload) {
            if (!resp.ok) throw new Error(String(resp.status));
            return payload;
          });
      })
      .then(function (data) {
        poiCatalogueCharge = true;
        construireControlesPoi(data);
        basculerPanneauCalques(true);
      })
      .catch(function () {
        showToast('Calques indisponibles pour le moment.');
      });
  }

  function construireControlesPoi(data) {
    var box = $('poi-controls');
    if (!box) return;
    var couches = data && Array.isArray(data.couches) ? data.couches : [];
    box.textContent = '';
    if (!couches.length) {
      box.appendChild(el('span', 'poi-controls-vide', 'Aucune couche disponible.'));
      return;
    }
    couches.forEach(function (couche) {
      var st = poiState[couche.id];
      if (!st) {
        st = poiState[couche.id] = {
          group: L.layerGroup(),
          active: false,
          loading: false,
          fetchedKey: null,
          icone: couche.icone || '\uD83D\uDCCD',
          label: couche.label || String(couche.id),
        };
      } else {
        st.icone = couche.icone || st.icone;
        st.label = couche.label || st.label;
      }

      var chip = el('button', 'btn btn-ghost btn-sm poi-chip');
      chip.type = 'button';
      chip.dataset.layerId = String(couche.id);
      chip.setAttribute('aria-pressed', st.active ? 'true' : 'false');
      chip.appendChild(el('span', 'poi-chip-label', st.icone + ' ' + st.label));
      var spin = el('span', 'poi-spin');
      spin.hidden = !st.loading;
      chip.appendChild(spin);
      chip.addEventListener('click', function () { togglePoiLayer(couche.id); });
      box.appendChild(chip);
    });
  }

  function basculerPanneauCalques(forceOuvert) {
    var box = $('poi-controls');
    var btn = $('poi-panel-btn');
    if (!box || !btn) return;
    box.hidden = (forceOuvert === true) ? false : !box.hidden;
    btn.setAttribute('aria-expanded', box.hidden ? 'false' : 'true');
  }

  function majChip(id) {
    var st = poiState[id];
    if (!st) return;
    var chip = document.querySelector('.poi-chip[data-layer-id="' + id + '"]');
    if (!chip) return;
    chip.classList.toggle('active', st.active);
    chip.setAttribute('aria-pressed', st.active ? 'true' : 'false');
    chip.setAttribute('aria-busy', st.loading ? 'true' : 'false');
    var spin = chip.querySelector('.poi-spin');
    if (spin) spin.hidden = !st.loading;
  }

  function togglePoiLayer(id) {
    var st = poiState[id];
    if (!st || !map) return;
    st.active = !st.active;
    majChip(id);
    if (st.active) {
      st.group.addTo(map);
      rafraichirCouche(id);
    } else if (map.hasLayer(st.group)) {
      map.removeLayer(st.group);
    }
  }

  function rafraichirCouche(id) {
    var st = poiState[id];
    if (!st || !st.active || st.loading || !map) return;
    var cle = bboxVisible();
    if (!cle || cle === st.fetchedKey) return;
    st.fetchedKey = cle;
    st.loading = true;
    majChip(id);

    fetch('/listings/poi/' + encodeURIComponent(id) + '?bbox=' + encodeURIComponent(cle))
      .then(function (resp) {
        return resp.json().catch(function () { throw new Error(String(resp.status)); })
          .then(function (payload) {
            if (!resp.ok) throw new Error(payload && payload.error ? payload.error : String(resp.status));
            return payload;
          });
      })
      .then(function (payload) {
        st.loading = false;
        majChip(id);
        dessinerPois(st, payload && Array.isArray(payload.points) ? payload.points : []);
        if (payload && payload.degrade) {
          showToast('Couche \u00AB ' + st.label + ' \u00BB indisponible pour le moment.');
        }
      })
      .catch(function () {
        st.loading = false;
        st.fetchedKey = null; // réessaier au prochain moveend / toggle
        majChip(id);
        showToast('Couche \u00AB ' + st.label + ' \u00BB indisponible.');
      });
  }

  function dessinerPois(st, points) {
    st.group.clearLayers();
    points.forEach(function (point) {
      var lat = Number(point.lat);
      var lon = Number(point.lon);
      if (!isFinite(lat) || !isFinite(lon)) return;
      var type = POI_TYPE_ICONES.hasOwnProperty(point.type) ? String(point.type) : '';
      var html = '<span class="poi-marker' + (type ? ' poi-' + type : '') + '">' +
                 (type ? POI_TYPE_ICONES[type] : st.icone) + '</span>';
      var icon = L.divIcon({ className: '', html: html, iconSize: [22, 22], iconAnchor: [11, 11] });
      var marker = L.marker([lat, lon], { icon: icon });

      var box = el('div', 'poi-popup');
      box.appendChild(el('strong', 'poi-popup-nom', String(point.nom || 'Point d\u2019int\u00E9r\u00EAt')));
      if (type && POI_TYPE_LIBELLES[type]) box.appendChild(el('div', 'poi-popup-type', POI_TYPE_LIBELLES[type]));
      marker.bindPopup(box);
      st.group.addLayer(marker);
    });
  }

  function onMapMoveEnd() {
    var uneActive = Object.keys(poiState).some(function (id) { return poiState[id].active; });
    if (!uneActive) return;
    clearTimeout(poiMoveTimer);
    poiMoveTimer = setTimeout(function () {
      Object.keys(poiState).forEach(function (id) {
        if (poiState[id].active) rafraichirCouche(id);
      });
    }, POI_MOVEEND_DELAI_MS);
  }

  var poiPanelBtn = $('poi-panel-btn');
  if (poiPanelBtn) {
    poiPanelBtn.addEventListener('click', function () {
      if (!map) return;
      if (!poiCatalogueCharge) {
        chargerCataloguePoi(); // premier clic seulement, jamais au chargement
      } else {
        basculerPanneauCalques();
      }
    });
  }
})();
