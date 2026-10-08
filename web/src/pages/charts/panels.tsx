/* Charts (ADR-111): the panels around the chart - colour picker, the drawing settings dialog (style / text /
   coordinates / visibility), the chart settings dialog, the object tree, the right-click menu and the floating
   toolbar of a selected drawing. They edit drawings through the chart controller only. */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ChartCtl, ChartStyle } from "./engine";
import { DEFAULT_STYLE, PALETTES } from "./engine";
import type { Drawing, Level, Style, Vis } from "./model";
import { BASE_STYLE, joinColor, resolveTz, splitColor, tzOffset } from "./model";
import type { Field, ToolDef } from "./tools";
import { EMOJIS, TOOL } from "./tools";

/** Event shape used by the handlers here (the JSX event props are not contextually typed in this setup). */
type Ev = { target: HTMLInputElement & HTMLSelectElement & HTMLTextAreaElement; currentTarget: EventTarget; key: string; ctrlKey: boolean;
  metaKey: boolean; stopPropagation(): void; preventDefault(): void };

export function ToolIcon({ path, size = 18 }: { path: string; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth={1.3} strokeLinecap="round"
    strokeLinejoin="round" aria-hidden="true"><path d={path} /></svg>;
}

// ------------------------------------------------------------------ colour picker
const SWATCHES = ["#ffffff", "#d1d4dc", "#b2b5be", "#9598a1", "#787b86", "#5d606b", "#434651", "#2a2e39", "#131722", "#000000",
  "#f23645", "#ff9800", "#ffeb3b", "#4caf50", "#089981", "#00bcd4", "#2962ff", "#673ab7", "#9c27b0", "#e91e63",
  "#fccbcd", "#ffe0b2", "#fff9c4", "#c8e6c9", "#ace5dc", "#b2ebf2", "#bbd9fb", "#d1c4e9", "#e1bee7", "#f8bbd0",
  "#b22833", "#e65100", "#f57f17", "#1b5e20", "#056656", "#006064", "#0c3299", "#311b92", "#4a148c", "#880e4f"];

export function ColorField({ value, onChange, label, testId }: { value: string; onChange: (v: string) => void; label?: string; testId?: string }) {
  const [open, setOpen] = useState(false);
  const [hex, a] = splitColor(value);
  const ref = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <span className="ch-color" ref={ref}>
      <button type="button" className="ch-swatch" aria-label={label ?? "colour"} data-testid={testId} onClick={() => setOpen(!open)}
        style={{ background: value }} />
      {open && <div className="ch-pop ch-color-pop" role="dialog">
        <div className="ch-swatches">{SWATCHES.map((c) => <button type="button" key={c} style={{ background: c }} aria-label={c}
          className={c.toLowerCase() === hex.toLowerCase() ? "on" : ""} onClick={() => onChange(joinColor(c, a))} />)}</div>
        <div className="ch-color-row"><label>Custom <input type="color" value={hex} onChange={(e: Ev) => onChange(joinColor(e.target.value, a))} /></label>
          <label>Opacity <input type="range" min={0} max={100} value={Math.round(a * 100)} onChange={(e: Ev) => onChange(joinColor(hex, +e.target.value / 100))} />
            <span className="num">{Math.round(a * 100)} %</span></label></div>
      </div>}
    </span>
  );
}

// ------------------------------------------------------------------ small inputs
const Row = ({ label, children }: { label: string; children: ReactNode }) => <div className="ch-row"><span className="ch-row-label">{label}</span><span className="ch-row-ctl">{children}</span></div>;
const Check = ({ v, set, label, testId }: { v: boolean; set: (b: boolean) => void; label: string; testId?: string }) =>
  <label className="ch-check"><input type="checkbox" checked={v} onChange={(e: Ev) => set(e.target.checked)} data-testid={testId} />{label}</label>;
function Sel<T extends string | number>({ v, set, opts, label }: { v: T; set: (x: T) => void; opts: [T, string][]; label?: string }) {
  return <select className="ch-select" value={String(v)} aria-label={label}
    onChange={(e: Ev) => { const o = opts.find(([k]) => String(k) === e.target.value); if (o) set(o[0]); }}>
    {opts.map(([k, l]) => <option key={String(k)} value={String(k)}>{l}</option>)}</select>;
}
const Num = ({ v, set, step = 1, min, max, label }: { v: number; set: (x: number) => void; step?: number; min?: number; max?: number; label?: string }) =>
  <input className="ch-num" type="number" value={Number.isFinite(v) ? v : ""} step={step} min={min} max={max} aria-label={label}
    onChange={(e: Ev) => { const x = parseFloat(e.target.value); if (Number.isFinite(x)) set(x); }} />;
export const WIDTHS: [number, string][] = [[1, "1 px"], [2, "2 px"], [3, "3 px"], [4, "4 px"], [6, "6 px"], [10, "10 px"], [14, "14 px"], [20, "20 px"]];
export const DASHES: [number, string][] = [[0, "Solid"], [1, "Dashed"], [2, "Dotted"]];
const SIZES: [number, string][] = [10, 11, 12, 14, 16, 20, 24, 28, 32, 40].map((x) => [x, String(x)]);
const STAT_NAMES: [keyof Style, string][] = [["showPrice", "Price change"], ["showPercent", "Percent change"], ["showTicks", "Ticks"],
  ["showBars", "Bars"], ["showTime", "Time span"], ["showAngle", "Angle"], ["showDistance", "Distance (px)"]];

/** Converts a real UTC time to the value of a datetime-local input in `tz` and back. */
const toLocalInput = (t: number, tz: string) => new Date((t + tzOffset(tz, t)) * 1000).toISOString().slice(0, 16);
function fromLocalInput(v: string, tz: string): number | null {
  const ms = Date.parse(`${v}:00Z`);
  if (!Number.isFinite(ms)) return null;
  const guess = ms / 1000;
  return guess - tzOffset(tz, guess - tzOffset(tz, guess));
}

// ------------------------------------------------------------------ drawing settings
type DTab = "style" | "text" | "coords" | "vis";
export function DrawingDialog({ ctl, id, initialTab, tz, onClose }: { ctl: ChartCtl; id: string; initialTab?: DTab; tz: string; onClose: () => void }) {
  const orig = useRef<string>(JSON.stringify(ctl.drawings));
  const first = ctl.drawings.find((d) => d.id === id);
  const [d, setD] = useState<Drawing | null>(first ? JSON.parse(JSON.stringify(first)) : null);
  const tool: ToolDef | undefined = d ? TOOL[d.type] : undefined;
  const hasText = !!tool?.fields.some((f) => ["text", "textColor", "fontSize"].includes(f as string));
  const [tab, setTab] = useState<DTab>(initialTab === "text" && !hasText ? "style" : initialTab ?? "style");
  useEffect(() => { if (d) ctl.preview(d); }, [d, ctl]);
  const keys = useRef<(e: KeyboardEvent) => void>(() => undefined);
  useEffect(() => {
    const f = (e: KeyboardEvent) => keys.current(e);
    window.addEventListener("keydown", f);
    return () => window.removeEventListener("keydown", f);
  }, []);
  if (!d || !tool) return null;
  const set = (f: (x: Drawing) => void) => setD((x) => { if (!x) return x; const y: Drawing = JSON.parse(JSON.stringify(x)); f(y); return y; });
  const st = <K extends keyof Style>(k: K, v: Style[K]) => set((x) => { x.style[k] = v; });
  const has = (f: Field) => tool.fields.includes(f);
  const cancel = () => { ctl.restore(orig.current); onClose(); };
  const ok = () => { ctl.commitFrom(orig.current); onClose(); };
  keys.current = (e: KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); cancel(); }
    else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ok(); }
  };
  const s = d.style;
  const styleFields = (
    <div className="ch-form">
      {has("color") && <Row label="Line"><ColorField value={s.color} onChange={(v) => st("color", v)} testId="ch-dlg-color" />
        {has("width") && <Sel v={s.width} set={(v) => st("width", v)} opts={WIDTHS} label="Width" />}
        {has("dash") && <Sel v={s.dash} set={(v) => st("dash", v)} opts={DASHES} label="Line style" />}</Row>}
      {!has("color") && has("width") && <Row label="Width"><Sel v={s.width} set={(v) => st("width", v)} opts={WIDTHS} /></Row>}
      {has("fill") && <Row label="Background"><Check v={s.fillOn} set={(v) => st("fillOn", v)} label="" /><ColorField value={s.fill} onChange={(v) => st("fill", v)} /></Row>}
      {has("middle") && <Row label="Middle line"><Check v={s.middle} set={(v) => st("middle", v)} label="Show" /></Row>}
      {(has("extendLeft") || has("extendRight")) && <Row label="Extend">
        {has("extendLeft") && <Check v={s.extendLeft} set={(v) => st("extendLeft", v)} label="Left" />}
        {has("extendRight") && <Check v={s.extendRight} set={(v) => st("extendRight", v)} label="Right" />}</Row>}
      {has("leftEnd") && <Row label="Ends"><Sel v={s.leftEnd} set={(v) => st("leftEnd", v)} opts={[["none", "Left: normal"], ["arrow", "Left: arrow"]]} />
        <Sel v={s.rightEnd} set={(v) => st("rightEnd", v)} opts={[["none", "Right: normal"], ["arrow", "Right: arrow"]]} /></Row>}
      {STAT_NAMES.some(([k]) => has(k)) && <Row label="Stats">{STAT_NAMES.filter(([k]) => has(k)).map(([k, l]) =>
        <Check key={k} v={!!s[k]} set={(v) => st(k, v as never)} label={l} />)}</Row>}
      {has("showPrice") && !STAT_NAMES.slice(1).some(([k]) => has(k)) && null}
      {has("upper") && <Row label="Deviation"><span>Upper <Num v={s.upper} set={(v) => st("upper", v)} step={0.1} /></span>
        <span>Lower <Num v={s.lower} set={(v) => st("lower", v)} step={0.1} /></span></Row>}
      {has("rows") && <Row label="Rows"><Num v={s.rows} set={(v) => st("rows", Math.round(v))} min={4} max={200} /></Row>}
      {has("valueArea") && <Row label="Value area"><Num v={s.valueArea} set={(v) => st("valueArea", v)} min={10} max={100} /> %</Row>}
      {has("upColor") && <Row label="Volume colours"><ColorField value={s.upColor} onChange={(v) => st("upColor", v)} label="up volume" />
        <ColorField value={s.downColor} onChange={(v) => st("downColor", v)} label="down volume" /></Row>}
      {has("account") && <Row label="Account size ($)"><Num v={s.account} set={(v) => st("account", v)} step={1000} /></Row>}
      {has("risk") && <Row label="Risk"><Num v={s.risk} set={(v) => st("risk", v)} step={s.riskPct ? 0.1 : 50} />
        <Sel v={s.riskPct ? "pct" : "usd"} set={(v) => st("riskPct", v === "pct")} opts={[["pct", "% of account"], ["usd", "$"]]} /></Row>}
      {has("qty") && <Row label="Contracts (if risk is 0)"><Num v={s.qty} set={(v) => st("qty", Math.max(0, Math.round(v)))} /></Row>}
      {has("candles") && <Row label="Draw as"><Sel v={s.candles} set={(v) => st("candles", v)} opts={[["candles", "Candles"], ["line", "Line"]]} />
        <Check v={s.mirror} set={(v) => st("mirror", v)} label="Mirrored (time reversed)" /><Check v={s.flip} set={(v) => st("flip", v)} label="Flipped (upside down)" /></Row>}
      {has("degree") && <Row label="Wave labels"><Sel v={s.degree} set={(v) => st("degree", v)}
        opts={[[0, "① circled"], [1, "[1] brackets"], [2, "(1) parentheses"], [3, "1 plain"], [4, "i roman"]]} /></Row>}
      {has("size") && <Row label="Size"><Num v={s.size} set={(v) => st("size", Math.max(8, Math.min(120, v)))} /></Row>}
      {has("emoji") && <Row label="Emoji"><span className="ch-emojis">{EMOJIS.map((x) => <button type="button" key={x} className={x === s.emoji ? "on" : ""}
        onClick={() => st("emoji", x)}>{x}</button>)}</span></Row>}
      {has("levels") && <LevelsEditor d={d} set={set} />}
      {tool.note && <p className="ch-note">{tool.note}</p>}
    </div>
  );
  const textFields = (
    <div className="ch-form">
      {has("text") && <textarea className="ch-text" value={d.text} rows={4} autoFocus={tab === "text"} data-testid="ch-dlg-text"
        onChange={(e: Ev) => { const v = e.target.value; set((x) => { x.text = v; }); }} placeholder="Text" />}
      {has("textColor") && <Row label="Text"><ColorField value={s.textColor} onChange={(v) => st("textColor", v)} />
        {has("fontSize") && <Sel v={s.fontSize} set={(v) => st("fontSize", v)} opts={SIZES} label="Font size" />}
        {has("bold") && <button type="button" className={`ch-tbtn${s.bold ? " on" : ""}`} onClick={() => st("bold", !s.bold)}><b>B</b></button>}
        {has("italic") && <button type="button" className={`ch-tbtn${s.italic ? " on" : ""}`} onClick={() => st("italic", !s.italic)}><i>I</i></button>}</Row>}
      {has("bg") && <Row label="Background">{has("bgOn") && <Check v={s.bgOn} set={(v) => st("bgOn", v)} label="" />}<ColorField value={s.bg} onChange={(v) => st("bg", v)} /></Row>}
      {has("border") && <Row label="Border">{has("borderOn") && <Check v={s.borderOn} set={(v) => st("borderOn", v)} label="" />}<ColorField value={s.border} onChange={(v) => st("border", v)} /></Row>}
      {has("hAlign") && <Row label="Position"><Sel v={s.hAlign} set={(v) => st("hAlign", v)} opts={[["left", "Left"], ["center", "Center"], ["right", "Right"]]} />
        <Sel v={s.vAlign} set={(v) => st("vAlign", v)} opts={[["top", "Top"], ["middle", "Middle"], ["bottom", "Bottom"]]} /></Row>}
    </div>
  );
  const coords = (
    <div className="ch-form">
      {d.screen ? <p className="ch-note">Stays at the same place on the screen ({Math.round(d.points[0].t * 100)} % from the left, {Math.round(d.points[0].p * 100)} % from the top).</p> :
        d.points.map((p, i) => (
          <Row key={i} label={`Point ${i + 1}`}>
            <span>Price <Num v={+p.p.toFixed(2)} step={0.25} set={(v) => set((x) => { x.points[i].p = v; })} label={`price ${i + 1}`} /></span>
            <input className="ch-num ch-dt" type="datetime-local" value={toLocalInput(p.t, tz)} aria-label={`time ${i + 1}`}
              onChange={(e: Ev) => { const t = fromLocalInput(e.target.value, tz); if (t !== null) set((x) => { x.points[i].t = t; }); }} />
          </Row>))}
      <p className="ch-note">Times in {resolveTz(tz)}.</p>
    </div>
  );
  const VIS: [keyof Vis, string][] = [["m", "Minutes"], ["h", "Hours"], ["d", "Days"], ["w", "Weeks"], ["mo", "Months"]];
  const vis = (
    <div className="ch-form">
      <p className="ch-note">Show this drawing on these timeframes:</p>
      {VIS.map(([k, l]) => <Check key={k} v={(d.vis ?? { m: true, h: true, d: true, w: true, mo: true })[k]} label={l}
        set={(v) => set((x) => { x.vis = { ...(x.vis ?? { m: true, h: true, d: true, w: true, mo: true }), [k]: v }; })} />)}
    </div>
  );
  return (
    <div className="ch-modal-back" onMouseDown={(e: Ev) => { if (e.target === e.currentTarget) ok(); }}>
      <div className="ch-modal" role="dialog" aria-label={`${tool.name} settings`} data-testid="ch-drawing-dialog">
        <div className="ch-modal-head"><b>{d.name || tool.name}</b><button type="button" className="ch-x" onClick={cancel} aria-label="close">×</button></div>
        <div className="ch-tabs">{([["style", "Style"], ...(hasText ? [["text", "Text"]] : []), ["coords", "Coordinates"], ["vis", "Visibility"]] as [DTab, string][])
          .map(([k, l]) => <button type="button" key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)} data-testid={`ch-dlg-tab-${k}`}>{l}</button>)}</div>
        <div className="ch-modal-body">{tab === "style" ? styleFields : tab === "text" ? textFields : tab === "coords" ? coords : vis}</div>
        <div className="ch-modal-foot">
          <span className="ch-tpl">
            <button type="button" className="ch-link" onClick={() => { ctl.toolDefaults[d.type] = templateOf(d.style, tool); ctl.onDefaults?.(); }}>Save as default</button>
            <button type="button" className="ch-link" onClick={() => set((x) => { x.style = { ...BASE_STYLE, ...tool.defaults }; if (tool.levels) x.levels = JSON.parse(JSON.stringify(tool.levels)); })}>Reset</button>
          </span>
          <button type="button" className="ch-btn" onClick={cancel}>Cancel</button>
          <button type="button" className="ch-btn primary" onClick={ok} data-testid="ch-dlg-ok">Ok</button>
        </div>
      </div>
    </div>
  );
}
function templateOf(s: Style, tool: ToolDef): Partial<Style> {
  const out: Partial<Style> = {};
  for (const f of tool.fields) if (f !== "text" && f !== "levels" && f in s) (out as Record<string, unknown>)[f] = s[f as keyof Style];
  return out;
}

function LevelsEditor({ d, set }: { d: Drawing; set: (f: (x: Drawing) => void) => void }) {
  const L = d.levels ?? [], s = d.style;
  const upd = (i: number, f: (l: Level) => void) => set((x) => { if (x.levels) f(x.levels[i]); });
  return (
    <div className="ch-levels">
      <div className="ch-levels-grid">{L.map((l, i) => (
        <span key={i} className="ch-level">
          <input type="checkbox" checked={l.on} onChange={(e: Ev) => upd(i, (z) => { z.on = e.target.checked; })} aria-label={`level ${l.v}`} />
          <input className="ch-num small" type="number" step={0.001} value={l.v} onChange={(e: Ev) => { const v = parseFloat(e.target.value); if (Number.isFinite(v)) upd(i, (z) => { z.v = v; }); }} />
          <ColorField value={l.color} onChange={(c) => upd(i, (z) => { z.color = c; })} />
          <button type="button" className="ch-x small" aria-label="remove level" onClick={() => set((x) => { x.levels = x.levels?.filter((_, j) => j !== i); })}>×</button>
        </span>))}</div>
      <div className="ch-row"><button type="button" className="ch-link" onClick={() => set((x) => { x.levels = [...(x.levels ?? []), { v: 1.5, color: "#2962ff", on: true }]; })}>+ Add level</button>
        {"showLabels" in s && <Check v={s.showLabels} set={(v) => set((x) => { x.style.showLabels = v; })} label="Levels" />}
        <Check v={s.showLevelPrices} set={(v) => set((x) => { x.style.showLevelPrices = v; })} label="Prices" />
        <Check v={s.labelsLeft} set={(v) => set((x) => { x.style.labelsLeft = v; })} label="Labels on the left" />
        <Check v={s.reverse} set={(v) => set((x) => { x.style.reverse = v; })} label="Reverse" /></div>
    </div>
  );
}

// ------------------------------------------------------------------ chart settings
type CTab = "symbol" | "status" | "scales" | "canvas";
export function ChartDialog({ value, onChange, onClose }: { value: ChartStyle; onChange: (s: ChartStyle) => void; onClose: () => void }) {
  const [tab, setTab] = useState<CTab>("symbol");
  const orig = useRef(value);
  useEffect(() => {
    const f = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", f);
    return () => window.removeEventListener("keydown", f);
  }, [onClose]);
  const s = value;
  const set = <K extends keyof ChartStyle>(k: K, v: ChartStyle[K]) => onChange({ ...s, [k]: v, ...(["up", "down", "wickUp", "wickDown", "borderUp", "borderDown", "line"].includes(k) ? { palette: "custom" } : {}) });
  const body = tab === "symbol" ? (
    <div className="ch-form">
      <Row label="Colours"><Sel v={s.palette} set={(p) => onChange(p === "custom" ? { ...s, palette: p } : { ...s, palette: p, ...PALETTES[p] })}
        opts={[["munyun", "Munyun (green up / grey down)"], ["tradingview", "TradingView (green up / red down)"], ["custom", "Custom"]]} /></Row>
      <Row label="Body"><ColorField value={s.up} onChange={(v) => set("up", v)} label="up body" /><ColorField value={s.down} onChange={(v) => set("down", v)} label="down body" /></Row>
      <Row label="Borders"><ColorField value={s.borderUp} onChange={(v) => set("borderUp", v)} /><ColorField value={s.borderDown} onChange={(v) => set("borderDown", v)} /></Row>
      <Row label="Wicks"><ColorField value={s.wickUp} onChange={(v) => set("wickUp", v)} /><ColorField value={s.wickDown} onChange={(v) => set("wickDown", v)} /></Row>
      <Row label="Line / area"><ColorField value={s.line} onChange={(v) => set("line", v)} /></Row>
      <Row label="Decimals"><Sel v={s.precision} set={(v) => set("precision", v)} opts={[[0, "0"], [1, "0.0"], [2, "0.00"], [3, "0.000"]]} /></Row>
    </div>) : tab === "status" ? (
    <div className="ch-form">
      <Check v={s.legendOHLC} set={(v) => set("legendOHLC", v)} label="Open / high / low / close" />
      <Check v={s.legendChange} set={(v) => set("legendChange", v)} label="Change from the previous bar" />
      <Check v={s.legendVolume} set={(v) => set("legendVolume", v)} label="Tick volume (Dukascopy price changes, not contracts)" />
      <Check v={s.countdown} set={(v) => set("countdown", v)} label="Countdown to the bar close" />
    </div>) : tab === "scales" ? (
    <div className="ch-form">
      <Check v={s.lastLabel} set={(v) => set("lastLabel", v)} label="Last price label" />
      <Check v={s.priceLine} set={(v) => set("priceLine", v)} label="Last price line" />
      <Check v={s.scaleLeft} set={(v) => set("scaleLeft", v)} label="Price scale on the left" />
      <Row label="Space on the right"><Num v={s.rightOffset} set={(v) => set("rightOffset", Math.max(0, Math.min(200, Math.round(v))))} /> bars</Row>
    </div>) : (
    <div className="ch-form">
      <Row label="Background"><Check v={s.bgAuto} set={(v) => set("bgAuto", v)} label="From the app theme" />{!s.bgAuto && <ColorField value={s.bg} onChange={(v) => set("bg", v)} />}</Row>
      <Row label="Grid"><Check v={s.gridV} set={(v) => set("gridV", v)} label="Vertical" /><Check v={s.gridH} set={(v) => set("gridH", v)} label="Horizontal" />
        <ColorField value={s.grid || "#222225"} onChange={(v) => set("grid", v)} /></Row>
      <Row label="Crosshair"><Sel v={s.crosshair} set={(v) => set("crosshair", v)} opts={[["normal", "Free"], ["magnet", "Snaps to the close"], ["hidden", "Hidden"]]} />
        <ColorField value={s.crossColor || "#a1a1aa"} onChange={(v) => set("crossColor", v)} /></Row>
      <Check v={s.watermark} set={(v) => set("watermark", v)} label="Symbol watermark" />
      <Check v={s.sessions} set={(v) => set("sessions", v)} label="Session breaks (18:00 New York)" />
    </div>);
  return (
    <div className="ch-modal-back" onMouseDown={(e: Ev) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="ch-modal" role="dialog" aria-label="Chart settings" data-testid="ch-chart-dialog">
        <div className="ch-modal-head"><b>Chart settings</b><button type="button" className="ch-x" onClick={onClose} aria-label="close">×</button></div>
        <div className="ch-tabs">{([["symbol", "Symbol"], ["status", "Status line"], ["scales", "Scales"], ["canvas", "Canvas"]] as [CTab, string][])
          .map(([k, l]) => <button type="button" key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l}</button>)}</div>
        <div className="ch-modal-body">{body}</div>
        <div className="ch-modal-foot"><span className="ch-tpl"><button type="button" className="ch-link" onClick={() => onChange({ ...DEFAULT_STYLE })}>Reset all</button></span>
          <button type="button" className="ch-btn" onClick={() => { onChange(orig.current); onClose(); }}>Cancel</button>
          <button type="button" className="ch-btn primary" onClick={onClose}>Ok</button></div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ object tree, menu, floating bar
export function ObjectTree({ ctl, drawings, selectedId }: { ctl: ChartCtl; drawings: Drawing[]; selectedId: string | null }) {
  const [edit, setEdit] = useState<string | null>(null);
  const list = [...drawings].sort((a, b) => b.z - a.z);
  return (
    <aside className="ch-tree" data-testid="ch-tree">
      <div className="ch-tree-head"><b>Object tree</b><span className="faint">{drawings.length}</span></div>
      {!list.length && <p className="ch-note">No drawings on this symbol yet.</p>}
      <ul>{list.map((d) => {
        const t = TOOL[d.type];
        return (
          <li key={d.id} className={d.id === selectedId ? "on" : ""} onClick={() => ctl.select(d.id)} onDoubleClick={() => setEdit(d.id)}>
            <ToolIcon path={t?.icon ?? ""} size={15} />
            {edit === d.id ? <input className="ch-num" autoFocus defaultValue={d.name || t?.name} onBlur={(e: Ev) => { const v = e.target.value.trim(); ctl.update(d.id, (x) => { x.name = v || undefined; }); setEdit(null); }}
              onKeyDown={(e: Ev) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setEdit(null); e.stopPropagation(); }} /> :
              <span className="ch-tree-name" title="Double-click to rename">{d.name || t?.name}{d.text && !d.name ? `: ${d.text.slice(0, 24)}` : ""}</span>}
            <button type="button" title={d.hidden ? "Show" : "Hide"} className={d.hidden ? "off" : ""} onClick={(e: Ev) => { e.stopPropagation(); ctl.update(d.id, (x) => { x.hidden = !x.hidden; }); }}>{d.hidden ? "◌" : "◉"}</button>
            <button type="button" title={d.locked ? "Unlock" : "Lock"} onClick={(e: Ev) => { e.stopPropagation(); ctl.update(d.id, (x) => { x.locked = !x.locked; }); }}>{d.locked ? "🔒" : "🔓"}</button>
            <button type="button" title="Remove" onClick={(e: Ev) => { e.stopPropagation(); ctl.remove(d.id); }}>🗑</button>
          </li>);
      })}</ul>
    </aside>
  );
}

export function ContextMenu({ ctl, menu, onSettings, onChartSettings }: { ctl: ChartCtl; menu: { x: number; y: number; id: string | null };
  onSettings: (id: string) => void; onChartSettings: () => void }) {
  const d = menu.id ? ctl.drawings.find((x) => x.id === menu.id) : null;
  const item = (label: string, f: () => void, testId?: string) =>
    <button type="button" onClick={() => { ctl.closeMenu(); f(); }} data-testid={testId}>{label}</button>;
  return (
    <div className="ch-menu" style={{ left: menu.x, top: menu.y }} role="menu" data-testid="ch-menu" onMouseDown={(e: Ev) => e.stopPropagation()}>
      {d ? <>
        {item("Settings…", () => onSettings(d.id), "ch-menu-settings")}
        {item("Clone", () => ctl.clone(d.id), "ch-menu-clone")}
        {item("Copy", () => { ctl.select(d.id); ctl.copy(); })}
        <hr />
        {item("Bring to front", () => ctl.order(d.id, "front"))}{item("Bring forward", () => ctl.order(d.id, "forward"))}
        {item("Send backward", () => ctl.order(d.id, "backward"))}{item("Send to back", () => ctl.order(d.id, "back"))}
        <hr />
        {item(d.locked ? "Unlock" : "Lock", () => ctl.update(d.id, (x) => { x.locked = !x.locked; }))}
        {item("Hide", () => ctl.update(d.id, (x) => { x.hidden = true; }))}
        {item("Remove", () => ctl.remove(d.id), "ch-menu-remove")}
      </> : <>
        {item("Reset chart view", () => ctl.resetView())}
        {item("Paste drawing", () => ctl.paste())}
        {item(ctl.hideAll ? "Show all drawings" : "Hide all drawings", () => { ctl.hideAll = !ctl.hideAll; ctl.emit(); ctl.redraw(); })}
        {item("Remove all drawings", () => ctl.removeAll())}
        <hr />
        {item("Chart settings…", onChartSettings)}
      </>}
    </div>
  );
}

export function FloatingBar({ ctl, d, pos, onSettings }: { ctl: ChartCtl; d: Drawing; pos: { x: number; y: number }; onSettings: () => void }) {
  const tool = TOOL[d.type];
  const has = (f: Field) => tool?.fields.includes(f);
  const colorKey: keyof Style = has("color") ? "color" : has("textColor") ? "textColor" : "color";
  return (
    <div className="ch-float" style={{ left: pos.x, top: pos.y }} data-testid="ch-float" onMouseDown={(e: Ev) => e.stopPropagation()}>
      <span className="ch-float-name">{d.name || tool?.name}</span>
      {(has("color") || has("textColor")) && <ColorField value={d.style[colorKey] as string} label="colour" onChange={(v) => ctl.update(d.id, (x) => { (x.style[colorKey] as string) = v; })} />}
      {has("width") && <Sel v={d.style.width} set={(v) => ctl.update(d.id, (x) => { x.style.width = v; })} opts={WIDTHS} label="Width" />}
      {has("dash") && <Sel v={d.style.dash} set={(v) => ctl.update(d.id, (x) => { x.style.dash = v; })} opts={DASHES} label="Line style" />}
      <button type="button" title="Settings" onClick={onSettings} data-testid="ch-float-settings">⚙</button>
      <button type="button" title={d.locked ? "Unlock" : "Lock"} onClick={() => ctl.update(d.id, (x) => { x.locked = !x.locked; })}>{d.locked ? "🔒" : "🔓"}</button>
      <button type="button" title="Clone" onClick={() => ctl.clone(d.id)}>⧉</button>
      <button type="button" title="Remove" onClick={() => ctl.remove(d.id)} data-testid="ch-float-remove">🗑</button>
    </div>
  );
}
