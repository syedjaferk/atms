// Wires the console together: sign in, load the map, follow the live stream, keep it fresh.
import { AuthError, call, stream } from "./api.js";
import { auth } from "./auth.js";
import { RoadMap } from "./map.js";
import { Panel } from "./panel.js";

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 15000;

const gate = $("gate"), gateError = $("gate-error"), toastEl = $("toast");
let stop = null, timer = null, debounce = null, toastTimer = null;

function toast(text) {
  toastEl.textContent = text; toastEl.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3500);
}

const map = new RoadMap($("map"), $("tip"), { pick: (id) => pick(id === panel.picked ? null : id) });
const panel = new Panel(
  { list: $("list"), empty: $("empty"), count: $("count"), detail: $("detail") },
  { pick: (id) => pick(id === panel.picked ? null : id), refresh: () => refresh(), toast });

function pick(id) {
  panel.setPicked(id); map.setPicked(id);
  if (id) panel.show(id);
}

async function refresh() {
  try {
    const data = await call("/v1/map");
    map.set(data);
    panel.setIncidents(data.incidents);
    feed(data.incidents);
    $("model").textContent = data.forecast_model ? `Forecast model ${data.forecast_model}` : "No forecast model loaded";
    if (panel.picked && !data.incidents.some((i) => i.id === panel.picked)) panel.show(panel.picked);
  } catch (e) {
    if (e instanceof AuthError) return signOut(e.message);
    toast(e.message);
  }
}

const STATUS_WORDS = { pending: "queued", sent: "sent", delivered: "delivered", failed: "failed" };
function feed(incidents) {
  const rows = incidents.flatMap((i) => (i.notices || []).map((n) => ({ ...n, road: i.segment }))).sort((a, b) => b.at.localeCompare(a.at)).slice(0, 10);
  $("feed").hidden = rows.length === 0;
  $("feed-list").replaceChildren(...rows.map((n) => {
    const li = document.createElement("li");
    const who = document.createElement("span"); who.textContent = `${n.channel === "sms" ? "SMS" : "Webhook"} to ${n.contact}`;
    const st = document.createElement("span"); st.textContent = STATUS_WORDS[n.status] || n.status;
    st.className = n.status === "failed" ? "bad" : (n.status === "delivered" ? "ok" : "");
    li.append(who, st);
    return li;
  }));
}

function live(on) {
  const link = $("link");
  link.classList.toggle("down", !on);
  $("link-text").textContent = on ? "Live" : "Reconnecting";
}

function onChange(msg) {
  clearTimeout(debounce);
  debounce = setTimeout(() => { refresh(); if (msg.incident_id === panel.picked) panel.show(panel.picked); }, 400);
}

function start() {
  refresh().then(() => { const m = location.hash.match(/incident=([0-9a-f-]{36})/); if (m) pick(m[1]); });
  timer = setInterval(refresh, REFRESH_MS);
  stop = stream({ onMessage: onChange, onState: live, onDenied: () => signOut("The live stream refused this token.") });
}

const gateControl = auth(
  { gate, form: $("gate-form"), input: $("token"), error: gateError, signout: $("signout") },
  { start, stop: () => { if (stop) stop(); stop = null; clearInterval(timer); timer = null; live(false); } });
const signOut = gateControl.signOut;

$("zoom-in").addEventListener("click", () => map.zoom(1.4));
$("zoom-out").addEventListener("click", () => map.zoom(1 / 1.4));
$("fit").addEventListener("click", () => { map.fit(); map.draw(); });
const streets = $("streets");
function setStreets(on) { streets.setAttribute("aria-pressed", String(on)); map.setTiles(on); try { localStorage.setItem("atms.streets", on ? "1" : "0"); } catch { /* not remembered */ } }
streets.addEventListener("click", () => setStreets(streets.getAttribute("aria-pressed") !== "true"));
try { if (localStorage.getItem("atms.streets") === "0") setStreets(false); } catch { /* default on */ }
document.querySelectorAll(".when button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".when button").forEach((x) => x.classList.toggle("on", x === b));
  map.setHorizon(Number(b.dataset.h));
}));

gateControl.begin();
