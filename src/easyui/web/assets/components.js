// Components: stat, table, text, controls, plus the shared frame that gives
// every data component its loading, empty and error states. Charts live in
// charts.js and use the same frame.

import { h, fill, nextId, icon } from "./dom.js";
import { display, isNumericFormat, numeric, splitUnit, NULL_TEXT } from "./format.js";
import { renderBlocks } from "./text.js";

// ---- shared helpers -----------------------------------------------------

/** A column name shown as a heading when the dashboard gives no label: "distance_m" -> "Distance m". */
export function humanize(name) {
  const s = String(name);
  if (!/^[a-z0-9_]+$/.test(s)) return s;
  const words = s.replace(/_+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : s;
}

/**
 * A right-edge fade on a sideways-scrolling box while more of it is off to
 * the right, so a cut-off table shows that it has more columns.
 */
export function scrollCue(scroller) {
  const frame = h("div", { class: "scroll-frame" }, scroller);
  const update = () => {
    const more = scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 2;
    frame.classList.toggle("has-more", more);
  };
  scroller.addEventListener("scroll", update, { passive: true });
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(update) : null;
  if (observer) observer.observe(scroller);
  requestAnimationFrame(update);
  frame.disposeCue = () => observer && observer.disconnect();
  return frame;
}

// ---- shared frame -------------------------------------------------------

/**
 * The outer element and states of a data component. Subclasses implement
 * render(result) and may override loadingView().
 */
export class Frame {
  constructor(comp, ctx, extraClass = "") {
    this.comp = comp;
    this.ctx = ctx;
    this.titleId = nextId("t");
    this.el = h("section", {
      class: ["c", `c-${comp.type}`, extraClass],
      attrs: { "aria-labelledby": comp.title ? this.titleId : null, "aria-label": comp.title ? null : comp.type },
    });
    this.head = h("div", { class: "c-head" });
    if (comp.title) this.head.append(h("h2", { class: "c-title", id: this.titleId, text: comp.title }));
    this.body = h("div", { class: "c-body" });
    this.el.append(this.head, this.body);
    this.hasContent = false;
  }

  /**
   * Before results: on first load a placeholder that keeps the space (it fades
   * in only if loading takes a moment, see .sk-wrap); later, keep the content
   * and dim it.
   */
  setLoading() {
    this.el.setAttribute("aria-busy", "true");
    if (this.hasContent) {
      this.el.classList.add("is-updating");
      return;
    }
    this.el.classList.remove("is-empty", "is-error");
    fill(this.body, this.loadingView());
  }

  loadingView() {
    return h("div", { class: "sk-wrap", attrs: { role: "status" } }, [
      h("span", { class: "sr-only", text: "Loading" }),
      h("div", { class: "sk sk-block" }),
    ]);
  }

  done() {
    this.el.removeAttribute("aria-busy");
    this.el.classList.remove("is-updating");
  }

  setResult(result) {
    this.done();
    if (!result || typeof result !== "object") {
      this.setError({ message: "This part of the page got no data from the server.", hint: "Reload the page to try again." });
      return;
    }
    if (result.error) {
      this.setError(result.error);
      return;
    }
    const columns = Array.isArray(result.columns) ? result.columns : [];
    const rows = Array.isArray(result.rows) ? result.rows : [];
    const missing = this.requiredColumns().filter((c) => c && !columns.includes(c));
    if (missing.length) {
      this.setError({
        message: `This needs a column called “${missing[0]}”, which the data does not have.`,
        hint: "The dashboard and the data no longer match. Ask your agent to run easy-ui validate and fix the dashboard.",
      });
      return;
    }
    if (rows.length === 0) {
      this.showEmpty();
      return;
    }
    this.hasContent = true;
    this.el.classList.remove("is-empty", "is-error");
    this.render({ columns, rows, truncated: Boolean(result.truncated) });
  }

  requiredColumns() {
    return [];
  }

  emptyHint() {
    if (!this.ctx.queryHasControls(this.comp.query)) {
      return "Nothing has been recorded for this yet. It will show here after the next data update.";
    }
    return this.ctx.paramsDiffer()
      ? "Try a wider range in the settings above, or reset them to their defaults."
      : "Try a wider range in the settings above.";
  }

  emptyTitle() {
    return this.ctx.queryHasControls(this.comp.query) ? "Nothing matches these settings" : "No data yet";
  }

  showEmpty() {
    this.hasContent = false;
    this.disposeContent();
    this.el.classList.remove("is-error");
    this.el.classList.add("is-empty");
    const reset = this.ctx.queryHasControls(this.comp.query) && this.ctx.paramsDiffer()
      ? h("button", {
        class: "btn-quiet",
        type: "button",
        text: "Reset settings",
        on: { click: () => this.ctx.resetParams() },
      })
      : null;
    fill(
      this.body,
      h("div", { class: "state state-empty" }, [
        h("p", { class: "state-title", text: this.emptyTitle() }),
        h("p", { class: "state-hint", text: this.emptyHint() }),
        reset,
      ]),
    );
  }

  setError(err) {
    this.done();
    this.hasContent = false;
    this.disposeContent();
    this.el.classList.remove("is-empty");
    this.el.classList.add("is-error");
    const message = (err && typeof err.message === "string" && err.message) || "This part of the page could not be shown.";
    const hint = err && typeof err.hint === "string" ? err.hint : null;
    const children = [
      h("p", { class: "state-title", text: message }),
      hint ? h("p", { class: "state-hint", text: hint }) : null,
    ];
    if (this.comp.query && err && err.code !== "no_data") {
      children.push(
        h("button", {
          class: "btn-quiet",
          type: "button",
          text: "Try again",
          on: { click: () => this.ctx.retry(this.comp.query) },
        }),
      );
    }
    fill(this.body, h("div", { class: "state state-error", attrs: { role: "alert" } }, children));
  }

  disposeContent() {}

  destroy() {
    this.disposeContent();
  }
}

// ---- stat ---------------------------------------------------------------

function figure(text, className) {
  const [num, unit] = splitUnit(text);
  return h("span", { class: className }, [
    h("span", { class: "fig", text: num }),
    unit ? h("span", { class: "unit", text: unit }) : null,
  ]);
}

export class Stat extends Frame {
  constructor(comp, ctx) {
    super(comp, ctx);
    this.hint = comp.hint ? h("p", { class: "stat-hint", text: comp.hint }) : null;
  }

  requiredColumns() {
    return [this.comp.value, this.comp.delta];
  }

  loadingView() {
    return h("div", { class: "sk-wrap", attrs: { role: "status" } }, [
      h("span", { class: "sr-only", text: "Loading" }),
      h("div", { class: "sk sk-figure" }),
    ]);
  }

  render({ columns, rows }) {
    const row = rows[0];
    const value = row[columns.indexOf(this.comp.value)];
    const text = display(this.comp.format, value);
    // A date or a word is not a quantity: it is shown smaller than numbers, so it never outweighs them.
    const isText = this.comp.format === "date" || this.comp.format === "datetime" || numeric(value) === null;
    const out = [h("p", { class: ["stat-value", isText ? "is-text" : null] }, figure(text, "stat-figure"))];
    if (this.comp.delta) {
      const d = numeric(row[columns.indexOf(this.comp.delta)]);
      if (d !== null) {
        const fmt = this.comp.delta_format || this.comp.format;
        const mag = display(fmt, Math.abs(d));
        if (mag !== NULL_TEXT) {
          const zero = !/[1-9]/.test(mag);
          const dir = zero ? "flat" : d > 0 ? "up" : "down";
          const sign = dir === "up" ? "+" : dir === "down" ? "−" : "";
          // Neutral unless the dashboard says which way is good news (better: higher | lower).
          let mood = "";
          if (dir !== "flat" && (this.comp.better === "higher" || this.comp.better === "lower")) {
            const good = (dir === "up") === (this.comp.better === "higher");
            mood = good ? "is-good" : "is-bad";
          }
          const moodWords = mood === "is-good" ? " (better)" : mood === "is-bad" ? " (worse)" : "";
          const words = dir === "up" ? `up${moodWords}` : dir === "down" ? `down${moodWords}` : "no change,";
          out.push(
            h("p", { class: ["stat-delta", `is-${dir}`, mood || null] }, [
              h("span", { class: "delta-mark", attrs: { "aria-hidden": "true" }, text: dir === "up" ? "↗" : dir === "down" ? "↘" : "→" }),
              h("span", { class: "sr-only", text: `${words} ` }),
              h("span", { text: `${sign}${mag}` }),
            ]),
          );
        }
      }
    }
    if (this.hint) out.push(this.hint);
    fill(this.body, out);
  }

  showEmpty() {
    super.showEmpty();
    fill(
      this.body,
      h("p", { class: "stat-value is-none" }, h("span", { class: "stat-figure" }, h("span", { class: "fig", text: NULL_TEXT }))),
      h("p", { class: "state-hint", text: this.ctx.queryHasControls(this.comp.query) ? "No data for these settings." : "No data yet." }),
    );
  }
}

// ---- table --------------------------------------------------------------

function compareValues(a, b) {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : 1;
  if (b === null || b === undefined) return -1;
  const na = numeric(a);
  const nb = numeric(b);
  if (na !== null && nb !== null) return na - nb;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

export class Table extends Frame {
  constructor(comp, ctx) {
    super(comp, ctx, "panel");
    this.sort = null; // {pos, dir: 1 | -1}: column position in the shown table
    this.data = null;
  }

  requiredColumns() {
    return Array.isArray(this.comp.columns) ? this.comp.columns.map((c) => c.column) : [];
  }

  loadingView() {
    return h("div", { class: "sk-wrap", attrs: { role: "status" } }, [
      h("span", { class: "sr-only", text: "Loading" }),
      ...[0, 1, 2, 3, 4].map(() => h("div", { class: "sk sk-line" })),
    ]);
  }

  render(data) {
    this.data = data;
    this.draw();
  }

  draw() {
    const { columns, rows, truncated } = this.data;
    const spec = Array.isArray(this.comp.columns)
      ? this.comp.columns
      : columns.map((c) => ({ column: c, label: c, format: null }));
    const cols = spec.map((c) => {
      const index = columns.indexOf(c.column);
      const values = rows.map((r) => r[index]);
      const numericCol = isNumericFormat(c.format) ||
        (c.format === null && values.every((v) => v === null || typeof v === "number"));
      const label = c.label && c.label !== c.column ? c.label : humanize(c.column);
      return { ...c, label, index, numeric: numericCol };
    });

    let shown = rows;
    if (this.sort && cols[this.sort.pos]) {
      const { index } = cols[this.sort.pos];
      const { dir } = this.sort;
      shown = rows
        .map((r, i) => [r, i])
        .sort((x, y) => compareValues(x[0][index], y[0][index]) * dir || x[1] - y[1])
        .map((x) => x[0]);
    }
    const limit = Number.isInteger(this.comp.limit) ? this.comp.limit : null;
    if (limit !== null) shown = shown.slice(0, limit);

    const thead = h("thead", null, h("tr", null, cols.map((c, pos) => {
      const sorted = this.sort && this.sort.pos === pos;
      const th = h("th", {
        class: c.numeric ? "num" : null,
        attrs: { scope: "col", "aria-sort": sorted ? (this.sort.dir > 0 ? "ascending" : "descending") : null },
      });
      th.append(
        h("button", {
          class: "th-sort",
          type: "button",
          dataset: { pos },
          on: { click: () => this.toggleSort(pos) },
        }, [
          h("span", { text: c.label }),
          h("span", { class: "sort-mark", attrs: { "aria-hidden": "true" }, text: sorted ? (this.sort.dir > 0 ? "↑" : "↓") : "" }),
        ]),
      );
      return th;
    })));
    const tbody = h("tbody", null, shown.map((r) => h("tr", null, cols.map((c) => {
      const v = r[c.index];
      return h("td", { class: [c.numeric ? "num" : null, v === null ? "is-null" : null], text: display(c.format, v) });
    }))));

    const label = this.comp.title || "Table";
    const scroller = h("div", {
      class: "table-scroll",
      tabIndex: 0,
      attrs: { role: "region", "aria-label": `${label} (scrolls sideways)` },
    }, h("table", null, [h("caption", { class: "sr-only", text: label }), thead, tbody]));

    const notes = [];
    if (shown.length < rows.length) notes.push(`Showing ${shown.length.toLocaleString("en")} of ${rows.length.toLocaleString("en")} rows.`);
    if (truncated) notes.push("Only the first part of the data was loaded, because there is a lot of it.");
    this.disposeContent();
    this.cue = scrollCue(scroller);
    fill(this.body, this.cue, notes.length ? h("p", { class: "c-note", text: notes.join(" ") }) : null);
  }

  disposeContent() {
    if (this.cue) this.cue.disposeCue();
    this.cue = null;
  }

  toggleSort(pos) {
    if (!this.sort || this.sort.pos !== pos) this.sort = { pos, dir: 1 };
    else if (this.sort.dir === 1) this.sort = { pos, dir: -1 };
    else this.sort = null;
    this.draw();
    const btn = this.body.querySelector(`.th-sort[data-pos="${pos}"]`);
    if (btn) btn.focus();
  }
}

// ---- text ---------------------------------------------------------------

export class TextBlock {
  constructor(comp) {
    this.comp = comp;
    const titleId = nextId("t");
    this.el = h("section", {
      class: "c c-text",
      attrs: { "aria-labelledby": comp.title ? titleId : null, "aria-label": comp.title ? null : "Notes" },
    }, [
      comp.title ? h("h2", { class: "c-title", id: titleId, text: comp.title }) : null,
      h("div", { class: "prose" }, renderBlocks(comp.blocks, comp.title ? 3 : 2)),
    ]);
  }

  destroy() {}
}

// ---- controls -----------------------------------------------------------

const SEGMENT_MAX_OPTIONS = 4;
const SEGMENT_MAX_CHARS = 28;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function debounce(fn, ms) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

export class Controls {
  constructor(comp, ctx) {
    this.comp = comp;
    this.ctx = ctx;
    this.inputs = new Map(); // name -> {sync(value)}
    const titleId = nextId("t");
    this.message = h("p", { class: "controls-message", attrs: { role: "alert" }, hidden: true });
    this.reset = h("button", {
      class: "btn-quiet controls-reset",
      type: "button",
      text: "Reset",
      attrs: { "aria-label": "Reset all settings to their defaults" },
      on: { click: () => this.ctx.resetParams() },
      hidden: true,
    });
    const names = (Array.isArray(comp.params) ? comp.params : []).filter((n) => ctx.paramDefs.has(n));
    this.el = h("section", {
      class: "c c-controls",
      attrs: { "aria-labelledby": comp.title ? titleId : null, "aria-label": comp.title ? null : "Settings" },
    }, [
      comp.title ? h("h2", { class: "c-title", id: titleId, text: comp.title }) : null,
      h("div", { class: "controls" }, [...names.map((n) => this.control(ctx.paramDefs.get(n))), this.reset]),
      this.message,
    ]);
    this.unsubscribe = ctx.onParamsChange(() => this.syncAll());
    this.syncAll();
  }

  syncAll() {
    for (const [name, input] of this.inputs) input.sync(this.ctx.params.get(name));
    this.reset.hidden = !this.ctx.paramsDiffer();
  }

  say(text) {
    this.message.textContent = text || "";
    this.message.hidden = !text;
  }

  control(p) {
    const id = nextId("p");
    if (p.type === "choice") return this.choice(p, id);
    const label = h("label", { class: "field-label", htmlFor: id, text: p.label });
    let input;
    if (p.type === "date") {
      input = h("input", { class: "input", type: "date", id, name: p.name });
      input.addEventListener("change", () => {
        if (ISO_DAY.test(input.value)) {
          this.say("");
          this.ctx.setParam(p.name, input.value);
        } else if (input.value === "") {
          this.say(`${p.label}: pick a date, or press Reset to go back to the default.`);
        }
      });
      this.inputs.set(p.name, { sync: (v) => { if (input.value !== v) input.value = v; } });
    } else if (p.type === "int") {
      input = h("input", {
        class: "input input-int",
        type: "number",
        id,
        name: p.name,
        step: 1,
        inputMode: "numeric",
        min: p.min ?? undefined,
        max: p.max ?? undefined,
      });
      const commit = () => {
        const raw = input.value.trim();
        const n = Number(raw);
        const okRange = (p.min === null || p.min === undefined || n >= p.min) && (p.max === null || p.max === undefined || n <= p.max);
        if (raw === "" || !Number.isInteger(n) || !okRange) {
          const range = p.min !== null && p.min !== undefined && p.max !== null && p.max !== undefined
            ? ` between ${p.min} and ${p.max}`
            : "";
          this.say(`${p.label}: use a whole number${range}. The page still shows the last valid value.`);
          return;
        }
        this.say("");
        this.ctx.setParam(p.name, n);
      };
      const later = debounce(commit, 600);
      input.addEventListener("input", later);
      input.addEventListener("change", () => { later.cancel(); commit(); });
      this.inputs.set(p.name, { sync: (v) => { if (Number(input.value) !== v || input.value === "") input.value = String(v); } });
    } else {
      input = h("input", {
        class: "input input-text",
        type: "search",
        id,
        name: p.name,
        maxLength: p.max_length || 200,
        autocomplete: "off",
        spellcheck: false,
      });
      const later = debounce(() => this.ctx.setParam(p.name, input.value), 400);
      input.addEventListener("input", later);
      input.addEventListener("change", () => { later.cancel(); this.ctx.setParam(p.name, input.value); });
      this.inputs.set(p.name, { sync: (v) => { if (document.activeElement !== input && input.value !== v) input.value = v; } });
    }
    return h("div", { class: `field field-${p.type}` }, [label, input]);
  }

  choice(p, id) {
    const options = Array.isArray(p.options) ? p.options : [];
    const chars = options.reduce((n, o) => n + String(o.label).length, 0);
    const byKey = new Map(options.map((o) => [String(o.value), o.value]));
    if (options.length <= SEGMENT_MAX_OPTIONS && chars <= SEGMENT_MAX_CHARS) {
      const legendId = `${id}-l`;
      const radios = options.map((o, i) => {
        const rid = `${id}-${i}`;
        const radio = h("input", { class: "seg-input", type: "radio", id: rid, name: id, value: String(o.value) });
        radio.addEventListener("change", () => {
          if (radio.checked) this.ctx.setParam(p.name, byKey.get(radio.value));
        });
        return [radio, h("label", { class: "seg-label", htmlFor: rid, text: o.label })];
      });
      this.inputs.set(p.name, {
        sync: (v) => {
          for (const [radio] of radios) radio.checked = radio.value === String(v);
        },
      });
      return h("div", { class: "field field-choice", attrs: { role: "radiogroup", "aria-labelledby": legendId } }, [
        h("span", { class: "field-label", id: legendId, text: p.label }),
        h("div", { class: "seg" }, radios),
      ]);
    }
    const select = h("select", { class: "input select", id, name: p.name },
      options.map((o) => h("option", { value: String(o.value), text: o.label })));
    select.addEventListener("change", () => this.ctx.setParam(p.name, byKey.get(select.value)));
    this.inputs.set(p.name, { sync: (v) => { select.value = String(v); } });
    return h("div", { class: "field field-choice" }, [
      h("label", { class: "field-label", htmlFor: id, text: p.label }),
      h("div", { class: "select-wrap" }, [select, icon("back")]),
    ]);
  }

  /** Show a problem with a value the server refused. */
  showParamError(err) {
    this.say(err && err.message ? `${err.message}${err.hint ? ` ${err.hint}` : ""}` : "");
  }

  destroy() {
    this.unsubscribe();
  }
}
