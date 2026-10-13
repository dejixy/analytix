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
const STYLES = [["market", "Market"], ["limit", "Limit"], ["maker_exit", "Market in, limit out"]];
const STYLE_SHORT = { market: "market", limit: "limit", maker_exit: "mkt in, lmt out" };
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
  const fin = style === "limit" ? fees.maker : fees.taker;
  const fout = style === "market" ? fees.taker : fees.maker;
  const win = tp - fin - fout;
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
            The auto hunter watches every coin around the clock and trades each setup the moment it appears. Your own rules
            run on the timeframe cards. Every trade is filled the way Hyperliquid itself would fill it: market orders pay the
            spread and {d ? (d.fees.taker * 100).toFixed(3) : "0.045"}% a side, limit orders pay{" "}
            {d ? (d.fees.maker * 100).toFixed(3) : "0.015"}% but only fill when price trades through them. Each idea runs with each
            order type side by side, so you can see what survives the costs. Live trading comes once something holds up here.
          </div>
        </div>

        {error && <div className="plan-error">{error}</div>}
        {!d ? (
          <p className="plan-note muted" style={{ padding: 12 }}>{error ? "" : "Loading…"}</p>
        ) : (
          <div className="plan-body bot-body">
            {d.auto && <Auto a={d.auto} busy={busy}
              onSave={(patch) => call("PUT", "/api/bot/auto", patch)}
              onReset={() => window.confirm("Clear the auto hunter's whole track record?") && call("POST", "/api/bot/auto/reset")} />}

            {draft ? (
              <Editor d={d} draft={draft} setDraft={setDraft} onSave={save} onCancel={() => setDraft(null)} busy={busy} />
            ) : (
              <section className="plan-sec bot-bar">
                <span className="plan-note muted" style={{ margin: 0 }}>
                  Your rules: {d.rules.length ? d.rules.length : "none yet"}
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

            <footer className="plan-foot muted">Stops fill on wicks. Funding is charged. Open trades are dropped on restart.</footer>
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
  const be = Object.fromEntries(STYLES.map(([k]) => [k, breakEven(draft, k, d.fees)]));
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
          Target +{marg(draft.take_profit_bps).toFixed(0)}%, stop −{marg(draft.stop_loss_bps).toFixed(0)}% of margin · liquidation ~{liqBps.toFixed(0)} bps away ·
          breaks even at {STYLES.map(([k, l]) => `${be[k] == null ? "never" : pct(be[k])} wins (${STYLE_SHORT[k]})`).join(", ")}
        </p>
        <label className="plan-field" title="Close at the market after this long">
          Max hold
          <span className="plan-input small"><input value={draft.max_hold_min} onChange={(e) => set("max_hold_min", e.target.value)} inputMode="decimal" aria-label="Max hold, minutes" />min</span>
        </label>
        <div className="plan-field">
          Flip
          <span className="plan-holds"><button type="button" className={`plan-chip ${draft.exit_on_flip ? "on" : ""}`} onClick={() => set("exit_on_flip", !draft.exit_on_flip)}>Exit if the rows flip</button></span>
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
          {draft.id && <span className="plan-note muted" style={{ margin: 0 }}>Changes reset the record.</span>}
        </div>
      </div>
    </section>
  );
}


const ago = (ms, now) => {
  if (!ms) return "-";
  const sec = Math.max(0, ((now || Date.now()) - ms) / 1000);
  return sec < 60 ? `${Math.round(sec)}s ago` : sec < 3600 ? `${Math.round(sec / 60)}m ago` : `${(sec / 3600).toFixed(1)}h ago`;
};
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const bps = (x, d = 1) => (x == null ? "-" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(d)}`);
const hLabel2 = (s) => (s < 60 ? `${s}s` : `${s / 60}m`);
const GRADE = { proven: "proven", promising: "promising" };

function Auto({ a, busy, onSave, onReset }) {
  const [edit, setEdit] = useState(null);
  const styles = (a.styles || []).map((k) => a.styles_all.find((x) => x.key === k)?.label || k);
  const openEdit = () => setEdit(edit ? null : {
    coins: a.coins, setups: a.setups, exits: a.exits, styles: a.styles, leverage: String(a.leverage),
    margin_usd: String(a.margin_usd), daily_loss_usd: String(a.daily_loss_usd), telegram: a.telegram,
    taker_bps: String(a.taker_bps), maker_bps: String(a.maker_bps), lead_min_bps: String(a.lead_min_bps),
    spread_max: String(a.spread_max), cost_gate: a.cost_gate,
  });
  const saveEdit = async () => {
    const num = ["leverage", "margin_usd", "daily_loss_usd", "taker_bps", "maker_bps", "lead_min_bps", "spread_max"];
    await onSave({ ...edit, ...Object.fromEntries(num.map((k) => [k, Number(edit[k])])) });
    setEdit(null);
  };
  return (
    <>
      <section className="plan-sec">
        <div className="bot-head">
          <h3 style={{ margin: 0 }}>Auto hunter</h3>
          <span className={`bot-state ${a.on ? "up" : "muted"}`}>{a.on ? "running 24/7" : "paused"}</span>
        </div>
        <p className="plan-note" style={{ marginTop: 0 }}>
          {a.on
            ? `Watching ${a.coins_on.join(", ")} for ${plural(a.setups.length, "setup")} (Binance leads on every Binance quote, the rest every second), each tried with ${plural(a.exits.length, "exit")} and ${styles.join(" / ").toLowerCase()}: ${a.running} strategies at ${a.leverage}× on $${a.margin_usd} each${a.benched_n ? `, ${a.benched_n} benched for losing` : ""}. `
              + `Timed exits carry a stop ${a.vol_stop_k} normal moves away. An entry is skipped when the spread is over ${a.spread_max}× usual`
              + `${a.cost_gate ? `, or when ${a.gate_min}+ signals show the setup doesn't clear the fees on that coin` : ""}.`
            : "Paused. Nothing new is entered."}
        </p>
        <p className="plan-note">
          Last 24h: <b>{a.day.events}</b> events ({a.day.signals} signals, counted once per move), <b>{a.day.trades}</b> trades across all strategies.
        </p>
        <p className="plan-note">
          All time, after fees: {STYLES.filter(([k]) => a.total[k]?.trades).map(([k, label], i) => (
            <span key={k}>{i > 0 && " · "}{label.toLowerCase()} <b className={tone(a.total[k].net_usd)}>{usd(a.total[k].net_usd)}</b>
              {" "}over {a.total[k].trades} ({usd(a.total[k].gross_usd)} before fees)</span>
          ))}
          {!STYLES.some(([k]) => a.total[k]?.trades) && "no trades yet."}
        </p>
        <div className="bot-actions">
          <button className="plan-chip" disabled={busy} onClick={() => onSave({ on: !a.on })}>{a.on ? "Pause" : "Resume"}</button>
          <button className="plan-chip" disabled={busy} onClick={openEdit}>{edit ? "Close settings" : "Settings"}</button>
          {a.benched_n > 0 && <button className="plan-chip" disabled={busy} onClick={() => onSave({ benched: [] })}>Unbench {a.benched_n}</button>}
          <button className="plan-chip" disabled={busy} onClick={onReset}>Clear record</button>
        </div>
        {edit && <AutoEditor a={a} e={edit} setE={setEdit} busy={busy} onSave={saveEdit} />}
      </section>

      <section className="plan-sec">
        <h3>Setups</h3>
        <div className="bot-scroll">
          <table className="plan-table">
            <thead>
              <tr>
                <th className="left">Setup</th><th>Events 24h</th><th>Last</th>
                <th title="By the price a market order pays, after fees, at the best hold and order type so far">Edge after fees</th>
                <th title="Coins where its record shows it doesn't clear the fees, so entries are skipped">Skipping</th>
              </tr>
            </thead>
            <tbody>
              {a.setup_stats.filter((r) => r.on).map((r) => {
                const e = r.edge;
                const sure = e && e.t != null && Math.abs(e.t) >= 2;
                return (
                  <tr key={r.key} title={r.help}>
                    <td className="left">{r.label}</td>
                    <td className="num">{r.events_24h}</td>
                    <td className="num muted">{ago(r.last_ms, a.now_ms)}</td>
                    <td className="num">
                      {!e ? <span className="muted">-</span> : e.learning ? <span className="muted">learning · n {e.n}</span> : (
                        <>
                          <span className={sure ? tone(e.avg_bps) : ""}>{bps(e.avg_bps)} bps</span>
                          <div className="bot-sub">{hLabel2(e.h)} · {STYLE_SHORT[e.style]} · n {e.n}{e.t != null ? ` · t ${e.t.toFixed(1)}` : ""}</div>
                        </>
                      )}
                    </td>
                    <td className="num muted">
                      {r.gated_coins.length ? r.gated_coins.join(", ") : "-"}
                      {r.skipped.spread ? <div className="bot-sub">{r.skipped.spread} wide spread</div> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {a.markouts?.length > 0 && <FollowThrough a={a} />}

      <section className="plan-sec">
        <h3>Proven and promising</h3>
        {a.graded.length === 0 ? (
          <p className="plan-note muted" style={{ marginTop: 0 }}>
            None yet. Proven needs {a.proven_rule[0]}+ trades in profit with t ≥ {a.proven_rule[1]}; promising {a.promising_rule[0]}+ with t ≥ {a.promising_rule[1]}.
            {a.learning ? ` ${a.learning} strategies are still learning.` : ""}
          </p>
        ) : (
          <div className="bot-scroll">
            <table className="plan-table">
              <thead><tr><th className="left">Strategy</th><th>How</th><th>Trades</th><th>Won</th><th>Net</th><th>t</th><th className="left">Grade</th></tr></thead>
              <tbody>
                {a.graded.map((b) => (
                  <tr key={`${b.id}-${b.style}`} title={b.verdict}>
                    <td className="left">{b.name}</td>
                    <td className="muted">{STYLE_SHORT[b.style]}</td>
                    <td className="num">{b.trades}</td>
                    <td className="num">{pct(b.win_rate)}</td>
                    <td className={`num ${tone(b.net_usd)}`}>{usd(b.net_usd)}</td>
                    <td className="num">{b.t_stat == null ? "-" : b.t_stat.toFixed(1)}</td>
                    <td className="left">{GRADE[b.grade]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {a.worst.length > 0 && (
          <p className="plan-note muted">
            Losing most: {a.worst.map((b) => `${b.name} (${STYLE_SHORT[b.style]}) ${usd(b.net_usd)} over ${b.trades}${b.benched ? ", benched" : ""}`).join(" · ")}
          </p>
        )}
      </section>

      <section className="plan-sec">
        <h3>Live</h3>
        {a.signals.length === 0 ? (
          <p className="plan-note muted" style={{ marginTop: 0 }}>{a.on ? "Watching." : "Paused."}</p>
        ) : (
          <div className="bot-feed">
            {a.signals.map((x, i) => (
              <div key={`${x.ms}-${x.coin}-${x.setup}-${i}`} className="bot-feed-row">
                <span className="num muted">{time(x.ms)}</span> <b>{x.coin}</b> {x.setup}{" "}
                <span className={x.side > 0 ? "up" : "down"}>{x.side > 0 ? "long" : "short"}</span>
                <span className="muted"> · event {x.event}</span>
              </div>
            ))}
          </div>
        )}
        {a.open.length > 0 && (
          <>
            <p className="plan-note" style={{ marginBottom: 4 }}>Open now</p>
            {a.open.map((o, i) => (
              <div key={`${o.name}-${o.style}-${i}`} className="bot-feed-row">
                {o.name} <span className="muted">({STYLE_SHORT[o.style]})</span>{" "}
                <span className={o.side > 0 ? "up" : "down"}>{o.side > 0 ? "long" : "short"}</span> from {fmtPrice(o.entry_px)},{" "}
                <b className={tone(o.pnl_usd)}>{usd(o.pnl_usd)}</b>
              </div>
            ))}
          </>
        )}
      </section>
    </>
  );
}

function AutoEditor({ a, e, setE, busy, onSave }) {
  const set = (k, v) => setE((x) => ({ ...x, [k]: v }));
  const flip = (k, v) => set(k, e[k].includes(v) ? e[k].filter((x) => x !== v) : [...e[k], v]);
  const allCoins = e.coins.length === 0;
  const field = (k, label, unit, title) => (
    <label key={k} className="plan-field" title={title || ""}>
      {label}
      <span className="plan-input small"><input value={e[k]} onChange={(ev) => set(k, ev.target.value)} inputMode="decimal" aria-label={label} />{unit}</span>
    </label>
  );
  return (
    <div className="plan-form" style={{ padding: "12px 0 0", border: 0 }}>
      <div className="plan-field">
        Coins
        <span className="plan-holds">
          <button type="button" className={`plan-chip ${allCoins ? "on" : ""}`} onClick={() => set("coins", [])}>All</button>
          {a.all_coins.map((c) => <button key={c} type="button" className={`plan-chip ${!allCoins && e.coins.includes(c) ? "on" : ""}`} onClick={() => flip("coins", c)}>{c}</button>)}
        </span>
      </div>
      <div className="plan-field">
        Setups
        <span className="plan-holds">{a.setups_all.map((x) => <button key={x.key} type="button" title={x.help} className={`plan-chip ${e.setups.includes(x.key) ? "on" : ""}`} onClick={() => flip("setups", x.key)}>{x.label}</button>)}</span>
      </div>
      <div className="plan-field">
        Exits
        <span className="plan-holds">{a.exits_all.map((x) => (
          <button key={x.key} type="button" className={`plan-chip ${e.exits.includes(x.key) ? "on" : ""}`} onClick={() => flip("exits", x.key)}
            title={x.timed ? `Out after ${dur(x.hold_s)}, stop ${a.vol_stop_k} normal moves away` : `Target +${x.target_bps} bps, stop −${x.stop_bps} bps, out after ${dur(x.hold_s)}`}>
            {x.timed ? `${x.label} timed` : `${x.label} +${x.target_bps}/−${x.stop_bps}`}
          </button>
        ))}</span>
      </div>
      <div className="plan-field">
        Orders
        <span className="plan-holds">{a.styles_all.map((x) => <button key={x.key} type="button" className={`plan-chip ${e.styles.includes(x.key) ? "on" : ""}`} onClick={() => flip("styles", x.key)}>{x.label}</button>)}</span>
      </div>
      {field("leverage", "Leverage", "×")}
      {field("margin_usd", "Margin", "$")}
      {field("daily_loss_usd", "Daily stop", "$", "Each strategy stops for the rest of the UTC day after losing this much")}
      {field("taker_bps", "Market fee", "bps", "Per side. Base 4.5; over $5M of 14-day volume 4.0, over $25M 3.5. Staking HYPE takes 5–40% off.")}
      {field("maker_bps", "Limit fee", "bps", "Per side. Base 1.5; over $5M 1.2, over $25M 0.8.")}
      {field("spread_max", "Max spread", "× usual", "Skip an entry when the spread is wider than this many times its usual size")}
      {field("lead_min_bps", "Lead size", "bps", "Smallest Binance move to act on (0 = automatic)")}
      <div className="plan-field">
        Fee check
        <span className="plan-holds">
          <button type="button" className={`plan-chip ${e.cost_gate ? "on" : ""}`} onClick={() => set("cost_gate", !e.cost_gate)}>Skip setups that don't clear fees</button>
        </span>
      </div>
      <div className="plan-field">
        Telegram
        <span className="plan-holds">{[["off", "Off"], ["hourly", "Hourly summary"]].map(([k, l]) => <button key={k} type="button" className={`plan-chip ${e.telegram === k ? "on" : ""}`} onClick={() => set("telegram", k)}>{l}</button>)}</span>
      </div>
      <div className="bot-actions" style={{ marginTop: 0 }}>
        <button className="plan-chip on" disabled={busy || !e.setups.length || !e.exits.length || !e.styles.length} onClick={onSave}>Save</button>
        <span className="plan-note muted" style={{ margin: 0 }}>Leverage, margin, daily stop or fee changes reset the record.</span>
      </div>
    </div>
  );
}

function MarkCell({ c, cost }) {
  if (!c?.n) return <td className="num"><span className="muted">-</span></td>;
  const clears = c.t != null && c.t >= 2 && c.avg_bps > cost;
  const short = c.t != null && c.t <= -2;
  return (
    <td className="num" title={`${c.n} signals · ${pct(c.won)} went its way${c.t != null ? ` · t ${c.t.toFixed(1)}` : ""}`}>
      <span className={clears ? "up" : short ? "down" : ""}>{bps(c.avg_bps)}</span>
      <div className="bot-sub">{pct(c.won)} · {c.n}</div>
    </td>
  );
}

function FollowThrough({ a }) {
  const [coin, setCoin] = useState("all");
  const coins = Object.keys(a.markouts[0]?.coins || {});
  const cost = a.costs?.maker_exit ?? 6;
  const lead = (a.lead_sizes || []).filter((r) => r.cells.some((c) => c.n));
  return (
    <section className="plan-sec">
      <div className="bot-head">
        <h3 style={{ margin: 0 }}>Follow-through</h3>
        <span className="plan-holds">
          {["all", ...coins].map((c) => (
            <button key={c} type="button" className={`plan-chip ${coin === c ? "on" : ""}`} onClick={() => setCoin(c)}>{c === "all" ? "All" : c}</button>
          ))}
        </span>
      </div>
      <div className="bot-scroll">
        <table className="plan-table">
          <thead><tr><th className="left">Setup</th>{a.markout_s.map((h) => <th key={h}>{hLabel2(h)}</th>)}</tr></thead>
          <tbody>
            {a.markouts.map((r) => {
              const cells = coin === "all" ? r.all : r.coins[coin] || [];
              return (
                <tr key={r.key}>
                  <td className="left">{r.label}</td>
                  {a.markout_s.map((h, i) => <MarkCell key={h} c={cells[i]} cost={cost} />)}
                </tr>
              );
            })}
            {coin === "all" && lead.length > 0 && (
              <>
                <tr><td className="left muted" colSpan={a.markout_s.length + 1}>Binance leads, by Binance's move</td></tr>
                {lead.map((r) => (
                  <tr key={r.label}>
                    <td className="left">{r.label} bps</td>
                    {a.markout_s.map((h, i) => <MarkCell key={h} c={r.cells[i]} cost={cost} />)}
                  </tr>
                ))}
              </>
            )}
          </tbody>
        </table>
      </div>
      <p className="plan-note muted">Bps by the price you'd pay, before fees. Green: clears {cost.toFixed(0)} bps of fees (market in, limit out) with t ≥ 2.</p>
    </section>
  );
}
