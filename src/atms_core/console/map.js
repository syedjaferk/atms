// A schematic map drawn in SVG. No tiles, so it works offline and shows only what the system knows.
const NS = "http://www.w3.org/2000/svg";
const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON = 111320;
const AGENCY_LETTER = { police: "P", ambulance: "A", highway_patrol: "H", fire: "F", tow: "T" };
const AGENCY_NAME = { police: "Police", ambulance: "Ambulance", highway_patrol: "Highway patrol", fire: "Fire", tow: "Tow" };
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/{a};{b}?overview=full&geometries=geojson";
const MOVING = new Set(["acked", "en_route"]);
const ARRIVED = new Set(["on_scene", "done"]);
const DONE_STATES = new Set(["on_scene", "done"]);
const GONE_STATES = new Set(["declined", "cancelled"]);

function el(tag, attrs = {}, text) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}

export class RoadMap {
  constructor(svg, tip, handlers) {
    this.svg = svg; this.tip = tip; this.on = handlers;
    this.data = null; this.horizon = 0; this.picked = null; this.area = null;     // area id, or "all"
    this.view = { lat: 18.46, lon: 73.85, ppm: 0.05 };      // pixels per metre
    this.fitted = false;
    this.tilesOn = true;
    this.tileLayer = el("g");
    this.vec = el("g");
    this.carLayer = el("g");
    this.svg.append(this.tileLayer, this.vec, this.carLayer);
    this.tiles = new Map();
    this.routes = new Map();         // "facility>incident" -> { pts, cum } along real roads once the router answers
    this.skew = 0;
    setInterval(() => this.drawCars(), 250);
    this._wire();
    new ResizeObserver(() => { if (this.data) { if (!this.userMoved) this.fit(); this.draw(); } }).observe(svg);
  }

  set(data) {
    this.data = data;
    this.skew = Date.parse(data.generated_at) - Date.now();      // so unit positions follow the server clock
    if (!this.fitted) {
      const first = data.incidents[0];
      this.area = (first && this.areaOf(first.lat, first.lon)) || (data.areas && data.areas[0] ? data.areas[0].id : "all");
      this.onArea && this.onArea(this.area);
      this.fit(); this.fitted = true;
    }
    this.draw();
  }

  setTiles(on) { this.tilesOn = on; this.draw(); }

  // Slippy map tiles from OpenStreetMap, placed through the same projection as the road so they always line up.
  drawTiles() {
    const { w, h } = this.size();
    const want = new Map();
    if (this.tilesOn) {
      const cos = Math.cos((this.view.lat * Math.PI) / 180);
      const z = Math.max(3, Math.min(19, Math.round(Math.log2(156543.03 * cos * this.view.ppm))));
      const n = 2 ** z;
      const tx = (lon) => ((lon + 180) / 360) * n;
      const ty = (lat) => { const r = (lat * Math.PI) / 180; return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * n; };
      const lonOf = (x) => (x / n) * 360 - 180;
      const latOf = (y) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n))) * 180) / Math.PI;
      const nw = this.unproject(0, 0), se = this.unproject(w, h);
      const x0 = Math.floor(tx(nw.lon)), x1 = Math.floor(tx(se.lon)), y0 = Math.floor(ty(nw.lat)), y1 = Math.floor(ty(se.lat));
      if ((x1 - x0 + 1) * (y1 - y0 + 1) <= 150) {
        for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) {
          if (x < 0 || y < 0 || x >= n || y >= n) continue;
          const key = `${z}/${x}/${y}`;
          const [px, py] = this.project(latOf(y), lonOf(x)), [qx, qy] = this.project(latOf(y + 1), lonOf(x + 1));
          let img = this.tiles.get(key);
          if (!img) {
            img = el("image", { href: TILE_URL.replace("{z}", z).replace("{x}", x).replace("{y}", y), preserveAspectRatio: "none" });
            img.addEventListener("error", () => img.setAttribute("opacity", "0"));
          }
          img.setAttribute("x", px); img.setAttribute("y", py);
          img.setAttribute("width", qx - px + 0.6); img.setAttribute("height", qy - py + 0.6);
          want.set(key, img);
        }
      }
    }
    for (const [key, img] of this.tiles) if (!want.has(key)) img.remove();
    for (const [key, img] of want) if (!this.tiles.has(key)) this.tileLayer.append(img);
    this.tiles = want;
    this.svg.classList.toggle("streets", this.tilesOn);
  }

  select(cameraId) { this.selected = cameraId; this.draw(); }

  setHorizon(h) { this.horizon = h; this.draw(); }
  setPicked(id) {
    this.picked = id;
    const inc = id && this.data && this.data.incidents.find((i) => i.id === id);
    const a = inc && this.areaOf(inc.lat, inc.lon);
    if (a && a !== this.area) { this.area = a; this.fit(); this.onArea && this.onArea(a); }
    this.draw();
  }

  size() { const r = this.svg.getBoundingClientRect(); return { w: Math.max(r.width, 1), h: Math.max(r.height, 1) }; }

  project(lat, lon) {
    const { w, h } = this.size(), v = this.view;
    const cos = Math.cos((v.lat * Math.PI) / 180);
    return [w / 2 + (lon - v.lon) * M_PER_DEG_LON * cos * v.ppm, h / 2 - (lat - v.lat) * M_PER_DEG_LAT * v.ppm];
  }

  // Which area a point belongs to, the nearest one within about 40 km, or null.
  areaOf(lat, lon) {
    let best = null, dist = 0.4;                          // degrees, roughly 40 km
    for (const a of this.data.areas || []) {
      const d = Math.hypot(a.lat - lat, (a.lon - lon) * Math.cos((lat * Math.PI) / 180));
      if (d < dist) { dist = d; best = a.id; }
    }
    return best;
  }

  setArea(id) { this.area = id; this.fit(); this.draw(); if (this.onArea) this.onArea(id); }

  // Frame the chosen area, or every camera when no area is chosen.
  fit() {
    if (!this.data) return;
    const d = this.data, all = !this.area || this.area === "all";
    const mine = (lat, lon) => all || this.areaOf(lat, lon) === this.area;
    const pts = [];
    for (const c of d.cameras || []) if (mine(c.lat, c.lon)) pts.push([c.lat, c.lon]);
    for (const i of d.incidents) if (mine(i.lat, i.lon)) pts.push([i.lat, i.lon]);
    if (!all) {
      for (const f of d.facilities) if (mine(f.lat, f.lon)) pts.push([f.lat, f.lon]);
      for (const s of d.segments) if (mine(s.from.lat, s.from.lon)) pts.push([s.from.lat, s.from.lon], [s.to.lat, s.to.lon]);
    }
    if (!pts.length) return;
    const lats = pts.map((p) => p[0]), lons = pts.map((p) => p[1]);
    const lat = (Math.min(...lats) + Math.max(...lats)) / 2, lon = (Math.min(...lons) + Math.max(...lons)) / 2;
    const cos = Math.cos((lat * Math.PI) / 180);
    const spanY = Math.max((Math.max(...lats) - Math.min(...lats)) * M_PER_DEG_LAT, 500);
    const spanX = Math.max((Math.max(...lons) - Math.min(...lons)) * M_PER_DEG_LON * cos, 500);
    const { w, h } = this.size();
    this.view = { lat, lon, ppm: Math.min((w * 0.7) / spanX, (h * 0.6) / spanY) };
    this.userMoved = false;
  }

  zoom(factor, at) {
    const { w, h } = this.size();
    const [px, py] = at || [w / 2, h / 2];
    const before = this.unproject(px, py);
    this.view.ppm = Math.min(Math.max(this.view.ppm * factor, 0.004), 2);
    const after = this.unproject(px, py);
    this.view.lat += before.lat - after.lat; this.view.lon += before.lon - after.lon;
    this.userMoved = true;
    this.draw();
  }

  unproject(px, py) {
    const { w, h } = this.size(), v = this.view;
    const cos = Math.cos((v.lat * Math.PI) / 180);
    return { lat: v.lat - (py - h / 2) / (M_PER_DEG_LAT * v.ppm), lon: v.lon + (px - w / 2) / (M_PER_DEG_LON * cos * v.ppm) };
  }

  _wire() {
    let drag = null;
    this.svg.addEventListener("pointerdown", (e) => {
      if (e.target.closest("[data-hit]")) return;
      drag = { x: e.clientX, y: e.clientY, lat: this.view.lat, lon: this.view.lon };
      this.svg.setPointerCapture(e.pointerId); this.svg.classList.add("drag");
    });
    this.svg.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const cos = Math.cos((this.view.lat * Math.PI) / 180);
      this.view.lon = drag.lon - (e.clientX - drag.x) / (M_PER_DEG_LON * cos * this.view.ppm);
      this.view.lat = drag.lat + (e.clientY - drag.y) / (M_PER_DEG_LAT * this.view.ppm);
      this.userMoved = true; this.draw();
    });
    const end = () => { drag = null; this.svg.classList.remove("drag"); };
    this.svg.addEventListener("pointerup", end);
    this.svg.addEventListener("pointercancel", end);
    this.svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = this.svg.getBoundingClientRect();
      this.zoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, [e.clientX - r.left, e.clientY - r.top]);
    }, { passive: false });
  }

  levelFor(seg) {
    if (seg.status !== "ok") return null;
    if (this.horizon === 0) return seg.now_level;
    const f = seg.forecasts.find((r) => r.horizon_min === this.horizon);
    return f ? f.level : null;
  }

  speedFor(seg) {
    if (seg.status !== "ok") return null;
    if (this.horizon === 0) return seg.speed_kmh;
    const f = seg.forecasts.find((r) => r.horizon_min === this.horizon);
    return f ? f.speed_kmh : null;
  }

  // The path a unit drives. Starts as a straight line and is replaced by the real road route when the router answers.
  routeFor(f, i) {
    const key = `${f.id}>${i.id}`;
    let r = this.routes.get(key);
    if (!r) {
      r = this.measure([[f.lat, f.lon], [i.lat, i.lon]]);
      this.routes.set(key, r);
      const a = `${f.lon},${f.lat}`, b = `${i.lon},${i.lat}`;
      fetch(ROUTE_URL.replace("{a}", a).replace("{b}", b), { signal: AbortSignal.timeout(6000) })
        .then((res) => (res.ok ? res.json() : Promise.reject()))
        .then((j) => {
          const pts = j.routes[0].geometry.coordinates.map(([lon, lat]) => [lat, lon]);
          if (pts.length > 1) { this.routes.set(key, { ...this.measure(pts), real: true }); this.draw(); }
        })
        .catch(() => { /* keep the straight line */ });
    }
    return r;
  }

  measure(pts) {
    const cum = [0];
    for (let k = 1; k < pts.length; k++) {
      const dy = (pts[k][0] - pts[k - 1][0]) * M_PER_DEG_LAT;
      const dx = (pts[k][1] - pts[k - 1][1]) * M_PER_DEG_LON * Math.cos((pts[k][0] * Math.PI) / 180);
      cum.push(cum[k - 1] + Math.hypot(dx, dy));
    }
    return { pts, cum, real: false };
  }

  along(route, t) {
    const end = route.cum[route.cum.length - 1] * t;
    let k = 1;
    while (k < route.cum.length - 1 && route.cum[k] < end) k++;
    const span = route.cum[k] - route.cum[k - 1] || 1, u = Math.min(1, Math.max(0, (end - route.cum[k - 1]) / span));
    const a = route.pts[k - 1], b = route.pts[k];
    return [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];
  }

  // Share of the trip done, from when the unit accepted and the ETA it gave.
  progress(a) {
    if (ARRIVED.has(a.state)) return 1;
    if (!a.left_at || !a.eta_s) return 0;
    const now = Date.now() + this.skew;
    return Math.min(0.97, Math.max(0, (now - Date.parse(a.left_at)) / (a.eta_s * 1000)));
  }

  drawCars() {
    const d = this.data;
    if (!d) return;
    const fac = new Map(d.facilities.map((f) => [f.id, f]));
    const out = [];
    for (const i of d.incidents) {
      for (const a of i.assignments) {
        const f = a.facility && fac.get(a.facility);
        if (!f || !(MOVING.has(a.state) || ARRIVED.has(a.state))) continue;
        const t = this.progress(a), [lat, lon] = this.along(this.routeFor(f, i), t);
        const [x, y] = this.project(lat, lon);
        const g = el("g", { class: `car ${a.agency}${ARRIVED.has(a.state) ? " there" : ""}`, transform: `translate(${x} ${y})`, "data-hit": "1" });
        g.append(el("circle", { r: 10 }), el("text", { x: 0, y: 0 }, AGENCY_LETTER[a.agency] || "?"));
        const left = a.eta_s ? Math.max(0, Math.round((a.eta_s * (1 - t)) / 60)) : null;
        this.hover(g, `${AGENCY_NAME[a.agency] || a.agency}${a.unit_ref ? ` ${a.unit_ref}` : ""}`,
          [`From ${f.name}`, ARRIVED.has(a.state) ? "On scene" : `${Math.round(t * 100)} percent of the way${left === null ? "" : `, about ${left} min to go`}`]);
        out.push(g);
      }
    }
    this.carLayer.replaceChildren(...out);
  }

  showTip(e, title, lines) {
    this.tip.replaceChildren();
    const b = document.createElement("b"); b.textContent = title; this.tip.append(b);
    for (const l of lines) { const d = document.createElement("div"); d.textContent = l; this.tip.append(d); }
    const r = this.svg.getBoundingClientRect();
    this.tip.hidden = false;
    this.tip.style.left = `${Math.min(e.clientX - r.left + 14, r.width - 270)}px`;
    this.tip.style.top = `${Math.min(e.clientY - r.top + 14, r.height - 120)}px`;
  }

  hideTip() { this.tip.hidden = true; }

  hover(node, title, lines) {
    node.addEventListener("pointerenter", (e) => this.showTip(e, title, lines));
    node.addEventListener("pointermove", (e) => this.showTip(e, title, lines));
    node.addEventListener("pointerleave", () => this.hideTip());
  }

  draw() {
    if (!this.data) return;
    const d = this.data, svg = this.svg, { w, h } = this.size();
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    this.drawTiles();
    this.vec.replaceChildren();
    const roads = el("g"), marks = el("g"), links = el("g"), facs = el("g"), pins = el("g");
    this.vec.append(roads, links, facs, marks, pins);

    // roads without sensors, drawn as a plain line along their cameras
    for (const a of d.areas || []) {
      if (!a.corridor.length) continue;
      const pts = a.corridor.map(([la, lo]) => this.project(la, lo));
      roads.append(el("polyline", { class: "corridor", points: pts.map((p) => p.join(",")).join(" ") }));
    }

    // the road
    const pickedSeg = this.picked ? (d.incidents.find((i) => i.id === this.picked) || {}).segment : null;
    d.segments.forEach((s, idx) => {
      const [x1, y1] = this.project(s.from.lat, s.from.lon), [x2, y2] = this.project(s.to.lat, s.to.lon);
      const lvl = this.levelFor(s) || "none";
      roads.append(el("line", { class: "seg-casing", x1, y1, x2, y2 }));
      const line = el("line", { class: `seg ${lvl}${pickedSeg === s.id ? " picked" : ""}`, x1, y1, x2, y2, "data-hit": "1" });
      const speed = this.speedFor(s);
      const when = this.horizon === 0 ? "now" : `in ${this.horizon} minutes`;
      const lines = s.status === "ok"
        ? [`${speed === null ? "No forecast" : `${Math.round(speed)} km/h ${when}`}, free flow ${Math.round(s.free_flow_kmh)} km/h`,
           ...s.forecasts.map((f) => `In ${f.horizon_min} min ${Math.round(f.speed_kmh)} km/h (${Math.round(f.low_kmh)} to ${Math.round(f.high_kmh)}), ${f.level}`)]
        : [s.status === "no recent data" ? "Readings are too old to say" : "No forecast model is loaded"];
      this.hover(line, s.id, lines);
      roads.append(line);
      // direction arrow and label
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2, ang = (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI;
      marks.append(el("path", { class: "arrow", d: "M -5 -4 L 5 0 L -5 4 Z", transform: `translate(${mx} ${my}) rotate(${ang})` }));
      const label = speed === null ? s.id.replace("NH48-", "") : `${s.id.replace("NH48-", "")}  ${Math.round(speed)} km/h`;
      if (this.view.ppm > 0.03) marks.append(el("text", { class: "seg-label", x: mx + 16, y: my + 4 }, label));
      void idx;
    });

    // message boards
    for (const v of d.vms) {
      const [x, y] = this.project(v.lat, v.lon);
      const g = el("g", { class: `vms${v.text ? " on" : ""}`, transform: `translate(${x - 30} ${y})`, "data-hit": "1" });
      g.append(el("rect", { x: -13, y: -8, width: 26, height: 16, rx: 3 }), el("text", { x: 0, y: 0 }, "VMS"));
      this.hover(g, v.id, [v.text || "Blank"]);
      marks.append(g);
    }

    // cameras, with a small cone for where each one looks
    const seen = new Set(d.incidents.flatMap((i) => i.cameras || []));
    for (const c of d.cameras || []) {
      const [x, y] = this.project(c.lat, c.lon);
      if (x < -40 || y < -40 || x > w + 40 || y > h + 40) continue;
      const g = el("g", { class: `cam${seen.has(c.id) ? " saw" : ""}${this.selected === c.id ? " sel" : ""}`, transform: `translate(${x} ${y})`, "data-hit": "1" });
      if (this.on.camera) { g.style.cursor = "pointer"; g.addEventListener("click", () => this.on.camera(c.id)); }
      g.append(el("path", { class: "cone", d: "M0 0 L-9 -30 L9 -30 Z", transform: `rotate(${c.heading_deg})` }),
               el("circle", { r: 7 }), el("text", { x: 0, y: 0 }, "C"));
      if (showNames0(this.view.ppm)) g.append(el("text", { class: "fac-name", x: 11, y: 4, style: "text-anchor:start" }, c.id));
      this.hover(g, `${c.name || "Camera"} (${c.id})`, [`${c.road}, facing ${Math.round(c.heading_deg)} degrees`,
        ...(c.good !== undefined ? [`${c.good} good and ${c.bad} bad videos`] : []), seen.has(c.id) ? "Reported an accident" : "Watching"]);
      marks.append(g);
    }

    // facilities and the routes of assigned units
    const fac = new Map(d.facilities.map((f) => [f.id, f]));
    const showNames = this.view.ppm > 0.03;
    const alerted = new Set();       // facilities of an agency that has been sent a notice for their incident
    for (const i of d.incidents) {
      const told = new Set((i.notices || []).map((n) => n.agency).filter(Boolean));
      for (const f of d.facilities) if (told.has(f.agency) && f.busy_with.includes(i.id)) alerted.add(f.id);
    }
    for (const i of d.incidents) {
      for (const a of i.assignments) {
        const f = a.facility && fac.get(a.facility);
        if (!f || GONE_STATES.has(a.state)) continue;
        const r = this.routeFor(f, i);
        const d2 = r.pts.map((p, k) => { const [px, py] = this.project(p[0], p[1]); return `${k ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`; }).join(" ");
        links.append(el("path", { class: `route${DONE_STATES.has(a.state) ? " done" : ""}${r.real ? " real" : ""}`, d: d2 }));
      }
    }
    for (const f of d.facilities) {
      const [x, y] = this.project(f.lat, f.lon);
      if (x < -40 || y < -40 || x > w + 40 || y > h + 40) continue;
      const g = el("g", { class: `fac${f.busy_with.length ? " busy" : ""}${alerted.has(f.id) ? " alerted" : ""}`, transform: `translate(${x} ${y})`, "data-hit": "1" });
      g.append(el("rect", { x: -9, y: -9, width: 18, height: 18, rx: 4 }), el("text", { x: 0, y: 0 }, AGENCY_LETTER[f.agency] || "?"));
      if (showNames || f.busy_with.length) g.append(el("text", { class: "fac-name", x: 14, y: 4, "text-anchor": "start", style: "text-anchor:start" }, f.name));
      this.hover(g, f.name, [AGENCY_NAME[f.agency] || f.agency, f.busy_with.length ? (alerted.has(f.id) ? "Assigned and notified" : "Assigned to an open incident") : "Available"]);
      if (alerted.has(f.id)) g.prepend(el("circle", { class: "ring", r: 12 }));
      facs.append(g);
    }

    // incidents
    for (const i of d.incidents) {
      const [x, y] = this.project(i.lat, i.lon);
      const g = el("g", { class: `pin ${i.state === "detected" ? "detected" : "live"}${this.picked === i.id ? " picked" : ""}`,
                          transform: `translate(${x} ${y})`, "data-hit": "1", tabindex: "0", role: "button",
                          "aria-label": `${i.severity} incident, ${i.state.replace("_", " ")}` });
      g.append(el("circle", { class: "halo", r: 12 }), el("circle", { class: "dot", r: 11 }), el("text", { x: 0, y: 0 }, "!"));
      g.addEventListener("click", () => this.on.pick(i.id));
      g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.on.pick(i.id); } });
      this.hover(g, `${cap(i.severity)} incident`, [`${i.state.replace("_", " ")}, ${i.lanes_closed} lane${i.lanes_closed === 1 ? "" : "s"} closed`]);
      pins.append(g);
    }
    this.drawCars();
  }
}

function showNames0(ppm) { return ppm > 0.09; }

function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
