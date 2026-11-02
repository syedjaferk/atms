// The controller area. Watch the evidence, see why the detector is or is not trusted, then act.
import { AuthError, call, newKey, stream } from "./api.js";
import { auth } from "./auth.js";
import { age, cap, clock, h, words } from "./dom.js";

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 15000;
const GROUPS = {
  review: new Set(["detected"]),
  active: new Set(["verified", "dispatching", "dispatched", "in_progress", "clearing"]),
  done: new Set(["resolved", "rejected", "closed"]),
};
const UNIT_LABEL = { requested: "asked", acked: "accepted", en_route: "on the way", on_scene: "on scene", done: "finished",
                     declined: "declined", timed_out: "no answer", cancelled: "stood down" };
const UNIT_ACTION = { en_route: "Mark on the way", on_scene: "Mark on scene", done: "Mark done" };

const S = { items: [], tab: "review", picked: null, view: null, clip: 0, whatif: null, busy: false, tabChosen: false, player: null, retried: new Set() };
let stop = null, timer = null, debounce = null, toastTimer = null, gen = 0;

function toast(text) {
  const el = $("toast");
  el.textContent = text; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

function live(on) {
  $("link").classList.toggle("down", !on);
  $("link-text").textContent = on ? "Live" : "Reconnecting";
}

const control = auth(
  { gate: $("gate"), form: $("gate-form"), input: $("token"), error: $("gate-error"), signout: $("signout") },
  { start, stop: () => { if (stop) stop(); stop = null; clearInterval(timer); timer = null; live(false); } });

function fail(e) {
  if (e instanceof AuthError) control.signOut(e.message); else toast(e.message);
}

// ------------------------------------------------------------------ queue

async function loadQueue() {
  try {
    S.items = (await call("/v1/incidents?limit=100")).items;
  } catch (e) { return fail(e); }
  for (const g of Object.keys(GROUPS)) $(`n-${g}`).textContent = String(S.items.filter((i) => GROUPS[g].has(i.state)).length);
  if (!S.tabChosen) S.tab = S.items.some((i) => GROUPS.review.has(i.state)) ? "review" : "active";
  drawQueue();
}

function drawQueue() {
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === S.tab)));
  const rows = S.items.filter((i) => GROUPS[S.tab].has(i.state));
  $("queue-empty").hidden = rows.length > 0;
  $("queue-empty").textContent = { review: "Nothing waits for you.", active: "No incident is being handled.", done: "Nothing finished yet." }[S.tab];
  $("queue").replaceChildren(...rows.map((i) => h("li", {
    tabindex: "0", class: i.id === S.picked ? "picked" : "", onclick: () => pick(i.id),
    onkeydown: (e) => { if (e.key === "Enter") pick(i.id); },
  },
  h("span", { class: `dot-s ${i.severity}` }),
  h("span", { class: "t" }, `${cap(i.severity)} incident${i.road_segment ? ` on ${i.road_segment}` : ""}`),
  h("span", { class: "badge" }, words(i.state)),
  h("span", { class: "s" }, `${age(i.opened_at)}, ${Math.round(i.confidence * 100)} percent sure`))));
}

document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => { S.tab = b.dataset.tab; S.tabChosen = true; drawQueue(); }));

// ------------------------------------------------------------------ one incident

async function pick(id) {
  S.picked = id; S.clip = 0; S.whatif = null; S.view = null; S.player = null;
  history.replaceState(null, "", `#incident=${id}`);
  drawQueue();
  await open(id);
}

async function open(id) {
  const mine = ++gen;
  try {
    const [review, detail, notices] = await Promise.all([
      call(`/v1/incidents/${id}/review`), call(`/v1/incidents/${id}`), call(`/v1/incidents/${id}/notifications`)]);
    if (mine !== gen) return;
    S.view = { review, detail, notices: notices.items };
  } catch (e) { return fail(e); }
  draw();
  const state = S.view.review.incident.state;
  if (!S.whatif && ["verified", "dispatching", "dispatched", "in_progress", "clearing"].includes(state)) loadWhatIf(id, mine);
}

async function loadWhatIf(id, mine) {
  try { S.whatif = await call(`/v1/incidents/${id}/whatif`, { method: "POST" }); }
  catch (e) { S.whatif = { error: e.message }; }
  if (mine === gen) drawStage();
}

async function refreshOpen() {
  if (S.picked && !S.busy) await open(S.picked);
}

function draw() { drawStage(); drawSide(); }

// ------------------------------------------------------------------ the stage, evidence and AI assessment

function explain(a) {
  const pct = Math.round(a.confidence * 100), auto = Math.round(a.auto_verify_confidence * 100), floor = Math.round(a.review_confidence * 100);
  if (a.outcome === "rejected") return `Rejected${a.rejected_by ? ` by ${a.rejected_by}` : ""}. The reason given was ${a.rejected_reason}. The detector had been ${pct} percent sure.`;
  if (a.outcome === "verified_automatically") return `The detector was ${pct} percent sure of a ${a.severity} accident. That is above ${auto} percent, so it verified the incident on its own and dispatch started.`;
  if (a.outcome === "verified_by_operator") return `Verified by ${a.verified_by}. The detector had been ${pct} percent sure.`;
  if (a.confidence >= a.auto_verify_confidence) return `The detector is ${pct} percent sure, but only ${a.auto_verify_min_severity} or worse is verified on its own and this one is ${a.severity}. Watch the clip, then verify or reject.`;
  return `The detector is ${pct} percent sure. Above ${auto} percent it acts on its own and below ${floor} percent it only logs. This one sits between, so it waits for you. Watch the clip, then verify or reject.`;
}

function meter(a) {
  const pct = a.confidence * 100;
  const zone = a.confidence >= a.auto_verify_confidence ? "high" : a.confidence >= a.review_confidence ? "" : "low";
  const tick = (v, label, up) => h("div", { class: `tick${up ? " up" : ""}`, style: `left:${v * 100}%` }, h("span", {}, label));
  return h("div", { class: "meter", role: "img", "aria-label": `Detector confidence ${Math.round(pct)} percent` },
    h("div", { class: `fill ${zone}`, style: `width:${pct}%` }),
    tick(a.review_confidence, `logs below ${Math.round(a.review_confidence * 100)}`, false),
    tick(a.auto_verify_confidence, `acts alone above ${Math.round(a.auto_verify_confidence * 100)}`, true));
}

function player(evidence) {
  const clips = evidence.filter((e) => e.kind === "clip");
  const box = h("div", { class: "player" });
  const meta = h("p", { class: "clipmeta" });
  const tabs = h("div", { class: "clips" });
  if (!clips.length) {
    box.append(h("p", { class: "none" }, "No clip has arrived for this incident yet. The camera uploads it a moment after the detection."));
    return [box];
  }
  const cur = clips[Math.min(S.clip, clips.length - 1)];
  clips.forEach((c, i) => tabs.append(h("button", { "aria-pressed": String(c === cur), onclick: () => { S.clip = i; drawStage(); } },
    `Clip ${i + 1}${c.camera_id ? `, camera ${c.camera_id}` : ""}`)));
  if (!cur.available) {
    box.append(h("p", { class: "none" }, "The clip file is not on this server, so it cannot be shown. Check the media folder the API was started with."));
  } else {
    const video = h("video", { controls: true, preload: "metadata", playsinline: true, src: cur.url });
    video.addEventListener("error", () => {
      const key = `${S.picked}|${cur.url.split("?")[0]}`;
      if (!S.retried.has(key)) {          // the signed link lasts a few minutes, so ask for a new one before giving up
        S.retried.add(key); S.player = null; open(S.picked); return;
      }
      box.replaceChildren(h("p", { class: "none" },
        "The browser cannot play this clip. It may be a placeholder file or a format browsers do not support. ",
        h("a", { href: cur.url, download: "" }, "Download it"), " to open it elsewhere."));
    });
    box.append(video);
  }
  meta.textContent = `${cur.camera_id ? `Camera ${cur.camera_id}, ` : ""}${cur.size_bytes ? `${(cur.size_bytes / 1048576).toFixed(1)} MB, ` : ""}${cur.sha256 ? `checksum ${cur.sha256.slice(0, 12)}` : ""}`;
  return [box, clips.length > 1 ? tabs : null, meta];
}

// A refresh must not restart a video someone is watching, so the player is rebuilt only when what it shows changes.
function keptPlayer(review) {
  const clips = review.evidence.filter((e) => e.kind === "clip");
  const cur = clips[Math.min(S.clip, clips.length - 1)];
  const key = `${S.picked}|${clips.length}|${S.clip}|${cur ? cur.url.split("?")[0] : ""}|${cur ? cur.available : ""}`;
  if (!S.player || S.player.key !== key) S.player = { key, nodes: player(review.evidence).filter(Boolean) };
  return S.player.nodes;
}

function impact() {
  const w = S.whatif;
  if (!w) return null;
  if (w.error) return h("p", { class: "muted" }, `The traffic twin could not be used. ${w.error}`);
  const best = w.scenarios.find((s) => s.name === w.recommendation.option);
  return h("div", {},
    h("p", { class: "say" }, `Lowest delay comes from ${w.recommendation.option}. It saves about ${w.recommendation.saves_vehicle_hours} vehicle hours against doing nothing extra. `
      + `Each extra 10 minutes the road stays closed costs about ${w.cost_of_delay.each_extra_10_min_closed_costs_vehicle_hours} vehicle hours.`),
    h("details", {}, h("summary", {}, "See every option"),
      h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Option"), h("th", {}, "Vehicle hours lost"), h("th", {}, "Worst trip min"), h("th", {}, "Queue km"))),
        h("tbody", {}, w.scenarios.map((s) => h("tr", { class: s === best ? "best" : "" }, h("td", {}, cap(s.name)),
          h("td", {}, s.kpis.vehicle_hours_lost), h("td", {}, s.kpis.worst_end_to_end_minutes), h("td", {}, s.kpis.longest_queue_km))))),
      h("p", { class: "note" }, `${w.assumptions.basis}. ${w.recommendation.note}`)));
}

function drawStage() {
  if (!S.view) return;
  const { review, detail } = S.view, inc = review.incident, a = review.assessment;
  const parts = [
    h("div", { class: "top" },
      h("h2", {}, `${cap(inc.severity)} incident on ${inc.road_segment}`),
      h("span", { class: "sub" }, `${words(inc.state)}, opened ${age(inc.opened_at)}, ${inc.location.lat.toFixed(4)}, ${inc.location.lon.toFixed(4)}`)),
    ...keptPlayer(review),
    h("div", { class: "card2" }, h("h3", {}, "What the detector saw"),
      h("p", { class: "say" }, explain(a)), meter(a),
      h("dl", { class: "facts" },
        h("div", {}, h("dt", {}, "Confidence"), h("dd", {}, `${Math.round(a.confidence * 100)} percent`)),
        h("div", {}, h("dt", {}, "Severity"), h("dd", {}, cap(a.severity))),
        h("div", {}, h("dt", {}, "Lanes closed"), h("dd", {}, a.lanes_closed)),
        h("div", {}, h("dt", {}, "Cameras"), h("dd", {}, a.cameras.join(", ") || "none")),
        h("div", {}, h("dt", {}, "Clips"), h("dd", {}, a.clips)))),
    ["verified", "dispatching", "dispatched", "in_progress", "clearing"].includes(inc.state)
      ? h("div", { class: "card2" }, h("h3", {}, "Traffic impact and suggestion"), impact() || h("p", { class: "muted" }, "Running the traffic twin")) : null];
  $("stage").replaceChildren(...parts.flat().filter(Boolean));
}

// ------------------------------------------------------------------ actions

async function act(label, fn) {
  if (S.busy) return;
  S.busy = true; drawSide();
  try { await fn(); toast(`${label} done`); } catch (e) { if (e instanceof AuthError) return fail(e); toast(e.message); }
  S.busy = false;
  await Promise.all([loadQueue(), open(S.picked)]);
}

function drawSide() {
  if (!S.view) { $("side").replaceChildren(); return; }
  const { review, detail, notices } = S.view, inc = review.incident, can = review.allowed;
  const id = inc.id, post = (verb, body) => call(`/v1/incidents/${id}/${verb}`, { method: "POST", body, key: newKey() });
  const reason = h("input", { id: "reason", placeholder: "Reason for rejecting", "aria-label": "Reason for rejecting", maxlength: "200" });
  const note = h("input", { id: "note", placeholder: "Add a note to the timeline", "aria-label": "Note", maxlength: "500" });

  const parts = [
    h("h3", {}, "Decide"),
    h("div", { class: "big" },
      h("button", { class: "primary", disabled: !can.verify || S.busy, onclick: () => act("Verify", () => post("verify")) }, "Verify"),
      h("button", { class: "danger", disabled: !can.reject || S.busy, onclick: () => act("Reject", () => {
        const text = reason.value.trim();
        if (text.length < 3) throw new Error("Give a reason of at least three characters.");
        return post("reject", { reason: text });
      }) }, "Reject"),
      h("button", { disabled: !can.resolve || S.busy, onclick: () => act("Resolve", () => post("resolve")) }, "Resolve road clear"),
      h("button", { disabled: !can.close || S.busy, onclick: () => act("Close", () => post("close")) }, "Close")),
    can.reject ? reason : null,
    h("p", { class: "hint" }, can.resolve ? "Resolve once the road is confirmed clear." : can.verify ? "Verifying starts dispatch and closes the lanes." : "Buttons turn on when the incident reaches the right stage."),
    h("p", { class: "hint" }, h("a", { href: `index.html#incident=${id}` }, "Show on the map")),

    h("h3", {}, "Units"),
    review.units.length
      ? review.units.map((u) => h("div", { class: "unit" },
          h("div", { class: "l" }, h("span", {}, `${cap(words(u.agency))}${u.unit_ref ? `, ${u.unit_ref}` : ""}`),
            h("span", { class: "muted" }, `${UNIT_LABEL[u.state] || words(u.state)}${u.eta_s ? `, ${Math.round(u.eta_s / 60)} min away` : ""}`)),
          u.can.length ? h("div", { class: "b" }, u.can.map((s) => h("button", { disabled: S.busy,
            onclick: () => act(UNIT_ACTION[s], () => call(`/v1/incidents/${id}/assignments/${u.id}`, { method: "PATCH", body: { status: s }, key: newKey() })) }, UNIT_ACTION[s]))) : null))
      : h("p", { class: "muted" }, "No units asked yet."),

    h("h3", {}, "Notices sent"),
    notices.length
      ? notices.map((n) => h("div", { class: "row2" },
          h("span", {}, `${n.contact}, ${n.channel}`),
          h("span", { class: n.status === "failed" ? "error" : "muted" }, words(n.status)),
          n.status === "failed" ? h("button", { disabled: S.busy, onclick: () => act("Retry", () => call(`/v1/notifications/${n.id}/retry`, { method: "POST" })) }, "Retry") : null))
      : h("p", { class: "muted" }, "None sent."),

    h("h3", {}, "Timeline"),
    h("form", { class: "inline", onsubmit: (e) => {
      e.preventDefault();
      const text = note.value.trim();
      if (text) act("Note", async () => { await call(`/v1/incidents/${id}/notes`, { method: "POST", body: { text } }); });
    } }, note, h("button", { type: "submit", disabled: S.busy }, "Add")),
    h("ul", { class: "rows tl" }, detail.timeline.slice().reverse().map((t) =>
      h("li", {}, h("span", {}, h("time", {}, clock(t.at)), t.summary), t.actor ? h("span", { class: "muted" }, t.actor) : null)))];
  $("side").replaceChildren(...parts.flat().filter(Boolean));
}

// ------------------------------------------------------------------ lifecycle

function onChange(msg) {
  clearTimeout(debounce);
  debounce = setTimeout(() => { loadQueue(); if (msg.incident_id === S.picked) refreshOpen(); }, 400);
}

function start() {
  loadQueue().then(() => {
    const m = location.hash.match(/incident=([0-9a-f-]{36})/);
    if (m) { const it = S.items.find((i) => i.id === m[1]); if (it) { for (const [g, set] of Object.entries(GROUPS)) if (set.has(it.state)) S.tab = g; S.tabChosen = true; } pick(m[1]); }
  });
  timer = setInterval(() => { loadQueue(); refreshOpen(); }, REFRESH_MS);
  stop = stream({ onMessage: onChange, onState: live, onDenied: () => control.signOut("The live stream refused this token.") });
}

control.begin();
