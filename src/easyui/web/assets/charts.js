// line, area and bar charts, drawn by Apache ECharts (assets/vendor).
//
// A chart is drawn on a canvas, which CSS cannot reach, so it reads its look
// from the custom properties of its own element (.eui-chart): the series
// colours --eui-chart-1 ... -8, the font, label, axis and grid colours, the
// line width, the gridline style, the area opacity and the bar radius (see
// docs/STYLES.md). Setting them on one component restyles just that chart.
//
// ECharts only ever receives data and colours. No formatter strings, rich
// text or HTML from a dashboard or its data reach it: axis labels use our own
// functions, and the hover readout above each chart is built here with text
// nodes (ECharts' own tooltip box stays switched off).

import { h, fill, icon, prefersReducedMotion } from "./dom.js";
import { Frame, humanize, scrollCue } from "./components.js";
import { axisFormatter, axisStep, display, looksLikeDates, numeric, shortDate, splitUnit, NULL_TEXT } from "./format.js";
import { onThemeChange, resolveColor } from "./theme.js";

let loading = null;

/** Load ECharts once, on first use (it is large and the sign-in page does not need it). */
export function loadECharts() {
  if (window.echarts) return Promise.resolve(window.echarts);
  if (!loading) {
    loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = new URL("vendor/echarts.min.js", import.meta.url).href;
      script.async = true;
      script.addEventListener("load", () => (window.echarts ? resolve(window.echarts) : reject(new Error("no echarts"))));
      script.addEventListener("error", () => {
        loading = null;
        reject(new Error("echarts failed to load"));
      });
      document.head.append(script);
    });
  }
  return loading;
}

const GRID_STYLES = new Set(["solid", "dashed", "dotted", "none"]);

function num(text, fallback, lo, hi) {
  const n = Number.parseFloat(text);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
}

/** The look of one chart, from the custom properties of its element. */
export function chartStyle(el) {
  const cs = getComputedStyle(el);
  const get = (name) => cs.getPropertyValue(name).trim();
  const colour = (name, fallback) => resolveColor(get(name)) || fallback;
  const series = [];
  for (let i = 1; i <= 8; i += 1) series.push(colour(`--eui-chart-${i}`, "#3d6fb6"));
  const grid = get("--eui-chart-grid-style").replace(/^["']|["']$/g, "");
  const family = get("--eui-chart-font") || cs.fontFamily || "system-ui, sans-serif";
  return {
    series,
    surface: colour("--eui-surface", "#ffffff"),
    label: colour("--eui-chart-label", "#5a6472"),
    axis: colour("--eui-chart-axis", "#dde2e8"),
    grid: colour("--eui-chart-grid", "#e9ecf0"),
    soft: colour("--eui-accent-soft", "#e6edf7"),
    font: family,
    fontSize: num(get("--eui-chart-font-size"), 11, 6, 32),
    lineWidth: num(get("--eui-chart-line-width"), 2, 0, 12),
    gridStyle: GRID_STYLES.has(grid) ? grid : "solid",
    areaOpacity: num(get("--eui-chart-area-opacity"), 0.16, 0, 1),
    barRadius: num(get("--eui-chart-bar-radius"), 3, 0, 40),
  };
}

function isTrue(v) {
  return v === true || v === 1 || v === "1" || v === "true" || v === "yes";
}

export class Chart extends Frame {
  constructor(comp, ctx) {
    super(comp, ctx, "panel");
    this.chart = null;
    this.data = null;
    this.index = -1;
    this.asTable = false;
    this.readout = h("div", { class: "readout eui-readout", attrs: { "aria-hidden": "true" } });
    this.head.append(this.readout);
    this.unsubscribeTheme = onThemeChange(() => this.redraw());
  }

  requiredColumns() {
    const partial = this.comp.partial && this.comp.partial !== "last" ? [this.comp.partial] : [];
    return [this.comp.x, ...(this.comp.y || []), ...partial];
  }

  loadingView() {
    return h("div", { class: "sk-wrap", attrs: { role: "status" } }, [
      h("span", { class: "sr-only", text: "Loading" }),
      h("div", { class: "sk sk-chart" }),
    ]);
  }

  // ---- data -------------------------------------------------------------

  prepare({ columns, rows, truncated }) {
    const xi = columns.indexOf(this.comp.x);
    const xs = rows.map((r) => r[xi]);
    const series = this.comp.y.map((name) => {
      const yi = columns.indexOf(name);
      return { name, values: rows.map((r) => numeric(r[yi])) };
    });
    const dates = looksLikeDates(xs);
    const years = dates ? new Set(xs.filter(Boolean).map((x) => x.slice(0, 4))) : new Set();
    const isDateTime = dates && xs.some((x) => typeof x === "string" && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(x));
    let max = 0;
    let lo = null;
    let hi = null;
    if (this.comp.type === "bar" && this.comp.stacked) {
      for (let i = 0; i < xs.length; i += 1) {
        max = Math.max(max, series.reduce((sum, s) => sum + Math.abs(s.values[i] ?? 0), 0));
      }
      lo = 0;
      hi = max;
    } else {
      for (const s of series) {
        for (const v of s.values) {
          if (v === null) continue;
          max = Math.max(max, Math.abs(v));
          lo = lo === null ? v : Math.min(lo, v);
          hi = hi === null ? v : Math.max(hi, v);
        }
      }
    }
    // Rows of a period that is still in progress (partial: last, or a column that is true for them).
    let partial = xs.map(() => false);
    if (this.comp.partial === "last") {
      if (xs.length) partial[xs.length - 1] = true;
    } else if (this.comp.partial) {
      const pi = columns.indexOf(this.comp.partial);
      partial = rows.map((r) => isTrue(r[pi]));
    }
    return { xs, series, dates, spansYears: years.size > 1, isDateTime, max, lo, hi, partial, truncated };
  }

  xText(x) {
    if (this.data.dates) return display(this.data.isDateTime ? "datetime" : "date", x);
    return display(null, x);
  }

  /** The x label with "so far" for a period still in progress. */
  atText(i) {
    const x = this.data.xs[i];
    const text = x === null || x === undefined ? "" : this.xText(x);
    return this.data.partial[i] ? `${text} · so far` : text;
  }

  valueText(v) {
    const text = display(this.comp.format, v);
    if (text === NULL_TEXT || !this.comp.unit) return text;
    return `${text} ${this.comp.unit}`;
  }

  // ---- rendering --------------------------------------------------------

  render(result) {
    this.data = this.prepare(result);
    this.index = this.restIndex();
    if (!this.chart || !this.canvas || !this.canvas.isConnected) this.buildBody();
    this.updateReadout();
    this.updateSummary();
    if (this.asTable) this.drawTable();
    loadECharts()
      .then((echarts) => {
        if (!this.data || !this.canvas || !this.canvas.isConnected) return;
        if (!this.chart) {
          this.chart = echarts.init(this.canvas, null, { renderer: "canvas" });
          this.chart.on("updateAxisPointer", (e) => {
            const info = e && Array.isArray(e.axesInfo) ? e.axesInfo[0] : null;
            if (info && Number.isInteger(info.value) && info.value !== this.index) {
              this.index = info.value;
              this.updateReadout();
            }
          });
          this.chart.getZr().on("globalout", () => {
            this.index = this.restIndex();
            this.updateReadout();
          });
          this.resizer = new ResizeObserver(() => this.chart && this.chart.resize());
          this.resizer.observe(this.canvas);
        }
        this.redraw();
      })
      .catch(() => {
        this.setError({
          message: "The chart drawing part of this page did not load.",
          hint: "Reload the page. The numbers are still available with View as table.",
        });
      });
  }

  /**
   * What the readout shows when the pointer is not on the chart: the latest
   * point for a date axis; nothing for categories ("walk" is not "the latest").
   */
  restIndex() {
    if ((this.comp.readout || "latest") !== "latest") return -1;
    return this.data.dates ? this.lastIndex() : -1;
  }

  lastIndex() {
    const { xs, series } = this.data;
    for (let i = xs.length - 1; i >= 0; i -= 1) {
      if (series.some((s) => s.values[i] !== null)) return i;
    }
    return xs.length - 1;
  }

  buildBody() {
    const label = this.comp.title || "Chart";
    this.canvas = h("div", { class: "chart eui-chart", attrs: { role: "img" } });
    this.legend = h("ul", { class: "legend eui-legend", attrs: { "aria-hidden": "true" } });
    this.tableHost = h("div", { class: "chart-table", hidden: true });
    this.note = h("p", { class: "c-note", hidden: true });
    this.toggle = h("button", {
      class: "btn-quiet btn-toggle",
      type: "button",
      attrs: { "aria-pressed": "false", "aria-label": `View ${label} as a table` },
      on: { click: () => this.toggleTable() },
    }, [icon("table"), h("span", { text: "View as table" })]);
    fill(this.body, this.legend, this.canvas, this.tableHost, h("div", { class: "c-foot" }, [this.note, this.toggle]));
  }

  option() {
    const p = chartStyle(this.canvas);
    this.look = p;
    const { xs, series, dates, spansYears, max } = this.data;
    const type = this.comp.type;
    const bar = type === "bar";
    const motion = !prefersReducedMotion();
    const axisLabel = { color: p.label, fontSize: p.fontSize, fontFamily: p.font, margin: 10 };
    const yFmt = axisFormatter(this.comp.format, max);
    const unitName = this.comp.unit || null;
    const partial = this.data.partial;
    const anyPartial = partial.some(Boolean);
    return {
      animation: motion,
      animationDuration: 450,
      animationDurationUpdate: 250,
      animationEasing: "cubicOut",
      textStyle: { fontFamily: p.font },
      color: p.series,
      // Room to the right for the last x label: centred under the last bar, and with some fonts
      // wider than the right-aligned space a line chart leaves it (it was clipped: "28 Ap").
      grid: { left: 4, right: Math.round(p.fontSize * 2), top: unitName ? 28 : 14, bottom: 4, containLabel: true },
      tooltip: {
        trigger: "axis",
        showContent: false,
        axisPointer: bar
          ? { type: "shadow", shadowStyle: { color: p.soft, opacity: 0.6 } }
          : { type: "line", lineStyle: { color: p.label, width: 1, type: "solid", opacity: 0.5 } },
      },
      xAxis: {
        type: "category",
        data: xs.map((x) => (x === null || x === undefined ? "" : String(x))),
        boundaryGap: bar,
        axisLine: { lineStyle: { color: p.axis } },
        axisTick: { show: false },
        axisLabel: {
          ...axisLabel,
          hideOverlap: true,
          alignMaxLabel: bar ? null : "right",
          formatter: dates ? (v) => shortDate(v, spansYears) : (v) => String(v),
        },
      },
      yAxis: {
        type: "value",
        scale: type === "line",
        splitNumber: 3,
        // The unit, once, above the axis ("bpm"), rather than on every label.
        name: unitName,
        nameLocation: "end",
        nameGap: 12,
        nameTextStyle: { ...axisLabel, align: "left", padding: [0, 0, 0, -4] },
        ...this.stepOptions(type),
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: p.gridStyle === "none"
          ? { show: false }
          // "dotted" as an explicit, sparser pattern: ECharts' own dots are so close that a faint
          // grid colour reads as a solid hairline.
          : { lineStyle: { color: p.grid, width: 1, type: p.gridStyle === "dotted" ? [1, 4] : p.gridStyle } },
        axisLabel: { ...axisLabel, formatter: (v) => yFmt(v) },
      },
      series: series.flatMap((s, i) => {
        const color = p.series[i % p.series.length];
        const base = {
          name: s.name,
          data: s.values,
          color,
        };
        if (bar) {
          // Bars of a period still in progress are drawn lighter.
          if (anyPartial) base.data = s.values.map((v, j) => (partial[j] ? { value: v, itemStyle: { opacity: 0.4 } } : v));
          return {
            ...base,
            type: "bar",
            stack: this.comp.stacked ? "total" : undefined,
            barMaxWidth: 22,
            barCategoryGap: "35%",
            large: s.values.length > 1500,
            emphasis: { disabled: true },
            itemStyle: { color, borderRadius: this.comp.stacked && i < series.length - 1 ? 0 : [p.barRadius, p.barRadius, 0, 0] },
          };
        }
        const line = {
          ...base,
          type: "line",
          showSymbol: false,
          symbol: "circle",
          symbolSize: 7,
          connectNulls: false,
          sampling: s.values.length > 800 ? "lttb" : undefined,
          lineStyle: { width: p.lineWidth, color, cap: "round", join: "round" },
          emphasis: { focus: "none", scale: false, lineStyle: { width: p.lineWidth } },
          itemStyle: { color, borderColor: p.surface, borderWidth: 2 },
          areaStyle: type === "area" ? { color, opacity: series.length > 1 ? p.areaOpacity * 0.6 : p.areaOpacity } : undefined,
        };
        if (!anyPartial) return line;
        // A period still in progress: the line into it is dashed and its point is an open circle.
        const solid = s.values.map((v, j) => (partial[j] ? null : v));
        const dashed = s.values.map((v, j) => {
          if (partial[j]) return { value: v, symbol: "circle", symbolSize: 7, itemStyle: { color: p.surface, borderColor: color, borderWidth: 2 } };
          if (j + 1 < partial.length && partial[j + 1]) return { value: v, symbol: "none" };
          return null;
        });
        return [
          { ...line, data: solid },
          {
            ...line,
            name: `${s.name} (so far)`,
            data: dashed,
            showSymbol: true,
            sampling: undefined,
            lineStyle: { ...line.lineStyle, type: [4, 4] },
            areaStyle: type === "area" ? { color, opacity: p.areaOpacity * 0.4 } : undefined,
          },
        ];
      }),
    };
  }

  // The step setting, or round ticks for times and distances (0, 30 min, 1 h; 0, 20 km, 40 km), with the ends
  // of the axis on whole steps. Line charts without a step keep their own zoomed-in scale.
  stepOptions(type) {
    const { max, lo, hi } = this.data;
    const chosen = Number(this.comp.step) > 0 ? Number(this.comp.step) : null;
    const step = chosen || (type === "line" ? null : axisStep(this.comp.format, max));
    // Whole numbers ("integer") never get ticks such as 0.5.
    const whole = this.comp.format === "integer" ? { minInterval: 1 } : {};
    if (!step || lo === null) return whole;
    const fromZero = type !== "line";
    const bottom = fromZero ? Math.min(0, Math.floor(lo / step) * step) : Math.floor(lo / step) * step;
    const top = Math.max(bottom + step, Math.ceil(hi / step) * step);
    if ((top - bottom) / step > 40) return {}; // a step far too small for these values: let the chart choose
    return { interval: step, min: bottom, max: top };
  }

  redraw() {
    if (!this.chart || !this.data) return;
    this.chart.setOption(this.option(), { notMerge: true });
    this.updateLegend();
  }

  /** The page's styles changed (a stylesheet loaded, the theme changed): read the look again. */
  restyle() {
    if (this.chart) this.chart.resize();
    this.redraw();
  }

  updateLegend() {
    if (!this.legend) return;
    const p = this.look || chartStyle(this.canvas || this.el);
    const many = this.data.series.length > 1;
    this.legend.hidden = !many;
    if (!many) return;
    fill(this.legend, this.data.series.map((s, i) => {
      const swatch = h("span", { class: "swatch" });
      swatch.style.setProperty("--swatch", p.series[i % p.series.length]);
      return h("li", { class: "legend-item" }, [
        swatch,
        h("span", { class: "legend-name", text: s.name }),
        h("span", { class: "legend-value", text: this.index >= 0 ? this.valueText(s.values[this.index] ?? null) : "" }),
      ]);
    }));
  }

  updateReadout() {
    if (!this.data) return;
    const { xs, series } = this.data;
    if (this.index < 0) {
      fill(this.readout);
      if (this.legend) {
        for (const v of this.legend.querySelectorAll(".legend-value")) v.textContent = "";
      }
      return;
    }
    if ((this.comp.readout || "latest") === "none") {
      fill(this.readout);
      return;
    }
    const at = this.atText(this.index);
    if (series.length === 1) {
      const text = display(this.comp.format, series[0].values[this.index] ?? null);
      let [num, unit] = splitUnit(text);
      if (this.comp.unit && text !== NULL_TEXT) unit = unit ? `${unit} ${this.comp.unit}` : this.comp.unit;
      fill(this.readout,
        h("span", { class: "readout-value eui-value" }, [h("span", { class: "fig", text: num }), unit ? h("span", { class: "unit eui-unit", text: unit }) : null]),
        h("span", { class: "readout-at", text: at }));
    } else {
      fill(this.readout, h("span", { class: "readout-at", text: at }));
      if (this.legend) {
        const values = this.legend.querySelectorAll(".legend-value");
        series.forEach((s, i) => {
          if (values[i]) values[i].textContent = this.valueText(s.values[this.index] ?? null);
        });
      }
    }
  }

  updateSummary() {
    if (!this.canvas) return;
    const { xs, series, truncated } = this.data;
    const last = this.lastIndex();
    const parts = [`${this.comp.title || "Chart"}.`];
    if (xs.length) {
      parts.push(`${xs.length} ${xs.length === 1 ? "value" : "values"} from ${this.xText(xs[0])} to ${this.xText(xs[xs.length - 1])}.`);
      const latest = series.map((s) => `${series.length > 1 ? `${s.name} ` : ""}${this.valueText(s.values[last] ?? null)}`).join(", ");
      parts.push(`Latest, ${this.atText(last)}: ${latest}.`);
    }
    parts.push("Use View as table for every value.");
    this.canvas.setAttribute("aria-label", parts.join(" "));
    this.note.textContent = truncated ? "Only the first part of the data is drawn, because there is a lot of it." : "";
    this.note.hidden = !truncated;
  }

  toggleTable() {
    this.asTable = !this.asTable;
    this.toggle.setAttribute("aria-pressed", String(this.asTable));
    fill(this.toggle, icon(this.asTable ? "chart" : "table"), h("span", { text: this.asTable ? "View as chart" : "View as table" }));
    this.toggle.setAttribute("aria-label", `View ${this.comp.title || "chart"} as a ${this.asTable ? "chart" : "table"}`);
    this.canvas.hidden = this.asTable;
    this.legend.classList.toggle("is-off", this.asTable);
    this.tableHost.hidden = !this.asTable;
    if (this.asTable) this.drawTable();
    else if (this.chart) this.chart.resize();
  }

  drawTable() {
    const { xs, series, dates } = this.data;
    const label = this.comp.title || "Chart";
    const head = h("tr", null, [
      h("th", { class: "eui-th", attrs: { scope: "col" }, text: humanize(this.comp.x) }),
      ...series.map((s) => h("th", { class: "num eui-th", attrs: { scope: "col" }, text: s.name })),
    ]);
    // Dates newest first, like every other list of recent things; categories in the query's order.
    const order = xs.map((_, i) => i);
    if (dates) order.reverse();
    const body = order.map((i) => h("tr", null, [
      h("th", { class: "eui-th", attrs: { scope: "row" }, text: this.atText(i) }),
      ...series.map((s) => h("td", { class: "num eui-td", text: this.valueText(s.values[i]) })),
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
    this.asTable = false;
  }

  destroy() {
    super.destroy();
    this.unsubscribeTheme();
  }
}
