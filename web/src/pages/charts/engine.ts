/* Charts (ADR-111): the chart controller. It owns the lightweight-charts chart (TradingView's open-source library,
   Apache-2.0, attribution logo kept), the price series in the chosen chart type, live polling, paging into older
   history, and the drawing layer: creating, selecting, moving, anchor editing, magnet, undo / redo and saving.
   React only renders the toolbars and dialogs around it. Nothing here changes a price: bars are shown as delivered. */
import {
  AreaSeries, BarSeries, BaselineSeries, CandlestickSeries, ColorType, CrosshairMode, HistogramSeries, LineSeries, LineStyle, LineType,
  PriceScaleMode, createChart, createTextWatermark,
} from "lightweight-charts";
import type {
  IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, ISeriesPrimitiveAxisView, ITextWatermarkPluginApi,
  Logical, MouseEventParams, PrimitiveHoveredItem, SeriesType, SeriesAttachedParameter, Time, UTCTimestamp,
} from "lightweight-charts";
import type { Bar, ChartSymbol } from "../../api/charts";
import { charts } from "../../api/charts";
import type { Drawing, Pt, Style } from "./model";
import { ALL_VIS, BASE_STYLE, DAY, TimeMap, fmtPrice, fmtTime, heikinAshi, newId, tfGroup, tfLabel, tzOffset, withAlpha } from "./model";
import type { Env, Shape, XY } from "./paint";
import { Painter, dist, hitShape, toPt, toXY } from "./paint";
import type { Anchor, ToolDef } from "./tools";
import { TOOL, draftPoints } from "./tools";

export type ChartType = "candles" | "hollow" | "bars" | "ha" | "line" | "line_markers" | "step" | "area" | "baseline" | "columns";
export const CHART_TYPES: { id: ChartType; name: string }[] = [
  { id: "bars", name: "Bars" }, { id: "candles", name: "Candles" }, { id: "hollow", name: "Hollow candles" }, { id: "ha", name: "Heikin Ashi" },
  { id: "line", name: "Line" }, { id: "line_markers", name: "Line with markers" }, { id: "step", name: "Step line" }, { id: "area", name: "Area" },
  { id: "baseline", name: "Baseline" }, { id: "columns", name: "Columns" },
];
export type CursorMode = "cross" | "dot" | "arrow" | "eraser";

/** Chart appearance (Settings dialog), stored in the layout. */
export interface ChartStyle {
  palette: "munyun" | "tradingview" | "custom";
  up: string; down: string; wickUp: string; wickDown: string; borderUp: string; borderDown: string; line: string;
  bg: string; bgAuto: boolean; gridV: boolean; gridH: boolean; grid: string; crosshair: "normal" | "magnet" | "hidden"; crossColor: string;
  lastLabel: boolean; priceLine: boolean; watermark: boolean; sessions: boolean; scaleLeft: boolean; legendOHLC: boolean; legendChange: boolean;
  legendVolume: boolean; countdown: boolean; rightOffset: number; precision: number;
}
export const PALETTES = {
  munyun: { up: "#16a877", down: "#6b6b74", wickUp: "#16a877", wickDown: "#6b6b74", borderUp: "#16a877", borderDown: "#6b6b74", line: "#4a8ff0" },
  tradingview: { up: "#089981", down: "#f23645", wickUp: "#089981", wickDown: "#f23645", borderUp: "#089981", borderDown: "#f23645", line: "#2962ff" },
};
export const DEFAULT_STYLE: ChartStyle = {
  palette: "munyun", ...PALETTES.munyun, bg: "#0e0e0f", bgAuto: true, gridV: true, gridH: true, grid: "", crosshair: "normal", crossColor: "",
  lastLabel: true, priceLine: true, watermark: false, sessions: false, scaleLeft: false, legendOHLC: true, legendChange: true, legendVolume: false,
  countdown: true, rightOffset: 10, precision: 2,
};

export interface Legend { bar: Bar | null; prev: Bar | null; index: number; live: boolean }
export interface CtlState {
  drawings: Drawing[]; selectedId: string | null; tool: string | null; creating: boolean; canUndo: boolean; canRedo: boolean;
  loading: boolean; error: string | null; more: boolean; bars: number; lastTime: number | null; source: string;
  menu: { x: number; y: number; id: string | null } | null; floating: { x: number; y: number } | null; measure: boolean; zoom: boolean;
}
export interface Theme { bg: string; text: string; muted: string; grid: string; axis: string; border: string }

const NS_COUNT = (tf: number) => (tf >= 31 * DAY ? 150 : tf >= 7 * DAY ? 300 : tf >= DAY ? 750 : tf <= 5 ? 1500 : 1000);   // older bars page in
const tfParam = (tf: number) => tfLabel(tf).replace(/^(\d+)m$/, "$1");
const deep = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

/** The drawing layer as a lightweight-charts series primitive. */
class Layer implements ISeriesPrimitive<Time> {
  req: (() => void) | null = null;
  private views: IPrimitivePaneView[];
  constructor(private ctl: ChartCtl) {
    const fg: IPrimitivePaneRenderer = { draw: (t) => t.useMediaCoordinateSpace(({ context, mediaSize }) => ctl.render(context, mediaSize.width, mediaSize.height)) };
    const bg: IPrimitivePaneRenderer = { draw: () => undefined, drawBackground: (t) => t.useMediaCoordinateSpace(({ context, mediaSize }) => ctl.renderBack(context, mediaSize.width, mediaSize.height)) };
    this.views = [{ zOrder: () => "bottom", renderer: () => bg }, { zOrder: () => "top", renderer: () => fg }];
  }
  attached(p: SeriesAttachedParameter<Time>) { this.req = p.requestUpdate; }
  detached() { this.req = null; }
  paneViews() { return this.views; }
  priceAxisViews() { return this.ctl.axisViews("price"); }
  timeAxisViews() { return this.ctl.axisViews("time"); }
  hitTest(x: number, y: number): PrimitiveHoveredItem | null {
    const h = this.ctl.hoverInfo(x, y);
    return h ? { externalId: h.id, zOrder: "top", cursorStyle: h.cursor } : null;
  }
}

interface Creating { tool: ToolDef; points: Pt[]; cursor: Pt | null; downAt: XY | null }
interface Drag { kind: "move" | "anchor"; id: string; anchor: number; start: XY; orig: Drawing; moved: boolean }

export class ChartCtl {
  chart: IChartApi;
  series: ISeriesApi<SeriesType> | null = null;
  layer = new Layer(this);
  watermark: ITextWatermarkPluginApi<Time> | null = null;
  bars: Bar[] = []; ha: Bar[] = []; map = new TimeMap([], 5);
  symbol = "NQ"; tf = 5; type: ChartType = "candles"; tz = "America/New_York"; sym: ChartSymbol | null = null;
  style: ChartStyle = { ...DEFAULT_STYLE }; theme: Theme;
  drawings: Drawing[] = []; selectedId: string | null = null; hoverId: string | null = null;
  tool: string | null = null; cursorMode: CursorMode = "cross"; magnet = 0; stay = false; lockAll = false; hideAll = false;
  toolDefaults: Record<string, Partial<Style>> = {};
  private creating: Creating | null = null; private drag: Drag | null = null;
  private undo: string[] = []; private redo: string[] = [];
  private shapes = new Map<string, Shape[]>();
  private measure: { a: Pt; b: Pt; done: boolean } | null = null; measureMode = false;
  private zoom: { a: XY; b: XY } | null = null; zoomMode = false;
  private loading = false; private error: string | null = null; private more = false; private source = "";
  private menu: CtlState["menu"] = null;
  private saveTimer = 0; private liveTimer = 0; private loadSeq = 0; private disposed = false;
  private legendSubs = new Set<(l: Legend) => void>();
  private stateSubs = new Set<(s: CtlState) => void>();
  private clipboard: Drawing | null = null;
  onTextEdit: ((d: Drawing) => void) | null = null;
  onSettings: ((d: Drawing) => void) | null = null;

  constructor(public el: HTMLElement, theme: Theme) {
    this.theme = theme;
    this.chart = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: theme.bg }, textColor: theme.muted, fontSize: 12,
        fontFamily: "Inter, system-ui, -apple-system, Segoe UI, sans-serif", attributionLogo: true },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: theme.axis, scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: { borderColor: theme.axis, timeVisible: true, secondsVisible: false, rightOffset: 10, barSpacing: 8, minBarSpacing: 0.3,
        shiftVisibleRangeOnNewBar: true },
      handleScale: { axisPressedMouseMove: { time: true, price: true }, axisDoubleClickReset: { time: true, price: true }, mouseWheel: true, pinch: true },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
      kineticScroll: { mouse: false, touch: true },
      localization: { priceFormatter: (p: number) => fmtPrice(p, this.style.precision).replace(/,/g, "") },
    });
    this.chart.subscribeCrosshairMove(this.onCross);
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(this.onRange);
    el.addEventListener("mousedown", this.onDown, true);
    el.addEventListener("mousemove", this.onHover);
    el.addEventListener("dblclick", this.onDbl, true);
    el.addEventListener("contextmenu", this.onContext, true);
    window.addEventListener("mousemove", this.onMove, true);
    window.addEventListener("mouseup", this.onUp, true);
    this.makeSeries();
  }

  dispose() {
    this.disposed = true;
    this.flushSave();
    window.clearTimeout(this.liveTimer);
    this.el.removeEventListener("mousedown", this.onDown, true);
    this.el.removeEventListener("mousemove", this.onHover);
    this.el.removeEventListener("dblclick", this.onDbl, true);
    this.el.removeEventListener("contextmenu", this.onContext, true);
    window.removeEventListener("mousemove", this.onMove, true);
    window.removeEventListener("mouseup", this.onUp, true);
    this.chart.remove();
  }

  // ================================================================== subscriptions
  subscribe(f: (s: CtlState) => void) { this.stateSubs.add(f); f(this.state()); return () => { this.stateSubs.delete(f); }; }
  subscribeLegend(f: (l: Legend) => void) { this.legendSubs.add(f); f(this.lastLegend()); return () => { this.legendSubs.delete(f); }; }
  state(): CtlState {
    const sel = this.selected();
    let floating: CtlState["floating"] = null;
    if (sel && !this.drag && !this.creating) {
      const sh = this.shapes.get(sel.id);
      const b = sh && bounds(sh);
      if (b) floating = { x: Math.max(8, Math.min(this.paneW() - 300, (b.x0 + b.x1) / 2 - 150)), y: Math.max(6, b.y0 - 52) };
    }
    return { drawings: this.drawings, selectedId: this.selectedId, tool: this.tool, creating: !!this.creating, canUndo: this.undo.length > 0,
      canRedo: this.redo.length > 0, loading: this.loading, error: this.error, more: this.more, bars: this.bars.length,
      lastTime: this.bars.length ? this.bars[this.bars.length - 1].time : null, source: this.source, menu: this.menu, floating,
      measure: this.measureMode, zoom: this.zoomMode };
  }
  emit() { const s = this.state(); this.stateSubs.forEach((f) => f(s)); }
  private lastLegend(i?: number): Legend {
    const n = this.bars.length, k = i === undefined || i < 0 || i >= n ? n - 1 : i;
    const src = this.type === "ha" ? this.ha : this.bars;
    return { bar: src[k] ?? null, prev: src[k - 1] ?? null, index: k, live: i === undefined || k === n - 1 };
  }
  private onCross = (p: MouseEventParams<Time>) => {
    const l = p.logical === undefined ? undefined : Math.round(p.logical as number);
    const lg = this.lastLegend(l === undefined || p.point === undefined ? undefined : l);
    this.legendSubs.forEach((f) => f(lg));
  };
  redraw() { this.layer.req?.(); }

  // ================================================================== series / data
  private seriesItem(i: number) {
    const b = (this.type === "ha" ? this.ha : this.bars)[i];
    const time = this.disp(this.bars[i].time, i) as UTCTimestamp;
    switch (this.type) {
      case "line": case "line_markers": case "step": case "area": case "baseline": return { time, value: b.close };
      case "columns": return { time, value: b.close, color: withAlpha(b.close >= b.open ? this.style.up : this.style.down, 0.8) };
      default: return { time, open: b.open, high: b.high, low: b.low, close: b.close };
    }
  }
  private dispCache: number[] = [];
  private disp(t: number, i: number) { return this.dispCache[i] ?? t; }
  private rebuildDisp() {
    const out: number[] = [];
    let prev = -Infinity;
    for (const b of this.bars) {
      let d = this.tf >= DAY ? b.time : b.time + tzOffset(this.tz, b.time);
      if (d <= prev) d = prev + 1;                                 // a clock change while the market trades (never on these markets)
      out.push(d); prev = d;
    }
    this.dispCache = out;
  }
  private seriesOptions() {
    const s = this.style, base = { priceFormat: { type: "price" as const, precision: s.precision, minMove: Math.pow(10, -s.precision) },
      lastValueVisible: s.lastLabel, priceLineVisible: s.priceLine, priceLineStyle: LineStyle.Dashed };
    switch (this.type) {
      case "bars": return { ...base, upColor: s.up, downColor: s.down, thinBars: false };
      case "hollow": return { ...base, upColor: "rgba(0,0,0,0)", downColor: s.down, borderUpColor: s.borderUp, borderDownColor: s.borderDown,
        wickUpColor: s.wickUp, wickDownColor: s.wickDown, borderVisible: true };
      case "line": return { ...base, color: s.line, lineWidth: 2 as const };
      case "line_markers": return { ...base, color: s.line, lineWidth: 2 as const, pointMarkersVisible: true };
      case "step": return { ...base, color: s.line, lineWidth: 2 as const, lineType: LineType.WithSteps };
      case "area": return { ...base, lineColor: s.line, topColor: withAlpha(s.line, 0.35), bottomColor: withAlpha(s.line, 0.02), lineWidth: 2 as const };
      case "baseline": {
        const v = this.bars.length ? this.bars[Math.max(0, this.bars.length - 300)].close : 0;
        return { ...base, baseValue: { type: "price" as const, price: v }, topLineColor: s.up, topFillColor1: withAlpha(s.up, 0.28), topFillColor2: withAlpha(s.up, 0.05),
          bottomLineColor: s.down, bottomFillColor1: withAlpha(s.down, 0.05), bottomFillColor2: withAlpha(s.down, 0.28) };
      }
      case "columns": return { ...base, color: s.up };
      default: return { ...base, upColor: s.up, downColor: s.down, borderUpColor: s.borderUp, borderDownColor: s.borderDown, wickUpColor: s.wickUp,
        wickDownColor: s.wickDown, borderVisible: true };
    }
  }
  private makeSeries() {
    if (this.series) { this.series.detachPrimitive(this.layer); this.chart.removeSeries(this.series); }
    const def = { bars: BarSeries, line: LineSeries, line_markers: LineSeries, step: LineSeries, area: AreaSeries, baseline: BaselineSeries,
      columns: HistogramSeries }[this.type as string] ?? CandlestickSeries;
    this.series = this.chart.addSeries(def as typeof CandlestickSeries, this.seriesOptions() as never);
    this.series.attachPrimitive(this.layer);
    this.setData(false);
  }
  private setData(keepView = true) {
    if (!this.series) return;
    const r = keepView ? this.chart.timeScale().getVisibleLogicalRange() : null;
    this.ha = this.type === "ha" ? heikinAshi(this.bars) : [];
    this.rebuildDisp();
    this.series.setData(this.bars.map((_, i) => this.seriesItem(i)) as never);
    if (r) this.chart.timeScale().setVisibleLogicalRange(r);
    this.redraw();
  }
  setType(t: ChartType) { if (t === this.type) return; this.type = t; this.makeSeries(); this.emitLegend(); }
  setTz(tz: string) { this.tz = tz; this.setData(true); }
  setTheme(t: Theme) { this.theme = t; this.applyStyle(this.style); }
  applyStyle(s: ChartStyle) {
    this.style = s;
    const t = this.theme, grid = s.grid || t.grid;
    this.chart.applyOptions({
      layout: { background: { type: ColorType.Solid, color: s.bgAuto ? t.bg : s.bg }, textColor: t.muted },
      grid: { vertLines: { visible: s.gridV, color: grid }, horzLines: { visible: s.gridH, color: grid } },
      crosshair: { mode: s.crosshair === "magnet" ? CrosshairMode.Magnet : s.crosshair === "hidden" ? CrosshairMode.Hidden : CrosshairMode.Normal,
        vertLine: { color: s.crossColor || t.muted, labelBackgroundColor: t.border }, horzLine: { color: s.crossColor || t.muted, labelBackgroundColor: t.border } },
      rightPriceScale: { visible: !s.scaleLeft, borderColor: t.axis }, leftPriceScale: { visible: s.scaleLeft, borderColor: t.axis },
      timeScale: { rightOffset: s.rightOffset, borderColor: t.axis },
    });
    this.series?.applyOptions({ ...this.seriesOptions(), priceScaleId: s.scaleLeft ? "left" : "right" } as never);
    if (this.type === "columns") this.setData(true);
    this.applyWatermark();
    this.redraw();
  }
  private applyWatermark() {
    const pane = this.chart.panes()[0];
    if (!pane) return;
    if (!this.style.watermark) { this.watermark?.detach(); this.watermark = null; return; }
    const opts = { horzAlign: "center" as const, vertAlign: "center" as const,
      lines: [{ text: `${this.symbol}, ${tfLabel(this.tf)}`, color: withAlpha(this.theme.muted, 0.12), fontSize: 64, fontStyle: "600" }] };
    if (this.watermark) this.watermark.applyOptions(opts); else this.watermark = createTextWatermark(pane, opts);
  }
  setScale(o: { mode?: "normal" | "log" | "percent" | "indexed"; invert?: boolean; auto?: boolean }) {
    const ps = this.chart.priceScale(this.style.scaleLeft ? "left" : "right");
    const mode = { normal: PriceScaleMode.Normal, log: PriceScaleMode.Logarithmic, percent: PriceScaleMode.Percentage, indexed: PriceScaleMode.IndexedTo100 };
    ps.applyOptions({ ...(o.mode ? { mode: mode[o.mode] } : {}), ...(o.invert !== undefined ? { invertScale: o.invert } : {}),
      ...(o.auto !== undefined ? { autoScale: o.auto } : {}) });
    this.redraw();
  }
  isAuto() { return this.chart.priceScale(this.style.scaleLeft ? "left" : "right").options().autoScale; }

  async load(symbol: string, tf: number, sym: ChartSymbol | null, view?: { from: number; to: number }) {
    if (symbol !== this.symbol) { this.flushSave(); this.drawings = []; this.undo = []; this.redo = []; this.selectedId = null; }
    const symChanged = symbol !== this.symbol || !this.drawingsLoaded;
    this.symbol = symbol; this.tf = tf; this.sym = sym;
    const seq = ++this.loadSeq;
    this.loading = true; this.error = null; this.creating = null; this.emit();
    window.clearTimeout(this.liveTimer);
    try {
      const [res, dr] = await Promise.all([charts.bars(symbol, tfParam(tf), undefined, NS_COUNT(tf)),
        symChanged ? charts.drawings(symbol) : Promise.resolve(null)]);
      if (seq !== this.loadSeq || this.disposed) return;
      this.bars = res.bars; this.more = res.more; this.source = res.source; this.map = new TimeMap(this.bars, tf);
      if (dr) {                                                      // keep anything drawn while the symbol was loading
        const loaded = (dr.drawings as Drawing[]).filter((d) => d && TOOL[d.type]).map(normalize), ids = new Set(loaded.map((d) => d.id));
        const early = this.drawings.filter((d) => !ids.has(d.id));
        this.drawings = [...loaded, ...early];
        this.drawingsLoaded = true;
        if (early.length) this.scheduleSave();
      }
      this.error = res.status.error;
      this.setData(false);
      this.applyWatermark();
      if (view) this.showTimes(view.from, view.to);
      else this.chart.timeScale().setVisibleLogicalRange({ from: (this.bars.length - 140) as Logical, to: (this.bars.length + 8) as Logical });
      this.setScale({ auto: true });
    } catch (e) {
      if (seq !== this.loadSeq) return;
      this.error = (e as Error).message;
    } finally {
      if (seq === this.loadSeq) { this.loading = false; this.emit(); this.emitLegend(); this.scheduleLive(); }
    }
  }
  private drawingsLoaded = false;
  private emitLegend() { const lg = this.lastLegend(); this.legendSubs.forEach((f) => f(lg)); }

  private scheduleLive() {
    window.clearTimeout(this.liveTimer);
    if (this.disposed) return;
    this.liveTimer = window.setTimeout(() => void this.pollLive(), document.hidden ? 10000 : 2000);
  }
  private async pollLive() {
    const seq = this.loadSeq, last = this.bars[this.bars.length - 1];
    if (!last) { this.scheduleLive(); return; }
    try {
      const res = await charts.live(this.symbol, tfParam(this.tf), last.time);
      if (seq !== this.loadSeq || this.disposed) return;
      this.error = res.status.error;
      let changed = false, appended = false;
      const nBefore = this.bars.length;
      for (const b of res.bars) {
        const n = this.bars.length, cur = this.bars[n - 1];
        if (b.time === cur.time) {
          if (b.close !== cur.close || b.high !== cur.high || b.low !== cur.low || b.volume !== cur.volume) { this.bars[n - 1] = b; changed = true; }
        } else if (b.time > cur.time) { this.bars.push(b); changed = true; appended = true; }
      }
      if (changed) {
        if (appended) this.map = new TimeMap(this.bars, this.tf);
        if (this.type === "ha") this.ha = heikinAshi(this.bars);
        this.rebuildDisp();
        const from = Math.max(0, nBefore - 1);                           // the chart only takes updates of its last bar onwards
        for (let i = from; i < this.bars.length; i++) this.series?.update(this.seriesItem(i) as never);
        this.emitLegend();
        this.redraw();
      }
      this.emit();
    } catch (e) {
      if (seq === this.loadSeq) { this.error = (e as Error).message; this.emit(); }
    } finally {
      if (seq === this.loadSeq) this.scheduleLive();
    }
  }
  private onRange = () => {
    const r = this.chart.timeScale().getVisibleLogicalRange();
    if (r && r.from < 40 && this.more && !this.loading) void this.loadOlder();
    this.redraw();
  };
  async loadOlder(): Promise<boolean> {
    if (this.loading || !this.more || !this.bars.length) return false;
    const seq = this.loadSeq;
    this.loading = true; this.emit();
    try {
      const res = await charts.bars(this.symbol, tfParam(this.tf), this.bars[0].time, NS_COUNT(this.tf));
      if (seq !== this.loadSeq) return false;
      const older = res.bars.filter((b) => b.time < this.bars[0].time);
      this.more = res.more && older.length > 0;
      if (older.length) {
        const r = this.chart.timeScale().getVisibleLogicalRange();
        this.bars = [...older, ...this.bars]; this.map = new TimeMap(this.bars, this.tf);
        this.setData(false);
        if (r) this.chart.timeScale().setVisibleLogicalRange({ from: (r.from + older.length) as Logical, to: (r.to + older.length) as Logical });
      }
      return older.length > 0;
    } catch (e) {
      this.error = (e as Error).message; return false;
    } finally {
      if (seq === this.loadSeq) { this.loading = false; this.emit(); }
    }
  }
  /** Shows real times [from, to] (loading older history as needed). */
  async showTimes(from: number, to: number) {
    for (let k = 0; k < 40 && this.bars.length && this.map.real[0] > from && this.more; k++) {
      this.loading = false;
      if (!(await this.loadOlder())) break;
    }
    const a = this.map.toLogical(from), b = this.map.toLogical(to);
    this.chart.timeScale().setVisibleLogicalRange({ from: a as Logical, to: Math.max(a + 5, b) as Logical });
  }
  async goTo(t: number) {
    const r = this.chart.timeScale().getVisibleLogicalRange(), span = r ? r.to - r.from : 120;
    await this.showTimes(t, t);
    const l = this.map.toLogical(t);
    this.chart.timeScale().setVisibleLogicalRange({ from: (l - span / 2) as Logical, to: (l + span / 2) as Logical });
  }
  resetView() {
    this.chart.timeScale().resetTimeScale();
    this.chart.timeScale().setVisibleLogicalRange({ from: (this.bars.length - 140) as Logical, to: (this.bars.length + 8) as Logical });
    this.setScale({ auto: true });
  }
  scrollToNow() { this.chart.timeScale().scrollToRealTime(); }
  screenshot(): string { return this.chart.takeScreenshot(true, false).toDataURL("image/png"); }

  // ================================================================== geometry
  paneW() { return this.chart.paneSize().width; }
  paneH() { return this.chart.paneSize().height; }
  env(w = this.paneW(), h = this.paneH()): Env {
    const ts = this.chart.timeScale(), s = this.series!;
    const x0 = ts.logicalToCoordinate(0 as Logical) ?? 0, x1 = ts.logicalToCoordinate(1 as Logical) ?? 1, sp = x1 - x0 || 1;
    return {
      w, h, tf: this.tf, tz: this.tz, dp: this.style.precision, map: this.map, bars: this.bars, sym: this.sym, spacing: sp,
      xOfL: (l) => x0 + l * sp, lOfX: (x) => (x - x0) / sp,
      yOf: (p) => s.priceToCoordinate(p) ?? NaN, pOf: (y) => s.coordinateToPrice(y) ?? NaN,
      theme: { text: this.theme.text, bg: this.theme.bg, muted: this.theme.muted, up: this.style.up, down: this.style.down }, selected: false,
    };
  }
  private local(ev: MouseEvent): XY { const r = this.el.getBoundingClientRect(); return { x: ev.clientX - r.left - (this.style.scaleLeft ? this.leftW() : 0), y: ev.clientY - r.top }; }
  private leftW() { return this.style.scaleLeft ? this.chart.priceScale("left").width() : 0; }
  private inPane(q: XY) { return q.x >= 0 && q.y >= 0 && q.x <= this.paneW() && q.y <= this.paneH(); }
  /** Cursor point -> stored point: snapped to the bar (time) and, with the magnet, to the bar's open / high / low / close. */
  private snap(q: XY, d: Drawing | null, free = false): Pt {
    const e = this.env();
    if (d?.screen) return toPt(e, d, q);
    const l = e.lOfX(q.x), li = free ? l : Math.round(l);
    let p = e.pOf(q.y);
    if (this.magnet && !free) {
      const b = this.bars[Math.round(l)];
      if (b) {
        const cand = [b.open, b.high, b.low, b.close].map((v) => ({ v, d: Math.abs(e.yOf(v) - q.y) })).sort((a, c) => a.d - c.d)[0];
        if (this.magnet === 2 || cand.d < 30) p = cand.v;
      }
    }
    return { t: this.map.toTime(li), p };
  }

  // ================================================================== drawing state
  selected() { return this.drawings.find((d) => d.id === this.selectedId) ?? null; }
  private visible(d: Drawing) { return !d.hidden && !this.hideAll && (d.vis ?? ALL_VIS)[tfGroup(this.tf)]; }
  private snapshot() { return JSON.stringify(this.drawings); }
  private commit(before: string) {
    if (before === this.snapshot()) return;
    this.undo.push(before); if (this.undo.length > 150) this.undo.shift();
    this.redo = [];
    this.scheduleSave(); this.emit(); this.redraw();
  }
  /** Settings dialog: show edits live, then keep them (one undo step) or go back. */
  preview(d: Drawing) { const i = this.drawings.findIndex((x) => x.id === d.id); if (i >= 0) { this.drawings[i] = JSON.parse(JSON.stringify(d)); this.redraw(); } }
  restore(snapshot: string) { this.drawings = JSON.parse(snapshot); this.emit(); this.redraw(); }
  commitFrom(before: string) { this.commit(before); this.emit(); }
  onDefaults: (() => void) | null = null;
  change(f: () => void) { const b = this.snapshot(); f(); this.commit(b); }
  undoStep() { const s = this.undo.pop(); if (s === undefined) return; this.redo.push(this.snapshot()); this.drawings = JSON.parse(s); this.afterHistory(); }
  redoStep() { const s = this.redo.pop(); if (s === undefined) return; this.undo.push(this.snapshot()); this.drawings = JSON.parse(s); this.afterHistory(); }
  private afterHistory() { if (!this.selected()) this.selectedId = null; this.scheduleSave(); this.emit(); this.redraw(); }
  private dirty = false;
  private scheduleSave() { this.dirty = true; window.clearTimeout(this.saveTimer); this.saveTimer = window.setTimeout(() => this.flushSave(), 700); }
  flushSave() {
    window.clearTimeout(this.saveTimer);
    if (!this.drawingsLoaded || !this.dirty) return;
    const sym = this.symbol, list = this.drawings;
    this.dirty = false;
    void charts.saveDrawings(sym, list).catch((e: Error) => { this.error = `Drawings not saved: ${e.message}`; this.emit(); });
  }
  update(id: string, f: (d: Drawing) => void) { this.change(() => { const d = this.drawings.find((x) => x.id === id); if (d) f(d); }); }
  remove(id: string) { this.change(() => { this.drawings = this.drawings.filter((d) => d.id !== id); }); if (this.selectedId === id) this.select(null); }
  removeAll() { this.change(() => { this.drawings = []; }); this.select(null); }
  select(id: string | null) { this.selectedId = id; this.menu = null; this.emit(); this.redraw(); }
  clone(id: string) {
    const d = this.drawings.find((x) => x.id === id);
    if (!d) return;
    const e = this.env(), c = deep(d);
    c.id = newId(); c.z = this.topZ() + 1; c.locked = false;
    c.points = c.points.map((p) => (c.screen ? { t: p.t + 0.02, p: p.p + 0.02 } : { t: this.map.toTime(this.map.toLogical(p.t) + 3), p: e.pOf(e.yOf(p.p) + 16) }));
    this.change(() => { this.drawings = [...this.drawings, c]; });
    this.select(c.id);
  }
  copy() { const d = this.selected(); if (d) this.clipboard = deep(d); }
  paste() { if (this.clipboard) { const c = deep(this.clipboard); c.id = newId(); this.drawings.push(c); this.clone(c.id); this.drawings = this.drawings.filter((x) => x.id !== c.id); } }
  private topZ() { return this.drawings.reduce((m, d) => Math.max(m, d.z), 0); }
  order(id: string, how: "front" | "back" | "forward" | "backward") {
    this.change(() => {
      const list = [...this.drawings].sort((a, b) => a.z - b.z), i = list.findIndex((d) => d.id === id);
      if (i < 0) return;
      const [d] = list.splice(i, 1);
      const j = how === "front" ? list.length : how === "back" ? 0 : how === "forward" ? Math.min(list.length, i + 1) : Math.max(0, i - 1);
      list.splice(j, 0, d);
      list.forEach((x, k) => { x.z = k + 1; });
    });
  }
  setTool(id: string | null) {
    this.tool = id; this.creating = null; this.measureMode = false; this.zoomMode = false;
    if (id) this.select(null); else this.emit();
    this.redraw();
  }
  setMeasureMode(on: boolean) { this.setTool(null); this.measureMode = on; this.measure = null; this.emit(); }
  setZoomMode(on: boolean) { this.setTool(null); this.zoomMode = on; this.emit(); }
  closeMenu() { if (this.menu) { this.menu = null; this.emit(); } }
  private newDrawing(tool: ToolDef, points: Pt[]): Drawing {
    return normalize({ id: newId(), type: tool.id, points, style: { ...BASE_STYLE, ...tool.defaults, ...this.toolDefaults[tool.id] },
      text: tool.text ?? "", levels: tool.levels ? deep(tool.levels) : undefined, z: this.topZ() + 1, screen: tool.screen, vis: { ...ALL_VIS } });
  }
  private finishCreate() {
    const c = this.creating;
    if (!c) return;
    const e = this.env();
    let pts = c.points;
    const clicks = c.tool.clicks ?? c.tool.n;
    if (clicks > 0) pts = draftPoints(c.tool, pts, null, e);
    if (clicks === -1 && pts.length < 2) { this.creating = null; this.redraw(); return; }
    const d = this.newDrawing(c.tool, pts);
    this.creating = null;
    this.change(() => { this.drawings = [...this.drawings, d]; });
    if (!this.stay) this.tool = null;
    this.select(d.id);
    if (c.tool.askText) this.onTextEdit?.(d);
  }

  // ================================================================== rendering
  renderBack(c: CanvasRenderingContext2D, w: number, h: number) {
    if (!this.style.sessions || this.tf >= DAY || !this.bars.length) return;
    const e = this.env(w, h), l0 = Math.max(1, Math.floor(e.lOfX(0))), l1 = Math.min(this.bars.length - 1, Math.ceil(e.lOfX(w)));
    c.strokeStyle = withAlpha(this.theme.muted, 0.35); c.lineWidth = 1; c.setLineDash([4, 4]);
    const day = (t: number) => Math.floor((t + 6 * 3600 + tzOffset("America/New_York", t)) / 86400);
    for (let i = l0; i <= l1; i++) {
      if (day(this.bars[i].time) !== day(this.bars[i - 1].time)) {
        const x = Math.round(e.xOfL(i - 0.5)) + 0.5;
        c.beginPath(); c.moveTo(x, 0); c.lineTo(x, h); c.stroke();
      }
    }
    c.setLineDash([]);
  }
  render(c: CanvasRenderingContext2D, w: number, h: number) {
    if (!this.series) return;
    const e = this.env(w, h);
    this.shapes.clear();
    const list = [...this.drawings].sort((a, b) => a.z - b.z);
    for (const d of list) {
      if (!this.visible(d)) continue;
      const tool = TOOL[d.type];
      if (!tool) continue;
      const px = d.points.map((p) => toXY(e, d, p));
      if (px.some((p) => !Number.isFinite(p.x) || !Number.isFinite(p.y))) continue;
      const P = new Painter(c, { ...e, selected: d.id === this.selectedId || d.id === this.hoverId }, d.style);
      c.save();
      try { tool.draw(P, px, d, P.e); } catch { /* a tool never breaks the chart */ }
      c.restore();
      this.shapes.set(d.id, P.shapes);
      if ((d.id === this.selectedId || d.id === this.hoverId) && !d.locked && !this.lockAll) this.drawAnchors(c, this.anchorsOf(d, e), d.id === this.selectedId);
      if (d.id === this.selectedId && (d.locked || this.lockAll)) this.drawLock(c, px[0]);
    }
    const cr = this.creating;
    if (cr) {
      const pts = (cr.tool.clicks ?? cr.tool.n) > 0 ? draftPoints(cr.tool, cr.points, cr.cursor, e) : cr.points;
      if (pts.length) {
        const d = this.newDrawing(cr.tool, pts);
        const px = pts.map((p) => toXY(e, d, p));
        const P = new Painter(c, e, d.style);
        c.save();
        try { cr.tool.draw(P, px, d, e); } catch { /* ignore while drawing */ }
        c.restore();
        this.drawAnchors(c, px.map((xy) => ({ xy, move: () => undefined })), true);
      }
    }
    if (this.measure) {
      const d = this.newDrawing(TOOL.date_price_range, [this.measure.a, this.measure.b]);
      const P = new Painter(c, e, d.style);
      c.save(); TOOL.date_price_range.draw(P, d.points.map((p) => toXY(e, d, p)), d, e); c.restore();
    }
    if (this.zoom) {
      const { a, b } = this.zoom;
      c.fillStyle = withAlpha("#2962ff", 0.12); c.strokeStyle = "#2962ff"; c.lineWidth = 1; c.setLineDash([4, 3]);
      c.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(a.x - b.x), Math.abs(a.y - b.y));
      c.strokeRect(Math.min(a.x, b.x) + 0.5, Math.min(a.y, b.y) + 0.5, Math.abs(a.x - b.x), Math.abs(a.y - b.y));
      c.setLineDash([]);
    }
  }
  private drawAnchors(c: CanvasRenderingContext2D, anchors: Anchor[], strong: boolean) {
    c.setLineDash([]);
    for (const a of anchors) {
      c.beginPath(); c.arc(a.xy.x, a.xy.y, strong ? 5.5 : 4.5, 0, Math.PI * 2);
      c.fillStyle = this.theme.bg; c.fill(); c.lineWidth = 2; c.strokeStyle = "#2962ff"; c.stroke();
    }
  }
  private drawLock(c: CanvasRenderingContext2D, p: XY) {
    c.fillStyle = this.theme.muted; c.font = "12px sans-serif"; c.fillText("🔒", p.x + 6, p.y - 6);
  }
  anchorsOf(d: Drawing, e: Env): Anchor[] {
    const tool = TOOL[d.type], px = d.points.map((p) => toXY(e, d, p));
    if (tool.anchors) return tool.anchors(px, d, e);
    if ((tool.clicks ?? tool.n) === 0) return [];                       // freehand: moved as a whole
    return px.map((xy, i) => ({ xy, move: (dd: Drawing, to: Pt) => { dd.points[i] = to; } }));
  }
  axisViews(kind: "price" | "time"): ISeriesPrimitiveAxisView[] {
    if (!this.series) return [];
    const e = this.env(), out: ISeriesPrimitiveAxisView[] = [];
    const lab = (coord: number, text: string, bg: string): ISeriesPrimitiveAxisView =>
      ({ coordinate: () => coord, text: () => text, textColor: () => "#ffffff", backColor: () => bg });
    for (const d of this.drawings) {
      if (!this.visible(d) || d.screen) continue;
      const tool = TOOL[d.type], sel = d.id === this.selectedId;
      const always = kind === "price" ? tool.priceAxis && d.style.showPrice : tool.timeAxis;
      if (!sel && !always) continue;
      const pts = always && !sel ? d.points.slice(0, 1) : d.points;
      for (const p of pts) {
        if (kind === "price") { const y = e.yOf(p.p); if (Number.isFinite(y)) out.push(lab(y, fmtPrice(p.p, e.dp).replace(/,/g, ""), withAlpha(d.style.color, 1))); }
        else { const x = e.xOfL(this.map.toLogical(p.t)); if (Number.isFinite(x)) out.push(lab(x, fmtTime(p.t, this.tz, this.tf >= DAY), withAlpha(d.style.color, 1))); }
      }
    }
    return out;
  }

  // ================================================================== hit testing
  private hitDrawing(q: XY): string | null {
    const list = [...this.drawings].sort((a, b) => b.z - a.z);
    let best: { id: string; d: number } | null = null;
    for (const d of list) {
      const sh = this.shapes.get(d.id);
      if (!sh || !this.visible(d)) continue;
      let m = Infinity;
      for (const s of sh) { const r = hitShape(s, q, 5); if (r !== null && r < m) m = r; }
      if (m < Infinity && (!best || m < best.d - 0.5)) { best = { id: d.id, d: m }; if (m <= 1) break; }
    }
    return best?.id ?? null;
  }
  private hitAnchor(q: XY): { id: string; i: number; cursor?: string } | null {
    const e = this.env();
    for (const id of [this.selectedId, this.hoverId]) {
      const d = this.drawings.find((x) => x.id === id);
      if (!d || d.locked || this.lockAll || !this.visible(d)) continue;
      const an = this.anchorsOf(d, e);
      for (let i = 0; i < an.length; i++) if (dist(an[i].xy, q) <= 8) return { id: d.id, i, cursor: an[i].cursor };
    }
    return null;
  }
  hoverInfo(x: number, y: number): { id: string; cursor: string } | null {
    if (this.tool || this.creating) return { id: "_draw", cursor: "crosshair" };
    if (this.zoomMode || this.measureMode) return { id: "_mode", cursor: "crosshair" };
    if (this.cursorMode === "eraser") { const id = this.hitDrawing({ x, y }); return { id: id ?? "_eraser", cursor: id ? "pointer" : "not-allowed" }; }
    const a = this.hitAnchor({ x, y });
    if (a) return { id: a.id, cursor: a.cursor ?? "pointer" };
    const id = this.hitDrawing({ x, y });
    if (id) { const d = this.drawings.find((z) => z.id === id); return { id, cursor: d?.locked || this.lockAll ? "default" : "move" }; }
    if (this.cursorMode === "arrow") return { id: "_arrow", cursor: "default" };
    if (this.cursorMode === "dot") return { id: "_dot", cursor: "none" };
    return null;
  }

  // ================================================================== mouse
  private stop(ev: Event) { ev.stopPropagation(); ev.preventDefault(); }
  private onDown = (ev: MouseEvent) => {
    if (ev.button !== 0) return;
    const q = this.local(ev);
    if (!this.inPane(q)) return;
    if (this.menu) { this.menu = null; this.emit(); }
    if (this.zoomMode) { this.zoom = { a: q, b: q }; this.stop(ev); return; }
    if (this.measureMode || (ev.shiftKey && !this.tool && !this.creating)) {
      const p = this.snap(q, null);
      if (this.measure && !this.measure.done) { this.measure.b = p; this.measure.done = true; this.measureMode = false; this.emit(); }
      else if (this.measure?.done) this.measure = null;
      else this.measure = { a: p, b: p, done: false };
      this.redraw(); this.stop(ev); return;
    }
    if (this.measure) { this.measure = null; this.redraw(); }
    if (this.tool) {
      const tool = TOOL[this.tool], clicks = tool.clicks ?? tool.n;
      const fake = tool.screen ? ({ screen: true } as Drawing) : null;
      if (clicks === 0) { this.creating = { tool, points: [this.snap(q, null, true)], cursor: null, downAt: q }; this.stop(ev); return; }
      const p = this.snap(q, fake);
      if (!this.creating) this.creating = { tool, points: [p], cursor: p, downAt: q };
      else { this.creating.points.push(p); this.creating.downAt = q; }
      if (clicks > 0 && this.creating.points.length >= clicks) this.finishCreate();
      else this.emit();
      this.redraw(); this.stop(ev); return;
    }
    if (this.cursorMode === "eraser") { const id = this.hitDrawing(q); if (id) { this.remove(id); this.stop(ev); } return; }
    const a = this.hitAnchor(q);
    if (a) {
      this.select(a.id);
      this.drag = { kind: "anchor", id: a.id, anchor: a.i, start: q, orig: deep(this.selected()!), moved: false };
      this.stop(ev); return;
    }
    const id = this.hitDrawing(q);
    if (id) {
      this.select(id);
      const d = this.selected()!;
      if (!d.locked && !this.lockAll) this.drag = { kind: "move", id, anchor: -1, start: q, orig: deep(d), moved: false };
      this.stop(ev); return;
    }
    if (this.selectedId) this.select(null);
  };
  private onMove = (ev: MouseEvent) => {
    const q = this.local(ev);
    if (this.zoom) { this.zoom.b = q; this.redraw(); return; }
    if (this.measure && !this.measure.done) { this.measure.b = this.snap(q, null); this.redraw(); }
    const cr = this.creating;
    if (cr) {
      const clicks = cr.tool.clicks ?? cr.tool.n;
      if (clicks === 0) { if (ev.buttons & 1) { const p = this.snap(q, null, true), last = cr.points[cr.points.length - 1], e = this.env();
        if (dist(toXY(e, { screen: false } as Drawing, last), q) >= 3) cr.points.push(p); } }
      else {
        let p = this.snap(q, cr.tool.screen ? ({ screen: true } as Drawing) : null);
        if (ev.shiftKey && cr.points.length) p = { ...p, p: cr.points[cr.points.length - 1].p };
        cr.cursor = p;
      }
      this.redraw(); return;
    }
    const dg = this.drag;
    if (!dg) return;
    if (!dg.moved && dist(q, dg.start) < 3) return;
    dg.moved = true;
    const d = this.drawings.find((x) => x.id === dg.id);
    if (!d) return;
    const e = this.env();
    if (dg.kind === "move") {
      const dx = q.x - dg.start.x, dy = q.y - dg.start.y;
      if (d.screen) d.points = dg.orig.points.map((p) => ({ t: p.t + dx / e.w, p: p.p + dy / e.h }));
      else {
        const dl = Math.round(e.lOfX(q.x) - e.lOfX(dg.start.x));
        d.points = dg.orig.points.map((p) => ({ t: this.map.toTime(this.map.toLogical(p.t) + dl), p: e.pOf(e.yOf(p.p) + dy) }));
      }
    } else {
      const copy = deep(dg.orig);
      let to = this.snap(q, d);
      if (ev.shiftKey) { const other = copy.points[dg.anchor === 0 ? 1 : 0]; if (other) to = { ...to, p: other.p }; }
      const an = this.anchorsOf(copy, e)[dg.anchor];
      an?.move(copy, to, e, q);
      d.points = copy.points;
    }
    this.redraw();
  };
  private onUp = (ev: MouseEvent) => {
    if (this.zoom) {
      const { a, b } = this.zoom, e = this.env();
      this.zoom = null; this.zoomMode = false;
      if (Math.abs(a.x - b.x) > 8 && Math.abs(a.y - b.y) > 8) {
        this.chart.timeScale().setVisibleLogicalRange({ from: e.lOfX(Math.min(a.x, b.x)) as Logical, to: e.lOfX(Math.max(a.x, b.x)) as Logical });
        const p1 = e.pOf(Math.min(a.y, b.y)), p2 = e.pOf(Math.max(a.y, b.y));
        this.chart.priceScale(this.style.scaleLeft ? "left" : "right").setVisibleRange({ from: Math.min(p1, p2), to: Math.max(p1, p2) });
      }
      this.emit(); this.redraw(); return;
    }
    const cr = this.creating;
    if (cr) {
      const clicks = cr.tool.clicks ?? cr.tool.n;
      if (clicks === 0) { if (cr.points.length > 1) this.finishCreate(); else { this.creating = null; this.redraw(); } return; }
      const q = this.local(ev);
      if (cr.downAt && dist(q, cr.downAt) > 6 && cr.points.length === 1 && clicks >= 2) {      // press-drag-release = second click
        cr.points.push(this.snap(q, cr.tool.screen ? ({ screen: true } as Drawing) : null));
        cr.downAt = null;
        if (cr.points.length >= clicks) this.finishCreate();
        this.redraw();
      }
      return;
    }
    const dg = this.drag;
    if (!dg) return;
    this.drag = null;
    if (dg.moved) this.commit(JSON.stringify(this.drawings.map((x) => (x.id === dg.id ? dg.orig : x))));
    this.emit();
  };
  private onHover = (ev: MouseEvent) => {
    if (this.drag || this.creating) return;
    const q = this.local(ev);
    const a = this.inPane(q) ? this.hitAnchor(q)?.id ?? this.hitDrawing(q) : null;
    if (a !== this.hoverId) { this.hoverId = a; this.redraw(); }
  };
  private onDbl = (ev: MouseEvent) => {
    const q = this.local(ev);
    if (!this.inPane(q)) return;
    const cr = this.creating;
    if (cr && (cr.tool.clicks ?? cr.tool.n) === -1) {
      cr.points = cr.points.filter((p, i) => i === 0 || p.t !== cr.points[i - 1].t || p.p !== cr.points[i - 1].p);
      this.finishCreate(); this.stop(ev); return;
    }
    if (this.tool) { this.stop(ev); return; }
    const id = this.hitDrawing(q);
    if (id) { this.select(id); const d = this.selected(); if (d) this.onSettings?.(d); this.stop(ev); }
  };
  private onContext = (ev: MouseEvent) => {
    const q = this.local(ev);
    if (!this.inPane(q)) return;
    ev.preventDefault();
    if (this.creating) { this.cancel(); return; }
    const id = this.hitDrawing(q);
    if (id) this.selectedId = id;
    const r = this.el.getBoundingClientRect();
    this.menu = { x: ev.clientX - r.left, y: ev.clientY - r.top, id };
    this.emit(); this.redraw();
  };
  cancel() {
    if (this.creating) {
      const cr = this.creating;
      if ((cr.tool.clicks ?? cr.tool.n) === -1 && cr.points.length >= 2) { this.finishCreate(); return; }
      this.creating = null;
    } else if (this.measure) this.measure = null;
    else if (this.tool || this.zoomMode || this.measureMode) { this.tool = null; this.zoomMode = false; this.measureMode = false; }
    else if (this.selectedId) this.selectedId = null;
    this.menu = null;
    this.emit(); this.redraw();
  }
  finishMulti() { if (this.creating && (this.creating.tool.clicks ?? this.creating.tool.n) === -1) this.finishCreate(); }
}

function normalize(d: Drawing): Drawing {
  const tool = TOOL[d.type];
  return { ...d, style: { ...BASE_STYLE, ...tool?.defaults, ...d.style }, text: d.text ?? "", z: d.z ?? 1, vis: { ...ALL_VIS, ...d.vis },
    points: (d.points ?? []).filter((p) => p && Number.isFinite(p.t) && Number.isFinite(p.p)) };
}
function bounds(sh: Shape[]) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const add = (x: number, y: number) => { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); };
  for (const s of sh) {
    if (s.k === "seg") { add(s.a.x, s.a.y); add(s.b.x, s.b.y); }
    else if (s.k === "area") s.pts.forEach((p) => add(p.x, p.y));
    else if (s.k === "box") { add(s.x, s.y); add(s.x + s.w, s.y + s.h); }
    else { add(s.cx - s.rx, s.cy - s.ry); add(s.cx + s.rx, s.cy + s.ry); }
  }
  return Number.isFinite(x0) ? { x0, y0: Math.max(0, y0), x1, y1 } : null;
}
