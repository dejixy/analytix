import { useEffect, useRef, useState } from "react";
import { fmtPrice } from "../format.js";

/* The trading bot, shadow mode: rules built from the timeframe cards, each run with market orders and with limit
   orders side by side, filled the way Hyperliquid would (spread, fees, slippage, funding, liquidation). No orders
   are sent; the track record shows whether a rule survives real costs. */

const ROWS = { flow: "Flow", who: "Who", forced: "Forced", book: "Book", trend: "Trend", vwap: "Avg price", positioning: "Positions", funding: "Funding" };
const ROW_HELP = {
  flow: "Market orders: more buying or more selling",
  who: "Which side the biggest traders are on",
  forced: "Sweeps, cascades and liquidations",
  book: "Which way resting orders lean",
  trend: "A steady move rather than chop",
  vwap: "Price against the window's average",
  positioning: "Open interest building with the move",
  funding: "Which side is crowded",
};
const REASONS = { target: "target", stop: "stop", time: "time", flip: "flipped", liquidated: "liquidated" };
const STYLES = [["market", "Market orders"], ["limit", "Limit orders"]];
const DEFAULT = {
  coin: "BTC", timeframe: "1m", signals: ["flow", "book"], min_strength: "0.3", sides: "both", leverage: "20",
  margin_usd: "100", take_profit_bps: "20", stop_loss_bps: "15", max_hold_s: "300", exit_on_flip: true,
  cooldown_s: "30", max_trades_per_hour: "20", daily_loss_usd: "50", limit_wait_s: "10", telegram: "hourly", on: true, name: "",
};
const NUMS = ["min_strength", "leverage", "margin_usd", "take_profit_bps", "stop_loss_bps", "max_hold_s", "cooldown_s",
  "max_trades_per_hour", "daily_loss_usd", "limit_wait_s"];

const usd = (x, sign = true) => (x == null ? "-" : `${x < 0 ? "−" : sign ? "+" : ""}$${Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (x, d = 0) => (x == null ? "-" : `${Math.round(x * 100 * 10 ** d) / 10 ** d}%`);
const tone = (x) => (x == null || Math.abs(x) < 1e-9 ? "" : x > 0 ? "up" : "down");
const dur = (s) => (s == null ? "-" : s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${(s / 60).toFixed(s < 600 ? 1 : 0)} min` : `${(s / 3600).toFixed(1)} h`);
const time = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

function breakEven(r, style, fees) {
  const tp = Number(r.take_profit_bps) / 1e4, sl = Number(r.stop_loss_bps) / 1e4;
  const fin = style === "market" ? fees.taker : fees.maker;
  const win = tp - fin - fin;
  const loss = sl + fin + fees.taker;
  return win > 0 ? loss / (win + loss) : null;
}

export default function Bot({ onClose }) {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);

  const load = async () => {
    try {
      const r = await fetch("/api/bot");
      const j = await r.json();
      if (!alive.current) return;
      if (r.ok) {
        setD(j);
        setError(null);
      } else setError(j.detail || "Couldn't read the bot.");
    } catch {
      if (alive.current) setError("Couldn't reach the backend.");
    }
  };
  useEffect(() => {
    alive.current = true;
    load();
    const t = setInterval(load, 2000);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, []);

  const call = async (method, url, body) => {
    setBusy(true);
    try {
      const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
      const j = await r.json();
      if (!r.ok) {
        setError(j.detail || "That didn't work.");
        return false;
      }
      setD(j);
      setError(null);
      return true;
    } catch {
      setError("Couldn't reach the backend.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const body = { ...draft };
    for (const k of NUMS) body[k] = Number(body[k]);
    body.max_hold_s = Math.round(Number(draft.max_hold_min) * 60);
    delete body.max_hold_min;
    if (await call("POST", "/api/bot/rules", body)) setDraft(null);
  };
  const edit = (r) => setDraft({ ...r, ...Object.fromEntries(NUMS.map((k) => [k, String(r[k])])), max_hold_min: String(r.max_hold_s / 60) });
  const fresh = () => {
    const coin = d?.coins?.includes("BTC") ? "BTC" : d?.coins?.[0] || "BTC";
    setDraft({ ...DEFAULT, coin, max_hold_min: String(Number(DEFAULT.max_hold_s) / 60) });
  };

  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className="planner" role="dialog" aria-label="Bot" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2 className="panel-title">Bot</h2>
          <button className="plan-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="plan-verdict">
          <div className="plan-kicker">Shadow mode · no orders are sent</div>
          <div className="plan-safe">
            Your rules run on the live timeframe cards, and every trade is filled the way Hyperliquid itself would fill it.
            Market orders pay the spread and {d ? (d.fees.taker * 100).toFixed(3) : "0.045"}% a side. Limit orders pay{" "}
            {d ? (d.fees.maker * 100).toFixed(3) : "0.015"}% but only fill when price trades through them. Each rule runs both
            ways side by side, so you can see which one survives the costs. Live trading comes once a rule holds up here.
          </div>
        </div>

        {error && <div className="plan-error">{error}</div>}
        {!d ? (
          <p className="plan-note muted" style={{ padding: 12 }}>{error ? "" : "Loading…"}</p>
        ) : (
          <div className="plan-body bot-body">
            {draft ? (
              <Editor d={d} draft={draft} setDraft={setDraft} onSave={save} onCancel={() => setDraft(null)} busy={busy} />
            ) : (
              <section className="plan-sec bot-bar">
                <span className="plan-note muted" style={{ margin: 0 }}>
                  {d.rules.length ? `${d.rules.length} rule${d.rules.length === 1 ? "" : "s"}` : "No rules yet."}
                </span>
                {d.rules.length < d.max_rules && <button className="plan-chip on" onClick={fresh}>New rule</button>}
              </section>
            )}

            {d.rules.map((r) => (
              <RuleCard key={r.id} r={r} busy={busy} onEdit={() => edit(r)}
                onToggle={() => call("POST", "/api/bot/rules", { ...r, on: !r.on })}
                onReset={() => window.confirm(`Clear the track record of "${r.name}"?`) && call("POST", `/api/bot/rules/${r.id}/reset`)}
                onDelete={() => window.confirm(`Delete "${r.name}" and its track record?`) && call("DELETE", `/api/bot/rules/${r.id}`)} />
            ))}

            {d.recent.length > 0 && (
              <section className="plan-sec">
                <h3>Latest trades</h3>
                <div className="bot-scroll">
                  <table className="plan-table">
                    <thead><tr><th>Closed</th><th className="left">Rule</th><th>Net</th><th>Side</th><th>How</th><th>Exit</th><th>Held</th></tr></thead>
                    <tbody>
                      {d.recent.slice(0, 30).map((t, i) => (
                        <tr key={`${t.rule_id}-${t.style}-${t.exit_ms}-${i}`}>
                          <td className="num">{time(t.exit_ms)}</td>
                          <td className="left">{t.rule}</td>
                          <td className={`num ${tone(t.net_usd)}`}>{usd(t.net_usd)}</td>
                          <td>{t.side > 0 ? "long" : "short"}</td>
                          <td className="muted">{t.style}</td>
                          <td className="muted">{REASONS[t.reason] || t.reason}</td>
                          <td className="num">{dur((t.exit_ms - t.entry_ms) / 1000)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}

            <footer className="plan-foot muted">
              How fills work: a market order lands half a second after the signal at the best ask (buying) or bid (selling),
              walking the book for size. A limit order joins the best bid or ask and only counts as filled when a trade prints
              through it, so it can miss. Take-profits rest as limits in limit mode; stops, time exits and flips are market
              orders. Stops are checked on every book update, so a fast wick fills past the stop, as it would for real.
              Funding is charged while a position is open. Positions open during a server restart are dropped.
            </footer>
          </div>
        )}
      </aside>
    </div>
  );
}

function RuleCard({ r, busy, onEdit, onToggle, onReset, onDelete }) {
  const signal = r.signal_now > 0 ? "long now" : r.signal_now < 0 ? "short now" : "waiting";
  return (
    <section className={`plan-sec bot-rule ${r.on ? "" : "off"}`}>
      <div className="bot-head">
        <h3 style={{ margin: 0 }}>{r.name}</h3>
        <span className={`bot-state ${r.signal_now > 0 ? "up" : r.signal_now < 0 ? "down" : "muted"}`}>{r.on ? signal : "paused"}</span>
      </div>
      <div className="bot-tags">
        {r.rows_now.map((row) => (
          <span key={row.key} className={`plan-ctx-tag ${row.lean === "up" ? "up" : row.lean === "down" ? "down" : "muted"}`}
            title={`${row.label}: ${row.tag || "no lean"} (strength ${row.bar.toFixed(2)}, needs ${r.min_strength})`}>
            {row.label} {row.lean === "neutral" ? "flat" : row.lean} {row.bar.toFixed(2)}
          </span>
        ))}
      </div>
      <p className="plan-note" style={{ marginTop: 6 }}>
        {r.timeframe} card · {r.sides === "both" ? "longs and shorts" : `${r.sides}s only`} · {r.leverage}× on ${r.margin_usd} ·
        target +{r.take_profit_bps} bps, stop −{r.stop_loss_bps} bps · up to {dur(r.max_hold_s)}
        {r.exit_on_flip ? " · out if it flips" : ""}
      </p>

      <table className="plan-table" style={{ marginTop: 8 }}>
        <thead><tr><th></th>{STYLES.map(([k, label]) => <th key={k}>{label}</th>)}</tr></thead>
        <tbody>
          <tr><td>Trades</td>{STYLES.map(([k]) => <td key={k} className="num">{r.styles[k].trades}{k === "limit" && r.styles[k].missed ? <span className="muted"> ({r.styles[k].missed} missed)</span> : ""}</td>)}</tr>
          <tr><td>Won</td>{STYLES.map(([k]) => <td key={k} className="num">{r.styles[k].trades ? pct(r.styles[k].win_rate) : "-"}</td>)}</tr>
          <tr title="The share of trades that must hit the target, if the rest hit the stop, to break even after fees">
            <td>Needs to win</td>{STYLES.map(([k]) => <td key={k} className="num muted">{r.styles[k].break_even_win_rate == null ? "can't" : pct(r.styles[k].break_even_win_rate)}</td>)}
          </tr>
          <tr><td>Before fees</td>{STYLES.map(([k]) => <td key={k} className={`num ${tone(r.styles[k].gross_usd)}`}>{usd(r.styles[k].gross_usd)}</td>)}</tr>
          <tr><td>Fees</td>{STYLES.map(([k]) => <td key={k} className="num muted">{usd(-r.styles[k].fees_usd)}</td>)}</tr>
          <tr><td><b>Net</b></td>{STYLES.map(([k]) => <td key={k} className={`num ${tone(r.styles[k].net_usd)}`}><b>{usd(r.styles[k].net_usd)}</b></td>)}</tr>
          <tr title="Average result per trade, as a share of the margin"><td>Per trade</td>{STYLES.map(([k]) => <td key={k} className={`num ${tone(r.styles[k].avg_net_pct)}`}>{r.styles[k].avg_net_pct == null ? "-" : `${r.styles[k].avg_net_pct >= 0 ? "+" : "−"}${Math.abs(r.styles[k].avg_net_pct).toFixed(1)}%`}</td>)}</tr>
          <tr><td>Worst dip</td>{STYLES.map(([k]) => <td key={k} className="num muted">{r.styles[k].trades ? usd(-r.styles[k].max_drawdown_usd) : "-"}</td>)}</tr>
          <tr><td>Today</td>{STYLES.map(([k]) => <td key={k} className={`num ${tone(r.styles[k].day_net_usd)}`}>{r.styles[k].stopped_for_day ? "stopped" : usd(r.styles[k].day_net_usd)}</td>)}</tr>
        </tbody>
      </table>
      {STYLES.map(([k, label]) => (
        <p key={k} className="plan-note"><span className="muted">{label}:</span> {r.styles[k].verdict}</p>
      ))}
      {STYLES.map(([k, label]) => {
        const o = r.styles[k].open;
        if (!o) return null;
        return (
          <p key={`o-${k}`} className="plan-note">
            <span className="muted">{label}:</span>{" "}
            {o.waiting
              ? `waiting to ${o.side > 0 ? "buy" : "sell"} at ${fmtPrice(o.price)}`
              : <>{o.side > 0 ? "long" : "short"} from {fmtPrice(o.entry_px)}, <b className={tone(o.pnl_usd)}>{usd(o.pnl_usd)}</b> now (target {fmtPrice(o.tp_px)}, stop {fmtPrice(o.sl_px)})</>}
          </p>
        );
      })}
      <div className="bot-actions">
        <button className="plan-chip" disabled={busy} onClick={onToggle}>{r.on ? "Pause" : "Resume"}</button>
        <button className="plan-chip" disabled={busy} onClick={onEdit}>Edit</button>
        <button className="plan-chip" disabled={busy} onClick={onReset}>Clear record</button>
        <button className="plan-chip" disabled={busy} onClick={onDelete}>Delete</button>
      </div>
    </section>
  );
}

function Editor({ d, draft, setDraft, onSave, onCancel, busy }) {
  const set = (k, v) => setDraft((x) => ({ ...x, [k]: v }));
  const rows = d.timeframes[draft.timeframe] || [];
  const setTf = (tf) => setDraft((x) => ({ ...x, timeframe: tf, signals: x.signals.filter((s) => (d.timeframes[tf] || []).includes(s)) }));
  const toggle = (k) => set("signals", draft.signals.includes(k) ? draft.signals.filter((s) => s !== k) : [...draft.signals, k]);
  const maxLev = d.max_leverage?.[draft.coin];
  const lev = Number(draft.leverage) || 1;
  const liqBps = (1 / lev - (maxLev ? 1 / (2 * maxLev) : 0)) * 1e4;
  const marg = (bps) => ((Number(bps) || 0) / 1e4) * lev * 100;
  const be = { market: breakEven(draft, "market", d.fees), limit: breakEven(draft, "limit", d.fees) };
  const num = (k, label, unit, title) => (
    <label className="plan-field" title={title}>
      {label}
      <span className="hc-row">
        <span className="plan-input small"><input value={draft[k]} onChange={(e) => set(k, e.target.value)} inputMode="decimal" aria-label={label} />{unit}</span>
      </span>
    </label>
  );
  return (
    <section className="plan-sec">
      <h3>{draft.id ? "Edit rule" : "New rule"}</h3>
      <div className="plan-form" style={{ padding: 0, border: 0 }}>
        <label className="plan-field">
          Coin
          <span className="plan-holds">{d.coins.map((c) => <button key={c} type="button" className={`plan-chip ${draft.coin === c ? "on" : ""}`} onClick={() => set("coin", c)}>{c}</button>)}</span>
        </label>
        <label className="plan-field">
          Card
          <span className="plan-holds">{Object.keys(d.timeframes).map((tf) => <button key={tf} type="button" className={`plan-chip ${draft.timeframe === tf ? "on" : ""}`} onClick={() => setTf(tf)}>{tf}</button>)}</span>
        </label>
        <label className="plan-field" title="Enter when every row you pick leans the same way">
          Rows
          <span className="plan-holds">{rows.map((k) => <button key={k} type="button" title={ROW_HELP[k]} className={`plan-chip ${draft.signals.includes(k) ? "on" : ""}`} onClick={() => toggle(k)}>{ROWS[k] || k}</button>)}</span>
        </label>
        {num("min_strength", "Strength", "", "How strong each row's bar must be, from 0 to 1. Higher means fewer, stronger signals.")}
        <div className="plan-field">
          Sides
          <span className="plan-holds">{[["both", "Both"], ["long", "Longs"], ["short", "Shorts"]].map(([k, l]) => <button key={k} type="button" className={`plan-chip ${draft.sides === k ? "on" : ""}`} onClick={() => set("sides", k)}>{l}</button>)}</span>
        </div>
        {num("leverage", "Leverage", "×", maxLev ? `Hyperliquid allows up to ${maxLev}× on ${draft.coin}` : "")}
        {num("margin_usd", "Margin", "$", "Margin per trade, in dollars")}
        {num("take_profit_bps", "Target", "bps", "How far price must move your way, in basis points (100 bps = 1%)")}
        {num("stop_loss_bps", "Stop", "bps", "How far price may move against you before closing, in basis points")}
        <p className="plan-note" style={{ margin: 0 }}>
          At {lev}× the target is +{marg(draft.take_profit_bps).toFixed(0)}% of margin and the stop −{marg(draft.stop_loss_bps).toFixed(0)}%.
          Liquidation is about {liqBps.toFixed(0)} bps away{maxLev ? "" : " (assuming no maintenance margin)"}. To break even after fees,
          market orders need to win {be.market == null ? "more than they can: the target doesn't cover the fees" : pct(be.market)} of trades,
          limit orders {be.limit == null ? "more than they can" : pct(be.limit)}.
        </p>
        <label className="plan-field" title="Close at the market after this long">
          Max hold
          <span className="plan-input small"><input value={draft.max_hold_min} onChange={(e) => set("max_hold_min", e.target.value)} inputMode="decimal" aria-label="Max hold, minutes" />min</span>
        </label>
        <div className="plan-field">
          Flip
          <span className="plan-holds"><button type="button" className={`plan-chip ${draft.exit_on_flip ? "on" : ""}`} onClick={() => set("exit_on_flip", !draft.exit_on_flip)}>Close if the rows lean the other way</button></span>
        </div>
        {num("cooldown_s", "Cooldown", "s", "Wait this long after a trade closes before the next")}
        {num("max_trades_per_hour", "Per hour", "max", "At most this many entries an hour")}
        {num("daily_loss_usd", "Daily stop", "$", "Stop for the rest of the UTC day after losing this much (each order style on its own)")}
        {num("limit_wait_s", "Limit wait", "s", "How long a limit entry waits to fill before it's cancelled")}
        <div className="plan-field">
          Telegram
          <span className="plan-holds">{[["off", "Off"], ["hourly", "Hourly summary"], ["trades", "Every trade"]].map(([k, l]) => <button key={k} type="button" className={`plan-chip ${draft.telegram === k ? "on" : ""}`} onClick={() => set("telegram", k)}>{l}</button>)}</span>
        </div>
        <label className="plan-field">
          Name
          <span className="plan-input"><input value={draft.name} onChange={(e) => set("name", e.target.value)} placeholder="optional" aria-label="Name" /></span>
        </label>
        <div className="bot-actions">
          <button className="plan-chip on" disabled={busy || !draft.signals.length} onClick={onSave}>Save</button>
          <button className="plan-chip" disabled={busy} onClick={onCancel}>Cancel</button>
          {draft.id && <span className="plan-note muted" style={{ margin: 0 }}>Changing trading settings starts a fresh track record.</span>}
        </div>
      </div>
    </section>
  );
}
