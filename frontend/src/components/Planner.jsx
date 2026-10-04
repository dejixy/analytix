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
  if (p < 0.05) return ["moderate", "warn"];
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
        const r = await fetch(`/api/plan?${q}`);
        const d = await r.json();
        if (id !== seq.current) return;
        if (!r.ok) {
          setError(typeof d.detail === "string" ? d.detail : "Check the inputs.");
        } else {
          setError(null);
          setPlan(d);
        }
      } catch {
        if (id === seq.current) setError("Couldn't reach the backend.");
      } finally {
        if (id === seq.current) setBusy(false);
      }
    };
    const t = setTimeout(run, 250);
    const every = setInterval(run, REFRESH_MS);
    return () => {
      clearTimeout(t);
      clearInterval(every);
    };
  }, [coin, side, margin, leverage, hours]);

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
          <label className="plan-field">
            <span>Leverage</span>
            <span className="plan-lev">
              <input type="range" min="1" max={maxLev} step="1" value={Math.min(leverage, maxLev)} onChange={(e) => setLeverage(Number(e.target.value))} aria-label="Leverage" />
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
          <div className="plan-sum muted">
            {usd(margin * leverage)} position · market entry ≈ {fmtPrice(mid)}
            {busy && <span className="plan-busy"> · updating…</span>}
          </div>
        </div>

        {error && <div className="plan-error">{error}</div>}

        {p && (
          <div className="plan-body">
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
                        <td className={`num ${t.pnl > 0 ? "up" : t.pnl < 0 ? "down" : ""}`}>{t.kind === "entry" ? "—" : usd(t.pnl, true)}</td>
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
            </section>

            <section className="plan-sec">
              <h3>Costs</h3>
              <dl className="plan-costs num">
                <dt>Fees (0.045% in and out)</dt><dd>{usd(p.costs.fees)}</dd>
                <dt>Slippage on the book now</dt><dd>{p.costs.slippage_known ? usd(p.costs.slippage) : "beyond visible book"}</dd>
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
              No direction assumed: this sizes the risk, it doesn't predict the move. Isolated margin; liquidation counted as losing the whole margin; maintenance {(p.maint * 100).toFixed(2)}% ({p.max_leverage}× max).
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
