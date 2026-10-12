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
            The auto hunter watches every coin around the clock and trades each setup the moment it appears. Your own rules
            run on the timeframe cards. Every trade is filled the way Hyperliquid itself would fill it: market orders pay the
            spread and {d ? (d.fees.taker * 100).toFixed(3) : "0.045"}% a side, limit orders pay{" "}
            {d ? (d.fees.maker * 100).toFixed(3) : "0.015"}% but only fill when price trades through them. Everything runs both
            ways side by side, so you can see what survives the costs. Live trading comes once something holds up here.
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
          Target +{marg(draft.take_profit_bps).toFixed(0)}%, stop −{marg(draft.stop_loss_bps).toFixed(0)}% of margin · liquidation ~{liqBps.toFixed(0)} bps away ·
          breaks even at {be.market == null ? "never" : pct(be.market)} wins (market), {be.limit == null ? "never" : pct(be.limit)} (limit)
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
  const s = Math.max(0, ((now || Date.now()) - ms) / 1000);
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`;
};
const cell = (x) => (x.trades ? (
  <>
    <span className={tone(x.net_usd)}>{usd(x.net_usd)}</span> <span className="muted">· {x.trades}</span>
    <div className="bot-sub">{usd(x.gross_usd)} before fees</div>
  </>
) : <span className="muted">-</span>);
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function Auto({ a, busy, onSave, onReset }) {
  const [edit, setEdit] = useState(null);
  const lh = a.last_hour;
  const openEdit = () => setEdit(edit ? null : {
    coins: a.coins, setups: a.setups, exits: a.exits, leverage: String(a.leverage), margin_usd: String(a.margin_usd),
    daily_loss_usd: String(a.daily_loss_usd), telegram: a.telegram,
  });
  const saveEdit = async () => {
    await onSave({ ...edit, leverage: Number(edit.leverage), margin_usd: Number(edit.margin_usd), daily_loss_usd: Number(edit.daily_loss_usd) });
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
            ? `Watching ${a.coins_on.join(", ")} every second for ${plural(a.setups.length, "setup")}, each tried with ${plural(a.exits.length, "exit")}: ${a.running} strategies running at ${a.leverage}× on $${a.margin_usd} each${a.benched_n ? `, ${a.benched_n} benched for losing` : ""}. Market orders are skipped where the target can't cover the fees.`
            : "Paused. Nothing new is entered."}
        </p>
        <p className="plan-note">
          Last hour: <b>{lh.signals}</b> signals, <b>{lh.trades}</b> trades · market orders{" "}
          <b className={tone(lh.market_net_usd)}>{usd(lh.market_net_usd)}</b> · limit orders{" "}
          <b className={tone(lh.limit_net_usd)}>{usd(lh.limit_net_usd)}</b> · {a.open.length} open now
        </p>
        <p className="plan-note">
          All time: market orders <b className={tone(a.total.market.net_usd)}>{usd(a.total.market.net_usd)}</b> over{" "}
          {a.total.market.trades} trades ({usd(-a.total.market.fees_usd)} fees) · limit orders{" "}
          <b className={tone(a.total.limit.net_usd)}>{usd(a.total.limit.net_usd)}</b> over {a.total.limit.trades} ({usd(-a.total.limit.fees_usd)} fees)
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
            <thead><tr><th className="left">Setup</th><th>Signals 1h</th><th>Last</th><th>Market orders</th><th>Limit orders</th></tr></thead>
            <tbody>
              {a.setup_stats.filter((r) => r.on).map((r) => (
                <tr key={r.key} title={r.help}>
                  <td className="left">{r.label}</td>
                  <td className="num">{r.signals_1h}</td>
                  <td className="num muted">{ago(r.last_ms, a.now_ms)}</td>
                  <td className="num">{cell(r.market)}</td>
                  <td className="num">{cell(r.limit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="plan-sec">
        <h3>Best so far</h3>
        {a.best.length === 0 ? (
          <p className="plan-note muted" style={{ marginTop: 0 }}>Ranked once strategies have a few trades each.</p>
        ) : (
          <div className="bot-scroll">
            <table className="plan-table">
              <thead><tr><th className="left">Strategy</th><th>How</th><th>Trades</th><th>Won</th><th>Net</th><th title="How steady the results are: 2 or more means luck is an unlikely explanation">t</th></tr></thead>
              <tbody>
                {[...a.best, ...(a.worst.length ? [null] : []), ...a.worst].map((b, i) => b == null ? (
                  <tr key="gap"><td className="left muted" colSpan={6}>Worst</td></tr>
                ) : (
                  <tr key={`${b.id}-${b.style}-${i}`} title={b.verdict}>
                    <td className={`left ${b.benched ? "muted" : ""}`}>{b.name}</td>
                    <td className="muted">{b.style}{b.benched ? " · benched" : ""}</td>
                    <td className="num">{b.trades}</td>
                    <td className="num">{pct(b.win_rate)}</td>
                    <td className={`num ${tone(b.net_usd)}`}>{usd(b.net_usd)}</td>
                    <td className="num muted">{b.t_stat == null ? "-" : b.t_stat.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="plan-note muted">After fees. Under 30 trades can be luck.</p>
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
              </div>
            ))}
          </div>
        )}
        {a.open.length > 0 && (
          <>
            <p className="plan-note" style={{ marginBottom: 4 }}>Open now</p>
            {a.open.map((o, i) => (
              <div key={`${o.name}-${o.style}-${i}`} className="bot-feed-row">
                {o.name} <span className="muted">({o.style})</span>{" "}
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
        <span className="plan-holds">{a.setups_all.map((s) => <button key={s.key} type="button" title={s.help} className={`plan-chip ${e.setups.includes(s.key) ? "on" : ""}`} onClick={() => flip("setups", s.key)}>{s.label}</button>)}</span>
      </div>
      <div className="plan-field">
        Exits
        <span className="plan-holds">{a.exits_all.map((x) => <button key={x.key} type="button" className={`plan-chip ${e.exits.includes(x.key) ? "on" : ""}`} onClick={() => flip("exits", x.key)}
          title={`Target +${x.target_bps} bps, stop −${x.stop_bps} bps, out after ${dur(x.hold_s)}`}>{x.label} +{x.target_bps}/−{x.stop_bps} · {dur(x.hold_s)}</button>)}</span>
      </div>
      {[["leverage", "Leverage", "×"], ["margin_usd", "Margin", "$"], ["daily_loss_usd", "Daily stop", "$"]].map(([k, label, unit]) => (
        <label key={k} className="plan-field" title={k === "daily_loss_usd" ? "Each strategy stops for the rest of the UTC day after losing this much" : ""}>
          {label}
          <span className="plan-input small"><input value={e[k]} onChange={(ev) => set(k, ev.target.value)} inputMode="decimal" aria-label={label} />{unit}</span>
        </label>
      ))}
      <div className="plan-field">
        Telegram
        <span className="plan-holds">{[["off", "Off"], ["hourly", "Hourly summary"]].map(([k, l]) => <button key={k} type="button" className={`plan-chip ${e.telegram === k ? "on" : ""}`} onClick={() => set("telegram", k)}>{l}</button>)}</span>
      </div>
      <div className="bot-actions" style={{ marginTop: 0 }}>
        <button className="plan-chip on" disabled={busy || !e.setups.length || !e.exits.length} onClick={onSave}>Save</button>
        <span className="plan-note muted" style={{ margin: 0 }}>Leverage, margin or daily stop changes reset the record.</span>
      </div>
    </div>
  );
}
