// timeline: spans of time in categories (sleep stages over a night), one lane per category, drawn by ECharts
// as SVG like the other charts. It reads its look from the same custom properties (the category colours are
// --eui-chart-1 ... -8 in the order of the lanes) and shows, above the chart, the span under the pointer or
// the whole period, and in the legend how long each category lasted.

import { h, fill, icon, prefersReducedMotion } from "./dom.js";
import { Frame, humanize, scrollCue } from "./components.js";
import { display, NULL_TEXT } from "./format.js";
import { onThemeChange } from "./theme.js";
import { chartStyle, loadECharts, RENDERER, withAlpha } from "./charts.js";

const WALL_TIME = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?/;

/**
 * A moment as milliseconds on a wall clock read as UTC: text "YYYY-MM-DD HH:MM[:SS]" exactly as written (no
 * time-zone conversion, like the datetime format), or seconds since 1970-01-01 UTC. null when it is neither.
 */
export function wallTime(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value * 1000 : null;
  if (typeof value !== "string") return null;
  const m = WALL_TIME.exec(value.trim());
  if (!m) return null;
  const t = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 0), Number(m[5] || 0), Number(m[6] || 0));
  return Number.isFinite(t) ? t : null;
}

function hhmm(t) {
  const d = new Date(t);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

function seconds(ms) {
  return Math.round(ms / 1000);
}

export class Timeline extends Frame {
  constructor(comp, ctx) {
    super(comp, ctx, "panel");
    this.chart = null;
    this.data = null;
    this.asTable = false;
    this.animate = true;
    this.readout = h("div", { class: "readout eui-readout", attrs: { "aria-hidden": "true" } });
    this.head.append(this.readout);
    this.unsubscribeTheme = onThemeChange(() => this.redraw());
  }

  requiredColumns() {
    return [this.comp.start, this.comp.end, this.comp.category];
  }

  loadingView() {
    return h("div", { class: "sk-wrap", attrs: { role: "status" } }, [
      h("span", { class: "sr-only", text: "Loading" }),
      h("div", { class: "sk sk-timeline" }),
    ]);
  }

  prepare({ columns, rows }) {
    const si = columns.indexOf(this.comp.start);
    const ei = columns.indexOf(this.comp.end);
    const ci = columns.indexOf(this.comp.category);
    const spans = [];
    for (const r of rows) {
      const start = wallTime(r[si]);
      const end = wallTime(r[ei]);
      const cat = r[ci];
      if (start === null || end === null || end <= start || cat === null || cat === undefined) continue;
      spans.push({ start, end, cat: String(cat), row: r });
    }
    spans.sort((a, b) => a.start - b.start);
    const seen = [];
    for (const s of spans) if (!seen.includes(s.cat)) seen.push(s.cat);
    const order = Array.isArray(this.comp.order) ? this.comp.order.map(String) : [];
    // The listed categories first (even with no span, so lanes keep their place night after night), then any others.
    const lanes = [...order, ...seen.filter((c) => !order.includes(c))];
    const totals = new Map(lanes.map((c) => [c, 0]));
    for (const s of spans) totals.set(s.cat, totals.get(s.cat) + (s.end - s.start));
    const from = spans.length ? Math.min(...spans.map((s) => s.start)) : null;
    const to = spans.length ? Math.max(...spans.map((s) => s.end)) : null;
    return { spans, lanes, totals, from, to, rows: rows.length };
  }

  emptyTitle() {
    return this.noSpans ? "No spans to draw" : super.emptyTitle();
  }

  emptyHint() {
    return this.noSpans
      ? "The rows have no start and end times that can be read (YYYY-MM-DD HH:MM, or seconds since 1970)."
      : super.emptyHint();
  }

  render(result) {
    const data = this.prepare(result);
    this.noSpans = !data.spans.length;
    if (this.noSpans) {
      this.showEmpty();
      return;
    }
    this.data = data;
    this.hover = -1;
    if (!this.chart || !this.canvas || !this.canvas.isConnected) this.buildBody();
    this.canvas.style.setProperty("--lanes", String(data.lanes.length));
    this.updateReadout();
    this.updateLegend();
    this.updateSummary();
    if (this.asTable) this.drawTable();
    loadECharts()
      .then((echarts) => {
        if (!this.data || !this.canvas || !this.canvas.isConnected) return;
        if (!this.chart) {
          this.echarts = echarts;
          this.chart = echarts.init(this.canvas, null, { renderer: RENDERER });
          this.chart.on("mouseover", (e) => {
            if (e && Number.isInteger(e.dataIndex)) {
              this.hover = e.dataIndex;
              this.updateReadout();
            }
          });
          this.chart.on("mouseout", () => {
            this.hover = -1;
            this.updateReadout();
          });
          this.size = [this.canvas.clientWidth, this.canvas.clientHeight];
          this.resizer = new ResizeObserver(() => {
            const now = [this.canvas.clientWidth, this.canvas.clientHeight];
            if (!this.chart || !now[0] || (now[0] === this.size[0] && now[1] === this.size[1])) return;
            this.size = now;
            this.chart.resize();
            this.redraw();
          });
          this.resizer.observe(this.canvas);
        }
        this.redraw();
      })
      .catch(() => {
        this.setError({
          message: "The chart drawing part of this page did not load.",
          hint: "Reload the page. The spans are still available with View as table.",
        });
      });
  }

  buildBody() {
    const label = this.comp.title || "Timeline";
    this.canvas = h("div", { class: "chart chart-timeline eui-chart", attrs: { role: "img" } });
    this.stage = h("div", { class: "chart-stage" }, this.canvas);
    this.legend = h("ul", { class: "legend eui-legend", attrs: { "aria-hidden": "true" } });
    this.legend.dataset.kind = "timeline";
    this.tableHost = h("div", { class: "chart-table", hidden: true });
    this.toggle = h("button", {
      class: "btn-quiet btn-toggle",
      type: "button",
      attrs: { "aria-pressed": "false", "aria-label": `View ${label} as a table` },
      on: { click: () => this.toggleTable() },
    }, [icon("table"), h("span", { text: "View as table" })]);
    fill(this.body, this.legend, this.stage, this.tableHost, h("div", { class: "c-foot" }, [h("span"), this.toggle]));
  }

  option() {
    const p = chartStyle(this.canvas);
    this.look = p;
    const { spans, lanes, from, to } = this.data;
    const colour = (cat) => p.series[Math.max(0, lanes.indexOf(cat)) % p.series.length];
    const radius = Math.min(p.barRadius, 4);
    const echarts = this.echarts;
    const long = to - from > 2 * 86400000;
    const axisLabel = { color: p.label, fontSize: p.fontSize, fontFamily: p.font };
    const gridLine = p.gridStyle === "none" ? { show: false }
      : { show: true, lineStyle: { color: p.grid, width: 1, type: p.gridStyle === "dotted" ? [1, 4] : p.gridStyle === "dashed" ? [4, 4] : "solid" } };
    return {
      animation: !prefersReducedMotion() && this.animate,
      animationDuration: 380,
      useUTC: true,
      aria: { enabled: false },
      textStyle: { fontFamily: p.font },
      grid: { left: 2, right: Math.round(p.fontSize * 1.6), top: 4, bottom: 2, containLabel: true },
      xAxis: {
        type: "time",
        min: from,
        max: to,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: gridLine,
        axisLabel: {
          ...axisLabel,
          hideOverlap: true,
          margin: Math.round(p.fontSize * 0.9),
          formatter: (v) => (long ? display("date", Math.floor(v / 1000)).replace(/ \d{4}$/, "") : hhmm(v)),
        },
      },
      yAxis: {
        type: "category",
        data: lanes,
        inverse: true,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { ...axisLabel, margin: Math.round(p.fontSize * 0.9), formatter: (v) => String(v) },
      },
      series: [{
        type: "custom",
        name: this.comp.title || "Timeline",
        encode: { x: [1, 2], y: 0 },
        data: spans.map((s) => ({ value: [lanes.indexOf(s.cat), s.start, s.end], itemStyle: { color: colour(s.cat) } })),
        renderItem: (params, api) => {
          const lane = api.value(0);
          const a = api.coord([api.value(1), lane]);
          const b = api.coord([api.value(2), lane]);
          const tall = Math.max(6, api.size([0, 1])[1] * 0.64);
          const shape = echarts.graphic.clipRectByRect(
            { x: a[0], y: a[1] - tall / 2, width: Math.max(1, b[0] - a[0]), height: tall },
            { x: params.coordSys.x, y: params.coordSys.y, width: params.coordSys.width, height: params.coordSys.height },
          );
          if (!shape) return null;
          const r = Math.min(radius, shape.width / 2, shape.height / 2);
          return { type: "rect", shape: { ...shape, r }, style: { fill: api.visual("color") }, emphasis: { style: { fill: withAlpha(api.visual("color"), 0.75) } } };
        },
      }],
    };
  }

  redraw() {
    if (!this.chart || !this.data) return;
    this.chart.setOption(this.option(), { notMerge: true });
    this.animate = false;
    this.updateLegend();
  }

  restyle() {
    if (this.chart) this.chart.resize();
    this.redraw();
  }

  spanText(s) {
    return `${hhmm(s.start)}–${hhmm(s.end)}`;
  }

  updateReadout() {
    if (!this.data) return;
    const s = this.hover >= 0 ? this.data.spans[this.hover] : null;
    if (s) {
      fill(this.readout,
        h("span", { class: "readout-value eui-value" }, h("span", { class: "fig", text: display("duration", seconds(s.end - s.start)) })),
        h("span", { class: "readout-at", text: `${s.cat} · ${this.spanText(s)}` }));
      return;
    }
    // At rest: a night (a day at most) reads from its first start to its last end; a longer period gives the
    // time of all its spans together.
    const { from, to, spans } = this.data;
    const day = to - from <= 86400000;
    const total = day ? to - from : spans.reduce((sum, sp) => sum + (sp.end - sp.start), 0);
    const when = day ? `${hhmm(from)}–${hhmm(to)}`
      : `${display("date", new Date(from).toISOString())} – ${display("date", new Date(to).toISOString())}`;
    fill(this.readout,
      h("span", { class: "readout-value eui-value" }, h("span", { class: "fig", text: display("duration", seconds(total)) })),
      h("span", { class: "readout-at", text: when }));
  }

  updateLegend() {
    if (!this.legend || !this.data) return;
    const p = this.look || chartStyle(this.canvas || this.el);
    const { lanes, totals } = this.data;
    fill(this.legend, lanes.map((cat, i) => {
      const swatch = h("span", { class: "swatch" });
      swatch.style.setProperty("--swatch", p.series[i % p.series.length]);
      return h("li", { class: "legend-item" }, [
        swatch,
        h("span", { class: "legend-name", text: cat }),
        h("span", { class: "legend-value legend-fixed", text: totals.get(cat) ? display("duration", seconds(totals.get(cat))) : NULL_TEXT }),
      ]);
    }));
  }

  updateSummary() {
    const { spans, lanes, totals, from, to } = this.data;
    const parts = [`${this.comp.title || "Timeline"}.`,
      `${spans.length} ${spans.length === 1 ? "span" : "spans"} from ${display("datetime", new Date(from).toISOString())} to ${hhmm(to)}.`,
      `${lanes.filter((c) => totals.get(c)).map((c) => `${c} ${display("duration", seconds(totals.get(c)))}`).join(", ")}.`,
      "Use View as table for every span."];
    this.canvas.setAttribute("aria-label", parts.join(" "));
  }

  toggleTable() {
    this.asTable = !this.asTable;
    this.toggle.setAttribute("aria-pressed", String(this.asTable));
    fill(this.toggle, icon(this.asTable ? "chart" : "table"), h("span", { text: this.asTable ? "View as chart" : "View as table" }));
    this.toggle.setAttribute("aria-label", `View ${this.comp.title || "timeline"} as a ${this.asTable ? "chart" : "table"}`);
    this.stage.hidden = this.asTable;
    this.tableHost.hidden = !this.asTable;
    if (this.asTable) this.drawTable();
    else if (this.chart) {
      this.chart.resize();
      this.redraw();
    }
  }

  drawTable() {
    const label = this.comp.title || "Timeline";
    const head = h("tr", null, ["Start", "End", humanize(this.comp.category), "Time"].map((t, i) =>
      h("th", { class: ["eui-th", i === 3 ? "num" : null], attrs: { scope: "col" }, text: t })));
    const body = this.data.spans.map((s) => h("tr", null, [
      h("th", { class: "eui-th", attrs: { scope: "row" }, text: display("datetime", new Date(s.start).toISOString()) }),
      h("td", { class: "eui-td", text: hhmm(s.end) }),
      h("td", { class: "eui-td", text: s.cat }),
      h("td", { class: "num eui-td", text: display("duration", seconds(s.end - s.start)) }),
    ]));
    if (this.tableCue) this.tableCue.disposeCue();
    this.tableCue = scrollCue(h("div", {
      class: "table-scroll table-scroll-tall",
      tabIndex: 0,
      attrs: { role: "region", "aria-label": `${label} as a table` },
    }, h("table", { class: "eui-table" }, [h("caption", { class: "sr-only", text: label }), h("thead", null, head), h("tbody", null, body)])));
    fill(this.tableHost, this.tableCue);
  }

  disposeContent() {
    fill(this.readout);
    if (this.tableCue) this.tableCue.disposeCue();
    this.tableCue = null;
    this.data = null;
    if (this.resizer) this.resizer.disconnect();
    this.resizer = null;
    if (this.chart) this.chart.dispose();
    this.chart = null;
    this.canvas = null;
    this.stage = null;
    this.animate = true;
    this.asTable = false;
  }

  destroy() {
    super.destroy();
    this.unsubscribeTheme();
  }
}
