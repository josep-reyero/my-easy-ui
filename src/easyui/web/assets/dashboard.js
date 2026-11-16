// One dashboard page: builds the layout from the compiled spec, keeps the
// param values (mirrored in the address so a reload or bookmark keeps them),
// and runs each query through POST /api/query when the page opens, when a
// control changes, and when the data changes.

import { h, fill } from "./dom.js";
import { api, describe } from "./api.js";
import { Stat, Table, TextBlock, Controls } from "./components.js";
import { Chart } from "./charts.js";

const BASIS = { stat: 9.5, line: 20, area: 20, bar: 20, table: 24, text: 18 }; // rem
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function validDay(s) {
  if (!ISO_DAY.test(s)) return false;
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

/** A typed value for a param from address text, or undefined when it does not fit. */
function coerce(def, raw) {
  if (typeof raw !== "string") return undefined;
  switch (def.type) {
    case "choice": {
      const opt = (def.options || []).find((o) => String(o.value) === raw);
      return opt ? opt.value : undefined;
    }
    case "int": {
      if (!/^-?\d{1,16}$/.test(raw)) return undefined;
      const n = Number(raw);
      if (def.min !== null && def.min !== undefined && n < def.min) return undefined;
      if (def.max !== null && def.max !== undefined && n > def.max) return undefined;
      return n;
    }
    case "date":
      return validDay(raw) ? raw : undefined;
    case "text":
      return raw.slice(0, def.max_length || 200);
    default:
      return undefined;
  }
}

class Unknown {
  constructor(comp) {
    this.el = h("section", { class: "c c-unknown" },
      h("div", { class: "state" }, [
        h("p", { class: "state-title", text: "This part of the page cannot be shown." }),
        h("p", { class: "state-hint", text: `It is a “${String(comp.type)}”, which this version of the dashboard page does not know. Updating easy-ui may fix it.` }),
      ]));
  }

  destroy() {}
}

export class DashboardView {
  constructor({ spec, initial, onLoaded }) {
    this.onLoaded = onLoaded || (() => {});
    this.seq = 0;
    this.latest = new Map();
    this.listeners = new Set();
    this.parts = [];
    this.consumers = new Map(); // query -> [component]
    this.controls = [];
    this.status = h("p", { class: "dash-status", attrs: { role: "status" } });
    this.banner = h("div", { class: "banner", attrs: { role: "alert" }, hidden: true });
    this.rowsEl = h("div", { class: "rows" });
    this.titleEl = h("h1", { class: "dash-title" });
    this.descEl = h("p", { class: "dash-desc" });
    this.el = h("article", { class: "dash" }, [
      h("header", { class: "dash-head" }, [h("div", { class: "dash-heading" }, [this.titleEl, this.descEl]), this.status]),
      this.banner,
      this.rowsEl,
    ]);
    this.build(spec, initial);
  }

  // ---- params -----------------------------------------------------------

  initParams(spec, initial) {
    // A shared copy (spec.fixed) has the numbers for its own settings only: they cannot be changed.
    if (spec.fixed) initial = null;
    this.paramDefs = new Map((spec.params || []).map((p) => [p.name, p]));
    const old = this.params || new Map();
    this.params = new Map();
    for (const [name, def] of this.paramDefs) {
      let value;
      if (initial && initial.has(name)) value = coerce(def, initial.get(name));
      if (value === undefined && old.has(name)) value = coerce(def, String(old.get(name)));
      this.params.set(name, value === undefined ? def.default : value);
    }
    this.controlled = new Set();
    for (const row of spec.layout || []) {
      for (const c of row) if (c.type === "controls") for (const n of c.params || []) this.controlled.add(n);
    }
  }

  paramsObject() {
    return Object.fromEntries(this.params);
  }

  paramsDiffer() {
    for (const [name, def] of this.paramDefs) if (this.params.get(name) !== def.default) return true;
    return false;
  }

  /** The non-default values, for the address bar. */
  search() {
    const q = new URLSearchParams();
    for (const [name, def] of this.paramDefs) {
      const v = this.params.get(name);
      if (v !== def.default) q.set(name, String(v));
    }
    const s = q.toString();
    return s ? `?${s}` : "";
  }

  writeAddress() {
    const hash = `#/d/${encodeURIComponent(this.spec.slug)}${this.search()}`;
    if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
  }

  get ctx() {
    return {
      fixed: this.spec.fixed === "picture" ? "picture" : Boolean(this.spec.fixed),
      params: this.params,
      paramDefs: this.paramDefs,
      queryHasControls: (q) => {
        const used = (this.spec.queries && this.spec.queries[q] && this.spec.queries[q].params) || [];
        return used.some((n) => this.controlled.has(n));
      },
      retry: (q) => this.run([q]),
      setParam: (name, value) => this.setParam(name, value),
      resetParams: () => this.resetParams(),
      paramsDiffer: () => this.paramsDiffer(),
      onParamsChange: (fn) => {
        this.listeners.add(fn);
        return () => this.listeners.delete(fn);
      },
    };
  }

  setParam(name, value) {
    if (!this.paramDefs.has(name) || this.params.get(name) === value) return;
    this.params.set(name, value);
    this.paramsChanged([name]);
  }

  resetParams() {
    const changed = [];
    for (const [name, def] of this.paramDefs) {
      if (this.params.get(name) !== def.default) {
        this.params.set(name, def.default);
        changed.push(name);
      }
    }
    if (changed.length) this.paramsChanged(changed);
  }

  paramsChanged(names) {
    this.writeAddress();
    for (const fn of this.listeners) fn();
    for (const c of this.controls) c.showParamError(null);
    const affected = Object.entries(this.spec.queries || {})
      .filter(([, q]) => (q.params || []).some((p) => names.includes(p)))
      .map(([q]) => q);
    this.run(affected);
  }

  // ---- layout -----------------------------------------------------------

  build(spec, initial) {
    for (const part of this.parts) part.destroy();
    this.parts = [];
    this.consumers = new Map();
    this.controls = [];
    this.listeners = new Set();
    this.spec = spec;
    this.specKey = JSON.stringify(spec);
    this.initParams(spec, initial);

    this.titleEl.textContent = spec.title || spec.slug;
    this.descEl.textContent = spec.description || "";
    this.descEl.hidden = !spec.description;

    const ctx = this.ctx;
    const rows = (spec.layout || []).map((row) => {
      const parts = row.map((comp) => this.make(comp, ctx));
      const types = row.map((c) => c.type);
      const el = h("div", {
        class: [
          "row",
          types.every((t) => t === "stat") ? "row-stats" : null,
          types.includes("controls") ? "row-controls" : null,
        ],
      }, parts.map((p) => p.el));
      const basis = Math.max(...types.map((t) => BASIS[t] || 18));
      el.style.setProperty("--basis", `${basis}rem`);
      return el;
    });
    fill(this.rowsEl, rows);
    this.writeAddress();
    if (this.consumers.size) this.run(Array.from(this.consumers.keys()));
    else queueMicrotask(() => this.onLoaded("ok")); // nothing to load (text only, or a page listing problems)
  }

  make(comp, ctx) {
    let part;
    switch (comp.type) {
      case "stat":
        part = new Stat(comp, ctx);
        break;
      case "line":
      case "area":
      case "bar":
        part = new Chart(comp, ctx);
        break;
      case "table":
        part = new Table(comp, ctx);
        break;
      case "text":
        part = new TextBlock(comp);
        break;
      case "controls":
        part = new Controls(comp, ctx);
        this.controls.push(part);
        break;
      default:
        part = new Unknown(comp);
    }
    this.parts.push(part);
    if (comp.query && typeof part.setResult === "function") {
      if (!this.consumers.has(comp.query)) this.consumers.set(comp.query, []);
      this.consumers.get(comp.query).push(part);
    }
    return part;
  }

  // ---- queries ----------------------------------------------------------

  showBanner(err) {
    if (!err) {
      this.banner.hidden = true;
      fill(this.banner);
      return;
    }
    fill(this.banner, [
      h("p", { class: "banner-title", text: err.message }),
      err.hint ? h("p", { class: "banner-hint", text: err.hint }) : null,
    ]);
    this.banner.hidden = false;
  }

  async run(names) {
    const wanted = [...new Set(names)].filter((n) => this.consumers.has(n));
    if (!wanted.length) return;
    const seq = ++this.seq;
    for (const n of wanted) {
      this.latest.set(n, seq);
      for (const c of this.consumers.get(n)) c.setLoading();
    }
    const current = (n) => this.latest.get(n) === seq;
    let noData = null;
    let outcome = "ok";
    try {
      const data = await api.query(this.spec.slug, wanted, this.paramsObject());
      const results = (data && data.results) || {};
      for (const n of wanted) {
        if (!current(n)) continue;
        const res = results[n];
        if (res && res.error && res.error.code === "no_data") noData = res.error;
        else if (!res || res.error) outcome = "failed";
        for (const c of this.consumers.get(n) || []) {
          if (res && res.error && res.error.code === "no_data") this.waiting(c);
          else c.setResult(res);
        }
      }
    } catch (err) {
      if (err && err.status === 401) return; // the app shows the sign-in page
      const info = describe(err);
      outcome = info.code === "offline" ? "offline" : "failed";
      if (err && err.code === "bad_param") {
        for (const ctl of this.controls) ctl.showParamError(info);
      }
      if (info.code === "no_data") noData = info;
      for (const n of wanted) {
        if (!current(n)) continue;
        for (const c of this.consumers.get(n) || []) {
          if (info.code === "no_data") this.waiting(c);
          else if (info.code === "bad_param" && c.hasContent) c.done();
          else c.setError(info);
        }
      }
    }
    if (seq === this.seq) {
      this.showBanner(noData);
      this.onLoaded(outcome);
    }
  }

  waiting(c) {
    c.done();
    c.showEmpty();
    const hint = c.body.querySelector(".state-hint");
    if (hint) hint.textContent = "Waiting for data. See the note at the top of the page.";
  }

  /** The data or the dashboard changed on the server. */
  async refresh() {
    let spec;
    try {
      spec = await api.dashboard(this.spec.slug);
    } catch (err) {
      if (err && err.status === 404) return "gone";
      return "kept";
    }
    if (JSON.stringify(spec) !== this.specKey) {
      const initial = new URLSearchParams(this.search().slice(1));
      this.build(spec, initial);
    } else {
      this.run(Array.from(this.consumers.keys()));
    }
    return "ok";
  }

  setStatus(text, quiet) {
    this.status.textContent = text;
    this.status.classList.toggle("is-quiet", Boolean(quiet));
  }

  destroy() {
    for (const part of this.parts) part.destroy();
    this.parts = [];
  }
}
