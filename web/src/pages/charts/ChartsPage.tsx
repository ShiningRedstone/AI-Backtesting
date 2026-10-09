/* Charts tab (ADR-111): a TradingView-style live chart of MNQ / NQ / ES / MES from Dukascopy (the index CFDs that
   follow the futures; labelled on the chart). Top bar: symbol, timeframes (favourites + custom), chart type, favourite
   tools, undo / redo, settings, screenshot, full screen. Left bar: cursors, every drawing tool group, measure, zoom,
   magnet, stay in drawing mode, lock / hide / remove all, object tree. Bottom bar: date ranges, go to date, clock and
   time zone, % / log / auto scale. Layout and drawings are saved in the workspace (<data>/charts/). */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ChartMeta, ChartSymbol } from "../../api/charts";
import { charts } from "../../api/charts";
import type { ChartStyle, ChartType, CtlState, CursorMode, Legend, Theme } from "./engine";
import { CHART_TYPES, ChartCtl, DEFAULT_STYLE } from "./engine";
import type { Drawing, Style } from "./model";
import { DAY, MONTH, TIME_ZONES, WEEK, fmtPrice, parseTf, resolveTz, sessionOpen, signed, tfLabel, tfSeconds, tfWords, tzName, tzOffset } from "./model";
import { ChartDialog, ContextMenu, DrawingDialog, FloatingBar, ObjectTree, ToolIcon } from "./panels";
import type { GroupId } from "./tools";
import { GROUPS, TOOL, TOOLS } from "./tools";
import { AccountPanel, TradePanel, useSim } from "./trading";
import { sim } from "../../api/sim";

type Ev = { target: HTMLInputElement & HTMLSelectElement; key: string; stopPropagation(): void; preventDefault(): void };
type ScaleMode = "normal" | "log" | "percent" | "indexed";
interface Layout {
  symbol: string; tf: number; type: ChartType; tz: string; scale: ScaleMode; invert: boolean; style: ChartStyle; favTfs: number[]; favTools: string[];
  lastTool: Partial<Record<GroupId, string>>; toolDefaults: Record<string, Partial<Style>>; magnet: number; stay: boolean; tree: boolean;
  trade: boolean; simAccount: string | null; tradeQty: number;                        // ADR-112: simulated trading
}
const STD_TFS = [1, 2, 3, 5, 10, 15, 30, 45, 60, 120, 180, 240, DAY, WEEK, MONTH];
const DEFAULT_LAYOUT: Layout = {
  symbol: "MNQ", tf: 5, type: "candles", tz: "America/New_York", scale: "normal", invert: false, style: DEFAULT_STYLE,
  favTfs: [1, 5, 15, 60, 240, DAY], favTools: ["trend_line", "horizontal_line", "fib_retracement", "rectangle", "long_position"],
  lastTool: {}, toolDefaults: {}, magnet: 0, stay: false, tree: false, trade: false, simAccount: null, tradeQty: 1,
};
const RANGES: { id: string; tf: number; days: number }[] = [
  { id: "1D", tf: 1, days: 1 }, { id: "5D", tf: 5, days: 5 }, { id: "1M", tf: 30, days: 31 }, { id: "3M", tf: 60, days: 92 },
  { id: "6M", tf: 120, days: 183 }, { id: "YTD", tf: DAY, days: -1 }, { id: "1Y", tf: DAY, days: 365 }, { id: "5Y", tf: WEEK, days: 1826 },
  { id: "All", tf: MONTH, days: 6000 },
];

function readTheme(): Theme {
  const cs = getComputedStyle(document.documentElement), v = (k: string, d: string) => cs.getPropertyValue(k).trim() || d;
  return { bg: v("--bg", "#0e0e0f"), text: v("--text", "#f4f4f5"), muted: v("--muted", "#a1a1aa"), grid: v("--grid", "#222225"),
    axis: v("--axis", "#36363c"), border: v("--surface-3", "#232326") };
}
const tfParamLabel = (tf: number) => tfLabel(tf);

export function ChartsPage() {
  const host = useRef<HTMLDivElement | null>(null);
  const wrap = useRef<HTMLDivElement | null>(null);
  const [ctl, setCtl] = useState<ChartCtl | null>(null);
  const [meta, setMeta] = useState<ChartMeta | null>(null);
  const [lay, setLay] = useState<Layout | null>(null);
  const [st, setSt] = useState<CtlState | null>(null);
  const [cursor, setCursor] = useState<CursorMode>("cross");
  const [dlg, setDlg] = useState<{ id: string; tab?: "style" | "text" } | null>(null);
  const [chartDlg, setChartDlg] = useState(false);
  const [full, setFull] = useState(false);
  const [auto, setAuto] = useState(true);
  const [lockAll, setLockAll] = useState(false);
  const [hideAll, setHideAll] = useState(false);
  const saveT = useRef(0);

  // ---- load meta + layout, then create the chart
  useEffect(() => {
    let live = true;
    Promise.all([charts.meta(), charts.layout().catch(() => ({}))]).then(([m, l]) => {
      if (!live) return;
      const L = { ...DEFAULT_LAYOUT, ...(l as Partial<Layout>) };
      L.style = { ...DEFAULT_STYLE, ...(l as Partial<Layout>).style };
      if (!m.symbols.some((s) => s.symbol === L.symbol)) L.symbol = DEFAULT_LAYOUT.symbol;
      setMeta(m); setLay(L);
    }).catch(() => { if (live) { setMeta({ symbols: [], timeframes: [], status: {} }); setLay(DEFAULT_LAYOUT); } });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    if (!lay || !host.current || ctl) return;
    const c = new ChartCtl(host.current, readTheme());
    c.toolDefaults = lay.toolDefaults; c.magnet = lay.magnet; c.stay = lay.stay;
    c.applyStyle(lay.style); c.tz = lay.tz;
    c.setType(lay.type);
    setCtl(c);
    return undefined;
  }, [lay, ctl]);
  useEffect(() => () => ctl?.dispose(), [ctl]);
  useEffect(() => ctl?.subscribe(setSt), [ctl]);
  const symOf = useCallback((s: string): ChartSymbol | null => meta?.symbols.find((x) => x.symbol === s) ?? null, [meta]);
  // ---- (re)load data when symbol / timeframe change
  const sym = lay?.symbol, tf = lay?.tf;
  const pendingView = useRef<{ from: number; to: number } | undefined>(undefined);
  useEffect(() => {
    if (!ctl || !sym || !tf) return;
    void ctl.load(sym, tf, symOf(sym), pendingView.current);
    pendingView.current = undefined;
  }, [ctl, sym, tf, symOf]);
  // ---- theme follows the app
  useEffect(() => {
    if (!ctl) return;
    const upd = () => ctl.setTheme(readTheme());
    const mo = new MutationObserver(upd);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class", "style"] });
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    mq?.addEventListener?.("change", upd);
    return () => { mo.disconnect(); mq?.removeEventListener?.("change", upd); };
  }, [ctl]);
  // ---- persist layout (debounced)
  const patch = useCallback((p: Partial<Layout>) => {
    setLay((L) => {
      if (!L) return L;
      const n = { ...L, ...p };
      window.clearTimeout(saveT.current);
      saveT.current = window.setTimeout(() => void charts.saveLayout(n as unknown as Record<string, unknown>).catch(() => undefined), 600);
      return n;
    });
  }, []);
  useEffect(() => { if (ctl) ctl.onDefaults = () => patch({ toolDefaults: { ...ctl.toolDefaults } }); }, [ctl, patch]);
  useEffect(() => {
    if (!ctl) return;
    ctl.onTextEdit = (d: Drawing) => setDlg({ id: d.id, tab: "text" });
    ctl.onSettings = (d: Drawing) => setDlg({ id: d.id });
  }, [ctl]);
  useEffect(() => { if (ctl && lay) { ctl.setScale({ mode: lay.scale, invert: lay.invert }); } }, [ctl, lay?.scale, lay?.invert]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (ctl && lay && ctl.tz !== lay.tz) ctl.setTz(lay.tz); }, [ctl, lay?.tz]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- keyboard shortcuts
  useEffect(() => {
    if (!ctl) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      if (dlg || chartDlg) return;
      const k = e.key, ctrl = e.ctrlKey || e.metaKey;
      if (k === "Escape") { ctl.cancel(); setCursor("cross"); ctl.cursorMode = "cross"; return; }
      if (k === "Enter") { ctl.finishMulti(); return; }
      if ((k === "Delete" || k === "Backspace") && ctl.selectedId) { e.preventDefault(); ctl.remove(ctl.selectedId); return; }
      if (ctrl && k.toLowerCase() === "z") { e.preventDefault(); if (e.shiftKey) ctl.redoStep(); else ctl.undoStep(); return; }
      if (ctrl && k.toLowerCase() === "y") { e.preventDefault(); ctl.redoStep(); return; }
      if (ctrl && k.toLowerCase() === "c" && ctl.selectedId) { ctl.copy(); return; }
      if (ctrl && k.toLowerCase() === "v") { ctl.paste(); return; }
      if (e.altKey) {
        const key = e.shiftKey ? k.toUpperCase() : k.toLowerCase();
        const tool = TOOLS.find((x) => x.key === key || (x.key === key.toUpperCase() && e.shiftKey));
        if (tool) { e.preventDefault(); pick(tool.id); return; }
        if (k.toLowerCase() === "i") { e.preventDefault(); patchRef.current({ invert: !layRef.current?.invert }); return; }
        if (k.toLowerCase() === "l") { e.preventDefault(); patchRef.current({ scale: layRef.current?.scale === "log" ? "normal" : "log" }); return; }
        if (k.toLowerCase() === "p") { e.preventDefault(); patchRef.current({ scale: layRef.current?.scale === "percent" ? "normal" : "percent" }); return; }
        if (k.toLowerCase() === "r") { e.preventDefault(); ctl.resetView(); setAuto(true); return; }
        if (k.toLowerCase() === "g") { e.preventDefault(); setGoto(true); return; }
      }
      if (/^[0-9]$/.test(k) && !ctrl && !e.altKey) { setTfInput(k); setTfOpen(true); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }); // re-bound each render: reads the current dialogs
  const layRef = useRef(lay); layRef.current = lay;
  const patchRef = useRef(patch); patchRef.current = patch;

  const pick = (id: string | null) => {
    if (!ctl) return;
    if (id && ctl.tool === id) { ctl.setTool(null); return; }
    ctl.setTool(id);
    if (id) patch({ lastTool: { ...layRef.current?.lastTool, [TOOL[id].group]: id } });
  };

  // ---- timeframe picker
  const [tfOpen, setTfOpen] = useState(false);
  const [tfInput, setTfInput] = useState("");
  const setTf = (v: number) => { patch({ tf: v }); setTfOpen(false); setTfInput(""); };
  const [goto, setGoto] = useState(false);
  const [symOpen, setSymOpen] = useState(false);
  const [typeOpen, setTypeOpen] = useState(false);

  const range = (r: (typeof RANGES)[number]) => {
    if (!ctl || !lay) return;
    const now = Date.now() / 1000;
    const from = r.days < 0 ? Date.UTC(new Date().getUTCFullYear(), 0, 1) / 1000 : now - r.days * 86400;
    const view = { from: r.id === "1D" ? sessionOpen(Math.floor((now + tzOffset("America/New_York", now) + 6 * 3600) / 86400) * 86400) : from, to: now };
    if (r.tf === lay.tf) void ctl.showTimes(view.from, view.to);
    else { pendingView.current = view; patch({ tf: r.tf }); }
  };
  const shot = () => {
    if (!ctl || !lay) return;
    const a = document.createElement("a");
    a.href = ctl.screenshot();
    a.download = `MunyunLab-${lay.symbol}-${tfLabel(lay.tf)}-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.png`;
    document.body.appendChild(a); a.click(); a.remove();
  };
  const selected = st?.selectedId ? st.drawings.find((d) => d.id === st.selectedId) ?? null : null;
  const symbol = lay ? symOf(lay.symbol) : null;

  const setSimAccount = useCallback((id: string | null) => patch({ simAccount: id }), [patch]);
  const simS = useSim(!!lay?.trade, lay?.symbol ?? "MNQ", meta?.symbols ?? [], ctl, lay?.simAccount ?? null, setSimAccount);
  useEffect(() => { if (ctl && symbol) ctl.tick = symbol.tick; }, [ctl, symbol]);
  const tradeMenu = (() => {
    const a = simS.acc, q = simS.quote, m = st?.menu;
    if (!lay?.trade || !a || a.state !== "active" || !m || m.id || m.price == null || !symbol) return undefined;
    const px = Math.round(m.price / symbol.tick) * symbol.tick, n = lay.tradeQty, sy = lay.symbol;
    const buyType = q?.ask != null && px >= q.ask ? "stop" : "limit", sellType = q?.bid != null && px <= q.bid ? "stop" : "limit";
    const go = (side: "buy" | "sell", type: "limit" | "stop") => void simS.act(() => sim.order(a.id,
      { symbol: sy, side, qty: n, type, tif: "day", ...(type === "limit" ? { price: px } : { stop: px }) }));
    return [{ label: `Buy ${n} ${sy} ${buyType} @ ${fmtPrice(px, 2)}`, run: () => go("buy", buyType), testId: "ch-menu-buy" },
      { label: `Sell ${n} ${sy} ${sellType} @ ${fmtPrice(px, 2)}`, run: () => go("sell", sellType), testId: "ch-menu-sell" }];
  })();
  if (!lay) return <div className="page ch-page"><div className="ch-loading">Loading the chart…</div></div>;
  return (
    <div className={`ch${full ? " ch-full" : ""}`} ref={wrap} data-testid="charts">
      {/* ---------------------------------------------------------------- top bar */}
      <div className="ch-top">
        <div className="ch-dd">
          <button type="button" className="ch-symbol" onClick={() => setSymOpen(!symOpen)} data-testid="ch-symbol">{lay.symbol}<span className="caret">▾</span></button>
          {symOpen && <div className="ch-pop ch-list" onMouseLeave={() => setSymOpen(false)}>
            {(meta?.symbols ?? []).map((s) => <button type="button" key={s.symbol} className={s.symbol === lay.symbol ? "on" : ""} data-testid={`ch-sym-${s.symbol}`}
              onClick={() => { patch({ symbol: s.symbol }); setSymOpen(false); }}><b>{s.symbol}</b><span>{s.name}</span>
              <span className="faint">${s.point_value} per point · {s.family}</span></button>)}
            <p className="ch-note">Prices: Dukascopy index CFDs (BID) that follow the futures; levels can differ from CME by a few points. MNQ and NQ draw the same
              series, MES and ES too (the contracts differ only in point value).</p>
          </div>}
        </div>
        <span className="ch-sep" />
        {lay.favTfs.map((t) => <button type="button" key={t} className={`ch-tb${t === lay.tf ? " on" : ""}`} onClick={() => setTf(t)} title={tfWords(t)}
          data-testid={`ch-tf-${tfLabel(t)}`}>{tfLabel(t)}</button>)}
        <div className="ch-dd">
          <button type="button" className={`ch-tb${lay.favTfs.includes(lay.tf) ? "" : " on"}`} onClick={() => setTfOpen(!tfOpen)} data-testid="ch-tf-more">
            {lay.favTfs.includes(lay.tf) ? "" : tfLabel(lay.tf)}<span className="caret">▾</span></button>
          {tfOpen && <div className="ch-pop ch-list ch-tf-pop">
            <form className="ch-tf-custom" onSubmit={(e: Ev) => { e.preventDefault(); const v = parseTf(tfInput); if (v) setTf(v); }}>
              <input autoFocus value={tfInput} placeholder="Custom: 7m, 6h, 1D…" onChange={(e: Ev) => setTfInput(e.target.value)} data-testid="ch-tf-input"
                onKeyDown={(e: Ev) => { if (e.key === "Escape") setTfOpen(false); e.stopPropagation(); }} />
              <button type="submit" className="ch-btn" disabled={!parseTf(tfInput)}>Go</button>
            </form>
            {[["Minutes", STD_TFS.filter((x) => x < 60)], ["Hours", STD_TFS.filter((x) => x >= 60 && x < DAY)], ["Days", STD_TFS.filter((x) => x >= DAY)]].map(([g, list]) =>
              <div key={g as string}><div className="ch-list-head">{g as string}</div>{(list as number[]).map((t) => (
                <div key={t} className={`ch-list-row${t === lay.tf ? " on" : ""}`}><button type="button" onClick={() => setTf(t)}>{tfWords(t)}</button>
                  <button type="button" className={`ch-star${lay.favTfs.includes(t) ? " on" : ""}`} title="Favourite"
                    onClick={() => patch({ favTfs: lay.favTfs.includes(t) ? lay.favTfs.filter((x) => x !== t) : [...lay.favTfs, t].sort((a, b) => a - b) })}>★</button></div>))}</div>)}
            {lay.favTfs.filter((t) => !STD_TFS.includes(t)).length > 0 && <div><div className="ch-list-head">Custom</div>{lay.favTfs.filter((t) => !STD_TFS.includes(t)).map((t) =>
              <div key={t} className="ch-list-row"><button type="button" onClick={() => setTf(t)}>{tfWords(t)}</button>
                <button type="button" className="ch-star on" onClick={() => patch({ favTfs: lay.favTfs.filter((x) => x !== t) })}>★</button></div>)}</div>}
            {!STD_TFS.includes(lay.tf) && !lay.favTfs.includes(lay.tf) && <div className="ch-list-row"><button type="button" onClick={() => setTfOpen(false)}>{tfWords(lay.tf)}</button>
              <button type="button" className="ch-star" onClick={() => patch({ favTfs: [...lay.favTfs, lay.tf].sort((a, b) => a - b) })}>★</button></div>}
          </div>}
        </div>
        <span className="ch-sep" />
        <div className="ch-dd">
          <button type="button" className="ch-tb" onClick={() => setTypeOpen(!typeOpen)} title="Chart type" data-testid="ch-type">
            {CHART_TYPES.find((x) => x.id === lay.type)?.name}<span className="caret">▾</span></button>
          {typeOpen && <div className="ch-pop ch-list" onMouseLeave={() => setTypeOpen(false)}>{CHART_TYPES.map((t) =>
            <button type="button" key={t.id} className={t.id === lay.type ? "on" : ""} data-testid={`ch-type-${t.id}`}
              onClick={() => { ctl?.setType(t.id); patch({ type: t.id }); setTypeOpen(false); }}>{t.name}</button>)}</div>}
        </div>
        <span className="ch-sep" />
        {lay.favTools.filter((id) => TOOL[id]).map((id) => <button type="button" key={id} className={`ch-ib${st?.tool === id ? " on" : ""}`} title={TOOL[id].name}
          onClick={() => pick(id)}><ToolIcon path={TOOL[id].icon} /></button>)}
        <span className="ch-spacer" />
        <button type="button" className="ch-ib" title="Undo (Ctrl+Z)" disabled={!st?.canUndo} onClick={() => ctl?.undoStep()} data-testid="ch-undo">↶</button>
        <button type="button" className="ch-ib" title="Redo (Ctrl+Y)" disabled={!st?.canRedo} onClick={() => ctl?.redoStep()} data-testid="ch-redo">↷</button>
        <span className="ch-sep" />
        <button type="button" className={`ch-tb tr-toggle${lay.trade ? " on" : ""}`} title="Simulated trading (LucidFlex 50K rules, no real orders)"
          onClick={() => patch({ trade: !lay.trade })} data-testid="ch-trade">Trade</button>
        <span className="ch-sep" />
        <button type="button" className={`ch-ib${lay.tree ? " on" : ""}`} title="Object tree" onClick={() => patch({ tree: !lay.tree })} data-testid="ch-tree-btn">☰</button>
        <button type="button" className="ch-ib" title="Chart settings" onClick={() => setChartDlg(true)} data-testid="ch-settings">⚙</button>
        <button type="button" className="ch-ib" title="Take a snapshot (PNG)" onClick={shot} data-testid="ch-shot">📷</button>
        <button type="button" className="ch-ib" title={full ? "Leave full screen" : "Full screen"} onClick={() => setFull(!full)} data-testid="ch-full">{full ? "⤡" : "⤢"}</button>
      </div>

      <div className="ch-body">
        {/* ---------------------------------------------------------------- left tool bar */}
        <LeftBar ctl={ctl} st={st} lay={lay} patch={patch} pick={pick} cursor={cursor}
          setCursor={(m) => { setCursor(m); if (ctl) { ctl.cursorMode = m; ctl.setTool(null); } }}
          lockAll={lockAll} setLockAll={(v) => { setLockAll(v); if (ctl) { ctl.lockAll = v; ctl.redraw(); } }}
          hideAll={hideAll} setHideAll={(v) => { setHideAll(v); if (ctl) { ctl.hideAll = v; ctl.redraw(); ctl.emit(); } }} />

        <div className="ch-center">
          <div className="ch-chart-wrap" onMouseDown={() => { setSymOpen(false); setTfOpen(false); setTypeOpen(false); }}>
            <div className={`ch-chart${cursor === "dot" ? " dot" : ""}`} ref={host} data-testid="ch-chart" />
            <LegendView ctl={ctl} sym={symbol} lay={lay} st={st} />
            {st?.menu && ctl && <ContextMenu ctl={ctl} menu={st.menu} onSettings={(id) => setDlg({ id })} onChartSettings={() => setChartDlg(true)} trade={tradeMenu} />}
            {selected && st?.floating && ctl && !dlg && <FloatingBar ctl={ctl} d={selected} pos={st.floating} onSettings={() => setDlg({ id: selected.id })} />}
            {st?.tool && <div className="ch-hint" data-testid="ch-hint">{hintFor(st.tool)} · Esc to stop</div>}
            {(st?.measure || st?.zoom) && <div className="ch-hint">{st.measure ? "Click, move and click again to measure (Shift + click works too)" : "Drag a box to zoom in"} · Esc to stop</div>}
          </div>
          <BottomBar ctl={ctl} lay={lay} patch={patch} range={range} auto={auto} setAuto={setAuto} goto={goto} setGoto={setGoto} />
          {lay.trade && <AccountPanel acc={simS.acc} act={simS.act} />}
        </div>
        {lay.trade && <TradePanel symbol={lay.symbol} sym={symbol} s={simS} accountId={lay.simAccount} setAccountId={setSimAccount}
          qty={lay.tradeQty} setQty={(n) => patch({ tradeQty: n })} />}
        {lay.tree && ctl && st && <ObjectTree ctl={ctl} drawings={st.drawings} selectedId={st.selectedId} />}
      </div>
      {dlg && ctl && <DrawingDialog key={dlg.id} ctl={ctl} id={dlg.id} initialTab={dlg.tab} tz={lay.tz} onClose={() => setDlg(null)} />}
      {chartDlg && ctl && <ChartDialog value={lay.style} onChange={(s) => { ctl.applyStyle(s); patch({ style: s }); }} onClose={() => setChartDlg(false)} />}
    </div>
  );
}

function hintFor(id: string) {
  const t = TOOL[id], c = t.clicks ?? t.n;
  if (c === 0) return `${t.name}: press and drag to draw`;
  if (c === -1) return `${t.name}: click each point, double-click or Enter to finish`;
  return `${t.name}: ${c === 1 ? "click to place it" : `click ${c} points (or press and drag)`}`;
}

// ------------------------------------------------------------------ left bar
function LeftBar({ ctl, st, lay, patch, pick, cursor, setCursor, lockAll, setLockAll, hideAll, setHideAll }: {
  ctl: ChartCtl | null; st: CtlState | null; lay: Layout; patch: (p: Partial<Layout>) => void; pick: (id: string | null) => void;
  cursor: CursorMode; setCursor: (m: CursorMode) => void; lockAll: boolean; setLockAll: (v: boolean) => void; hideAll: boolean; setHideAll: (v: boolean) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const active = st?.tool ?? null;
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!(e.target as HTMLElement).closest?.(".ch-left")) setOpen(null); };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  const CURSORS: [CursorMode, string, string][] = [["cross", "Cross", "M9 2v14M2 9h14"], ["dot", "Dot", "M9 9m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0"],
    ["arrow", "Arrow", "M4 2l10 7l-5 1l3 6l-2 1l-3-6l-3 4z"], ["eraser", "Eraser", "M3 13l7-9l6 5l-5 6H6zM8 15l-3-3"]];
  const cur = CURSORS.find((c) => c[0] === cursor)!;
  return (
    <div className="ch-left" data-testid="ch-left">
      <Group open={open === "cursor"} setOpen={(v) => setOpen(v ? "cursor" : null)} title={cur[1]} icon={cur[2]} on={!active && !st?.measure && !st?.zoom}
        onMain={() => { setCursor(cursor); pick(null); }}>
        {CURSORS.map(([k, l, ic]) => <button type="button" key={k} className={`ch-fly-item${cursor === k ? " on" : ""}`} onClick={() => { setCursor(k); setOpen(null); }}>
          <ToolIcon path={ic} /><span>{l}</span></button>)}
      </Group>
      <hr />
      {GROUPS.map((g) => {
        const last = lay.lastTool[g.id] && TOOL[lay.lastTool[g.id]!]?.group === g.id ? TOOL[lay.lastTool[g.id]!] : TOOLS.find((t) => t.group === g.id)!;
        const inGroup = active && TOOL[active]?.group === g.id;
        const shown = inGroup ? TOOL[active!] : last;
        const sections = [...new Set(TOOLS.filter((t) => t.group === g.id).map((t) => t.section))];
        return (
          <Group key={g.id} open={open === g.id} setOpen={(v) => setOpen(v ? g.id : null)} title={`${shown.name} (${g.name})`} icon={shown.icon} on={!!inGroup}
            onMain={() => pick(shown.id)} testId={`ch-group-${g.id}`}>
            {sections.map((sec) => <div key={sec}><div className="ch-list-head">{sec}</div>
              {TOOLS.filter((t) => t.group === g.id && t.section === sec).map((t) => (
                <div key={t.id} className={`ch-fly-item${active === t.id ? " on" : ""}`}>
                  <button type="button" className="ch-fly-main" onClick={() => { pick(t.id); setOpen(null); }} data-testid={`ch-tool-${t.id}`}>
                    <ToolIcon path={t.icon} /><span>{t.name}</span>{t.key && <kbd>Alt+{t.key === t.key.toUpperCase() && /[A-Z]/.test(t.key) ? `Shift+${t.key}` : t.key.toUpperCase()}</kbd>}</button>
                  <button type="button" className={`ch-star${lay.favTools.includes(t.id) ? " on" : ""}`} title="Show in the top bar"
                    onClick={() => patch({ favTools: lay.favTools.includes(t.id) ? lay.favTools.filter((x) => x !== t.id) : [...lay.favTools, t.id] })}>★</button>
                </div>))}</div>)}
          </Group>);
      })}
      <hr />
      <button type="button" className={`ch-lb${st?.measure ? " on" : ""}`} title="Measure (or Shift + click on the chart)" onClick={() => ctl?.setMeasureMode(!st?.measure)} data-testid="ch-measure">
        <ToolIcon path="M2 12L12 2l4 4L6 16zM5 9l2 2M8 6l2 2M11 3l2 2" /></button>
      <button type="button" className={`ch-lb${st?.zoom ? " on" : ""}`} title="Zoom in (drag a box)" onClick={() => ctl?.setZoomMode(!st?.zoom)} data-testid="ch-zoom">
        <ToolIcon path="M8 8m-5 0a5 5 0 1 0 10 0a5 5 0 1 0-10 0M12 12l4 4M6 8h4M8 6v4" /></button>
      <Group open={open === "magnet"} setOpen={(v) => setOpen(v ? "magnet" : null)} title={lay.magnet === 2 ? "Strong magnet" : lay.magnet === 1 ? "Weak magnet" : "Magnet off"}
        icon="M4 3v6a5 5 0 0 0 10 0V3h-3v6a2 2 0 0 1-4 0V3zM4 6h3M11 6h3" on={lay.magnet > 0}
        onMain={() => { const m = lay.magnet ? 0 : 1; patch({ magnet: m }); if (ctl) ctl.magnet = m; }} testId="ch-magnet">
        {[[1, "Weak magnet (snaps when near a bar's open / high / low / close)"], [2, "Strong magnet (always snaps)"], [0, "Off"]].map(([m, l]) =>
          <button type="button" key={m} className={`ch-fly-item${lay.magnet === m ? " on" : ""}`} onClick={() => { patch({ magnet: m as number }); if (ctl) ctl.magnet = m as number; setOpen(null); }}>
            <span>{l}</span></button>)}
      </Group>
      <button type="button" className={`ch-lb${lay.stay ? " on" : ""}`} title="Stay in drawing mode" onClick={() => { patch({ stay: !lay.stay }); if (ctl) ctl.stay = !lay.stay; }} data-testid="ch-stay">
        <ToolIcon path="M3 15l2-5l8-8l3 3l-8 8zM11 4l3 3M2 6h4M4 4v4" /></button>
      <button type="button" className={`ch-lb${lockAll ? " on" : ""}`} title="Lock all drawings" onClick={() => setLockAll(!lockAll)} data-testid="ch-lockall">
        <ToolIcon path={lockAll ? "M4 8h10v8H4zM6 8V5a3 3 0 0 1 6 0v3" : "M4 8h10v8H4zM6 8V5a3 3 0 0 1 6 0"} /></button>
      <button type="button" className={`ch-lb${hideAll ? " on" : ""}`} title={hideAll ? "Show all drawings" : "Hide all drawings"} onClick={() => setHideAll(!hideAll)} data-testid="ch-hideall">
        <ToolIcon path={hideAll ? "M2 9s3-5 7-5s7 5 7 5s-3 5-7 5s-7-5-7-5zM3 3l12 12" : "M2 9s3-5 7-5s7 5 7 5s-3 5-7 5s-7-5-7-5zM9 9m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0"} /></button>
      <span className="ch-dd">
        <button type="button" className="ch-lb" title="Remove all drawings" onClick={() => setConfirmAll(!confirmAll)} data-testid="ch-removeall">
          <ToolIcon path="M3 5h12M7 5V3h4v2M5 5l1 11h6l1-11" /></button>
        {confirmAll && <div className="ch-pop ch-fly ch-confirm"><p>Remove all {st?.drawings.length ?? 0} drawings on {lay.symbol}? Undo brings them back.</p>
          <button type="button" className="ch-btn" onClick={() => setConfirmAll(false)}>Cancel</button>
          <button type="button" className="ch-btn danger" onClick={() => { ctl?.removeAll(); setConfirmAll(false); }} data-testid="ch-removeall-ok">Remove all</button></div>}
      </span>
    </div>
  );
}

function Group({ open, setOpen, title, icon, on, onMain, children, testId }: { open: boolean; setOpen: (v: boolean) => void; title: string; icon: string;
  on: boolean; onMain: () => void; children: ReactNode; testId?: string }) {
  return (
    <span className="ch-group">
      <button type="button" className={`ch-lb${on ? " on" : ""}`} title={title} onClick={onMain} data-testid={testId}><ToolIcon path={icon} /></button>
      <button type="button" className={`ch-more${open ? " on" : ""}`} aria-label={`more: ${title}`} onClick={() => setOpen(!open)} data-testid={testId && `${testId}-more`}>›</button>
      {open && <div className="ch-pop ch-fly">{children}</div>}
    </span>
  );
}

// ------------------------------------------------------------------ legend
function LegendView({ ctl, sym, lay, st }: { ctl: ChartCtl | null; sym: ChartSymbol | null; lay: Layout; st: CtlState | null }) {
  const [lg, setLg] = useState<Legend | null>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => ctl?.subscribeLegend(setLg), [ctl]);
  useEffect(() => { const t = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(t); }, []);
  const s = lay.style, b = lg?.bar, prev = lg?.prev, dp = s.precision;
  const ch = b && prev ? b.close - prev.close : null;
  const left = b && lg?.live && lay.tf < DAY ? Math.max(0, b.time + tfSeconds(lay.tf) - now / 1000) : null;
  const lastAge = st?.lastTime ? now / 1000 - st.lastTime : null;
  const stale = lastAge !== null && lay.tf < DAY && lastAge > tfSeconds(lay.tf) + 15 * 60;
  return (
    <div className="ch-legend" data-testid="ch-legend">
      <div className="ch-legend-row">
        <b>{sym?.name ?? lay.symbol}</b><span className="faint">· {tfWords(lay.tf)} · Dukascopy CFD (BID)</span>
        {st?.loading && <span className="ch-chip">loading…</span>}
        {st?.error ? <span className="ch-chip warn" title={st.error}>{st.error.length > 60 ? `${st.error.slice(0, 60)}…` : st.error}</span> :
          !st?.loading && (stale ? <span className="ch-chip" title="No new prices for a while: the market is closed or the feed is quiet.">market closed / no new prices</span> :
            <span className="ch-chip live">● live</span>)}
      </div>
      {b && <div className="ch-legend-row num">
        {s.legendOHLC && <>O <b>{fmtPrice(b.open, dp)}</b> H <b>{fmtPrice(b.high, dp)}</b> L <b>{fmtPrice(b.low, dp)}</b> C <b>{fmtPrice(b.close, dp)}</b></>}
        {s.legendChange && ch !== null && prev && <span className={ch > 0 ? "pos" : ch < 0 ? "neg" : ""}> {signed(ch, dp)} ({signed((ch / prev.close) * 100)}%)</span>}
        {s.legendVolume && <span className="faint"> tick vol {Math.round(b.volume).toLocaleString()}</span>}
        {s.countdown && left !== null && left < tfSeconds(lay.tf) && <span className="faint"> · closes in {Math.floor(left / 3600) ? `${Math.floor(left / 3600)}:` : ""}
          {String(Math.floor((left % 3600) / 60)).padStart(2, "0")}:{String(Math.floor(left % 60)).padStart(2, "0")}</span>}
      </div>}
    </div>
  );
}

// ------------------------------------------------------------------ bottom bar
function BottomBar({ ctl, lay, patch, range, auto, setAuto, goto, setGoto }: { ctl: ChartCtl | null; lay: Layout; patch: (p: Partial<Layout>) => void;
  range: (r: (typeof RANGES)[number]) => void; auto: boolean; setAuto: (v: boolean) => void; goto: boolean; setGoto: (v: boolean) => void }) {
  const [now, setNow] = useState(Date.now());
  const [tzOpen, setTzOpen] = useState(false);
  const [date, setDate] = useState("");
  useEffect(() => { const t = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(t); }, []);
  useEffect(() => {
    if (!ctl) return;
    const ps = () => setAuto(ctl.isAuto());
    const t = window.setInterval(ps, 500);
    return () => window.clearInterval(t);
  }, [ctl, setAuto]);
  const sec = now / 1000, wall = new Date((sec + tzOffset(lay.tz, sec)) * 1000);
  const clock = `${String(wall.getUTCHours()).padStart(2, "0")}:${String(wall.getUTCMinutes()).padStart(2, "0")}:${String(wall.getUTCSeconds()).padStart(2, "0")}`;
  const go = () => {
    const ms = Date.parse(date.length <= 10 ? `${date}T12:00:00Z` : `${date}:00Z`);
    if (!Number.isFinite(ms) || !ctl) return;
    const g = ms / 1000, t = g - tzOffset(lay.tz, g);
    void ctl.goTo(t); setGoto(false);
  };
  const tzLabel = useMemo(() => TIME_ZONES.find((z) => z.id === lay.tz)?.label ?? resolveTz(lay.tz), [lay.tz]);
  return (
    <div className="ch-bottom">
      {RANGES.map((r) => <button type="button" key={r.id} className="ch-tb" onClick={() => range(r)} title={`Last ${r.id} on ${tfParamLabel(r.tf)}`}
        data-testid={`ch-range-${r.id}`}>{r.id}</button>)}
      <span className="ch-sep" />
      <span className="ch-dd">
        <button type="button" className="ch-tb" onClick={() => setGoto(!goto)} title="Go to date (Alt+G)" data-testid="ch-goto">Go to…</button>
        {goto && <div className="ch-pop ch-goto">
          <label>Date and time ({tzLabel})<input type="datetime-local" autoFocus value={date} onChange={(e: Ev) => setDate(e.target.value)} data-testid="ch-goto-input"
            onKeyDown={(e: Ev) => { if (e.key === "Enter") go(); if (e.key === "Escape") setGoto(false); e.stopPropagation(); }} /></label>
          <div className="ch-row"><button type="button" className="ch-btn" onClick={() => setGoto(false)}>Cancel</button>
            <button type="button" className="ch-btn primary" onClick={go} disabled={!date} data-testid="ch-goto-ok">Go to</button></div>
        </div>}
      </span>
      <button type="button" className="ch-tb" title="Scroll to the newest bar" onClick={() => ctl?.scrollToNow()}>»</button>
      <span className="ch-spacer" />
      <span className="ch-dd">
        <button type="button" className="ch-tb" onClick={() => setTzOpen(!tzOpen)} data-testid="ch-tz" title="Time zone of the chart">{clock} {tzName(lay.tz, sec)} · {tzLabel}</button>
        {tzOpen && <div className="ch-pop ch-list ch-tz-pop" onMouseLeave={() => setTzOpen(false)}>{TIME_ZONES.map((z) =>
          <button type="button" key={z.id} className={z.id === lay.tz ? "on" : ""} onClick={() => { patch({ tz: z.id }); setTzOpen(false); }}>
            <span>{z.label}</span><span className="faint">{tzName(z.id, sec)}</span></button>)}</div>}
      </span>
      <span className="ch-sep" />
      <button type="button" className={`ch-tb${lay.scale === "percent" ? " on" : ""}`} title="Percent scale (Alt+P)" onClick={() => patch({ scale: lay.scale === "percent" ? "normal" : "percent" })} data-testid="ch-pct">%</button>
      <button type="button" className={`ch-tb${lay.scale === "indexed" ? " on" : ""}`} title="Indexed to 100" onClick={() => patch({ scale: lay.scale === "indexed" ? "normal" : "indexed" })}>100</button>
      <button type="button" className={`ch-tb${lay.scale === "log" ? " on" : ""}`} title="Logarithmic scale (Alt+L)" onClick={() => patch({ scale: lay.scale === "log" ? "normal" : "log" })} data-testid="ch-log">log</button>
      <button type="button" className={`ch-tb${lay.invert ? " on" : ""}`} title="Invert scale (Alt+I)" onClick={() => patch({ invert: !lay.invert })}>inv</button>
      <button type="button" className={`ch-tb${auto ? " on" : ""}`} title="Auto-fit the price scale (drag the price scale to stretch it by hand)"
        onClick={() => { ctl?.setScale({ auto: true }); setAuto(true); }} data-testid="ch-auto">auto</button>
    </div>
  );
}
