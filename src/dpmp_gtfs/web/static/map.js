// Map page behaviour. Kept out of the template so it is cached separately,
// checked by a syntax-aware tool, and never mixed with Jinja braces.
// Its one server-supplied value arrives on #map as data-refresh-ms.

(function () {
  "use strict";

  var mapEl = document.getElementById("map");
  var REFRESH_MS = Number(mapEl.dataset.refreshMs) || 15000;

  // Brand colours straight from the palette -- on a dark basemap they need no
  // adjustment, which is what they were designed for.
  // Vehicle colours live in CSS (.veh.bus / .trolley / .picked); these are
  // only the ones Leaflet needs for polylines.
  var TROLLEY = "#2ad4c5", BUS = "#8b93a3", PICKED = "#c6f432";

  // Leaflet's own defaults, deliberately. There used to be thirty lines below
  // reimplementing pinch-to-zoom, because scrollWheelZoom was off to stop the
  // map trapping the page scroll -- and turning it off kills trackpad pinch
  // too, which browsers deliver as a wheel event with ctrlKey set. Sizing the
  // page to the window removed the reason: with nothing to scroll past, the
  // wheel has no other job, so Leaflet handles wheel, trackpad pinch and
  // two-finger touch on its own. online.dpmp.cz gets it right the same way,
  // on the same Leaflet version.
  //
  // No zoom options at all, deliberately. zoomSnap 0 and a raised
  // wheelPxPerZoomLevel were both tried and both made the wheel feel sluggish:
  // with snapping off, Leaflet's wheel maths spreads one gesture across
  // fractional levels, so a flick that should cross a level barely moves.
  // Stock Leaflet steps one level per gesture, which is what every other map
  // does and what people are expecting.
  var map = L.map("map").setView([50.0343, 15.7812], 12);

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


  var routeLayer = L.layerGroup().addTo(map);
  var stopLayer = L.layerGroup().addTo(map);
  var vehicleLayer = L.layerGroup().addTo(map);
  // Platforms of the selected line only: all 390 at once would bury the map.
  var platformLayer = L.layerGroup().addTo(map);

  // Below this the whole network is in view, vehicles are a few pixels apart
  // and an arrow on each is noise rather than information.
  var HEADING_MIN_ZOOM = 14;

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
  // Set from /healthz while the static feed is (re)building. A cold start
  // fetches ~2,700 trips over several minutes -- during that window
  // /coverage.geojson and /vehicles.json both 404, and that is not a
  // failure, just data that does not exist yet.
  var buildPhase = null;
  var coverageError = null;
  var buildPollTimer = null;
  // Which vehicle's popup is open, if any. Markers are rebuilt from scratch on
  // every refresh and on every selection, so without remembering this the
  // popup would close by itself every fifteen seconds -- and clicking a
  // vehicle, which now also picks its line, would close the popup it opened.
  var openVehicle = null;
  // Leaflet fires popupclose when a marker is removed, not only when the user
  // dismisses it. Without this flag the teardown at the start of a rebuild
  // would clear openVehicle before the rebuild could act on it.
  var rebuilding = false;

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // --- styling --------------------------------------------------------------

  map.on("zoomend", syncHeadings);
  syncHeadings();

  function syncHeadings() {
    mapEl.classList.toggle("show-headings", map.getZoom() >= HEADING_MIN_ZOOM);
  }

  function restyle() {
    Object.keys(lines).forEach(function (number) {
      var line = lines[number];
      var isPicked = selected === number;
      line.polylines.forEach(function (p) {
        // On the map only while its line is picked. Drawing every route at
        // once buries the vehicles and stops under 425 overlapping shapes.
        if (isPicked) {
          p.setStyle({ color: PICKED, weight: 5, opacity: 0.95 });
          if (!routeLayer.hasLayer(p)) routeLayer.addLayer(p);
          p.bringToFront();
        } else if (routeLayer.hasLayer(p)) {
          routeLayer.removeLayer(p);
        }
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

  function loadDepartures(p, popup) {
    var id = p.stop_id || p.id;
    // A station board merges platforms, so it has to say which one each
    // departure leaves from; a platform board obviously does not.
    var isStation = id.indexOf("P") === -1;
    var meta = p.meta ? p.meta
             : "nástupiště " + escapeHtml(p.platform) + " · " + escapeHtml(id);
    var head = '<div class="popup-title">' + escapeHtml(p.name) + "</div>" +
               '<div class="popup-meta">' + meta + "</div>";

    fetch("/departures/" + encodeURIComponent(id) + ".json")
      .then(function (r) { if (!r.ok) throw new Error("nedostupné"); return r.json(); })
      .then(function (payload) {
        popup.setContent(
          '<div class="dep-pop">' + head + departureRows(payload, isStation) + "</div>");
      })
      .catch(function () {
        popup.setContent('<div class="dep-pop">' + head +
          '<div class="dep-none">Odjezdy se nepodařilo načíst.</div></div>');
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
        // Built now, attached only while its line is selected. All 425 shapes
        // at once turn the city into a thicket that hides the vehicles and
        // stops, which are the parts that are actually live.
        var poly = L.polyline(latlngs, { weight: 2.5, opacity: 0.55 })
          .on("click", function () { select(number); });

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
        // Stations carry a board too, merged across their platforms. Until a
        // line is picked they are the only thing on the map to click, so a
        // board that answered only for platforms answered for nothing.
        var station = {
          stop_id: p.stop_id, name: p.name,
          meta: escapeHtml(p.stop_id) + (p.step_free ? " · bezbariérová" : "")
        };
        L.circleMarker([f.geometry.coordinates[1], f.geometry.coordinates[0]], {
          radius: 3.5, color: "#0a0a0a", weight: 1.5,
          fillColor: "#f5f5f0", fillOpacity: 0.9
        }).bindPopup(
          '<div class="popup-title">' + escapeHtml(p.name) + "</div>" +
          '<div class="popup-meta">' + station.meta + "</div>" +
          '<div class="dep-none">Načítám odjezdy…</div>'
        ).on("popupopen", function (e) { loadDepartures(station, e.popup); })
         .addTo(stopLayer);
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
    // Indistinguishable from a real failure by status code alone: a cold
    // start 404s here for as long as the crawl takes. /healthz tells the
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
    rebuilding = true;
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

      var marker = L.marker([v.latitude, v.longitude], {
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
      });

      marker
        .bindPopup(vehiclePopup(v))
        // Clicking a vehicle picks its line too: the question that follows
        // "which bus is that" is almost always "and where does it go".
        .on("click", function () {
          openVehicle = v.vehicle_id;
          if (selected !== v.line) select(v.line, true);
        })
        .on("popupclose", function () {
          if (!rebuilding && openVehicle === v.vehicle_id) openVehicle = null;
        })
        .addTo(vehicleLayer);

      // Restore the popup this vehicle had open before the rebuild.
      if (openVehicle === v.vehicle_id) marker.openPopup();
    });

    rebuilding = false;
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
