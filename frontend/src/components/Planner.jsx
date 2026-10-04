import { useEffect, useRef, useState } from "react";
import { fmtPrice } from "../format.js";

const HOLDS = [
  ["15m", 0.25], ["1h", 1], ["4h", 4], ["12h", 12], ["24h", 24], ["3d", 72], ["1w", 168],
];
const REFRESH_MS = 30_000;

const holdLabel = (h) => (HOLDS.find(([, v]) => v === h) || [`${h}h`])[0];
const usd = (x, sign = false) => {
  if (x == null) return "—";
  const a = Math.abs(x);
  const d = a < 100 && Math.round(a * 100) % 100 !== 0 ? 2 : 0;   // $4.50, $81.51, but $50 and $220
  const s = a >= 1e6 ? `$${(a / 1e6).toFixed(2)}M` : a >= 1e4 ? `$${(a / 1e3).toFixed(1)}K`
    : `$${a.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
  return x < 0 ? `−${s}` : sign ? `+${s}` : s;
};
const pct = (x, d = 1) => (x == null ? "—" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(d)}%`);
function chance(p) {
  if (p == null) return "—";
  if (p === 0) return "<0.01%";
  if (p < 0.001) return "<0.1%";
  if (p < 0.1) return `${(p * 100).toFixed(1)}%`;
  return `${Math.round(p * 100)}%`;
}
function oneIn(p) {
  if (!p || p < 0.0005) return "";
  if (p >= 0.5) return "";
  return `about 1 in ${Math.round(1 / p).toLocaleString()}`;
}
function riskBand(p) {
  if (p < 0.01) return ["low", "up"];
  if (p < 0.05) return ["moderate", "mid"];
  if (p < 0.2) return ["high", "down"];
  return ["very high", "down"];
}
/** Which card to quote as context for a hold of h hours. */
function contextWindow(h) {
  if (h <= 0.25) return "10m";
  if (h <= 1) return "60m";
  if (h <= 6) return "6h";
  if (h <= 12) return "12h";
  if (h <= 24) return "24h";
  return "1w";
}

/** One drawdown point: "−1.6% (−$81.68)", or the liquidation it would mean. */
function Dd({ d }) {
  return (
    <>
      <b>{pct(d.move_pct)}</b>{" "}
      <span className="nowrap">({d.liquidated ? "liquidated" : usd(d.pnl, true)})</span>
    </>
  );
}

/** Stop and target: which closes the trade first, what each is worth, and whether the odds beat break-even. */
function BracketSection({ b, p, hl }) {
  const parts = [
    ["target", b.p_target, "Target first"],
    ["stop", b.p_stop, "Stop first"],
    ["liq", b.p_liq, "Liquidated"],
    ["time", b.p_time, `Neither by ${hl}`],
  ].filter(([, v]) => v > 0);
  const decided = b.p_target + b.p_stop;
  const winShare = decided > 0 ? b.p_target / decided : null;
  return (
    <section className="plan-sec">
      <h3>With your stop and target</h3>
      <div className="plan-brk-bar" aria-hidden="true">
        {parts.map(([k, v]) => <div key={k} className={`seg ${k}`} style={{ width: `${v * 100}%` }} />)}
      </div>
      <dl className="plan-costs num">
        {b.target_price != null && (
          <><dt>Target first <span className="muted">at {fmtPrice(b.target_price)}{b.hours_target != null ? `, typically within ${fmtHours(b.hours_target)}` : ""}</span></dt>
            <dd><b>{chance(b.p_target)}</b> <span className="up">{usd(b.pnl_target, true)}</span></dd></>
        )}
        {b.stop_price != null && !b.stop_beyond_liq && (
          <><dt>Stop first <span className="muted">at {fmtPrice(b.stop_price)}{b.hours_stop != null ? `, typically within ${fmtHours(b.hours_stop)}` : ""}</span></dt>
            <dd><b>{chance(b.p_stop)}</b> <span className="down">{usd(b.pnl_stop, true)}</span></dd></>
        )}
        {b.p_liq > 0 && (<><dt>Liquidated first</dt><dd><b>{chance(b.p_liq)}</b></dd></>)}
        <dt>Neither — closed at the end of {hl}</dt><dd><b>{chance(b.p_time)}</b></dd>
        <dt><b>Average result</b> <span className="muted">after costs</span></dt>
        <dd><b className={b.ev >= 0 ? "up" : "down"}>{usd(b.ev, true)}</b></dd>
      </dl>
      {b.pnl_stop != null && b.pnl_stop < 0 && (
        <p className="plan-note">
          Getting stopped costs <b>{usd(-b.pnl_stop)}</b>, {(-b.pnl_stop / p.equity * 100).toFixed(1)}% of your{" "}
          {p.margin_mode === "cross" ? "account" : "margin"}.{" "}
          {[0.01, 0.02].map((k, i) => {
            const notional = (k * p.equity) / (-b.pnl_stop / p.notional);
            return (
              <span key={k}>
                {i ? "; " : "To risk "}<b>{k * 100}%</b>{i ? "" : ` (${usd(k * p.equity)})`}: a {usd(notional)} position
                {` (${usd(notional / p.leverage)} margin at ${p.leverage}×)`}
              </span>
            );
          })}.
        </p>
      )}
      <p className="plan-note">
        In profit {Math.round(b.p_profit * 100)}% of the time. With no view on direction, the average result is roughly
        minus your costs whatever the stop and target: they change the shape of your results (many small losses and
        fewer larger wins, or the reverse), not the average. An edge has to come from your read of the market.
        {b.breakeven_win != null && winShare != null && b.p_time < 0.25 && (
          <>
            {" "}To break even, the target has to come first in <b>{Math.round(b.breakeven_win * 100)}%</b> of trades;
            with no edge it does in about <b>{Math.round(winShare * 100)}%</b>.
          </>
        )}
        {b.p_time >= 0.25 && (
          <>
            {" "}{Math.round(b.p_time * 100)}% of trades reach neither before your {hl} exit, so judge this by the average
            result rather than the win rate.
          </>
        )}
      </p>
    </section>
  );
}

/** One plain sentence: what this trade means, before any numbers. */
function summaryLine(p, hl) {
  const lp = p.liq_prob;
  const risk = !p.liq_price ? "it can't be liquidated"
    : lp < 0.001 ? "liquidation is very unlikely"
    : lp < 0.01 ? `liquidation is unlikely (${chance(lp)})`
    : lp < 0.05 ? `liquidation is possible (${chance(lp)})`
    : lp < 0.5 ? `liquidation is a real risk (${chance(lp)})`
    : `you're more likely than not to be liquidated (${chance(lp)})`;
  const dd = p.drawdown[0];
  const parts = [
    `${p.leverage}× ${p.side > 0 ? "long" : "short"} for ${hl}${p.margin_mode === "cross"
      ? ` (${(p.notional / p.equity).toFixed(1)}× your account, in cross)` : ""}: ${risk}.`,
    `Expect about ±${p.sigma_pct.toFixed(1)}% of movement, and to be ${Math.abs(dd.move_pct).toFixed(1)}% against you at some point${dd.liquidated ? "" : ` (${usd(dd.pnl, true)})`}.`,
    `Costs ${usd(p.costs.total)}.`,
  ];
  if (p.liq_price && p.safe_leverage["1"] < p.leverage) parts.push(`Drop to ${p.safe_leverage["1"]}× to keep liquidation risk under 1%.`);
  return parts.join(" ");
}

/** The price cone over the hold: 5–95% and 25–75% bands, the median, and the levels that matter. */
function Cone({ p, hl, bracket }) {
  const [hover, setHover] = useState(null);
  const fan = p.fan;
  if (!fan || fan.length < 2) return null;
  const W = 456, H = 176, L = 6, R = 112, T = 10, B = 22;
  const H_END = fan[fan.length - 1][0];
  const lo = Math.min(...fan.map((r) => r[1])), hi = Math.max(...fan.map((r) => r[5]));
  const span = hi - lo;
  const lines = [{ k: "entry", price: p.entry, label: "entry" }];
  if (bracket?.target_price) lines.push({ k: "target", price: bracket.target_price, label: "target" });
  if (bracket?.stop_price && !bracket.stop_beyond_liq) lines.push({ k: "stop", price: bracket.stop_price, label: "stop" });
  if (p.liq_price) lines.push({ k: "liq", price: p.liq_price, label: "liq" });
  // keep the cone readable: a level far outside it is pinned to the edge with an arrow instead of squashing the chart
  const near = (x) => x >= lo - 0.6 * span && x <= hi + 0.6 * span;
  const yLo = Math.min(lo, ...lines.filter((l) => near(l.price)).map((l) => l.price));
  const yHi = Math.max(hi, ...lines.filter((l) => near(l.price)).map((l) => l.price));
  const pad = (yHi - yLo) * 0.06 || 1;
  const y = (v) => T + (1 - (v - (yLo - pad)) / (yHi - yLo + 2 * pad)) * (H - T - B);
  const x = (h) => L + (h / H_END) * (W - L - R);
  const band = (a, b) => fan.map((r) => `${x(r[0])},${y(r[a])}`).join(" ") + " " +
    [...fan].reverse().map((r) => `${x(r[0])},${y(r[b])}`).join(" ");
  const liqAt = (h) => {
    const c = p.liq_curve;
    if (!c || !c.length) return null;
    let best = c[0];
    for (const r of c) if (r[0] <= h + 1e-9) best = r;
    return best[1];
  };
  const onMove = (e) => {
    const box = e.currentTarget.getBoundingClientRect();
    const h = ((e.clientX - box.left) * (W / box.width) - L) / (W - L - R) * H_END;
    let best = fan[0];
    for (const r of fan) if (Math.abs(r[0] - h) < Math.abs(best[0] - h)) best = r;
    setHover(best);
  };
  return (
    <div className="plan-cone">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        aria-label={`Price over the next ${hl}: 90% of paths end between ${fmtPrice(fan[fan.length - 1][1])} and ${fmtPrice(fan[fan.length - 1][5])}`}>
        <polygon className="cone-outer" points={band(1, 5)} />
        <polygon className="cone-inner" points={band(2, 4)} />
        <polyline className="cone-median" points={fan.map((r) => `${x(r[0])},${y(r[3])}`).join(" ")} />
        {lines.map((l) => {
          const inside = near(l.price);
          const yy = inside ? y(l.price) : l.price < yLo ? H - B : T;
          return (
            <g key={l.k} className={`cone-line ${l.k}`}>
              {inside && <line x1={L} x2={W - R} y1={yy} y2={yy} />}
              <text x={W - R + 6} y={yy + 3.5}>
                {!inside && (l.price < yLo ? "↓ " : "↑ ")}{l.label} {fmtPrice(l.price)}
              </text>
            </g>
          );
        })}
        {[0, H_END / 2, H_END].map((h) => (
          <text key={h} className="cone-axis" x={x(h)} y={H - 6} textAnchor={h === 0 ? "start" : h === H_END ? "end" : "middle"}>
            {h === 0 ? "now" : `+${fmtHours(h)}`}
          </text>
        ))}
        {hover && <line className="cone-cross" x1={x(hover[0])} x2={x(hover[0])} y1={T} y2={H - B} />}
      </svg>
      <div className="plan-cone-tip num">
        {hover && hover[0] > 0 ? (
          <>
            after {fmtHours(hover[0])} · half of paths {fmtPrice(hover[2])}–{fmtPrice(hover[4])} · 90% {fmtPrice(hover[1])}–{fmtPrice(hover[5])}
            {liqAt(hover[0]) != null && p.liq_price ? ` · liquidated by then ${chance(liqAt(hover[0]))}` : ""}
          </>
        ) : (
          <span className="muted">Shaded: where half and 90% of simulated paths are at each moment · hover for numbers</span>
        )}
      </div>
    </div>
  );
}

/** How the model did on this coin's own history, out of sample. */
function CalibrationSection({ p, pending }) {
  const c = p.calibration;
  if (!c && !pending) return null;
  const pc = (x) => (x < 0.1 ? `${(x * 100).toFixed(1)}%` : `${Math.round(x * 100)}%`);
  return (
    <section className="plan-sec">
      <h3>Checked on {p.coin}'s own history</h3>
      {!c ? (
        <p className="plan-note muted">Testing the model against {p.coin}'s past {fmtHours(p.hours)} holds…</p>
      ) : (
        <>
          <p className="plan-note">
            Fitted on the older part of the history, then tested on the last <b>{Math.round(c.days)} days</b> it never
            saw: at {c.starts} past moments it predicted the chance of price reaching levels ½ to 3 typical moves
            away before a {fmtHours(c.hours)} hold ended, and we checked what happened.
          </p>
          <table className="plan-table plan-calib">
            <thead><tr><th>It said</th><th>It happened</th><th>Levels</th></tr></thead>
            <tbody>
              {c.bins.filter((b) => b.n >= 30).map((b) => (
                <tr key={b.lo}>
                  <td className="num">{pc(b.predicted)}</td>
                  <td className="num">{pc(b.happened)}</td>
                  <td className="num muted">{b.n}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="plan-note">
            For rare levels (given under 10%) it said <b>{pc(c.tail_predicted)}</b> and they were reached <b>{pc(c.tail_happened)}</b> of the time.
            {c.bell_n > 0 && <> A plain bell-curve model said {pc(c.bell_predicted)} for its rare levels; they were reached {pc(c.bell_happened)}.</>}
            {" "}Its 90% range held <b>{pc(c.range_coverage)}</b> of the time.
          </p>
        </>
      )}
    </section>
  );
}

const fmtHours = (h) => (h < 1 ? `${Math.max(1, Math.round(h * 60))}m` : h < 48 ? `${h.toFixed(h < 10 ? 1 : 0)}h` : `${(h / 24).toFixed(1)}d`);

function RangeBar({ outcome, margin }) {
  // A box plot of P&L at exit: whiskers 5th–95th, box 25th–75th, tick at the median. Scale: ±max(|p5|,|p95|, margin/10).
  const lim = Math.max(Math.abs(outcome.p5), Math.abs(outcome.p95), margin / 10);
  const x = (v) => `${50 + (v / lim) * 48}%`;
  return (
    <div className="plan-range" aria-hidden="true">
      <div className="plan-range-zero" style={{ left: x(0) }} />
      <div className="plan-range-whisker" style={{ left: x(outcome.p5), width: `calc(${x(outcome.p95)} - ${x(outcome.p5)})` }} />
      <div className="plan-range-box" style={{ left: x(outcome.p25), width: `calc(${x(outcome.p75)} - ${x(outcome.p25)})` }} />
      <div className="plan-range-median" style={{ left: x(outcome.p50) }} />
    </div>
  );
}

export default function Planner({ coin, mid, explanations, onClose }) {
  const [side, setSide] = useState("long");
  const [margin, setMargin] = useState(1000);
  const [leverage, setLeverage] = useState(5);
  const [hours, setHours] = useState(24);
  const [stop, setStop] = useState("");     // % from entry, against you ("" = none)
  const [target, setTarget] = useState(""); // % from entry, your way
  const [mode, setMode] = useState("isolated");
  const [account, setAccount] = useState(""); // cross margin: account balance
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (!(margin > 0) || !(leverage >= 1)) return undefined;
    const run = async () => {
      const id = ++seq.current;
      setBusy(true);
      try {
        const q = new URLSearchParams({ coin, side, margin: String(margin), leverage: String(leverage), hours: String(hours) });
        if (Number(stop) > 0) q.set("stop", String(Number(stop)));
        if (Number(target) > 0) q.set("target", String(Number(target)));
        if (mode === "cross" && Number(account) >= margin) q.set("account", String(Number(account)));
        const r = await fetch(`/api/plan?${q}`);
        const d = await r.json();
        if (id !== seq.current) return;
        if (!r.ok) {
          setError(typeof d.detail === "string" ? d.detail : "Check the inputs.");
        } else {
          setError(null);
          setPlan(d);
          if (d.calibration_pending) retry = setTimeout(run, 2500);   // the history check finishes in a moment
        }
      } catch {
        if (id === seq.current) setError("Couldn't reach the backend.");
      } finally {
        if (id === seq.current) setBusy(false);
      }
    };
    let retry = null;
    const t = setTimeout(run, 250);
    const every = setInterval(run, REFRESH_MS);
    return () => {
      clearTimeout(t);
      clearTimeout(retry);
      clearInterval(every);
    };
  }, [coin, side, margin, leverage, hours, stop, target, mode, account]);

  const maxLev = plan?.max_leverage || 50;
  const p = plan && plan.coin === coin ? plan : null;
  const hl = holdLabel(hours);
  const ctxW = contextWindow(hours);
  const ctx = explanations?.[ctxW]?.summary?.filter((m) => m.tag && m.tag !== "—" && !m.partial) || [];

  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className="planner" role="dialog" aria-label="Plan a trade" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2 className="panel-title">Plan a trade · {coin}-PERP</h2>
          <button className="plan-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="plan-form">
          <div className="plan-side" role="group" aria-label="Side">
            {["long", "short"].map((s) => (
              <button key={s} className={`plan-seg ${side === s ? `on ${s}` : ""}`} onClick={() => setSide(s)} aria-pressed={side === s}>
                {s === "long" ? "Long ▲" : "Short ▼"}
              </button>
            ))}
          </div>
          <label className="plan-field">
            <span>Margin</span>
            <span className="plan-input">
              $<input type="number" min="1" step="100" value={margin} onChange={(e) => setMargin(Number(e.target.value))} />
            </span>
          </label>
          <div className="plan-field">
            <span title="Isolated: only this margin backs the position. Cross (Hyperliquid's default): your whole account balance does">Mode</span>
            <div className="plan-holds" role="group" aria-label="Margin type">
              {["isolated", "cross"].map((m) => (
                <button key={m} className={`plan-chip ${mode === m ? "on" : ""}`} onClick={() => setMode(m)} aria-pressed={mode === m}>
                  {m === "isolated" ? "Isolated" : "Cross"}
                </button>
              ))}
              {mode === "cross" && (
                <span className="plan-input acct" title="Your account balance: in cross margin it all backs the position">
                  $<input type="number" min={margin} step="100" placeholder="account balance" value={account} onChange={(e) => setAccount(e.target.value)} />
                </span>
              )}
            </div>
          </div>
          <label className="plan-field">
            <span>Leverage</span>
            <span className="plan-lev">
              <input
                type="range" min="1" max={maxLev} step="1" value={Math.min(leverage, maxLev)}
                onChange={(e) => setLeverage(Number(e.target.value))} aria-label="Leverage"
                style={{ "--fill": `${((Math.min(leverage, maxLev) - 1) / Math.max(maxLev - 1, 1)) * 100}%` }}
              />
              <span className="plan-input small">
                <input type="number" min="1" max={maxLev} step="0.5" value={leverage} onChange={(e) => setLeverage(Number(e.target.value))} />×
              </span>
            </span>
          </label>
          <div className="plan-field">
            <span>Hold for</span>
            <div className="plan-holds" role="group" aria-label="Hold time">
              {HOLDS.map(([label, h]) => (
                <button key={label} className={`plan-chip ${hours === h ? "on" : ""}`} onClick={() => setHours(h)} aria-pressed={hours === h}>
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="plan-field">
            <span title="Optional stop-loss and take-profit, in % from entry">Stop/TP</span>
            <span className="plan-brk">
              <span className="plan-input small" title="Stop-loss: how far against you, in % from entry (optional)">
                −<input type="number" min="0" step="0.5" placeholder="stop" value={stop} onChange={(e) => setStop(e.target.value)} />%
              </span>
              <span className="plan-input small" title="Take-profit: how far your way, in % from entry (optional)">
                +<input type="number" min="0" step="0.5" placeholder="target" value={target} onChange={(e) => setTarget(e.target.value)} />%
              </span>
            </span>
          </div>
          <div className="plan-sum muted">
            {usd(margin * leverage)} position · market entry ≈ {fmtPrice(mid)}
            {busy && <span className="plan-busy"> · updating…</span>}
          </div>
        </div>

        {error && <div className="plan-error">{error}</div>}

        {p && (
          <div className="plan-body">
            <p className="plan-summary">{summaryLine(p, hl)}</p>
            <section className="plan-verdict">
              <div className="plan-kicker">Chance of liquidation before you exit ({hl})</div>
              <div className="plan-big">
                <span className={`plan-risk ${riskBand(p.liq_prob)[1]}`}>{chance(p.liq_prob)}</span>
                <span className={`plan-band ${riskBand(p.liq_prob)[1]}`}>{riskBand(p.liq_prob)[0]}</span>
                <span className="muted">{oneIn(p.liq_prob)}</span>
              </div>
              {p.liq_price ? (
                <div className="ink2">
                  Liquidated at <b>{fmtPrice(p.liq_price)}</b> ({pct(p.liq_distance_pct)}) — {p.liq_sigmas?.toFixed(1)} typical {hl} moves away
                </div>
              ) : (
                <div className="ink2">A 1× long can't be liquidated.</div>
              )}
              <div className="plan-ladder" title="Chance of liquidation before you exit, at each leverage, for this hold">
                {p.leverage_curve.map(([lv, pr]) => (
                  <button
                    key={lv}
                    className={`plan-rung ${lv === p.leverage ? "on" : ""} ${riskBand(pr)[1]}`}
                    onClick={() => setLeverage(lv)}
                    title={`${lv}×: ${chance(pr)} chance of liquidation over ${hl}`}
                  >
                    <span>{lv}×</span>
                    <span className="num">{chance(pr)}</span>
                  </button>
                ))}
              </div>
              <div className="plan-safe">
                For {hl}: up to <b>{p.safe_leverage["1"]}×</b> keeps it under 1%, up to <b>{p.safe_leverage["5"]}×</b> under 5%
                {p.safe_leverage["1"] >= p.max_leverage ? " (any leverage Hyperliquid allows)" : ""}.
              </div>
            </section>

            <section className="plan-sec">
              <h3>The next {hl} · where price could be</h3>
              <Cone p={p} hl={hl} bracket={p.bracket} />
            </section>

            {p.bracket && <BracketSection b={p.bracket} p={p} hl={hl} />}

            <section className="plan-sec">
              <h3>Before you exit · where price trades</h3>
              <table className="plan-table">
                <thead>
                  <tr><th>Price</th><th>Move</th><th>Your P&amp;L</th><th title="Chance price trades there at some point before you exit (for gains: while you're still in the trade, not liquidated first)">Chance</th></tr>
                </thead>
                <tbody>
                  {[...p.touches, { kind: "entry", price: p.entry, move_pct: 0, pnl: 0, prob: null }]
                    .sort((a, b) => b.price - a.price)
                    .map((t) => (
                      <tr key={`${t.kind}-${t.price}`} className={`plan-row ${t.kind}`}>
                        <td className="num">{fmtPrice(t.price)}{t.kind === "liq" ? " liq" : t.kind === "entry" ? " entry" : ""}</td>
                        <td className="num">{t.kind === "entry" ? "—" : pct(t.move_pct, Math.abs(t.move_pct) < 1 ? 2 : 1)}</td>
                        <td className={`num ${t.pnl > 0 ? "up" : t.pnl < 0 ? "loss" : ""}`}>{t.kind === "entry" ? "—" : usd(t.pnl, true)}</td>
                        <td className="num">{t.kind === "entry" ? "" : chance(t.prob)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
              <p className="plan-note">
                Typical worst point before you exit: <Dd d={p.drawdown[0]} />; 1 time in 4: <Dd d={p.drawdown[1]} />.{" "}
                A stop inside that range is likely to be hit by noise alone.
              </p>
            </section>

            <section className="plan-sec">
              <h3>When you close · after costs</h3>
              <RangeBar outcome={p.outcome} margin={p.margin} />
              <div className="plan-range-labels num">
                <span className="down">{usd(p.outcome.p5, true)}</span>
                <span>{usd(p.outcome.p25, true)}</span>
                <span><b>{usd(p.outcome.p50, true)}</b></span>
                <span>{usd(p.outcome.p75, true)}</span>
                <span className="up">{usd(p.outcome.p95, true)}</span>
              </div>
              <div className="plan-range-caption muted">5th · 25th · median · 75th · 95th percentile</div>
              <p className="plan-note">
                In profit at exit about <b>{Math.round(p.prob_profit * 100)}%</b> of the time. Typical {hl} move ±{p.sigma_pct.toFixed(1)}%;
                90% of the time price ends between {pct(p.range_pct[0])} and {pct(p.range_pct[1])}.
              </p>
              {p.tail?.p1 != null && (
                <p className="plan-note">
                  Bad cases: the worst 1 in 100 ends at <b>{usd(p.tail.p1, true)}</b>; the worst 5% average{" "}
                  <b>{usd(p.tail.es5, true)}</b>. Chance of losing half your {p.margin_mode === "cross" ? "account" : "margin"} or
                  more: <b>{chance(p.tail.p_lose_half)}</b>.
                </p>
              )}
            </section>

            <section className="plan-sec">
              <h3>Costs</h3>
              <dl className="plan-costs num">
                <dt>Fees (0.045% in and out)</dt><dd>{usd(p.costs.fees)}</dd>
                <dt>
                  {p.costs.slippage_source === "model" ? "Slippage" : "Slippage on the book now"}
                  {p.costs.slippage_source === "model" && <span className="muted"> (estimated: beyond the visible book)</span>}
                </dt>
                <dd>{p.costs.slippage_known ? usd(p.costs.slippage) : "not included"}</dd>
                <dt>
                  Funding over {hl}
                  <span className="muted"> ({p.costs.funding_now_apr != null ? `${p.costs.funding_now_apr.toFixed(1)}% APR now` : "no rate yet"}
                    {p.costs.funding_avg_apr != null ? `, ${p.costs.funding_avg_apr.toFixed(1)}% week avg` : ""})</span>
                </dt>
                <dd>{p.costs.funding >= 0 ? usd(p.costs.funding) : `${usd(-p.costs.funding)} received`}</dd>
                <dt><b>Total</b></dt><dd><b>{usd(p.costs.total)}</b></dd>
              </dl>
              <p className="plan-note">
                {p.breakeven_pct >= 0
                  ? <>Price has to move {pct(p.breakeven_pct * p.side, 2)} your way just to cover them.</>
                  : <>Funding pays you more than fees and slippage cost: if price doesn't move, you're up {usd(-p.costs.total)}.</>}
              </p>
            </section>

            {ctx.length > 0 && (
              <section className="plan-sec">
                <h3>What the {ctxW} card says now</h3>
                <div className="plan-ctx">
                  {ctx.map((m) => (
                    <span key={m.key} className={`plan-ctx-tag ${m.lean}`} title={`${m.label}: ${m.value}`}>
                      <span className="muted">{m.label}</span> {m.tag}
                    </span>
                  ))}
                </div>
              </section>
            )}

            <CalibrationSection p={p} pending={plan.calibration_pending} />

            <footer className="plan-foot muted">
              {p.model.kind === "fhs" ? (
                <>
                  {p.paths_n.toLocaleString()} simulated paths from {p.model.candles.toLocaleString()} {p.model.step_s === 3600 ? "hourly" : "5-minute"} candles
                  ({p.model.days} days), wicks included. Volatility now {p.vol_ratio.toFixed(2)}× its usual for this hour
                  {p.model.half_life_h ? `; shocks fade with a half-life of ~${p.model.half_life_h}h` : ""}.
                </>
              ) : (
                <>{p.paths_n.toLocaleString()} simulated paths, rough model.</>
              )}{" "}
              No direction assumed: this sizes the risk, it doesn't predict the move.{" "}
              {p.margin_mode === "cross" ? "Cross margin; liquidation counted as losing the whole account" : "Isolated margin; liquidation counted as losing the whole margin"};
              maintenance {(p.maint * 100).toFixed(2)}% ({p.max_leverage}× max).
              {p.notes.map((n) => (
                <div key={n} className="plan-warn">{n}</div>
              ))}
            </footer>
          </div>
        )}
      </aside>
    </div>
  );
}
