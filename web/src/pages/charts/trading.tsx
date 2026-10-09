/* Charts → trading (ADR-112): simulated Lucid accounts. The order ticket (every Tradovate order type, brackets, OCO), the
   account's LucidFlex rule status, the account panel (positions, orders, fills, trades, days, attempts) and the chart
   lines of working orders / positions (drag to move an order, x to cancel / close). Nothing is ever sent to a broker. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ChartSymbol } from "../../api/charts";
import type { OrderSpec, OrderType, Quote, SimAccount, SimOrder, Tif } from "../../api/sim";
import { sim } from "../../api/sim";
import type { ChartCtl, TradeLine } from "./engine";
import { fmtPrice } from "./model";

type Ev = { target: HTMLInputElement & HTMLSelectElement; key: string; preventDefault(): void; stopPropagation(): void };
export const TYPE_NAMES: Record<OrderType, string> = { market: "Market", limit: "Limit", stop: "Stop", stop_limit: "Stop limit",
  mit: "Market if touched", trailing_stop: "Trailing stop", trailing_stop_limit: "Trailing stop limit" };
const TYPE_SHORT: Record<OrderType, string> = { market: "MKT", limit: "LMT", stop: "STP", stop_limit: "STP LMT", mit: "MIT",
  trailing_stop: "TRAIL", trailing_stop_limit: "TRAIL LMT" };
const TIF_NAMES: Record<Tif, string> = { day: "Day", gtc: "GTC", ioc: "IOC", fok: "FOK" };
const BUY = "#2962ff", SELL = "#f57c00";
const money = (v: number | null | undefined, sign = false) => v == null || !Number.isFinite(v) ? "–" :
  `${sign && v > 0 ? "+" : v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tone = (v: number | null | undefined) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
const when = (ms: number | null | undefined) => (ms ? new Date(ms).toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–");
const err = (e: unknown) => (e as Error)?.message ?? String(e);
export const orderPrice = (o: SimOrder) => (o.type === "limit" || o.type === "mit" || ((o.type === "stop_limit" || o.type === "trailing_stop_limit") && o.triggered)
  ? o.price : o.stop ?? o.price);

/** Account + quote polling while trading is open; the chart's order / position lines follow the account. */
export function useSim(on: boolean, symbol: string, symbols: ChartSymbol[], ctl: ChartCtl | null, accountId: string | null,
  setAccountId: (id: string | null) => void) {
  const [accounts, setAccounts] = useState<SimAccount[] | null>(null);
  const [acc, setAcc] = useState<SimAccount | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadList = useCallback(async () => {
    try {
      const r = await sim.accounts();
      setAccounts(r.accounts);
      if (!accountId && r.accounts.length) setAccountId(r.accounts[0].id);
    } catch (e) { setError(err(e)); }
  }, [accountId, setAccountId]);
  useEffect(() => { if (on) void loadList(); }, [on]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!on) { setAcc(null); return; }
    let live = true, t = 0;
    const poll = async () => {
      try {
        const [a, q] = await Promise.all([accountId ? sim.account(accountId) : Promise.resolve(null), sim.quote(symbol)]);
        if (!live) return;
        setAcc(a); setQuote(q);
      } catch (e) { if (live && accountId) { setAcc(null); setError(err(e)); } }
      if (live) t = window.setTimeout(poll, document.hidden ? 5000 : 1000);
    };
    void poll();
    return () => { live = false; window.clearTimeout(t); };
  }, [on, accountId, symbol]);
  // chart lines: orders and positions of the symbols drawn by this chart's price series
  const family = symbols.find((s) => s.symbol === symbol)?.code;
  useEffect(() => {
    if (!ctl) return;
    if (!on || !acc) { ctl.setTradeLines([]); return; }
    const same = (s: string) => symbols.find((x) => x.symbol === s)?.code === family;
    const lines: TradeLine[] = [];
    for (const p of acc.positions) if (same(p.symbol)) {
      lines.push({ id: `p:${p.symbol}`, kind: "position", price: p.avg, draggable: false, pnl: p.pnl,
        color: p.pnl >= 0 ? "#089981" : "#f23645", label: `${p.symbol} ${p.qty > 0 ? "LONG" : "SHORT"} ${Math.abs(p.qty)} @ ${fmtPrice(p.avg, 2)}   ${money(p.pnl, true)}` });
    }
    for (const o of acc.working) if (o.status === "working" && same(o.symbol)) {
      const px = orderPrice(o);
      if (px == null) continue;
      lines.push({ id: `o:${o.id}`, kind: "order", price: px, draggable: !o.type.startsWith("trailing"), color: o.side === "buy" ? BUY : SELL,
        label: `${o.side === "buy" ? "BUY" : "SELL"} ${TYPE_SHORT[o.type]} ${o.qty} ${o.symbol}${o.role === "tp" ? " TP" : o.role === "sl" ? " SL" : ""} @ ${fmtPrice(px, 2)}` });
    }
    ctl.setTradeLines(lines);
  }, [ctl, on, acc, family, symbols]);
  const act = useCallback(async (f: () => Promise<{ account?: SimAccount } | unknown>) => {
    setError(null);
    try {
      const r = (await f()) as { account?: SimAccount };
      if (r && r.account) setAcc(r.account);
      return true;
    } catch (e) { setError(err(e)); return false; }
  }, []);
  useEffect(() => {
    if (!ctl || !accountId) return;
    ctl.onTradeDrag = (line, price) => {
      const o = acc?.working.find((x) => `o:${x.id}` === line.id);
      if (!o) return;
      const key = o.type === "limit" || o.type === "mit" || o.triggered ? "price" : "stop";
      void act(() => sim.modify(accountId, o.id, { [key]: price }));
    };
    ctl.onTradeClose = (line) => {
      if (line.kind === "position") void act(() => sim.flatten(accountId, line.id.slice(2)));
      else void act(() => sim.cancel(accountId, line.id.slice(2)));
    };
  }, [ctl, accountId, acc, act]);
  return { accounts, acc, quote, error, setError, act, loadList };
}

// ------------------------------------------------------------------ right panel: account status + order ticket
export function TradePanel({ symbol, sym, s, accountId, setAccountId, qty, setQty }: { symbol: string; sym: ChartSymbol | null;
  s: ReturnType<typeof useSim>; accountId: string | null; setAccountId: (id: string | null) => void; qty: number; setQty: (n: number) => void }) {
  const { accounts, acc, quote, error, act, loadList } = s;
  const [creating, setCreating] = useState(false);
  const [confirm, setConfirm] = useState<"reset" | "delete" | null>(null);
  return (
    <aside className="tr-panel" data-testid="tr-panel">
      <div className="tr-head">
        <select className="ch-select" value={accountId ?? ""} onChange={(e: Ev) => setAccountId(e.target.value || null)} aria-label="Account" data-testid="tr-account">
          {!accounts?.length && <option value="">No account yet</option>}
          {(accounts ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}{a.state && a.state !== "active" ? ` (${a.state})` : ""}</option>)}
        </select>
        <button type="button" className="ch-btn" onClick={() => setCreating(true)} data-testid="tr-new">+ New</button>
      </div>
      {error && <div className="tr-error" data-testid="tr-error" onClick={() => s.setError(null)}>{error}</div>}
      {!acc ? <div className="tr-empty">{accounts && !accounts.length ? <>Create a simulated LucidFlex 50K account to start: it follows the
        rules of a Lucid account on Tradovate (no money, no broker).<br /><button type="button" className="ch-btn primary" onClick={() => setCreating(true)}
          data-testid="tr-new-first">Create account</button></> : "Loading…"}</div> : <>
        <AccountStatus acc={acc} />
        {acc.state === "active" ? <Ticket symbol={symbol} sym={sym} quote={quote} acc={acc} act={act} qty={qty} setQty={setQty} /> :
          <div className="tr-closed" data-testid="tr-closed"><b>{acc.state === "failed" ? "Account failed" : acc.state === "complete" ? "Account finished" : "Closed"}</b>
            <p>{acc.end_reason}</p><button type="button" className="ch-btn primary" onClick={() => setConfirm("reset")} data-testid="tr-reset">Reset (new attempt)</button></div>}
        <div className="tr-foot">
          <button type="button" className="ch-link" onClick={() => setConfirm("reset")}>Reset account</button>
          <button type="button" className="ch-link" onClick={() => setConfirm("delete")}>Delete</button>
        </div>
        <p className="ch-note">{acc.label} Rules {acc.rule_basis.toLowerCase()}.</p>
      </>}
      {creating && <NewAccount onClose={() => setCreating(false)} onCreate={async (name, bal) => {
        try { const a = await sim.create(name, bal); setCreating(false); setAccountId(a.id); await loadList(); }
        catch (e) { s.setError(err(e)); }
      }} />}
      {confirm && acc && <ConfirmBox title={confirm === "reset" ? "Reset this account?" : "Delete this account?"}
        body={confirm === "reset" ? <>A new attempt starts at {money(acc.start_balance)}; open positions and orders of the current attempt are dropped.
          The old attempts stay in the history (like buying a reset at Lucid).</> : <>The account and its whole history are removed.</>}
        ok={confirm === "reset" ? "Reset" : "Delete"} onCancel={() => setConfirm(null)}
        onOk={async () => {
          if (confirm === "reset") await act(() => sim.reset(acc.id));
          else { await act(() => sim.remove(acc.id)); setAccountId(null); await loadList(); }
          setConfirm(null);
        }} />}
    </aside>
  );
}

function Meter({ value, max, label, testId, warn }: { value: number; max: number; label: ReactNode; testId?: string; warn?: boolean }) {
  const p = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return <div className="tr-meter" data-testid={testId}><div className="tr-meter-label">{label}</div>
    <div className="tr-meter-track"><span className={warn ? "warn" : ""} style={{ width: `${p}%` }} /></div></div>;
}

function AccountStatus({ acc }: { acc: SimAccount }) {
  const r = acc.rules;
  const stage = acc.state !== "active" ? (acc.state === "failed" ? "FAILED" : acc.state === "complete" ? "FINISHED" : "CLOSED") :
    r.stage === "evaluation" ? "EVALUATION" : "FUNDED";
  const roomPct = r.max_loss > 0 ? r.room / r.max_loss : 1;
  return (
    <div className="tr-status" data-testid="tr-status">
      <div className="tr-stage"><span className={`tr-badge ${acc.state !== "active" ? "bad" : r.stage}`} data-testid="tr-stage">{stage}</span>
        <span className="faint">attempt {acc.attempt} · start {money(r.start)}</span></div>
      <div className="tr-kv">
        <span>Balance</span><b className="num">{money(r.balance)}</b>
        <span>Equity</span><b className={`num ${tone(r.equity - r.balance)}`}>{money(r.equity)}</b>
        <span>Open P&amp;L</span><b className={`num ${tone(r.unrealized)}`}>{money(r.unrealized, true)}</b>
        <span>Today</span><b className={`num ${tone(r.today_pnl)}`}>{money(r.today_pnl, true)}</b>
      </div>
      <Meter value={Math.max(0, r.room)} max={r.max_loss} warn={roomPct < 0.35} testId="tr-room"
        label={<>Max loss floor <b className="num">{money(r.floor)}</b> · room <b className={`num ${roomPct < 0.35 ? "neg" : ""}`}>{money(r.room)}</b>
          <span className="faint"> ({r.floor_locked ? "locked" : `trails the highest end-of-day balance ${money(r.highest_eod)} by ${money(r.max_loss)}${r.lock_at ? `, locks at ${money(r.lock_at)}` : ""}`})</span></>} />
      {r.stage === "evaluation" && r.target != null && <>
        <Meter value={Math.max(0, r.profit)} max={r.target} testId="tr-target"
          label={<>Profit target <b className="num">{money(r.profit, true)}</b> of {money(r.target)}{(r.target_left ?? 0) > 0 ? <span className="faint"> · {money(r.target_left)} to go</span> :
            <span className="pos"> · reached{r.consistency_ok ? ", passes at 18:00 New York" : ", consistency not met yet"}</span>}</>} />
        <div className="tr-line" data-testid="tr-consistency">Consistency: {r.profit > 0 && r.consistency_now != null ? <>best day <b className="num">{money(r.best_day)}</b> =
          {" "}<b className={r.consistency_ok ? "" : "neg"}>{r.consistency_now} %</b> of the profit (max {r.consistency_percent} %)</> :
          <>the best day may be at most {r.consistency_percent} % of the profit (checked for the pass)</>}</div>
      </>}
      {r.stage === "funded" && <>
        <Meter value={r.winning_days ?? 0} max={r.winning_days_required ?? 5} testId="tr-payout"
          label={<>Payout: <b>{r.winning_days} of {r.winning_days_required}</b> winning days (≥ {money(r.winning_day_threshold)}) · min {money(r.payout_minimum)}</>} />
        <div className="tr-line">Payouts {r.payouts?.length ?? 0}{r.payouts_left != null ? ` (${r.payouts_left} left)` : ""} · you received <b className="num">{money(r.trader_paid)}</b>
          {r.pass_date && <span className="faint"> · evaluation passed {r.pass_date}</span>}</div>
      </>}
      <div className="tr-line">Contracts: <b className="num">{r.micros_now}</b> of <b className="num">{r.micros_allowed}</b> micros (NQ / ES = 10)
        · trading days {r.trading_days}</div>
      {acc.breach && <div className="tr-error">Breach: {acc.breach.detail}</div>}
    </div>
  );
}

function Ticket({ symbol, sym, quote, acc, act, qty, setQty }: { symbol: string; sym: ChartSymbol | null; quote: Quote | null; acc: SimAccount;
  act: (f: () => Promise<unknown>) => Promise<boolean>; qty: number; setQty: (n: number) => void }) {
  const [type, setType] = useState<OrderType>("market");
  const [tif, setTif] = useState<Tif>("day");
  const [price, setPrice] = useState("");
  const [stop, setStop] = useState("");
  const [trail, setTrail] = useState("10");
  const [offset, setOffset] = useState("2");
  const [tpOn, setTpOn] = useState(false); const [tp, setTp] = useState("40");
  const [slOn, setSlOn] = useState(false); const [sl, setSl] = useState("20"); const [slTrail, setSlTrail] = useState(false);
  const [oco, setOco] = useState(false);
  const [ocoType, setOcoType] = useState<OrderType>("stop"); const [ocoPrice, setOcoPrice] = useState("");
  const tick = sym?.tick ?? 0.25;
  const mid = quote?.bid != null && quote.ask != null ? (quote.bid + quote.ask) / 2 : null;
  const seeded = useRef<string>("");
  useEffect(() => {                                                    // prefill prices near the market once per symbol / type
    if (mid == null || seeded.current === `${symbol}${type}`) return;
    seeded.current = `${symbol}${type}`;
    const r = (v: number) => (Math.round(v / tick) * tick).toFixed(2);
    setPrice(r(mid)); setStop(r(mid)); setOcoPrice(r(mid));
  }, [mid, symbol, type, tick]);
  const needs = { price: ["limit", "stop_limit", "mit"].includes(type), stop: ["stop", "stop_limit"].includes(type),
    trail: type.startsWith("trailing"), offset: type === "trailing_stop_limit" };
  const spec = (side: "buy" | "sell"): OrderSpec => {
    const o: OrderSpec = { symbol, side, qty, type, tif };
    if (needs.price) o.price = parseFloat(price);
    if (needs.stop) o.stop = parseFloat(stop);
    if (needs.trail) o.trail = parseFloat(trail);
    if (needs.offset) o.offset = parseFloat(offset);
    if (!oco) {
      if (tpOn) o.tp_ticks = parseInt(tp, 10);
      if (slOn) { o.sl_ticks = parseInt(sl, 10); o.sl_trailing = slTrail; }
    }
    return o;
  };
  const send = (side: "buy" | "sell") => {
    if (oco) {
      const second: OrderSpec = { symbol, side, qty, type: ocoType, tif: tif === "ioc" || tif === "fok" ? "day" : tif,
        ...(ocoType === "stop" ? { stop: parseFloat(ocoPrice) } : { price: parseFloat(ocoPrice) }) };
      return act(() => sim.order(acc.id, { oco: [spec(side), second] }));
    }
    return act(() => sim.order(acc.id, spec(side)));
  };
  const pos = acc.positions.find((p) => p.symbol === symbol);
  const typeLabel = TYPE_SHORT[type];
  return (
    <div className="tr-ticket" data-testid="tr-ticket">
      <div className="tr-quote">
        <span><b>{symbol}</b> <span className="faint">${sym?.point_value ?? "–"}/pt</span></span>
        <span className="num">Bid <b>{quote?.bid != null ? fmtPrice(quote.bid, 2) : "–"}</b> · Ask <b>{quote?.ask != null ? fmtPrice(quote.ask, 2) : "–"}</b></span>
      </div>
      {quote?.error && <div className="tr-error">Price feed: {quote.error}</div>}
      <div className="tr-row">
        <label>Qty</label>
        <span className="tr-qty"><button type="button" onClick={() => setQty(Math.max(1, qty - 1))}>−</button>
          <input value={qty} onChange={(e: Ev) => { const v = parseInt(e.target.value, 10); if (v > 0 && v <= 400) setQty(v); }} data-testid="tr-qty" />
          <button type="button" onClick={() => setQty(Math.min(400, qty + 1))}>+</button></span>
      </div>
      <div className="tr-row"><label>Type</label>
        <select className="ch-select" value={type} onChange={(e: Ev) => { setType(e.target.value as OrderType); if (!["market", "limit"].includes(e.target.value) && (tif === "ioc" || tif === "fok")) setTif("day"); }} data-testid="tr-type">
          {(Object.keys(TYPE_NAMES) as OrderType[]).map((k) => <option key={k} value={k}>{TYPE_NAMES[k]}</option>)}</select>
        <select className="ch-select" value={tif} onChange={(e: Ev) => setTif(e.target.value as Tif)} aria-label="Time in force" data-testid="tr-tif">
          {(Object.keys(TIF_NAMES) as Tif[]).filter((k) => !(k === "ioc" || k === "fok") || type === "market" || type === "limit")
            .map((k) => <option key={k} value={k}>{TIF_NAMES[k]}</option>)}</select></div>
      {needs.stop && <div className="tr-row"><label>Stop</label><input className="ch-num" type="number" step={tick} value={stop} onChange={(e: Ev) => setStop(e.target.value)} data-testid="tr-stop" /></div>}
      {needs.price && <div className="tr-row"><label>{type === "mit" ? "Touch price" : "Limit"}</label><input className="ch-num" type="number" step={tick} value={price} onChange={(e: Ev) => setPrice(e.target.value)} data-testid="tr-price" /></div>}
      {needs.trail && <div className="tr-row"><label>Trail (points)</label><input className="ch-num" type="number" step={tick} value={trail} onChange={(e: Ev) => setTrail(e.target.value)} data-testid="tr-trail" /></div>}
      {needs.offset && <div className="tr-row"><label>Limit offset</label><input className="ch-num" type="number" step={tick} value={offset} onChange={(e: Ev) => setOffset(e.target.value)} /></div>}
      {!oco && <div className="tr-bracket">
        <label className="ch-check"><input type="checkbox" checked={tpOn} onChange={(e: Ev) => setTpOn(e.target.checked)} data-testid="tr-tp-on" />Take profit</label>
        <input className="ch-num small" type="number" value={tp} onChange={(e: Ev) => setTp(e.target.value)} disabled={!tpOn} data-testid="tr-tp" /> <span className="faint">ticks</span>
        <label className="ch-check"><input type="checkbox" checked={slOn} onChange={(e: Ev) => setSlOn(e.target.checked)} data-testid="tr-sl-on" />Stop loss</label>
        <input className="ch-num small" type="number" value={sl} onChange={(e: Ev) => setSl(e.target.value)} disabled={!slOn} data-testid="tr-sl" /> <span className="faint">ticks</span>
        {slOn && <label className="ch-check tr-span"><input type="checkbox" checked={slTrail} onChange={(e: Ev) => setSlTrail(e.target.checked)} />trailing stop loss</label>}
      </div>}
      <label className="ch-check"><input type="checkbox" checked={oco} onChange={(e: Ev) => setOco(e.target.checked)} disabled={type === "market"} data-testid="tr-oco" />
        OCO with a second order (one fills, the other is cancelled)</label>
      {oco && <div className="tr-row"><select className="ch-select" value={ocoType} onChange={(e: Ev) => setOcoType(e.target.value as OrderType)}>
        <option value="limit">Limit</option><option value="stop">Stop</option><option value="mit">Market if touched</option></select>
        <input className="ch-num" type="number" step={tick} value={ocoPrice} onChange={(e: Ev) => setOcoPrice(e.target.value)} data-testid="tr-oco-price" /></div>}
      <div className="tr-buttons">
        <button type="button" className="tr-buy" onClick={() => void send("buy")} data-testid="tr-buy">Buy {typeLabel}<span className="num">
          {type === "market" ? (quote?.ask != null ? fmtPrice(quote.ask, 2) : "") : ""}</span></button>
        <button type="button" className="tr-sell" onClick={() => void send("sell")} data-testid="tr-sell">Sell {typeLabel}<span className="num">
          {type === "market" ? (quote?.bid != null ? fmtPrice(quote.bid, 2) : "") : ""}</span></button>
      </div>
      <div className="tr-buttons small">
        <button type="button" className="ch-btn" onClick={() => void act(() => sim.flatten(acc.id, symbol))} disabled={!pos} data-testid="tr-flatten">Flatten {symbol}</button>
        <button type="button" className="ch-btn" onClick={() => void act(() => sim.reverse(acc.id, symbol))} disabled={!pos} data-testid="tr-reverse">Reverse</button>
        <button type="button" className="ch-btn" onClick={() => void act(() => sim.cancel(acc.id))} disabled={!acc.working.length} data-testid="tr-cancel-all">Cancel all</button>
      </div>
      <button type="button" className="ch-btn tr-wide" onClick={() => void act(() => sim.flatten(acc.id))} disabled={!acc.positions.length && !acc.working.length}
        data-testid="tr-flatten-all">Exit all positions + cancel all orders</button>
      {pos && <div className="tr-line">Position: <b>{pos.qty > 0 ? "long" : "short"} {Math.abs(pos.qty)}</b> @ {fmtPrice(pos.avg, 2)} ·
        <b className={tone(pos.pnl)}> {money(pos.pnl, true)}</b></div>}
    </div>
  );
}

function NewAccount({ onClose, onCreate }: { onClose: () => void; onCreate: (name: string, bal: number) => void }) {
  const [name, setName] = useState("Lucid 50K sim");
  const [bal, setBal] = useState("50000");
  return (
    <div className="ch-modal-back" onMouseDown={(e: { target: unknown; currentTarget: unknown }) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="ch-modal" role="dialog" aria-label="New simulated account" data-testid="tr-new-dialog">
        <div className="ch-modal-head"><b>New simulated LucidFlex 50K account</b><button type="button" className="ch-x" onClick={onClose} aria-label="close">×</button></div>
        <div className="ch-modal-body"><div className="ch-form">
          <div className="ch-row"><span className="ch-row-label">Name</span><input className="ch-num tr-wide" value={name} onChange={(e: Ev) => setName(e.target.value)} data-testid="tr-new-name" /></div>
          <div className="ch-row"><span className="ch-row-label">Starting balance ($)</span><input className="ch-num" type="number" step={1000} value={bal}
            onChange={(e: Ev) => setBal(e.target.value)} data-testid="tr-new-balance" /></div>
          <p className="ch-note">Follows the app's LucidFlex 50K rules as on Tradovate: evaluation with a $3,000 profit target, a $2,000 end-of-day
            trailing max loss (locks at start + $100 once the end-of-day balance reaches start + $2,100), 50 % consistency, up to 40 micros (4 minis); then a
            funded account with scaling (20 / 30 / 40 micros), payouts after 5 winning days of $150+. All amounts count from the starting balance you set.
            Some of these rules are the app's default assumptions (listed in the account).</p>
        </div></div>
        <div className="ch-modal-foot"><span className="ch-tpl" /><button type="button" className="ch-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="ch-btn primary" onClick={() => onCreate(name, parseFloat(bal))} data-testid="tr-new-ok">Create</button></div>
      </div>
    </div>
  );
}

function ConfirmBox({ title, body, ok, onOk, onCancel }: { title: string; body: ReactNode; ok: string; onOk: () => void; onCancel: () => void }) {
  return (
    <div className="ch-modal-back"><div className="ch-modal" role="dialog" aria-label={title} data-testid="tr-confirm">
      <div className="ch-modal-head"><b>{title}</b></div><div className="ch-modal-body"><p className="small">{body}</p></div>
      <div className="ch-modal-foot"><span className="ch-tpl" /><button type="button" className="ch-btn" onClick={onCancel}>Cancel</button>
        <button type="button" className="ch-btn primary" onClick={onOk} data-testid="tr-confirm-ok">{ok}</button></div>
    </div></div>
  );
}

// ------------------------------------------------------------------ bottom panel: account details
type BTab = "positions" | "working" | "orders" | "fills" | "trades" | "days" | "attempts" | "rules";
export function AccountPanel({ acc, act }: { acc: SimAccount | null; act: (f: () => Promise<unknown>) => Promise<boolean> }) {
  const [tab, setTab] = useState<BTab>("positions");
  const [full, setFull] = useState<SimAccount | null>(null);
  const needsFull = !["positions", "working"].includes(tab);
  useEffect(() => {                                                    // the long lists only for their tabs (every 3 s)
    if (!acc || !needsFull) return;
    let live = true, t = 0;
    const poll = () => sim.account(acc.id).then((a) => { if (live) setFull(a); }).catch(() => undefined)
      .finally(() => { if (live) t = window.setTimeout(poll, 3000); });
    poll();
    return () => { live = false; window.clearTimeout(t); };
  }, [acc?.id, needsFull]); // eslint-disable-line react-hooks/exhaustive-deps
  const a = needsFull ? full ?? acc : acc;
  const TABS: [BTab, string][] = [["positions", `Positions${acc?.positions.length ? ` (${acc.positions.length})` : ""}`],
    ["working", `Working orders${acc?.working.length ? ` (${acc.working.length})` : ""}`], ["orders", "Order history"], ["fills", "Fills"],
    ["trades", "Trades"], ["days", "Days"], ["attempts", "Attempts"], ["rules", "Rules"]];
  const rows = useMemo(() => [...(a?.orders ?? [])].reverse(), [a?.orders]);
  return (
    <div className="tr-bottom" data-testid="tr-bottom">
      <div className="ch-tabs tr-tabs">{TABS.map(([k, l]) => <button type="button" key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}
        data-testid={`tr-tab-${k}`}>{l}</button>)}</div>
      <div className="tr-table">{!a ? <p className="ch-note">No account selected.</p> :
        tab === "positions" ? <table><thead><tr><th>Symbol</th><th>Side</th><th className="num">Qty</th><th className="num">Avg price</th><th className="num">Open P&amp;L</th><th /></tr></thead>
          <tbody>{a.positions.map((p) => <tr key={p.symbol}><td>{p.symbol}</td><td>{p.qty > 0 ? "Long" : "Short"}</td><td className="num">{Math.abs(p.qty)}</td>
            <td className="num">{fmtPrice(p.avg, 2)}</td><td className={`num ${tone(p.pnl)}`}>{money(p.pnl, true)}</td>
            <td><button type="button" className="ch-link" onClick={() => void act(() => sim.flatten(a.id, p.symbol))}>Close</button></td></tr>)}
            {!a.positions.length && <tr><td colSpan={6} className="faint">Flat.</td></tr>}</tbody></table> :
        tab === "working" || tab === "orders" ? <table><thead><tr><th>Placed</th><th>Symbol</th><th>Side</th><th className="num">Qty</th><th>Type</th><th className="num">Price</th>
          <th>TIF</th><th>Status</th><th>Note</th><th /></tr></thead>
          <tbody>{(tab === "working" ? a.working : rows).map((o) => <tr key={o.id}><td>{when(o.placed_ms)}</td><td>{o.symbol}</td>
            <td style={{ color: o.side === "buy" ? BUY : SELL }}>{o.side === "buy" ? "Buy" : "Sell"}</td><td className="num">{o.qty}</td>
            <td>{TYPE_NAMES[o.type]}{o.role === "tp" ? " · take profit" : o.role === "sl" ? " · stop loss" : o.role === "liquidation" ? " · liquidation" : o.oco ? " · OCO" : ""}</td>
            <td className="num">{o.status === "filled" ? fmtPrice(o.fill_price ?? 0, 2) : o.status === "pending" ? `${o.ticks} ticks from the fill` : (() => { const p = orderPrice(o); return p == null ? "–" : fmtPrice(p, 2); })()}{o.trail ? ` (trail ${o.trail})` : ""}</td>
            <td>{TIF_NAMES[o.tif]}</td><td>{o.status}</td><td className="faint">{o.reason ?? ""}</td>
            <td>{(o.status === "working" || o.status === "pending") && <button type="button" className="ch-link" onClick={() => void act(() => sim.cancel(a.id, o.id))}>Cancel</button>}</td></tr>)}
            {!(tab === "working" ? a.working : rows).length && <tr><td colSpan={10} className="faint">None.</td></tr>}</tbody></table> :
        tab === "fills" ? <table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="num">Qty</th><th className="num">Price</th><th className="num">Realized</th>
          <th className="num">Costs</th><th className="num">Position after</th><th>How</th></tr></thead>
          <tbody>{[...(a.fills ?? [])].reverse().map((f) => <tr key={f.id}><td>{when(f.ms)}</td><td>{f.symbol}</td><td>{f.side}</td><td className="num">{f.qty}</td>
            <td className="num">{fmtPrice(f.price, 2)}</td><td className={`num ${tone(f.realized)}`}>{f.realized ? money(f.realized, true) : "–"}</td>
            <td className="num">{money(f.fee)}</td><td className="num">{f.position}</td><td className="faint">{f.why}</td></tr>)}</tbody></table> :
        tab === "trades" ? <table><thead><tr><th>#</th><th>Entry</th><th>Exit</th><th className="num">Net (after costs)</th><th className="num">Worst open loss</th>
          <th className="num">Max micros</th><th className="num">Fills</th></tr></thead>
          <tbody>{[...(a.trades ?? [])].reverse().map((t) => <tr key={t.n}><td>{t.n}</td><td>{when(t.entry_ms)}</td><td>{when(t.exit_ms)}</td>
            <td className={`num ${tone(t.net)}`}>{money(t.net, true)}</td><td className="num">{t.mae ? money(-t.mae, true) : "–"}</td><td className="num">{t.micros}</td>
            <td className="num">{t.fills}</td></tr>)}</tbody></table> :
        tab === "days" ? <table><thead><tr><th>Trading day</th><th>Stage</th><th className="num">Day P&amp;L</th><th className="num">End balance</th><th className="num">Floor after</th>
          <th className="num">Trades</th></tr></thead>
          <tbody>{[...(a.days ?? [])].reverse().map((d) => <tr key={`${d.stage}${d.date}`}><td>{d.date}</td><td>{d.stage}</td><td className={`num ${tone(d.day_pnl)}`}>{money(d.day_pnl, true)}</td>
            <td className="num">{money(d.balance_end)}</td><td className="num">{money(d.floor)}</td><td className="num">{d.n_trades}</td></tr>)}
            {!(a.days ?? []).length && <tr><td colSpan={6} className="faint">No completed trading day yet (a day ends at 18:00 New York).</td></tr>}</tbody></table> :
        tab === "attempts" ? <table><thead><tr><th>Attempt</th><th className="num">Start</th><th>Started</th><th>Ended</th><th>State</th><th className="num">Trades</th>
          <th className="num">Net</th><th>Why it ended</th></tr></thead>
          <tbody>{[...(a.history ?? [])].reverse().map((h) => <tr key={h.n}><td>{h.n}</td><td className="num">{money(h.start_balance)}</td><td>{when(h.started_ms)}</td>
            <td>{when(h.ended_ms)}</td><td>{h.state}</td><td className="num">{h.trades}</td><td className={`num ${tone(h.net)}`}>{money(h.net, true)}</td>
            <td className="faint">{h.end_reason ?? ""}</td></tr>)}</tbody></table> :
        <div className="tr-rules">{(a.headline ?? []).map((l, i) => <p key={i} className="small">{l}</p>)}
          {(a.notes ?? []).map((n, i) => <p key={`n${i}`} className="ch-note">{n}</p>)}
          <p className="small"><b>Rules that are the app's default assumptions</b> (not confirmed Lucid rules):</p>
          <ul className="small">{Object.entries(a.assumed_rules).map(([k, v]) => <li key={k}><code>{k}</code> = {JSON.stringify(v.value)}: {v.basis}</li>)}</ul></div>}
      </div>
    </div>
  );
}
