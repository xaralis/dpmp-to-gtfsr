// Map page behaviour. Kept out of the template so it is cached separately,
// checked by a syntax-aware tool, and never mixed with Jinja braces.
// Its one server-supplied value arrives on #map as data-refresh-ms.

import * as maplibregl from "/static/vendor/maplibre-gl.mjs";

(function () {
  "use strict";

  var mapEl = document.getElementById("map");
  var statusEl = document.getElementById("map-status");
  var detailEl = document.getElementById("detail");
  var chipsEl = document.getElementById("chips");
  var footEl = document.getElementById("map-foot");
  var REFRESH_MS = Number(mapEl.dataset.refreshMs) || 15000;

  // Brand colours straight from the palette -- on a dark basemap they need no
  // adjustment, which is what they were designed for.
  var palette = getComputedStyle(document.documentElement);
  var TROLLEY = "#2ad4c5", BUS = "#8b93a3", PICKED = "#c6f432";
  var COAL = palette.getPropertyValue("--coal").trim();
  var LINEN = palette.getPropertyValue("--linen").trim();
  var VEHICLE_FILL = {
    trolley: palette.getPropertyValue("--teal").trim(),
    bus: palette.getPropertyValue("--warning").trim()
  };

  // Free and keyless: no registration, no limits on views or requests. Its tile
  // metadata carries the OpenFreeMap / OpenMapTiles / OpenStreetMap attribution,
  // which the attribution control below picks up by itself.
  var BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/dark";
  // The public instance comes with no availability promise, and the vehicles
  // are the point of this page. Without the style there are no glyphs either,
  // so the overlays below drop their text and keep working.
  var NO_BASEMAP = {
    version: 8, sources: {},
    layers: [{ id: "background", type: "background", paint: { "background-color": COAL } }]
  };

  // This style is tuned for maps that carry their own data, so at city zoom the
  // street grid all but disappears. Lifting these brings roads and labels back
  // without swapping to a light basemap.
  var LIFTED_ROADS = {
    highway_path: "#2c2c30", highway_minor: "#343434",
    highway_major_casing: "#5c5c5c", highway_major_inner: "#2a2a2a", highway_major_subtle: "#484848",
    highway_motorway_casing: "#6a6a6a", highway_motorway_inner: "#2e2e2e",
    highway_motorway_subtle: "#3c3c3c"
  };
  var LIFTED_LABEL = "#9a9a95";

  // Vendored too; left to itself MapLibre would look for it beside a bundle.
  maplibregl.setWorkerUrl("/static/vendor/maplibre-gl-worker.mjs");

  // Zoom numbers are one lower than Leaflet's were for the same picture:
  // MapLibre counts in 512 px tiles, Leaflet counted in 256 px ones.
  //
  // Wheel, trackpad pinch (a wheel event with ctrlKey set) and two-finger touch
  // are all stock behaviour. Only rotation and tilt are off: a passenger map
  // gains nothing from them, and a stray two-finger twist on a phone would
  // leave it turned with no obvious way back.
  var map;
  try {
    map = new maplibregl.Map({
      container: mapEl,
      style: BASEMAP_STYLE,
      center: [15.7812, 50.0343],
      zoom: 11,
      maxZoom: 18,
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      // Always the full text. The compact form hides the OpenStreetMap credit
      // behind a button on exactly the screens where it fits fine.
      attributionControl: { compact: false },
      // The vehicle source is replaced every refresh; fading its symbols back
      // in each time reads as flicker.
      fadeDuration: 0
    });
  } catch (err) {
    // No WebGL, which MapLibre cannot do without.
    statusEl.textContent = "Mapu nelze zobrazit: prohlížeč nepodporuje WebGL.";
    return;
  }
  map.touchZoomRotate.disableRotation();
  map.keyboard.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");

  // The legend and status sit just above the attribution, which wraps to a
  // second line on the narrowest phones.
  var attributionEl = mapEl.querySelector(".maplibregl-ctrl-attrib");
  if (attributionEl && window.ResizeObserver) {
    new ResizeObserver(function () {
      footEl.style.bottom = attributionEl.offsetHeight + 6 + "px";
    }).observe(attributionEl);
  }

  // Exposed for debugging from the console, and so tests can assert on zoom
  // without depending on tile URLs appearing in the DOM.
  window.dpmpMap = map;

  var basemapSettled = false;
  var hasGlyphs = true;
  map.once("style.load", function () {
    basemapSettled = true;
    map.getStyle().layers.forEach(function (layer) {
      if (LIFTED_ROADS[layer.id]) map.setPaintProperty(layer.id, "line-color", LIFTED_ROADS[layer.id]);
      if (/^(place|highway_name)/.test(layer.id)) map.setPaintProperty(layer.id, "text-color", LIFTED_LABEL);
      // Names a pattern the style's own sprite does not have; the flat fill
      // underneath is all it ever shows, minus a warning on every tile.
      if (layer.id === "landcover_wood") map.setPaintProperty(layer.id, "fill-pattern", undefined);
    });
  });
  map.on("error", function (e) {
    console.warn("map:", e.error && e.error.message);
    if (basemapSettled) return;
    basemapSettled = true;
    hasGlyphs = false;
    map.setStyle(NO_BASEMAP);
  });
  var mapReady = new Promise(function (resolve) { map.once("load", resolve); });

  // Below this the whole network is in view, vehicles are a few pixels apart
  // and an arrow on each is noise rather than information.
  var HEADING_MIN_ZOOM = 13;
  // Generous for a fingertip; a 3.5 px stop is otherwise close to untappable.
  var HIT_RADIUS = 8;
  // Topmost first, so whatever is drawn on top also wins the click.
  var CLICK_ORDER = ["vehicles", "platforms", "stops", "routes"];
  var LAYER_GROUPS = [
    { label: "Trasy", key: "routes", layers: ["routes"] },
    { label: "Zastávky", key: "stops", layers: ["stops"] },
    { label: "Zastávky linky", key: "platforms", layers: ["platforms", "platform-labels"] },
    { label: "Vozidla", key: "vehicles", layers: ["vehicle-arrows", "vehicles"] }
  ];

  // route number -> { routes (GeoJSON features), bounds, name, trolleybus }
  var lines = {};
  var selected = null;
  var allBounds = null;
  var counts = { routes: 0, stops: 0 };
  var lastVehicles = [];
  var stations = [];    // every stop; layer features refer to it by index
  var platforms = [];   // every platform, with the lines that call there
  var feedTime = null;
  var failure = null;
  var nextRefreshAt = 0;
  // Set from /healthz while the static feed is (re)building. A cold start
  // fetches ~2,700 trips over several minutes -- during that window
  // /coverage.geojson and /vehicles.json both 404, and that is not a
  // failure, just data that does not exist yet.
  var buildPhase = null;
  var coverageError = null;
  var buildPollTimer = null;
  // The one open popup, and which layer group it belongs to.
  var popup = null;
  var popupOwner = null;
  // Which vehicle's popup should be open, if any. It outlives the popup itself:
  // a vehicle that drops out of the feed for one refresh, or is hidden by the
  // current selection, gets its popup back when it returns.
  var openVehicle = null;
  // Set while a refresh closes a popup, which is not the user dismissing it.
  var syncing = false;

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function featureCollection(features) {
    return { type: "FeatureCollection", features: features };
  }

  function point(lon, lat, properties) {
    return {
      type: "Feature", properties: properties,
      geometry: { type: "Point", coordinates: [lon, lat] }
    };
  }

  // --- layers ---------------------------------------------------------------

  // Drawn rather than taken from CSS, because a symbol layer can only use
  // images. Same shape language as the chips: buses are rounded, trolleybuses
  // are square. The stretch zones keep the corners intact when a longer line
  // number widens the badge.
  function addBadgeImage(id, fill, radius) {
    var scale = 2, size = 20 * scale, middle = size / 2;
    var canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = COAL;
    ctx.beginPath();
    ctx.roundRect(0, 0, size, size, radius * scale);
    ctx.fill();
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.roundRect(scale, scale, size - 2 * scale, size - 2 * scale, Math.max(radius - 1, 0) * scale);
    ctx.fill();
    map.addImage(id, ctx.getImageData(0, 0, size, size), {
      pixelRatio: scale,
      stretchX: [[middle - 1, middle + 1]],
      stretchY: [[middle - 1, middle + 1]]
    });
  }

  // Direction of travel: an arrow orbiting the marker, pointing at the next
  // stop. Outside the badge rather than on top of it -- drawn over the fill it
  // read as a bite taken out of the circle, not as a heading. The outline is
  // there because the arrow crosses lime route lines and the dark background
  // alike.
  //
  // The bearing comes from the server, because the upstream's gps_course is
  // null on every vehicle ever recorded.
  function addArrowImage() {
    // 27 px from the centre to the tip, which clears the 20 px badge at any bearing.
    var scale = 2, size = 54 * scale, middle = size / 2;
    var canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    var ctx = canvas.getContext("2d");
    ctx.beginPath();
    ctx.moveTo(middle, 2 * scale);
    ctx.lineTo(middle + 8 * scale, 15 * scale);
    ctx.lineTo(middle - 8 * scale, 15 * scale);
    ctx.closePath();
    ctx.lineJoin = "round";
    ctx.lineWidth = 3 * scale;
    ctx.strokeStyle = COAL;
    ctx.stroke();
    ctx.fillStyle = LINEN;
    ctx.fill();
    map.addImage("vehicle-arrow", ctx.getImageData(0, 0, size, size), { pixelRatio: scale });
  }

  function addDataLayers() {
    addBadgeImage("vehicle-bus", VEHICLE_FILL.bus, 10);
    addBadgeImage("vehicle-trolley", VEHICLE_FILL.trolley, 0);
    addArrowImage();

    ["routes", "stops", "platforms", "vehicles"].forEach(function (id) {
      map.addSource(id, { type: "geojson", data: featureCollection([]) });
    });

    map.addLayer({
      id: "routes", type: "line", source: "routes",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": PICKED, "line-width": 5, "line-opacity": 0.95 }
    });
    map.addLayer({
      id: "stops", type: "circle", source: "stops",
      paint: {
        "circle-radius": 3.5, "circle-color": LINEN, "circle-opacity": 0.9,
        "circle-stroke-color": COAL, "circle-stroke-width": 1.5
      }
    });
    map.addLayer({
      id: "platforms", type: "circle", source: "platforms",
      paint: {
        "circle-radius": 4, "circle-color": PICKED,
        "circle-stroke-color": COAL, "circle-stroke-width": 1.5
      }
    });
    if (hasGlyphs) {
      // Stop names for the selected line. Where they would collide the less
      // important one waits for the next zoom level instead of stacking.
      map.addLayer({
        id: "platform-labels", type: "symbol", source: "platforms",
        layout: {
          "text-field": ["get", "name"],
          "text-font": ["Noto Sans Regular"],
          "text-size": 11,
          "text-anchor": "left",
          "text-offset": [0.8, -0.5]
        },
        paint: { "text-color": LINEN, "text-halo-color": COAL, "text-halo-width": 1.5 }
      });
    }
    // Above stops and lines, so a vehicle is never hidden behind them.
    map.addLayer({
      id: "vehicle-arrows", type: "symbol", source: "vehicles",
      minzoom: HEADING_MIN_ZOOM,
      filter: ["has", "heading"],
      layout: {
        "icon-image": "vehicle-arrow",
        "icon-rotate": ["get", "heading"],
        "icon-rotation-alignment": "map",
        "icon-allow-overlap": true,
        "icon-ignore-placement": true
      }
    });
    var vehicleLayout = {
      "icon-image": ["concat", "vehicle-", ["get", "kind"]],
      "icon-allow-overlap": true,
      "icon-ignore-placement": true
    };
    if (hasGlyphs) {
      vehicleLayout["icon-text-fit"] = "both";
      // Top, right, bottom, left: makes a one-digit badge the same 20 px circle
      // the legend shows, and lets a longer number grow it sideways.
      vehicleLayout["icon-text-fit-padding"] = [2, 6, 3, 6];
      // Vehicles are labelled with their line number: a plain dot tells you a
      // bus is there, not which one, and "which line is that" is the first
      // thing anyone asks of a live map.
      vehicleLayout["text-field"] = ["get", "line"];
      vehicleLayout["text-font"] = ["Noto Sans Bold"];
      vehicleLayout["text-size"] = 11;
      vehicleLayout["text-allow-overlap"] = true;
      vehicleLayout["text-ignore-placement"] = true;
    }
    map.addLayer({
      id: "vehicles", type: "symbol", source: "vehicles",
      layout: vehicleLayout, paint: { "text-color": COAL }
    });

    map.on("click", onMapClick);
    map.on("mousemove", function (e) {
      map.getCanvas().style.cursor = hitAt(e.point) ? "pointer" : "";
    });
  }

  // Whatever is under the pointer, by CLICK_ORDER and then by distance: with a
  // fingertip-sized radius several stops can fall inside it at once.
  function hitAt(where) {
    var hits = map.queryRenderedFeatures([
      [where.x - HIT_RADIUS, where.y - HIT_RADIUS],
      [where.x + HIT_RADIUS, where.y + HIT_RADIUS]
    ], { layers: CLICK_ORDER });

    for (var i = 0; i < CLICK_ORDER.length; i++) {
      var inLayer = hits.filter(function (f) { return f.layer.id === CLICK_ORDER[i]; });
      if (inLayer.length && CLICK_ORDER[i] === "routes") return inLayer[0];
      if (inLayer.length) {
        return inLayer.reduce(function (best, f) {
          return distanceTo(where, f) < distanceTo(where, best) ? f : best;
        });
      }
    }
    return null;
  }

  function distanceTo(where, feature) {
    return where.dist(map.project(feature.geometry.coordinates));
  }

  function onMapClick(e) {
    var hit = hitAt(e.point);
    if (!hit) return;
    var layer = hit.layer.id, props = hit.properties;

    if (layer === "routes") {
      select(String(props.route));
    } else if (layer === "stops") {
      openStopPopup(stations[props.index], "stops");
    } else if (layer === "platforms") {
      openStopPopup(platforms[props.index], "platforms");
    } else if (layer === "vehicles") {
      // Clicking a vehicle picks its line too: the question that follows
      // "which bus is that" is almost always "and where does it go".
      openVehicle = props.vehicle_id;
      var vehicle = lastVehicles.filter(function (v) { return v.vehicle_id === openVehicle; })[0];
      if (vehicle && selected !== vehicle.line) select(vehicle.line, true);
      else syncVehiclePopup();
    }
  }

  // The layer switch Leaflet used to supply. MapLibre has none built in.
  function addLayerSwitch() {
    var box = document.createElement("div");
    box.className = "maplibregl-ctrl maplibregl-ctrl-group layer-switch";

    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.setAttribute("aria-label", "Vrstvy mapy");
    toggle.setAttribute("aria-expanded", "false");
    toggle.innerHTML =
      '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" fill="none" ' +
      'stroke="currentColor" stroke-width="1.5" stroke-linejoin="round">' +
      '<path d="M10 3 18 7.5 10 12 2 7.5Z"/><path d="M4.5 11 2 12.5 10 17 18 12.5 15.5 11"/></svg>';

    var panel = document.createElement("div");
    panel.className = "layer-panel";
    panel.hidden = true;

    function setOpen(open) {
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
    }
    toggle.addEventListener("click", function () { setOpen(panel.hidden); });
    map.on("click", function () { setOpen(false); });

    LAYER_GROUPS.forEach(function (group) {
      var label = document.createElement("label");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      box.addEventListener("change", function () { setGroupVisible(group, box.checked); });
      label.appendChild(box);
      label.appendChild(document.createTextNode(" " + group.label));
      panel.appendChild(label);
    });

    box.appendChild(toggle);
    box.appendChild(panel);
    map.addControl({
      onAdd: function () { return box; },
      onRemove: function () { box.remove(); }
    }, "top-right");
  }

  function setGroupVisible(group, visible) {
    group.layers.forEach(function (id) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    });
    // A popup does not outlive the thing it points at.
    if (group.key === "vehicles") syncVehiclePopup();
    else if (!visible) closePopupOf(group.key);
  }

  // --- popups ---------------------------------------------------------------

  function openPopup(lngLat, html, owner) {
    if (popup) popup.remove();
    var opened = new maplibregl.Popup({ anchor: "bottom", offset: 12, maxWidth: "min(360px, calc(100vw - 16px))" });
    opened.on("close", function () {
      if (popup !== opened) return;
      popup = null;
      popupOwner = null;
      if (!syncing) openVehicle = null;
    });
    popup = opened;
    popupOwner = owner;
    opened.setLngLat(lngLat).setHTML(html).addTo(map);
    keepInView(opened);
    return opened;
  }

  function closePopupOf(owner) {
    if (popup && popupOwner === owner) popup.remove();
  }

  // MapLibre leaves this to the page. Fixed anchor plus panning, rather than
  // letting the popup flip around its point, so it never ends up under the line
  // chips or half off a phone screen.
  function keepInView(target) {
    var el = target.getElement();
    if (!el) return;
    // Measured from the projected anchor rather than the element, which is
    // mid-transition while an earlier pan is still running.
    map.stop();
    var at = map.project(target.getLngLat());
    var width = el.offsetWidth, height = el.offsetHeight;
    var margin = 8;
    var top = chipsEl.getBoundingClientRect().bottom - mapEl.getBoundingClientRect().top + margin;
    var dy = Math.min(at.y - height - top, 0);
    var dx = Math.min(at.x - width / 2 - margin, 0) ||
             Math.max(at.x + width / 2 - mapEl.clientWidth + margin, 0);
    if (dx || dy) map.panBy([dx, dy]);
  }

  function openStopPopup(stop, owner) {
    var id = stop.stop_id || stop.id;
    var opened = openPopup([stop.lon, stop.lat],
      '<div class="popup-title">' + escapeHtml(stop.name) + "</div>" +
      '<div class="popup-meta">' + (stop.meta || platformLabel(stop) + escapeHtml(id)) + "</div>" +
      '<div class="dep-none">Načítám odjezdy…</div>', owner);
    loadDepartures(stop, opened);
  }

  function syncVehiclePopup() {
    if (openVehicle === null) return;
    var hidden = map.getLayoutProperty("vehicles", "visibility") === "none";
    var vehicle = hidden ? null : lastVehicles.filter(function (v) {
      return v.vehicle_id === openVehicle && (selected === null || v.line === selected);
    })[0];

    if (!vehicle) {
      syncing = true;
      closePopupOf("vehicles");
      syncing = false;
    } else if (popupOwner === "vehicles") {
      popup.setLngLat([vehicle.longitude, vehicle.latitude]).setHTML(vehiclePopup(vehicle));
    } else {
      var wanted = openVehicle;
      openPopup([vehicle.longitude, vehicle.latitude], vehiclePopup(vehicle), "vehicles");
      // Replacing another popup counts as dismissing that one.
      openVehicle = wanted;
    }
  }

  function restyle() {
    // On the map only while its line is picked. Drawing every route at once
    // buries the vehicles and stops under 425 overlapping shapes.
    map.getSource("routes").setData(featureCollection(selected === null ? [] : lines[selected].routes));

    Array.prototype.forEach.call(chipsEl.querySelectorAll(".chip[data-line]"), function (chip) {
      chip.setAttribute("aria-pressed", String(chip.dataset.line === selected));
    });
    document.getElementById("chip-all").setAttribute("aria-pressed", String(selected === null));
  }

  // CIS does not know platforms, so most stops have no number to show.
  function platformLabel(p) {
    return p.platform ? "nástupiště " + escapeHtml(p.platform) + " · " : "";
  }

  // Platforms of the selected line only: all 390 at once would bury the map.
  function renderPlatforms() {
    closePopupOf("platforms");
    var features = [];
    if (selected !== null) {
      platforms.forEach(function (p, index) {
        if (p.lines.indexOf(selected) !== -1) {
          features.push(point(p.lon, p.lat, { index: index, name: p.name }));
        }
      });
    }
    map.getSource("platforms").setData(featureCollection(features));
  }

  function departureRows(payload, showPlatform) {
    var deps = payload.departures || [];
    if (!deps.length) return '<div class="dep-none">Dnes odsud už nic nejede.</div>';

    return '<div class="dep-board">' + deps.map(function (d) {
      // The expected time is the one to read when it differs from the
      // timetable; showing both would just make the row harder to scan.
      var shifted = d.expected !== d.scheduled;
      var mins = Math.round(d.in_seconds / 60);
      return '<span class="dep-time' + (shifted ? " shifted" : "") + '">' +
               escapeHtml(d.expected) + "</span>" +
             '<span class="dep-line">' + escapeHtml(d.line) +
               (showPlatform && d.platform
                 ? '<span class="dep-plat">/' + escapeHtml(d.platform) + "</span>" : "") +
             "</span>" +
             '<span class="dep-to">' +
               '<span class="dep-name">' + escapeHtml(d.headsign) + "</span>" +
               '<span class="dep-in">· ' + (mins < 1 ? "teď" : mins + " min") + "</span>" +
               (shifted ? '<span class="dep-in">(řád ' + escapeHtml(d.scheduled) + ")</span>" : "") +
             "</span>";
    }).join("") + "</div>";
  }

  function loadDepartures(p, target) {
    var id = p.stop_id || p.id;
    // A station board merges platforms, so it has to say which one each
    // departure leaves from; a platform board obviously does not.
    var isStation = id.indexOf("P") === -1;
    var meta = p.meta ? p.meta
             : platformLabel(p) + escapeHtml(id);
    var head = '<div class="popup-title">' + escapeHtml(p.name) + "</div>" +
               '<div class="popup-meta">' + meta + "</div>";

    function show(html) {
      if (popup !== target) return;
      target.setHTML('<div class="dep-pop">' + head + html + "</div>");
      // The board is far taller than the placeholder it replaces.
      keepInView(target);
    }

    fetch("/departures/" + encodeURIComponent(id) + ".json")
      .then(function (r) { if (!r.ok) throw new Error("nedostupné"); return r.json(); })
      .then(function (payload) { show(departureRows(payload, isStation)); })
      .catch(function () {
        show('<div class="dep-none">Odjezdy se nepodařilo načíst.</div>');
      });
  }

  function select(number, keepView) {
    selected = (selected === number) ? null : number;
    restyle();
    describe();
    renderPlatforms();
    renderVehicles(lastVehicles);
    paintStatus();

    // Picking a line from the chips means "show me this line", so the map
    // frames it. Picking it by clicking a vehicle does not: the view would
    // jump away from the vehicle just clicked, taking its popup with it.
    if (keepView) return;
    var target = selected ? lines[selected].bounds : allBounds;
    if (target && !target.isEmpty()) fitTo(target);
  }

  // Clear of the chips and the detail box above and the legend and status
  // below, which on a phone take up a good part of the screen. Where they leave
  // too little room, the detail box is the one allowed to cover the map.
  function fitTo(bounds) {
    var frame = mapEl.getBoundingClientRect();
    var bottom = frame.bottom - footEl.getBoundingClientRect().top + 16;
    var top = 96;
    if (!detailEl.hidden) {
      var belowDetail = detailEl.getBoundingClientRect().bottom - frame.top + 16;
      if (frame.height - belowDetail - bottom > 160) top = belowDetail;
    }
    map.fitBounds(bounds, { padding: { top: top, bottom: bottom, left: 24, right: 24 } });
  }

  function describe() {
    // With no line picked the box has nothing to say, and an empty panel just
    // pushes the map down.
    if (selected === null) {
      detailEl.hidden = true;
      return;
    }
    detailEl.hidden = false;
    var line = lines[selected];
    var running = lastVehicles.filter(function (v) { return v.line === selected; }).length;
    detailEl.innerHTML =
      '<span class="detail-line">Linka ' + escapeHtml(selected) + "</span> — " +
      escapeHtml(line.name || "") +
      '<br><span class="muted">' + (line.trolleybus ? "trolejbus" : "autobus") +
      " · " + line.routes.length + " tras · " +
      (running === 1 ? "1 vůz právě jede" : running + " vozů právě jede") + "</span>";
  }

  // --- static coverage ------------------------------------------------------

  addLayerSwitch();

  Promise.all([fetch("/coverage.geojson").then(function (r) {
    if (!r.ok) throw new Error("feed není hotový (" + r.status + ")");
    return r.json();
  }), mapReady]).then(function (loaded) {
    var geo = loaded[0];
    var stopFeatures = [];
    allBounds = new maplibregl.LngLatBounds();

    addDataLayers();

    geo.features.forEach(function (f) {
      var p = f.properties;

      if (p.kind === "route") {
        counts.routes++;
        var number = String(p.route);

        if (!lines[number]) {
          lines[number] = {
            routes: [], bounds: new maplibregl.LngLatBounds(),
            name: p.name, trolleybus: p.trolleybus
          };
        }
        var line = lines[number];
        // Kept here, handed to the map only while its line is selected. All 425
        // shapes at once turn the city into a thicket that hides the vehicles
        // and stops, which are the parts that are actually live.
        line.routes.push({
          type: "Feature", geometry: f.geometry, properties: { route: number }
        });
        f.geometry.coordinates.forEach(function (c) {
          line.bounds.extend(c);
          allBounds.extend(c);
        });

      } else if (p.kind === "platform") {
        platforms.push({
          id: p.stop_id, name: p.name, platform: p.platform, lines: p.lines || [],
          lat: f.geometry.coordinates[1], lon: f.geometry.coordinates[0]
        });

      } else if (p.kind === "stop") {
        counts.stops++;
        // Stations carry a board too, merged across their platforms. Until a
        // line is picked they are the only thing on the map to click, so a
        // board that answered only for platforms answered for nothing.
        var lon = f.geometry.coordinates[0], lat = f.geometry.coordinates[1];
        stopFeatures.push(point(lon, lat, { index: stations.length }));
        stations.push({
          stop_id: p.stop_id, name: p.name, lat: lat, lon: lon,
          meta: escapeHtml(p.stop_id) + (p.step_free ? " · bezbariérová" : "")
        });
      }
    });

    map.getSource("stops").setData(featureCollection(stopFeatures));
    if (!allBounds.isEmpty()) fitTo(allBounds);

    buildChips();
    restyle();
    describe();

    refreshVehicles();
    setInterval(refreshVehicles, REFRESH_MS);
    // Ticks the age display between fetches, so a stalled feed shows its
    // age climbing rather than freezing on a stale number.
    setInterval(paintStatus, 1000);
  }).catch(function (err) {
    // Indistinguishable from a real failure by status code alone: a cold
    // start 404s here for as long as the build takes. /healthz tells the
    // difference, so hold off blaming anything until it has answered once.
    coverageError = err;
    startBuildPoll();
  });

  // --- build progress ---------------------------------------------------

  function pollBuildStatus() {
    fetch("/healthz").then(function (r) { return r.json(); }).then(function (data) {
      var phase = (data.static && data.static.phase) || null;
      var wasBuilding = buildPhase !== null;
      buildPhase = phase;
      paintStatus();
      // The coverage fetch above failed only because the build was not done
      // yet; once the phase clears, the new file exists but nothing here
      // will look at it again unless asked to.
      if (coverageError && wasBuilding && phase === null) location.reload();
    }).catch(function () {
      // /healthz itself unreachable is a real failure, not "still building".
      if (coverageError) {
        statusEl.textContent = "Trasy se nepodařilo načíst: " + coverageError.message;
      }
    });
  }

  function startBuildPoll() {
    if (buildPollTimer) return;
    pollBuildStatus();
    buildPollTimer = setInterval(pollBuildStatus, 2000);
  }

  function buildChips() {
    // Numeric sort, so 2 comes before 10 and the night lines land at the end.
    Object.keys(lines).sort(function (a, b) { return Number(a) - Number(b); })
      .forEach(function (number) {
        var line = lines[number];
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.dataset.line = number;
        chip.setAttribute("aria-pressed", "false");
        chip.title = line.name || ("Linka " + number);
        chip.innerHTML = escapeHtml(number) +
          '<i class="kind ' + (line.trolleybus ? "trolley" : "bus") + '" style="background:' +
          (line.trolleybus ? TROLLEY : BUS) + '"></i>';
        chip.addEventListener("click", function () { select(number); });
        chipsEl.appendChild(chip);
      });

    document.getElementById("chip-all").addEventListener("click", function () {
      selected = null;
      restyle();
      describe();
      renderPlatforms();
      renderVehicles(lastVehicles);
      paintStatus();
      if (allBounds && !allBounds.isEmpty()) fitTo(allBounds);
    });
  }

  // --- live vehicles --------------------------------------------------------

  function delayHtml(v) {
    if (v.delay_seconds === null || v.delay_seconds === undefined) {
      return '<span class="unknown">zatím neznámé</span>' +
             '<div class="est">vůz ještě nevyjel</div>';
    }
    var d = v.delay_seconds;
    var mins = Math.floor(Math.abs(d) / 60), secs = Math.abs(d) % 60;
    var text = mins ? mins + " min " + secs + " s" : secs + " s";
    var cls = d > 45 ? "late" : (d < -45 ? "early" : "ontime");
    var label = d > 45 ? ("+" + text) : (d < -45 ? ("−" + text) : "jede načas");
    return '<span class="' + cls + '">' + escapeHtml(label) + "</span>";
  }

  function vehiclePopup(v) {
    function row(label, stop) {
      if (!stop) return "";
      return "<dt>" + label + "</dt><dd>" + escapeHtml(stop.name) +
             ' <span class="est">' + escapeHtml(stop.scheduled) + "</span></dd>";
    }
    return '<div class="veh-pop">' +
      '<div class="popup-title">Linka ' + escapeHtml(v.line) + " → " +
      escapeHtml(v.destination) + "</div>" +
      "<dl>" +
      row("předchozí", v.previous_stop) +
      row("následující", v.next_stop) +
      "<dt>zpoždění</dt><dd>" + delayHtml(v) + "</dd>" +
      "<dt>vůz</dt><dd>" + escapeHtml(v.vehicle_id) +
      (v.stop_index !== null ? ' <span class="est">' + (v.stop_index + 1) + "/" +
        v.stops_total + " zast.</span>" : "") + "</dd>" +
      "</dl></div>";
  }

  function renderVehicles(vehicles) {
    var features = [];

    vehicles.forEach(function (v) {
      if (selected !== null && v.line !== selected) return;

      var props = {
        vehicle_id: v.vehicle_id, line: v.line,
        // Always the vehicle's own mode colours. Recolouring the selected line
        // lime cost the bus/trolleybus distinction exactly when the map is
        // focused on that line -- and when a line is selected its vehicles are
        // the only ones drawn, so they need no further emphasis.
        kind: v.trolleybus ? "trolley" : "bus"
      };
      // The arrow points at the next stop. It is computed on the server,
      // because the upstream never populates gps_course.
      if (v.heading !== null && v.heading !== undefined) props.heading = v.heading;
      features.push(point(v.longitude, v.latitude, props));
    });

    map.getSource("vehicles").setData(featureCollection(features));
    // Keeps an open popup on its vehicle and its numbers current.
    syncVehiclePopup();
    return features.length;
  }

  function refreshVehicles() {
    nextRefreshAt = Date.now() + REFRESH_MS;
    // /vehicles.json is the same data as the protobuf feed, but with stop ids
    // already resolved to names and times by the server. Doing that join here
    // would mean shipping the timetable to the browser.
    fetch("/vehicles.json").then(function (r) {
      if (!r.ok) throw new Error("realtime feed není hotový");
      return r.json();
    }).then(function (payload) {
      lastVehicles = payload.vehicles || [];
      feedTime = payload.built_at ? new Date(payload.built_at) : new Date();
      failure = null;

      renderVehicles(lastVehicles);
      describe();
      paintStatus();
    }).catch(function (err) {
      // While the static build runs, the realtime loop has not made its
      // first pass yet either -- that is not a failure, just not ready.
      if (buildPhase) return;
      failure = err.message;
      paintStatus();
    });
  }

  // --- status line ----------------------------------------------------------

  function paintStatus() {
    if (buildPhase) {
      statusEl.innerHTML = '<i class="pulse"></i><span>Načítám data: ' +
        escapeHtml(buildPhase) + "</span>";
      return;
    }

    var shown = lastVehicles.filter(function (v) {
      return selected === null || v.line === selected;
    }).length;

    if (failure) {
      statusEl.innerHTML = '<i class="pulse dead"></i><span class="stale">' +
        escapeHtml("vozidla nedostupná: " + failure) + "</span>";
      return;
    }
    if (!feedTime) return;

    var age = Math.max(0, Math.round((Date.now() - feedTime.getTime()) / 1000));
    // Two refresh intervals without an update means something is wrong
    // upstream; say so rather than showing a quietly ageing number.
    var stale = age > (REFRESH_MS / 1000) * 2;

    var left = Math.max(0, Math.ceil((nextRefreshAt - Date.now()) / 1000));

    statusEl.innerHTML =
      '<i class="pulse' + (stale ? " stale" : "") + '"></i>' +
      "<span>" +
      (selected !== null
        ? shown + " z " + lastVehicles.length + " vozidel na lince " + escapeHtml(selected)
        : shown + " vozidel") +
      "</span>" +
      '<span class="sep">·</span>' +
      '<span class="' + (stale ? "stale" : "fresh") + '">data stará ' + age + " s</span>" +
      '<span class="countdown' + (stale ? " stale" : (left === 0 ? " refreshing" : "")) + '">' +
      (left === 0 ? "obnovuji…" : "další za " + left + " s") + "</span>";
  }
})();
