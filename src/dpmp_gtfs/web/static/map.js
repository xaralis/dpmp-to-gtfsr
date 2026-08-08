// Map page behaviour. Kept out of the template so it is cached separately,
// checked by a syntax-aware tool, and never mixed with Jinja braces.
// Its one server-supplied value arrives on #map as data-refresh-ms.

(function () {
  "use strict";

  var REFRESH_MS = Number(document.getElementById("map").dataset.refreshMs) || 15000;

  // Brand colours straight from the palette -- on a dark basemap they need no
  // adjustment, which is what they were designed for.
  // Vehicle colours live in CSS (.veh.bus / .trolley / .picked); these are
  // only the ones Leaflet needs for polylines.
  var TROLLEY = "#2ad4c5", BUS = "#8b93a3", PICKED = "#c6f432";

  // scrollWheelZoom stays off so plain scrolling moves the page rather than
  // trapping it in the map. Pinch still has to work, though: a trackpad pinch
  // arrives as a wheel event with ctrlKey set, which the disabled handler
  // would otherwise swallow. touchZoom (two fingers on a touchscreen) is
  // Leaflet's default and is untouched.
  // zoomSnap 0 lets the map settle between integer zoom levels. It has to be
  // 0, not a small fraction: a trackpad pinch arrives as many tiny wheel
  // deltas, and any snapping rounds each one back to where it started, so the
  // map never moves at all.
  var map = L.map("map", { scrollWheelZoom: false, zoomSnap: 0, zoomDelta: 0.5 })
    .setView([50.0343, 15.7812], 12);

  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    maxZoom: 19,
    subdomains: "abcd",
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
      '&copy; <a href="https://carto.com/attributions">CARTO</a>'
  }).addTo(map);

  // Exposed for debugging from the console, and so tests can assert on zoom
  // without depending on tile URLs appearing in the DOM.
  window.dpmpMap = map;

  // Pinch-to-zoom, by hand. scrollWheelZoom stays off so plain scrolling moves
  // the page instead of trapping it in the map, but that also disables the
  // ctrl+wheel events a trackpad pinch actually sends, so they are handled
  // here. touchZoom (two fingers on a touchscreen) is Leaflet's own default
  // and is untouched.
  (function () {
    var el = document.getElementById("map");

    el.addEventListener("wheel", function (e) {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      // Trackpads report pinches as a stream of small deltas, so the factor
      // has to be generous or the gesture feels stuck. The clamp keeps a
      // single coarse event (a mouse wheel with ctrl held) from jumping half
      // the country.
      var step = Math.max(-0.6, Math.min(0.6, -e.deltaY * 0.045));
      // animate:false is what makes this feel like a pinch rather than a
      // ratchet. A trackpad sends a stream of deltas, and Leaflet's zoom
      // animation swallows every event that arrives while one is running --
      // roughly nine in ten, which is exactly as sluggish as it sounds.
      map.setZoomAround(map.mouseEventToContainerPoint(e), map.getZoom() + step,
                        { animate: false });
    }, { passive: false });

    // Safari reports pinches as gesture events rather than ctrl+wheel.
    var gestureZoom = 0;
    el.addEventListener("gesturestart", function (e) {
      e.preventDefault();
      gestureZoom = map.getZoom();
    });
    el.addEventListener("gesturechange", function (e) {
      e.preventDefault();
      map.setZoom(gestureZoom + Math.log2(e.scale));
    });
    el.addEventListener("gestureend", function (e) { e.preventDefault(); });
  })();

  var routeLayer = L.layerGroup().addTo(map);
  var stopLayer = L.layerGroup().addTo(map);
  var vehicleLayer = L.layerGroup().addTo(map);
  // Platforms of the selected line only: all 390 at once would bury the map.
  var platformLayer = L.layerGroup().addTo(map);

  var statusEl = document.getElementById("map-status");
  var detailEl = document.getElementById("detail");
  var chipsEl = document.getElementById("chips");

  // route number -> { polylines, bounds, name, trolleybus }
  var lines = {};
  var selected = null;
  var allBounds = null;
  var counts = { routes: 0, stops: 0 };
  var lastVehicles = [];
  var platforms = [];   // every platform, with the lines that call there
  var feedTime = null;
  var failure = null;
  var nextRefreshAt = 0;

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // --- styling --------------------------------------------------------------

  function restyle() {
    Object.keys(lines).forEach(function (number) {
      var line = lines[number];
      var isPicked = selected === number;
      var dimmed = selected !== null && !isPicked;
      line.polylines.forEach(function (p) {
        p.setStyle({
          color: isPicked ? PICKED : (line.trolleybus ? TROLLEY : BUS),
          weight: isPicked ? 5 : 2.5,
          // Dimmed rather than hidden: the selected line reads better against
          // the rest of the network than floating on an empty map.
          opacity: isPicked ? 0.95 : (dimmed ? 0.12 : 0.55)
        });
        if (isPicked) p.bringToFront();
      });
    });

    Array.prototype.forEach.call(chipsEl.querySelectorAll(".chip[data-line]"), function (chip) {
      chip.setAttribute("aria-pressed", String(chip.dataset.line === selected));
    });
    document.getElementById("chip-all").setAttribute("aria-pressed", String(selected === null));
  }

  function renderPlatforms() {
    platformLayer.clearLayers();
    if (selected === null) return;

    platforms.filter(function (p) { return p.lines.indexOf(selected) !== -1; })
      .forEach(function (p) {
        L.circleMarker([p.lat, p.lon], {
          radius: 4, color: "#0a0a0a", weight: 1.5,
          fillColor: "#c6f432", fillOpacity: 1
        }).bindPopup(
          '<div class="popup-title">' + escapeHtml(p.name) + "</div>" +
          '<div class="popup-meta">nástupiště ' + escapeHtml(p.platform) +
          " · " + escapeHtml(p.stop_id || p.id) + "</div>" +
          '<div class="dep-none">Načítám odjezdy…</div>'
        ).on("popupopen", function (e) { loadDepartures(p, e.popup); }).addTo(platformLayer);

        L.marker([p.lat, p.lon], {
          icon: L.divIcon({
            className: "", iconSize: null,
            html: '<div class="stop-label">' + escapeHtml(p.name) + "</div>"
          }),
          interactive: false
        }).addTo(platformLayer);
      });
  }

  function departureRows(payload) {
    var deps = payload.departures || [];
    if (!deps.length) return '<div class="dep-none">Dnes odsud už nic nejede.</div>';

    return '<div class="dep-board">' + deps.map(function (d) {
      // The expected time is the one to read when it differs from the
      // timetable; showing both would just make the row harder to scan.
      var shifted = d.expected !== d.scheduled;
      var mins = Math.round(d.in_seconds / 60);
      return '<span class="dep-time' + (shifted ? " shifted" : "") + '">' +
               escapeHtml(d.expected) + "</span>" +
             '<span class="dep-line">' + escapeHtml(d.line) + "</span>" +
             '<span class="dep-to">' +
               '<span class="dep-name">' + escapeHtml(d.headsign) + "</span>" +
               '<span class="dep-in">· ' + (mins < 1 ? "teď" : mins + " min") + "</span>" +
               (shifted ? '<span class="dep-in">(řád ' + escapeHtml(d.scheduled) + ")</span>" : "") +
             "</span>";
    }).join("") + "</div>";
  }

  function loadDepartures(p, popup) {
    var head = '<div class="popup-title">' + escapeHtml(p.name) + "</div>" +
               '<div class="popup-meta">nástupiště ' + escapeHtml(p.platform) +
               " · " + escapeHtml(p.stop_id || p.id) + "</div>";

    fetch("/departures/" + encodeURIComponent(p.stop_id || p.id) + ".json")
      .then(function (r) { if (!r.ok) throw new Error("nedostupné"); return r.json(); })
      .then(function (payload) {
        popup.setContent('<div class="dep-pop">' + head + departureRows(payload) + "</div>");
      })
      .catch(function () {
        popup.setContent('<div class="dep-pop">' + head +
          '<div class="dep-none">Odjezdy se nepodařilo načíst.</div></div>');
      });
  }

  function select(number) {
    selected = (selected === number) ? null : number;
    restyle();
    describe();
    renderPlatforms();
    renderVehicles(lastVehicles);
    paintStatus();

    var target = selected ? lines[selected].bounds : allBounds;
    if (target && target.isValid()) map.fitBounds(target, { padding: [24, 24] });
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
      " · " + line.polylines.length + " tras · " +
      (running === 1 ? "1 vůz právě jede" : running + " vozů právě jede") + "</span>";
  }

  // --- static coverage ------------------------------------------------------

  fetch("/coverage.geojson").then(function (r) {
    if (!r.ok) throw new Error("feed není hotový (" + r.status + ")");
    return r.json();
  }).then(function (geo) {
    var everything = [];

    geo.features.forEach(function (f) {
      var p = f.properties;

      if (p.kind === "route") {
        counts.routes++;
        var latlngs = f.geometry.coordinates.map(function (c) { return [c[1], c[0]]; });
        var number = String(p.route);

        if (!lines[number]) {
          lines[number] = {
            polylines: [], bounds: L.latLngBounds([]),
            name: p.name, trolleybus: p.trolleybus
          };
        }
        var line = lines[number];
        var poly = L.polyline(latlngs, { weight: 2.5, opacity: 0.55 })
          .on("click", function () { select(number); })
          .addTo(routeLayer);

        line.polylines.push(poly);
        line.bounds.extend(poly.getBounds());
        everything = everything.concat(latlngs);

      } else if (p.kind === "platform") {
        platforms.push({
          id: p.stop_id, name: p.name, platform: p.platform, lines: p.lines || [],
          lat: f.geometry.coordinates[1], lon: f.geometry.coordinates[0]
        });

      } else if (p.kind === "stop") {
        counts.stops++;
        L.circleMarker([f.geometry.coordinates[1], f.geometry.coordinates[0]], {
          radius: 3.5, color: "#0a0a0a", weight: 1.5,
          fillColor: "#f5f5f0", fillOpacity: 0.9
        }).bindPopup(
          '<div class="popup-title">' + escapeHtml(p.name) + "</div>" +
          '<div class="popup-meta">' + escapeHtml(p.stop_id) +
          (p.step_free ? " · bezbariérová" : "") + "</div>"
        ).addTo(stopLayer);
      }
    });

    allBounds = L.latLngBounds(everything);
    if (allBounds.isValid()) map.fitBounds(allBounds, { padding: [20, 20] });

    buildChips();
    restyle();
    describe();

    L.control.layers(null, {
      "Trasy": routeLayer, "Zastávky": stopLayer,
      "Zastávky linky": platformLayer, "Vozidla": vehicleLayer
    }, { collapsed: true }).addTo(map);

    refreshVehicles();
    setInterval(refreshVehicles, REFRESH_MS);
    // Ticks the age display between fetches, so a stalled feed shows its
    // age climbing rather than freezing on a stale number.
    setInterval(paintStatus, 1000);
  }).catch(function (err) {
    statusEl.textContent = "Trasy se nepodařilo načíst: " + err.message;
  });

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
      if (allBounds && allBounds.isValid()) map.fitBounds(allBounds, { padding: [20, 20] });
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
    // A measured value comes from watching the vehicle pass a stop; the
    // fallback only ever proves lateness, never punctuality.
    var note = v.delay_measured ? "" : '<div class="est">odhad, dolní mez</div>';
    return '<span class="' + cls + '">' + escapeHtml(label) + "</span>" + note;
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
    vehicleLayer.clearLayers();
    var shown = 0;

    vehicles.forEach(function (v) {
      if (selected !== null && v.line !== selected) return;
      shown++;

      // Always the vehicle's own mode colours. Recolouring the selected line
      // lime cost the bus/trolleybus distinction exactly when the map is
      // focused on that line -- and when a line is selected its vehicles are
      // the only ones drawn, so they need no further emphasis.
      var kind = v.trolleybus ? "trolley" : "bus";

      L.marker([v.latitude, v.longitude], {
        icon: L.divIcon({
          className: "",
          // The arrow points at the next stop. It is computed on the server,
          // because the upstream never populates gps_course.
          html: '<div class="veh ' + kind + '">' + escapeHtml(v.line) +
                (v.heading === null || v.heading === undefined ? "" :
                  '<i class="veh-arrow" style="transform:rotate(' +
                  Number(v.heading).toFixed(0) + 'deg)"></i>') + "</div>",
          iconSize: null,
          iconAnchor: [10, 10]
        }),
        // Above stops and lines, so a vehicle is never hidden behind them.
        zIndexOffset: 1000
      }).bindPopup(vehiclePopup(v)).addTo(vehicleLayer);
    });

    return shown;
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
      failure = err.message;
      paintStatus();
    });
  }

  // --- status line ----------------------------------------------------------

  function paintStatus() {
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
