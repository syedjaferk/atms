// The CCTV page. Pick a camera on the map, watch one of its videos, run the AI on it and follow what the platform does next.
import { AuthError, call, stream } from "./api.js";
import { auth } from "./auth.js";
import { cap, clock, h, words } from "./dom.js";
import { RoadMap } from "./map.js";

const $ = (id) => document.getElementById(id);
const THRESHOLD = 0.5;                       // the detector counts a frame as showing an accident above this score
const S = { camera: null, videos: [], video: null, job: null, started: 0, flow: null, overview: null };
let stop = null, timer = null, poll = null, flowTimer = null, toastTimer = null, debounce = null;

function toast(text) {
  const el = $("toast");
  el.textContent = text; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}
function live(on) { $("link").classList.toggle("down", !on); $("link-text").textContent = on ? "Live" : "Reconnecting"; }

const map = new RoadMap($("map"), $("tip"), { pick: () => {}, camera: (id) => chooseCamera(id) });
const control = auth(
  { gate: $("gate"), form: $("gate-form"), input: $("token"), error: $("gate-error"), signout: $("signout") },
  { start, stop: () => { if (stop) stop(); stop = null; clearInterval(timer); clearInterval(poll); clearInterval(flowTimer); live(false); } });
const fail = (e) => { if (e instanceof AuthError) control.signOut(e.message); else toast(e.message); };

let areasBuilt = false;
function areaButtons(data) {
  if (areasBuilt) return;
  areasBuilt = true;
  const bar = $("areas");
  const mk = (id, name) => { const b = h("button", { class: "ghost", "data-area": id, onclick: () => map.setArea(id) }, name); return b; };
  bar.replaceChildren(mk("all", "All areas"), ...data.areas.map((a) => mk(a.id, a.name.replace(/ \(.*/, ""))));
  map.onArea = (id) => bar.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.area === id)));
}

async function refreshMap() {
  const [data, overview] = await Promise.all([call("/v1/map"), call("/v1/cctv")]);
  S.overview = overview;
  const counts = new Map(overview.areas.flatMap((a) => a.cameras.map((c) => [c.id, c])));
  for (const c of data.cameras) { const k = counts.get(c.id); if (k) { c.good = k.good; c.bad = k.bad; } }
  areaButtons(data);
  map.set(data);
  const d = overview.detector;
  $("detector").textContent = d.up ? (d.model_loaded ? "AI models ready" : "AI models loading") : "Detector node offline";
  return data;
}

function start() {
  refreshMap().then(() => { if (!S.camera) draw(); }).catch(fail);
  timer = setInterval(() => refreshMap().catch(() => {}), 15000);
  stop = stream({ onMessage: () => { clearTimeout(debounce); debounce = setTimeout(() => refreshMap().catch(() => {}), 500); }, onState: live,
                  onDenied: () => control.signOut("The live stream refused this token.") });
}

async function chooseCamera(id) {
  S.camera = id; S.video = null; S.job = null; S.flow = null;
  clearInterval(poll); clearInterval(flowTimer);
  map.select(id);
  const c = map.data.cameras.find((x) => x.id === id);
  if (c && map.areaOf(c.lat, c.lon) !== map.area) map.setArea(map.areaOf(c.lat, c.lon));
  try { S.videos = (await call(`/v1/cctv/cameras/${id}/videos`)).videos; } catch (e) { return fail(e); }
  draw();
}

// ------------------------------------------------------------------ side panel

function draw() {
  const side = $("side");
  if (!S.camera) {
    side.replaceChildren(h("h2", {}, "Choose a camera"), h("p", { class: "muted" }, "Every camera has its own set of recorded videos. Some show nothing unusual, some show an accident. Pick a camera on the map, then a video, then let the AI check it."),
      h("h3", {}, "Cameras by area"), ...(S.overview ? S.overview.areas.map((a) => h("p", {}, h("b", {}, a.name), ` , ${a.cameras.length} cameras, ${a.road}`)) : []));
    return;
  }
  const cam = map.data.cameras.find((x) => x.id === S.camera);
  const group = (scenario, title) => {
    const list = S.videos.filter((v) => v.scenario === scenario);
    return [h("h3", {}, `${title} (${list.length})`),
      h("ul", { class: "vlist" }, list.map((v) => h("li", { tabindex: "0", "aria-selected": String(S.video && S.video.id === v.id), onclick: () => chooseVideo(v.id),
          onkeydown: (e) => { if (e.key === "Enter") chooseVideo(v.id); } },
        h("span", { class: "t" }, v.title),
        h("span", { class: `tag ${scenario}` }, scenario === "good" ? "good" : "bad"),
        h("span", { class: "m" }, `${v.id}, ${Math.round(v.duration_s)} s, ${v.size_mb} MB, ${v.label_source === "ground truth" ? "labelled" : "sorted by the AI"}`))))];
  };
  side.replaceChildren(
    h("h2", {}, cam ? `${cam.name} (${cam.id})` : S.camera),
    h("p", { class: "muted" }, cam ? `${cam.road}, facing ${Math.round(cam.heading_deg)} degrees` : ""),
    ...group("good", "Good scenarios, nothing happens"), ...group("bad", "Bad scenarios, an accident"),
    S.video ? videoPanel() : h("p", { class: "muted" }, "Pick a video to watch it and check it."));
}

function chooseVideo(id) {
  S.video = S.videos.find((v) => v.id === id); S.job = null; S.flow = null;
  clearInterval(poll); clearInterval(flowTimer);
  draw();
}

function videoPanel() {
  const v = S.video;
  const busy = S.job && ["queued", "running"].includes(S.job.state);
  return h("div", { id: "vp" },
    h("h3", {}, `Video ${v.id}`),
    v.play_url ? h("div", { class: "player2" }, h("video", { controls: true, preload: "metadata", muted: true, src: v.play_url })) : h("p", { class: "error" }, "This file is not in the mounted folder."),
    h("p", { class: "muted" }, v.label_source === "ground truth"
      ? `Labelled by people as ${v.truth === "accident" ? "an accident" : "normal traffic"}.`
      : "Nobody has labelled this one. It was sorted by the AI, so treat the label as a guess."),
    h("div", { class: "runrow" },
      h("button", { class: "primary", disabled: busy || !v.play_url, onclick: () => analyze(true) }, busy ? "Checking" : "Check with AI and raise the alarm"),
      h("button", { class: "ghost", disabled: busy || !v.play_url, onclick: () => analyze(false) }, "Check only")),
    h("div", { id: "run" }, runView()));
}

function runView() {
  const j = S.job;
  if (!j) return null;
  const parts = [];
  if (j.state === "queued") parts.push(h("p", { class: "muted" }, j.queue_position ? `Waiting, ${j.queue_position} job${j.queue_position === 1 ? "" : "s"} ahead.` : "Waiting for the models."));
  if (j.state !== "queued") {
    parts.push(h("p", { class: "muted" }, j.state === "running" ? `Looking at frame ${j.frames_done} of ${j.frames_total || "?"}` : `Looked at ${j.frames_total} frames in ${j.seconds} s`));
    const scores = j.frame_scores || [];
    const total = j.frames_total || scores.length;
    parts.push(h("div", { class: "bars", "aria-label": "Accident score of each frame" },
      Array.from({ length: total }, (_, i) => scores[i] === undefined ? h("i", { class: "wait" })
        : h("i", { class: scores[i] >= THRESHOLD ? "hit" : "", style: `height:${Math.max(3, Math.round(scores[i] * 100))}%`, title: `${j.frame_times_s[i]} s, score ${scores[i]}` })),
      h("span", { class: "thr", style: `bottom:${THRESHOLD * 100}%` })));
    parts.push(h("p", { class: "hint" }, "Each bar is a frame. Red bars are above the accident threshold. The AI needs several red frames before it believes it."));
  }
  if (j.state === "failed") parts.push(h("p", { class: "error" }, j.error));
  if (j.state === "done" && j.verdict) {
    const a = j.verdict.accident, v = S.video;
    const agree = v.truth === "unlabelled" ? "" : (a === (v.truth === "accident") ? " That matches the label." : " That does not match the label.");
    parts.push(h("div", { class: `verdict ${a ? "bad" : "good"}` },
      a ? [h("b", {}, `Accident confirmed, ${j.verdict.severity}`), `${j.verdict.vehicles} vehicle${j.verdict.vehicles === 1 ? "" : "s"}, confidence ${Math.round(j.verdict.confidence * 100)} percent, seen in ${j.verdict.frames_confirmed} of ${j.verdict.frames_sampled} frames.${agree}`]
        : [h("b", {}, "No accident seen"), `Best frame scored ${Math.round(j.verdict.best_score * 100)} percent, so nothing was reported.${agree}`]));
    if (a && !j.published) parts.push(h("p", { class: "muted" }, "Nothing was sent, this was a check only."));
  }
  if (j.published) parts.push(flowView());
  return parts;
}

// ------------------------------------------------------------------ what the platform does next

function flowView() {
  const f = S.flow, inc = f && f.incident;
  const step = (state, text, right) => h("li", { class: state }, h("i", { class: "n" }), h("span", {}, text), h("span", { class: "r" }, right || ""));
  const rows = [step("done", `Alarm sent from camera ${S.job.published.camera_id} over MQTT`, S.job.published.sent ? "sent" : "queued on the camera")];
  if (!inc) rows.push(step("wait", "Waiting for the platform to open an incident"));
  else {
    rows.push(step("done", `Incident opened, ${inc.severity}, ${Math.round(inc.confidence * 100)} percent`, words(inc.state)));
    if (inc.state === "detected") rows.push(step("wait", "Below the automatic line, so an operator has to verify", ""));
    for (const a of inc.assignments) {
      const mapNames = new Map(map.data.facilities.map((x) => [x.id, x.name]));
      rows.push(step(["declined", "timed_out", "requested"].includes(a.state) ? "wait" : "done", `${cap(words(a.agency))} unit from ${mapNames.get(a.facility) || "a station"}`,
        a.state === "requested" ? "asked" : `${words(a.state)}${a.eta_s ? `, ${Math.round(a.eta_s / 60)} min` : ""}`));
    }
    for (const n of inc.notices) rows.push(step(n.status === "failed" ? "wait" : "done", `${n.channel === "sms" ? "SMS" : "Webhook"} to ${n.contact}`, n.status));
  }
  const links = inc ? h("div", { class: "linkrow" },
    inc.state === "detected" ? h("button", { class: "primary", onclick: () => verify(inc.id) }, "Verify as operator") : null,
    h("a", { href: `index.html#incident=${inc.id}` }, "See it on the map"), h("a", { href: `controller.html#incident=${inc.id}` }, "Open in the controller")) : null;
  return h("div", {}, h("h3", {}, "What the platform does"), h("ul", { class: "steps" }, rows), links);
}

async function verify(id) {
  try { await call(`/v1/incidents/${id}/verify`, { method: "POST", key: crypto.randomUUID() }); toast("Verified"); trackFlow(); } catch (e) { fail(e); }
}

async function analyze(publish) {
  try {
    S.job = await call(`/v1/cctv/videos/${S.video.id}/analyze`, { method: "POST", body: { publish } });
  } catch (e) { return fail(e); }
  S.started = Date.now(); S.flow = null;
  draw();
  clearInterval(poll);
  poll = setInterval(async () => {
    try { S.job = await call(`/v1/cctv/jobs/${S.job.job_id}`); } catch (e) { clearInterval(poll); return fail(e); }
    if (["done", "failed"].includes(S.job.state)) { clearInterval(poll); if (S.job.published) trackFlow(); }
    redraw();
  }, 1200);
}

// After an alarm, look at the map data every two seconds for the incident this camera raised.
function trackFlow() {
  clearInterval(flowTimer);
  let tries = 0;
  const look = async () => {
    tries++;
    try {
      const data = await call("/v1/map");
      map.set(data);
      const mine = data.incidents.filter((i) => i.cameras.includes(S.camera) && Date.parse(i.opened_at) >= S.started - 5000);
      S.flow = { incident: mine[0] || null };
      if (mine[0]) map.setPicked(mine[0].id);
      redraw();
    } catch { /* try again */ }
    if (tries > 90) clearInterval(flowTimer);
  };
  look();
  flowTimer = setInterval(look, 2000);
}

function redraw() {
  const run = $("run");
  if (run) run.replaceChildren(...[runView()].flat().filter(Boolean));
  const vp = $("vp");
  if (vp) {                                          // keep the video element, only the button state changes
    const busy = S.job && ["queued", "running"].includes(S.job.state);
    vp.querySelectorAll(".runrow button").forEach((b) => { b.disabled = !!busy; });
    vp.querySelector(".runrow .primary").textContent = busy ? "Checking" : "Check with AI and raise the alarm";
  }
}

control.begin();
