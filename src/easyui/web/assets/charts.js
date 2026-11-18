// line, area and bar charts, drawn by Apache ECharts (assets/vendor) as SVG.
//
// SVG rather than a canvas: the browser draws it at the screen's own density at every zoom level and after
// every resize, on any screen a window moves to, with the page's own fonts, so charts are never blurred by a
// bitmap made for another pixel ratio. ECharts sets its few styles through the style object, never through a
// style attribute, so the page's policy (no inline styles) holds.
//
// A chart reads its look from the custom properties of its own element (.eui-chart): the series colours
// --eui-chart-1 ... -8, the font, label, axis, grid and reference colours, the line width, the gridline
// style, the area and band opacity and the bar radius (see docs/STYLES.md). Setting them on one component
// restyles just that chart.
//
// ECharts only ever receives data and colours. No formatter strings, rich text or HTML from a dashboard or
// its data reach it: labels use our own functions, and the hover readout above each chart and the hover
// card are built here with text nodes (ECharts' own tooltip box stays switched off).

import { h, fill, icon, prefersReducedMotion } from "./dom.js";
import { Frame, humanize, scrollCue } from "./components.js";
import { axisFormatter, display, looksLikeDates, numeric, shortDate, splitUnit, NULL_TEXT } from "./format.js";
import { onThemeChange, resolveColor } from "./theme.js";
import { dateTicks, fillDateGaps, valueRange } from "./chartmath.js";

let loading = null;
// The hash of vendor/echarts.min.js (also in vendor/MANIFEST.json; tests/test_web_static.py checks both),
// so the browser refuses a changed file. A page opened straight from the disk (file://) cannot check it
// (each file is its own origin there), so it is set only for pages served over http(s).
export const ECHARTS_INTEGRITY = "sha384-C2iskrW/uPW46KzOjrvJIQo4YkV8lkD+QS0CrDN18IIPIpT/g2USu8bTP3nvmIAD";
export const RENDERER = "svg";

/** Load ECharts once, on first use (it is large and the sign-in page does not need it). */
export function loadECharts() {
  if (window.echarts) return Promise.resolve(window.echarts);
  if (!loading) {
    loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = new URL("vendor/echarts.min.js", import.meta.url).href;
      if (window.location.protocol !== "file:") script.integrity = ECHARTS_INTEGRITY;
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

/** "#rrggbb" or "rgba(...)" with its opacity multiplied by alpha. */
export function withAlpha(colour, alpha) {
  const a = Math.max(0, Math.min(1, alpha));
  const hex = /^#([0-9a-f]{6})$/i.exec(colour || "");
  if (hex) {
    const n = parseInt(hex[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a.toFixed(3)})`;
  }
  const rgba = /^rgba\((\d+), (\d+), (\d+), ([\d.]+)\)$/.exec(colour || "");
  if (rgba) return `rgba(${rgba[1]}, ${rgba[2]}, ${rgba[3]}, ${(Number(rgba[4]) * a).toFixed(3)})`;
  return colour;
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
    reference: colour("--eui-chart-reference", "#5a6472"),
    soft: colour("--eui-accent-soft", "#e6edf7"),
    font: family,
    fontSize: num(get("--eui-chart-font-size"), 11, 6, 32),
    lineWidth: num(get("--eui-chart-line-width"), 2, 0, 12),
    gridStyle: GRID_STYLES.has(grid) ? grid : "solid",
    areaOpacity: num(get("--eui-chart-area-opacity"), 0.16, 0, 1),
    bandOpacity: num(get("--eui-chart-band-opacity"), 0.14, 0, 1),
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
    this.animate = true;
    this.readout = h("div", { class: "readout eui-readout", attrs: { "aria-hidden": "true" } });
    this.head.append(this.readout);
    this.unsubscribeTheme = onThemeChange(() => this.redraw());
  }

  requiredColumns() {
    const c = this.comp;
    const partial = c.partial && c.partial !== "last" ? [c.partial] : [];
    const band = c.band ? [c.band.low, c.band.high] : [];
    const ref = c.reference && c.reference.column ? [c.reference.column] : [];
    return [c.x, ...(c.y || []), ...partial, ...band, ...ref, c.colors_by];
  }

  loadingView() {
    return h("div", { class: "sk-wrap", attrs: { role: "status" } }, [
      h("span", { class: "sr-only", text: "Loading" }),
      h("div", { class: "sk sk-chart" }),
    ]);
  }

  // ---- data -------------------------------------------------------------

  prepare({ columns, rows, truncated }) {
    const c = this.comp;
    const col = (name) => columns.indexOf(name);
    const xi = col(c.x);
    let xs = rows.map((r) => r[xi]);
    // Missing days (or weeks) become empty points, so a line stops there instead of joining across.
    const filled = fillDateGaps(xs);
    const rowOf = filled ? filled.rowOf : xs.map((_, i) => i);
    if (filled) xs = filled.xs;
    const pick = (ci) => rowOf.map((r) => (r < 0 ? null : numeric(rows[r][ci])));
    const series = c.y.map((name) => ({ name, values: pick(col(name)) }));
    const band = c.band ? { low: pick(col(c.band.low)), high: pick(col(c.band.high)), label: c.band.label || null } : null;
    let reference = null;
    if (c.reference) {
      let value = typeof c.reference.value === "number" ? c.reference.value : null;
      if (c.reference.column) {
        const ri = col(c.reference.column);
        const found = rows.find((r) => numeric(r[ri]) !== null);
        value = found ? numeric(found[ri]) : null;
      }
      if (value !== null) reference = { value, label: c.reference.label || null };
    }
    // colors_by: each point's category, and the categories in the order they first appear.
    let groups = null;
    if (c.colors_by && col(c.colors_by) >= 0) {
      const gi = col(c.colors_by);
      const of = rowOf.map((r) => (r < 0 ? null : rows[r][gi]));
      const names = [];
      for (const g of of) if (g !== null && g !== undefined && !names.includes(String(g))) names.push(String(g));
      groups = { of: of.map((g) => (g === null || g === undefined ? -1 : names.indexOf(String(g)))), names };
    }
    const dates = looksLikeDates(xs);
    const years = dates ? new Set(xs.filter(Boolean).map((x) => String(x).slice(0, 4))) : new Set();
    const isDateTime = dates && xs.some((x) => typeof x === "string" && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(x));
    let lo = null;
    let hi = null;
    let max = 0;
    const see = (v) => {
      if (v === null || v === undefined) return;
      max = Math.max(max, Math.abs(v));
      lo = lo === null ? v : Math.min(lo, v);
      hi = hi === null ? v : Math.max(hi, v);
    };
    if (c.type === "bar" && c.stacked) {
      for (let i = 0; i < xs.length; i += 1) {
        let up = null;
        let down = null;
        for (const s of series) {
          const v = s.values[i];
          if (v > 0) up = (up || 0) + v;
          else if (v !== null && v <= 0) down = (down || 0) + v;
        }
        see(up);
        see(down);
      }
    } else {
      for (const s of series) for (const v of s.values) see(v);
    }
    const allNull = lo === null;
    if (band) for (const k of ["low", "high"]) for (const v of band[k]) see(v);
    if (reference) see(reference.value);
    // Rows of a period that is still in progress (partial: last, or a column that is true for them).
    let partial = xs.map(() => false);
    if (c.partial === "last") {
      const lastRow = rowOf.lastIndexOf(rows.length - 1);
      if (lastRow >= 0) partial[lastRow] = true;
    } else if (c.partial) {
      const pi = col(c.partial);
      partial = rowOf.map((r) => r >= 0 && isTrue(rows[r][pi]));
    }
    return {
      xs, rowOf, rows: rows.length, series, band, reference, groups, dates, spansYears: years.size > 1, isDateTime,
      max, lo, hi, partial, truncated, allNull,
    };
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

  emptyTitle() {
    return this.allNull ? "No values to draw" : super.emptyTitle();
  }

  emptyHint() {
    if (this.allNull) {
      return this.ctx.queryHasControls(this.comp.query)
        ? "There are dates here, but none of them has a value. Try a wider range in the settings above."
        : "There are dates here, but none of them has a value yet. It will show here after the next data update.";
    }
    return super.emptyHint();
  }

  render(result) {
    const data = this.prepare(result);
    this.allNull = data.allNull;
    if (data.allNull) {
      this.showEmpty();
      return;
    }
    this.data = data;
    this.index = this.restIndex();
    if (!this.chart || !this.canvas || !this.canvas.isConnected) this.buildBody();
    this.updateReadout();
    this.updateSummary();
    if (this.asTable) this.drawTable();
    loadECharts()
      .then((echarts) => {
        if (!this.data || !this.canvas || !this.canvas.isConnected) return;
        if (!this.chart) {
          this.chart = echarts.init(this.canvas, null, { renderer: RENDERER });
          this.chart.on("updateAxisPointer", (e) => {
            const info = e && Array.isArray(e.axesInfo) ? e.axesInfo[0] : null;
            if (info && Number.isInteger(info.value)) {
              if (info.value !== this.index) {
                this.index = info.value;
                this.updateReadout();
              }
              this.showCard(info.value);
            }
          });
          this.chart.getZr().on("globalout", () => {
            this.index = this.restIndex();
            this.updateReadout();
            this.hideCard();
          });
          this.width = this.canvas.clientWidth;
          this.height = this.canvas.clientHeight;
          this.resizer = new ResizeObserver(() => this.resized());
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

  /** The chart's box changed size (a window resize, a zoom, a turned phone): fit it and lay the labels out again. */
  resized() {
    if (!this.chart || !this.canvas) return;
    const w = this.canvas.clientWidth;
    const hgt = this.canvas.clientHeight;
    if (!w || !hgt || (w === this.width && hgt === this.height)) return;
    this.width = w;
    this.height = hgt;
    this.chart.resize();
    this.redraw();
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
    this.card = h("div", { class: "chart-card eui-chart-card", attrs: { "aria-hidden": "true" }, hidden: true });
    this.stage = h("div", { class: "chart-stage" }, [this.canvas, this.card]);
    this.legend = h("ul", { class: "legend eui-legend", attrs: { "aria-hidden": "true" } });
    this.tableHost = h("div", { class: "chart-table", hidden: true });
    this.note = h("p", { class: "c-note", hidden: true });
    this.toggle = h("button", {
      class: "btn-quiet btn-toggle",
      type: "button",
      attrs: { "aria-pressed": "false", "aria-label": `View ${label} as a table` },
      on: { click: () => this.toggleTable() },
    }, [icon("table"), h("span", { text: "View as table" })]);
    fill(this.body, this.legend, this.stage, this.tableHost, h("div", { class: "c-foot" }, [this.note, this.toggle]));
  }

  /** How many gaps the value axis gets: fewer on a short chart. */
  tickCount() {
    const hgt = (this.canvas && this.canvas.clientHeight) || 240;
    return hgt < 210 ? 3 : hgt < 330 ? 4 : 5;
  }

  /** Labels of a date axis: which points get one, and its text. */
  xLabels(p) {
    const { xs, dates } = this.data;
    if (!dates) return null;
    const width = Math.max(80, ((this.canvas && this.canvas.clientWidth) || 400) - p.fontSize * 5);
    return dateTicks(xs, width, p.fontSize * 0.6);
  }

  option() {
    const p = chartStyle(this.canvas);
    this.look = p;
    const { xs, series, dates, spansYears, max, band, reference, groups } = this.data;
    const dashed = new Set(this.comp.dashed || []);
    const type = this.comp.type;
    const bar = type === "bar";
    const motion = !prefersReducedMotion() && this.animate;
    const fontSize = p.fontSize;
    const axisLabel = { color: p.label, fontSize, fontFamily: p.font };
    const unitName = this.comp.unit || null;
    const partial = this.data.partial;
    const anyPartial = partial.some(Boolean);
    const range = valueRange({
      lo: this.data.lo,
      hi: this.data.hi,
      ticks: this.tickCount(),
      zero: bar || type === "area",
      step: Number(this.comp.step) > 0 ? Number(this.comp.step) : null,
      format: this.comp.format,
    });
    const yFmt = axisFormatter(this.comp.format, range ? Math.max(Math.abs(range.min), Math.abs(range.max)) : max);
    const ticks = this.xLabels(p);
    // ECharts sizes the room for the labels from a sample of them, also from points that get none: those
    // answer with the longest label, so the room is always there (only the chosen ones are drawn).
    const longest = ticks ? [...ticks.labels.values()].reduce((a, b) => (b.length > a.length ? b : a), "") : "";
    const xLabel = ticks
      ? { interval: (i) => ticks.labels.has(i), formatter: (v, i) => ticks.labels.get(i) || longest }
      : { interval: "auto", formatter: dates ? (v) => shortDate(v, spansYears) : (v) => String(v) };
    const gridLine = p.gridStyle === "none"
      ? { show: false }
      // "dotted" as an explicit, sparser pattern: ECharts' own dots are so close that a faint
      // grid colour reads as a solid hairline.
      : { show: true, lineStyle: { color: p.grid, width: 1, type: p.gridStyle === "dotted" ? [1, 4] : p.gridStyle === "dashed" ? [4, 4] : "solid" } };
    const out = {
      animation: motion,
      animationDuration: 380,
      animationDurationUpdate: 220,
      animationEasing: "cubicOut",
      animationEasingUpdate: "cubicOut",
      aria: { enabled: false },
      textStyle: { fontFamily: p.font },
      color: p.series,
      // Room to the right for the last x label (centred under the last bar), and above for the unit.
      grid: {
        left: 2,
        right: Math.round(fontSize * 1.6),
        top: unitName ? Math.round(fontSize * 2.4) : Math.round(fontSize * 0.8),
        bottom: 2,
        containLabel: true,
      },
      tooltip: {
        trigger: "axis",
        showContent: false,
        axisPointer: bar
          // A faint column behind the bars under the pointer (never over them).
          ? { type: "shadow", z: 0, shadowStyle: { color: withAlpha(p.label, 0.09) } }
          : { type: "line", lineStyle: { color: p.label, width: 1, type: "solid", opacity: 0.45 } },
      },
      xAxis: {
        type: "category",
        data: xs.map((x) => (x === null || x === undefined ? "" : String(x))),
        boundaryGap: bar,
        // Bars stand on a baseline; lines need none (the lowest gridline is there).
        axisLine: { show: bar, lineStyle: { color: p.axis, width: 1 } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: {
          ...axisLabel,
          margin: Math.round(fontSize * 0.9),
          hideOverlap: true,
          interval: xLabel.interval,
          formatter: (v, i) => xLabel.formatter(v, i),
        },
      },
      yAxis: {
        type: "value",
        // The unit, once, above the axis ("bpm"), rather than on every label.
        name: unitName,
        nameLocation: "end",
        nameGap: Math.round(fontSize * 1.1),
        nameTextStyle: { ...axisLabel, align: "left", padding: [0, 0, 0, -2] },
        ...(range ? { min: range.min, max: range.max, interval: range.interval } : {}),
        ...(this.comp.format === "integer" ? { minInterval: 1 } : {}),
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: gridLine,
        axisLabel: { ...axisLabel, margin: Math.round(fontSize * 0.9), formatter: (v) => yFmt(v) },
      },
      series: [],
    };
    // A band behind the first series (a usual range): an invisible lower line and the gap to the upper one
    // filled, stacked on it.
    if (band) {
      const colour = p.series[0];
      const both = band.low.map((lo, i) => (lo === null || band.high[i] === null ? null : [lo, band.high[i]]));
      const bandBase = {
        type: "line", stack: "eui-band", symbol: "none", silent: true, connectNulls: false, z: 1,
        lineStyle: { opacity: 0, width: 0 }, emphasis: { disabled: true }, animation: motion,
      };
      out.series.push(
        { ...bandBase, name: "band-low", data: both.map((v) => (v ? Math.min(v[0], v[1]) : null)) },
        {
          ...bandBase,
          name: "band-high",
          data: both.map((v) => (v ? Math.abs(v[1] - v[0]) : null)),
          areaStyle: { color: withAlpha(colour, p.bandOpacity), opacity: 1 },
        },
      );
    }
    const stackTop = bar && this.comp.stacked ? this.stackTops() : null;
    series.forEach((s, i) => {
      const color = p.series[i % p.series.length];
      const base = { name: s.name, data: s.values, color, z: 3 };
      if (bar) {
        const r = p.barRadius;
        const many = s.values.length > 1500;
        const radius = (v, j) => {
          if (stackTop && stackTop[j] !== i) return 0;
          return v < 0 ? [0, 0, r, r] : [r, r, 0, 0];
        };
        const data = many && !groups ? s.values : s.values.map((v, j) => {
          if (v === null) return null;
          const itemStyle = { borderRadius: radius(v, j) };
          if (groups && groups.of[j] >= 0) itemStyle.color = p.series[groups.of[j] % p.series.length];
          // A period still in progress: a lighter bar with a dashed edge.
          if (partial[j]) {
            const own = itemStyle.color || color;
            Object.assign(itemStyle, { color: withAlpha(own, 0.38), borderColor: own, borderWidth: 1, borderType: [3, 2] });
          }
          return { value: v, itemStyle };
        });
        out.series.push({
          ...base,
          data,
          type: "bar",
          stack: this.comp.stacked ? "total" : undefined,
          barMaxWidth: series.length > 1 && !this.comp.stacked ? 20 : xs.length <= 12 ? 64 : 36,
          barCategoryGap: xs.length > 60 ? "22%" : "34%",
          barGap: "14%",
          large: many && !groups,
          emphasis: { disabled: true },
          itemStyle: { color },
        });
        return;
      }
      const lw = p.lineWidth;
      const line = {
        ...base,
        type: "line",
        showSymbol: false,
        symbol: "circle",
        symbolSize: Math.max(6, lw * 3 + 1),
        connectNulls: false,
        smooth: false,
        sampling: s.values.length > 1200 ? "lttb" : undefined,
        // dashed: an average or a forecast beside the measured values.
        lineStyle: { width: lw, color, cap: dashed.has(s.name) ? "butt" : "round", join: "round", type: dashed.has(s.name) ? [Math.max(4, lw * 2.5), Math.max(3, lw * 2)] : "solid" },
        emphasis: { focus: "none", scale: false, lineStyle: { width: lw } },
        itemStyle: { color, borderColor: p.surface, borderWidth: Math.max(1.5, lw * 0.8) },
        areaStyle: type === "area" ? { color: this.fade(color, series.length > 1 ? p.areaOpacity * 0.7 : p.areaOpacity) } : undefined,
      };
      if (i === 0 && reference) line.markLine = this.referenceLine(p, reference);
      const lone = this.lonePoints(s.values);
      const dots = lone.some((v) => v !== null)
        ? [{
          type: "scatter", name: `${s.name} (single)`, data: lone, symbolSize: Math.max(4, lw * 2 + 1), silent: true,
          itemStyle: { color }, z: 4, animation: motion,
        }]
        : [];
      if (!anyPartial) {
        out.series.push(line, ...dots);
        return;
      }
      // A period still in progress: the line into it is dashed and its point is an open circle.
      const solid = s.values.map((v, j) => (partial[j] ? null : v));
      const soFar = s.values.map((v, j) => {
        if (partial[j]) {
          return { value: v, symbol: "circle", symbolSize: Math.max(6, lw * 3 + 1), itemStyle: { color: p.surface, borderColor: color, borderWidth: Math.max(1.5, lw) } };
        }
        if (j + 1 < partial.length && partial[j + 1]) return { value: v, symbol: "none" };
        return null;
      });
      out.series.push(
        { ...line, data: solid },
        {
          ...line,
          name: `${s.name} (so far)`,
          data: soFar,
          markLine: undefined,
          showSymbol: true,
          sampling: undefined,
          lineStyle: { ...line.lineStyle, type: [4, 4] },
          areaStyle: type === "area" ? { color: this.fade(color, p.areaOpacity * 0.5) } : undefined,
        },
        ...dots,
      );
    });
    if (bar && reference) {
      const first = out.series.find((s) => s.type === "bar");
      if (first) first.markLine = this.referenceLine(p, reference);
    }
    return out;
  }

  /** A soft vertical fade under a line: the colour at `opacity` at the top, almost nothing at the bottom. */
  fade(color, opacity) {
    return {
      type: "linear", x: 0, y: 0, x2: 0, y2: 1,
      colorStops: [{ offset: 0, color: withAlpha(color, opacity) }, { offset: 1, color: withAlpha(color, opacity * 0.08) }],
    };
  }

  /** A goal or an average across the chart: a thin dashed line with its label at the right end. */
  referenceLine(p, ref) {
    const text = ref.label ? `${ref.label} ${this.valueText(ref.value)}` : this.valueText(ref.value);
    return {
      silent: true,
      symbol: "none",
      animation: false,
      precision: 6,
      lineStyle: { color: p.reference, width: 1, type: [5, 4], opacity: 0.9 },
      label: {
        show: true,
        position: "insideEndTop",
        distance: [0, 3],
        padding: [2, 5],
        borderRadius: 3,
        // On the panel's colour, so the label stays readable where a line crosses it.
        backgroundColor: withAlpha(p.surface, 0.88),
        color: p.reference,
        fontSize: p.fontSize,
        fontFamily: p.font,
        formatter: () => text,
      },
      emphasis: { disabled: true },
      data: [{ yAxis: ref.value }],
    };
  }

  /** Points with no neighbour on either side: a line cannot show them, so they get a dot. */
  lonePoints(values) {
    return values.map((v, j) => {
      if (v === null) return null;
      const before = j > 0 ? values[j - 1] : null;
      const after = j + 1 < values.length ? values[j + 1] : null;
      return before === null && after === null ? v : null;
    });
  }

  /** For stacked bars: the series on top of each stack (the one that gets the rounded corners). */
  stackTops() {
    const { series, xs } = this.data;
    return xs.map((_, j) => {
      let top = -1;
      series.forEach((s, i) => {
        if (s.values[j] !== null && s.values[j] !== 0) top = i;
      });
      return top;
    });
  }

  redraw() {
    if (!this.chart || !this.data) return;
    this.chart.setOption(this.option(), { notMerge: true });
    this.animate = false; // only the first drawing grows in; later ones (a resize, a new theme) are at once
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
    const { band, reference, groups } = this.data;
    const many = this.data.series.length > 1 || Boolean(band) || Boolean(reference) || Boolean(groups && groups.names.length > 1);
    this.legend.hidden = !many;
    if (!many) return;
    this.legend.dataset.kind = this.comp.type;
    const dashed = new Set(this.comp.dashed || []);
    if (groups) {
      // One colour per category: the legend names them (the value is in the heading).
      const items = groups.names.map((name, i) => {
        const swatch = h("span", { class: "swatch" });
        swatch.style.setProperty("--swatch", p.series[i % p.series.length]);
        return h("li", { class: "legend-item" }, [swatch, h("span", { class: "legend-name", text: name })]);
      });
      if (reference) items.push(this.referenceItem(p, reference));
      fill(this.legend, items);
      return;
    }
    const items = this.data.series.map((s, i) => {
      const swatch = h("span", { class: ["swatch", dashed.has(s.name) ? "swatch-dashed" : null] });
      swatch.style.setProperty("--swatch", p.series[i % p.series.length]);
      return h("li", { class: "legend-item" }, [
        swatch,
        h("span", { class: "legend-name", text: s.name }),
        h("span", { class: "legend-value", text: this.index >= 0 ? this.valueText(s.values[this.index] ?? null) : "" }),
      ]);
    });
    if (band) {
      const swatch = h("span", { class: "swatch swatch-band" });
      swatch.style.setProperty("--swatch", withAlpha(p.series[0], Math.min(1, Math.max(0.28, p.bandOpacity * 2))));
      items.push(h("li", { class: "legend-item legend-band" }, [swatch, h("span", { class: "legend-name", text: this.bandLabel() })]));
    }
    if (reference) items.push(this.referenceItem(p, reference));
    fill(this.legend, items);
  }

  referenceItem(p, reference) {
    const swatch = h("span", { class: "swatch swatch-reference" });
    swatch.style.setProperty("--swatch", p.reference);
    return h("li", { class: "legend-item legend-reference" }, [
      swatch,
      h("span", { class: "legend-name", text: reference.label || "Reference" }),
      h("span", { class: "legend-value legend-fixed", text: this.valueText(reference.value) }),
    ]);
  }

  bandLabel() {
    return (this.data.band && this.data.band.label) || "Usual range";
  }

  bandText(i) {
    const { band } = this.data;
    if (!band || band.low[i] === null || band.high[i] === null) return "";
    const lo = Math.min(band.low[i], band.high[i]);
    const hi = Math.max(band.low[i], band.high[i]);
    return `${this.bandLabel()} ${display(this.comp.format, lo)}–${this.valueText(hi)}`;
  }

  updateReadout() {
    if (!this.data) return;
    const { series } = this.data;
    if (this.index < 0) {
      fill(this.readout);
      if (this.legend) {
        for (const v of this.legend.querySelectorAll(".legend-value:not(.legend-fixed)")) v.textContent = "";
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
      const band = this.bandText(this.index);
      fill(this.readout,
        h("span", { class: "readout-value eui-value" }, [h("span", { class: "fig", text: num }), unit ? h("span", { class: "unit eui-unit", text: unit }) : null]),
        h("span", { class: "readout-at", text: at }),
        band ? h("span", { class: "readout-band", text: band }) : null);
    } else {
      fill(this.readout, h("span", { class: "readout-at", text: at }));
      if (this.legend) {
        const values = this.legend.querySelectorAll(".legend-value:not(.legend-fixed)");
        series.forEach((s, i) => {
          if (values[i]) values[i].textContent = this.valueText(s.values[this.index] ?? null);
        });
      }
    }
  }

  /**
   * The hover card for a chart of several series: the date and each series' value, beside the pointer,
   * styled like the page's panels. A chart of one series has its value in the heading instead.
   */
  showCard(i) {
    if (!this.card || !this.chart || !this.data || this.data.series.length < 2) return;
    const p = this.look || chartStyle(this.canvas);
    const { series } = this.data;
    const dashed = new Set(this.comp.dashed || []);
    const rows = series.map((s, k) => {
      const swatch = h("span", { class: ["swatch", dashed.has(s.name) ? "swatch-dashed" : null] });
      swatch.style.setProperty("--swatch", p.series[k % p.series.length]);
      return h("div", { class: "card-row" }, [swatch, h("span", { class: "card-name", text: s.name }),
        h("span", { class: "card-value", text: this.valueText(s.values[i] ?? null) })]);
    });
    if (this.comp.type === "bar" && this.comp.stacked) {
      const vals = series.map((s) => s.values[i]).filter((v) => v !== null && v !== undefined);
      if (vals.length) {
        rows.push(h("div", { class: "card-row card-total" }, [h("span", { class: "swatch swatch-none" }), h("span", { class: "card-name", text: "Total" }),
          h("span", { class: "card-value", text: this.valueText(vals.reduce((a, b) => a + b, 0)) })]));
      }
    }
    fill(this.card, h("div", { class: "card-at", text: this.atText(i) }), rows);
    this.card.hidden = false;
    let x = null;
    try {
      x = this.chart.convertToPixel({ xAxisIndex: 0 }, i);
    } catch {
      x = null;
    }
    if (!Number.isFinite(x)) {
      this.card.hidden = true;
      return;
    }
    const w = this.card.offsetWidth;
    const room = this.canvas.clientWidth;
    const gap = 16;
    // Beside the pointer, on the side with room, never over the point itself.
    const left = x + gap + w <= room ? x + gap : Math.max(0, x - gap - w);
    this.card.style.transform = `translate(${Math.round(left)}px, 0)`;
  }

  hideCard() {
    if (this.card) this.card.hidden = true;
  }

  updateSummary() {
    if (!this.canvas) return;
    const { xs, series, truncated, rows } = this.data;
    const last = this.lastIndex();
    const parts = [`${this.comp.title || "Chart"}.`];
    if (xs.length) {
      parts.push(`${rows} ${rows === 1 ? "value" : "values"} from ${this.xText(xs[0])} to ${this.xText(xs[xs.length - 1])}.`);
      const latest = series.map((s) => `${series.length > 1 ? `${s.name} ` : ""}${this.valueText(s.values[last] ?? null)}`).join(", ");
      parts.push(`Latest, ${this.atText(last)}: ${latest}.`);
      const band = this.bandText(last);
      if (band) parts.push(`${band}.`);
      if (this.data.reference) parts.push(`${this.data.reference.label || "Reference"}: ${this.valueText(this.data.reference.value)}.`);
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
    this.stage.hidden = this.asTable;
    this.legend.classList.toggle("is-off", this.asTable);
    this.tableHost.hidden = !this.asTable;
    if (this.asTable) this.drawTable();
    else this.resized();
  }

  drawTable() {
    const { xs, series, dates, rowOf, band } = this.data;
    const label = this.comp.title || "Chart";
    const head = h("tr", null, [
      h("th", { class: "eui-th", attrs: { scope: "col" }, text: humanize(this.comp.x) }),
      ...series.map((s) => h("th", { class: "num eui-th", attrs: { scope: "col" }, text: s.name })),
      band ? h("th", { class: "num eui-th", attrs: { scope: "col" }, text: this.bandLabel() }) : null,
    ]);
    // Only the query's own rows (not the dates filled in to break a line). Dates newest first, like every
    // other list of recent things; categories in the query's order.
    const order = xs.map((_, i) => i).filter((i) => rowOf[i] >= 0);
    if (dates) order.reverse();
    const range = (i) => (band.low[i] === null || band.high[i] === null ? NULL_TEXT
      : `${display(this.comp.format, Math.min(band.low[i], band.high[i]))}–${this.valueText(Math.max(band.low[i], band.high[i]))}`);
    const body = order.map((i) => h("tr", null, [
      h("th", { class: "eui-th", attrs: { scope: "row" }, text: this.atText(i) }),
      ...series.map((s) => h("td", { class: "num eui-td", text: this.valueText(s.values[i]) })),
      band ? h("td", { class: "num eui-td", text: range(i) }) : null,
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
    this.card = null;
    this.stage = null;
    this.width = undefined;
    this.height = undefined;
    this.animate = true;
    this.asTable = false;
  }

  destroy() {
    super.destroy();
    this.unsubscribeTheme();
  }
}
