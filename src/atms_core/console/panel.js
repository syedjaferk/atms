// The incident list and the detail view with the actions an operator can take.
import { call, newKey } from "./api.js";
import { age, cap, clock, h, words } from "./dom.js";

const OPEN_FOR = {
  verify: new Set(["detected"]),
  reject: new Set(["detected", "verified", "dispatching", "dispatched"]),
  resolve: new Set(["clearing"]),
  close: new Set(["resolved", "rejected"]),
};
const ASSIGN_LABEL = { requested: "asked", acked: "accepted", en_route: "on the way", on_scene: "on scene", done: "finished",
                       declined: "declined", timed_out: "no answer", cancelled: "stood down" };

export class Panel {
  constructor({ list, empty, count, detail }, handlers) {
    this.list = list; this.empty = empty; this.count = count; this.detailEl = detail; this.on = handlers;
    this.picked = null; this.incidents = []; this.busy = false; this.options = null; this.optionsFor = null;
  }

  setIncidents(items) {
    this.incidents = items;
    this.count.textContent = String(items.length);
    this.empty.hidden = items.length > 0;
    this.list.replaceChildren(...items.map((i) => {
      const live = i.assignments.filter((a) => !["declined", "cancelled"].includes(a.state));
      const acted = live.filter((a) => ["acked", "en_route", "on_scene", "done"].includes(a.state)).length;
      const li = h("li", { tabindex: "0", class: i.id === this.picked ? "picked" : "", onclick: () => this.on.pick(i.id),
                           onkeydown: (e) => { if (e.key === "Enter") this.on.pick(i.id); } },
        h("span", { class: `dot-s ${i.severity}` }),
        h("span", { class: "t" }, `${cap(i.severity)} incident${i.segment ? ` on ${i.segment}` : ""}`),
        h("span", { class: "badge" }, words(i.state)),
        h("span", { class: "s" }, `${age(i.opened_at)}, ${i.lanes_closed} lane${i.lanes_closed === 1 ? "" : "s"} closed`
          + (live.length ? `, ${acted} of ${live.length} units responding` : "")));
      return li;
    }));
  }

  setPicked(id) {
    this.picked = id;
    if (!id) { this.detailEl.hidden = true; this.detailEl.replaceChildren(); }
    this.setIncidents(this.incidents);
  }

  async show(id) {
    if (id !== this.picked) return;
    let inc, notices = [];
    try {
      inc = await call(`/v1/incidents/${id}`);
      notices = (await call(`/v1/incidents/${id}/notifications`)).items;
    } catch (e) {
      if (!inc) { this.detailEl.hidden = false; this.detailEl.replaceChildren(h("p", { class: "error" }, e.message)); return; }
    }
    if (id !== this.picked) return;
    this.draw(inc, notices);
  }

  draw(inc, notices) {
    const can = (a) => OPEN_FOR[a].has(inc.state) && !this.busy;
    const run = (label, fn) => async () => {
      this.busy = true; this.draw(inc, notices);
      try { await fn(); this.on.toast(`${label} done`); } catch (e) { this.on.toast(e.message); }
      this.busy = false; this.on.refresh(); await this.show(inc.id);
    };
    const act = (verb) => `/v1/incidents/${inc.id}/${verb}`;
    const reason = h("input", { id: "reject-reason", placeholder: "Reason for rejecting", "aria-label": "Reason for rejecting", maxlength: "200" });
    const noteBox = h("input", { id: "note-text", placeholder: "Add a note to the timeline", "aria-label": "Note", maxlength: "500" });

    const root = h("div", {},
      h("div", { class: "head" }, h("h2", {}, `${cap(inc.severity)} incident`), h("span", { class: "badge act" }, words(inc.state))),
      h("dl", { class: "kv" },
        h("dt", {}, "Road"), h("dd", {}, inc.road_segment),
        h("dt", {}, "Confidence"), h("dd", {}, `${Math.round(inc.confidence * 100)} percent`),
        h("dt", {}, "Position"), h("dd", {}, `${inc.location.lat.toFixed(4)}, ${inc.location.lon.toFixed(4)}`),
        h("dt", {}, "Opened"), h("dd", {}, `${clock(inc.opened_at)}, ${age(inc.opened_at)}`)),
      h("div", { class: "actions" },
        h("button", { class: "primary", disabled: !can("verify"), onclick: run("Verify", () => call(act("verify"), { method: "POST", key: newKey() })) }, "Verify"),
        h("button", { class: "danger", disabled: !can("reject"), onclick: run("Reject", () => {
          const text = reason.value.trim();
          if (text.length < 3) throw new Error("Give a reason of at least three characters.");
          return call(act("reject"), { method: "POST", body: { reason: text }, key: newKey() });
        }) }, "Reject"),
        h("button", { disabled: !can("resolve"), onclick: run("Resolve", () => call(act("resolve"), { method: "POST", key: newKey() })) }, "Resolve"),
        h("button", { disabled: !can("close"), onclick: run("Close", () => call(act("close"), { method: "POST", key: newKey() })) }, "Close")),
      OPEN_FOR.reject.has(inc.state) ? h("div", { class: "inline-wrap" }, reason) : null,

      h("h3", {}, "Units"),
      inc.assignments.length
        ? h("ul", { class: "rows" }, inc.assignments.map((a) => h("li", {},
            h("span", {}, `${words(a.agency)}${a.unit_ref ? `, ${a.unit_ref}` : ""}`),
            h("span", { class: "muted" }, `${ASSIGN_LABEL[a.state] || words(a.state)}${a.eta_s ? `, ${Math.round(a.eta_s / 60)} min away` : ""}`))))
        : h("p", { class: "muted" }, "No units asked yet."),

      h("h3", {}, "Notices"),
      notices.length
        ? h("ul", { class: "rows" }, notices.map((n) => h("li", {},
            h("span", {}, `${n.contact}, ${n.channel}`),
            h("span", { class: n.status === "failed" ? "error" : "muted" }, words(n.status)))))
        : h("p", { class: "muted" }, "None sent."),

      h("h3", {}, "Timeline"),
      h("ul", { class: "rows tl" }, inc.timeline.slice().reverse().map((t) =>
        h("li", {}, h("span", {}, h("time", {}, clock(t.at)), t.summary), t.actor ? h("span", { class: "muted" }, t.actor) : null))),
      h("form", { class: "inline", onsubmit: async (e) => {
        e.preventDefault();
        const text = noteBox.value.trim();
        if (!text) return;
        try { await call(`/v1/incidents/${inc.id}/notes`, { method: "POST", body: { text } }); noteBox.value = ""; await this.show(inc.id); }
        catch (err) { this.on.toast(err.message); }
      } }, noteBox, h("button", { type: "submit" }, "Add")),

      h("h3", {}, "What if"),
      h("div", { class: "actions" }, h("button", { id: "whatif-run", disabled: this.busy, onclick: () => this.whatIf(inc) }, "Compare ways to handle it")),
      h("div", { id: "whatif-out" }, this.optionsFor === inc.id ? this.optionsView() : null));

    this.detailEl.hidden = false;
    const keep = { reason: document.getElementById("reject-reason")?.value, note: document.getElementById("note-text")?.value };
    this.detailEl.replaceChildren(root);
    if (keep.reason) document.getElementById("reject-reason").value = keep.reason;
    if (keep.note) document.getElementById("note-text").value = keep.note;
  }

  async whatIf(inc) {
    const out = document.getElementById("whatif-out");
    out.replaceChildren(h("p", { class: "muted" }, "Running the traffic twin"));
    try {
      this.options = await call(`/v1/incidents/${inc.id}/whatif`, { method: "POST" });
      this.optionsFor = inc.id;
      out.replaceChildren(this.optionsView());
    } catch (e) {
      out.replaceChildren(h("p", { class: "error" }, e.message));
    }
  }

  optionsView() {
    const o = this.options;
    if (!o) return null;
    const best = o.recommendation && o.recommendation.option;
    return h("div", {},
      h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Option"), h("th", {}, "Vehicle hours lost"), h("th", {}, "Worst trip min"), h("th", {}, "Queue km"), h("th", {}, "Saves"))),
        h("tbody", {}, o.scenarios.map((s) => h("tr", { class: s.name === best ? "best" : "" },
          h("td", {}, cap(s.name)), h("td", {}, s.kpis.vehicle_hours_lost), h("td", {}, s.kpis.worst_end_to_end_minutes),
          h("td", {}, s.kpis.longest_queue_km), h("td", {}, s.saves_vs_as_things_stand.vehicle_hours))))),
      h("p", { class: "note" }, `Lowest delay is ${best}. Each extra 10 minutes closed costs about ${o.cost_of_delay.each_extra_10_min_closed_costs_vehicle_hours} vehicle hours.`),
      h("p", { class: "note" }, `${o.assumptions.basis}. ${o.recommendation.note}`),
      h("p", { class: "note" }, o.start && o.start.note ? o.start.note : null));
  }
}
