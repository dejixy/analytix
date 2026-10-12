import { useEffect, useRef, useState } from "react";
import { fmtPrice } from "../format.js";
import { chance } from "./Planner.jsx";

/* Hold clock: how long a very high-leverage position survives, measured on the real best bid and ask of the last
   24 hours (no bell curves). Built for Papertrade-style 800x, where the bust is ~0.12% away and the trigger is
   the best bid or ask. */

const LEVERAGE = [100, 250, 500, 800, 1000];
const fmtS = (s) => {
  if (s == null) return null;
  if (s < 90) return `${Math.max(1, Math.round(s))}s`;
  if (s < 3600) return `${(s / 60).toFixed(s < 600 ? 1 : 0)} min`;
  return `${(s / 3600).toFixed(1)} h`;
};
const hLabel = (h) => (h < 60 ? `${h}s` : `${h / 60} min`);
const band = (p) => (p == null ? "" : p >= 0.9 ? "up" : p >= 0.5 ? "mid" : "down");
const num = (v) => (v === "" || v == null || Number.isNaN(Number(v)) ? null : Number(v));

export default function HoldClock({ coins, defaultCoin, onClose }) {
  const [coin, setCoin] = useState(defaultCoin);
  const [side, setSide] = useState("long");
  const [lev, setLev] = useState("800");
  const [target, setTarget] = useState("50");
  const [entry, setEntry] = useState("");
  const [bust, setBust] = useState("");
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const alive = useRef(true);

  const q = new URLSearchParams({ coin, side, leverage: String(num(lev) || 800) });
  if (num(target)) q.set("target_pct", String(num(target)));
  if (num(entry)) q.set("entry", String(num(entry)));
  if (num(entry) && num(bust)) q.set("bust", String(num(bust)));
  const url = `/api/hold?${q}`;

  useEffect(() => {
    alive.current = true;
    let current = true;                       // this effect's URL is still the one on screen
    setD((old) => (old && old.leverage === (num(lev) || 800) && (old.entry ?? null) === num(entry) ? old : null));
    const load = async () => {
      try {
        const r = await fetch(url);
        const j = await r.json();
        if (!alive.current || !current) return;
        if (r.ok) {
          setD(j);
          setError(null);
        } else setError(j.detail || "Couldn't read the hold clock.");
      } catch {
        if (alive.current) setError("Couldn't reach the backend.");
      }
    };
    const first = setTimeout(load, 250);      // wait for typing to pause
    const t = setInterval(load, 3000);
    return () => {
      current = false;
      clearTimeout(first);
      clearInterval(t);
    };
  }, [url]);
  useEffect(() => () => { alive.current = false; }, []);

  const L = d?.leverage || num(lev) || 800;
  const now = d?.now || {};
  const live = d?.live || {};
  const curveNow = d?.survival_like_now || [];
  const curveAll = d?.survival || [];
  const enough = curveAll.some((c) => c.survive != null);
  const r = d?.race;

  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className="planner" role="dialog" aria-label="Hold clock" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2 className="panel-title">Hold clock</h2>
          <button className="plan-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="plan-form">
          <div className="plan-holds" role="group" aria-label="Coin">
            {coins.map((c) => (
              <button key={c} className={`plan-chip ${coin === c ? "on" : ""}`} onClick={() => setCoin(c)}>{c}</button>
            ))}
          </div>
          <div className="plan-side">
            <button className={`plan-seg long ${side === "long" ? "on" : ""}`} onClick={() => setSide("long")}>Long</button>
            <button className={`plan-seg short ${side === "short" ? "on" : ""}`} onClick={() => setSide("short")}>Short</button>
          </div>
          <label className="plan-field">
            Leverage
            <span className="hc-row">
              <span className="plan-input small"><input value={lev} onChange={(e) => setLev(e.target.value)} inputMode="decimal" aria-label="Leverage" />×</span>
              <span className="plan-holds">
                {LEVERAGE.map((x) => (
                  <button key={x} className={`plan-chip ${L === x ? "on" : ""}`} onClick={() => setLev(String(x))}>{x}×</button>
                ))}
              </span>
            </span>
          </label>
          <label className="plan-field" title="The profit you're aiming for, as a % of your margin">
            Target
            <span className="plan-input small"><input value={target} onChange={(e) => setTarget(e.target.value)} inputMode="decimal" aria-label="Target, % of margin" />%</span>
          </label>
          <label className="plan-field" title="Already in? Your fill price, and the bust price your venue shows, for the live view">
            In a trade
            <span className="plan-brk">
              <span className="plan-input small"><input value={entry} onChange={(e) => setEntry(e.target.value)} placeholder="entry" inputMode="decimal" aria-label="Entry price" /></span>
              <span className="plan-input small"><input value={bust} onChange={(e) => setBust(e.target.value)} placeholder="bust" inputMode="decimal" aria-label="Bust price" /></span>
            </span>
          </label>
        </div>

        {error && <div className="plan-error">{error}</div>}
        {!d ? (
          <p className="plan-note muted" style={{ padding: 12 }}>{error ? "" : "Measuring…"}</p>
        ) : (
          <div className="plan-body">
            <div className="plan-verdict">
              <div className="plan-kicker">At {L}× your bust is {d.dist_bps.toFixed(1)} bps away</div>
              <div className="plan-big">
                <span className={`plan-risk ${d.safe_90_s == null ? (enough ? "up" : "") : d.safe_90_s < 30 ? "down" : d.safe_90_s < 120 ? "mid" : "up"}`}>
                  {d.safe_90_s != null ? `~${fmtS(d.safe_90_s)}` : enough ? "15 min+" : "learning"}
                </span>
                <span className="plan-safe">held 9 times in 10{d.history_s < 6 * 3600 ? ` (from only ${fmtS(d.history_s)} of data so far)` : ""}</span>
              </div>
              <div className="plan-safe">
                {d.half_gone_s != null ? `Half of these positions are gone by ~${fmtS(d.half_gone_s)}. ` : ""}
                {d.bust_price ? `The best ${side === "long" ? "bid must stay above" : "ask must stay below"} ${fmtPrice(d.bust_price)}.` : ""}
              </div>
            </div>

            {position(d, side)}

            <section className="plan-sec">
              <h3>Survival by hold time</h3>
              {!enough ? (
                <p className="plan-note">Still learning: it needs a few minutes of the live book to start, and gets steadier as it builds up 24 hours.</p>
              ) : (
                <table className="plan-table">
                  <thead><tr><th>Hold</th><th title="Only starts whose last 5 minutes moved about as much as the last 5 minutes have">Like now</th><th>Last {Math.max(1, Math.round(d.history_s / 3600))}h</th></tr></thead>
                  <tbody>
                    {curveAll.map((c, i) => {
                      const n = curveNow[i];
                      return (
                        <tr key={c.horizon}>
                          <td>{hLabel(c.horizon)}</td>
                          <td className={band(n?.survive)}><b>{n?.survive == null ? "-" : chance(n.survive)}</b></td>
                          <td className={band(c.survive)}>{c.survive == null ? "-" : chance(c.survive)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
              <p className="plan-note">
                The share of real starts over the last {(d.history_s / 3600).toFixed(1)} h where the best{" "}
                {side === "long" ? "bid" : "ask"} didn't reach your bust within that time. Spread blowouts and wicks count, because
                they're what trigger a bust at this leverage.
              </p>
            </section>

            {Object.keys(d.drift || {}).length > 0 && (
              <section className="plan-sec">
                <h3>Fill drift</h3>
                <p className="plan-note">
                  Your fill is the mid when your transaction lands, a second or two after you click. In {d.drift["1s"] ? "1 second" : "2 seconds"} the mid
                  typically moves {(d.drift["1s"] || d.drift["2s"]).p50.toFixed(2)} bps ({(d.drift["1s"] || d.drift["2s"]).margin_pct_p50.toFixed(1)}% of your margin at {L}×);
                  1 time in 10, {(d.drift["1s"] || d.drift["2s"]).p90.toFixed(2)} bps ({(d.drift["1s"] || d.drift["2s"]).margin_pct_p90.toFixed(0)}%); the worst 1%,{" "}
                  {(d.drift["1s"] || d.drift["2s"]).p99.toFixed(2)} bps ({(d.drift["1s"] || d.drift["2s"]).margin_pct_p99.toFixed(0)}%).
                </p>
              </section>
            )}

            {r && (
              <section className="plan-sec">
                <h3>The race: +{num(target)}% before the bust</h3>
                <div className="plan-brk-bar" aria-hidden="true">
                  <span className="seg target" style={{ width: `${r.target_first * 100}%` }} />
                  <span className="seg time" style={{ width: `${r.neither * 100}%` }} />
                  <span className="seg liq" style={{ width: `${r.bust_first * 100}%` }} />
                </div>
                <p className="plan-note">
                  A +{num(target)}% target is a {r.target_bps.toFixed(1)} bp move. From the last {(d.history_s / 3600).toFixed(0)} hours: target first{" "}
                  <b className="up">{chance(r.target_first)}</b>, bust first <b>{chance(r.bust_first)}</b>, neither within{" "}
                  {r.within_s / 60} min {chance(r.neither)}.
                  {r.median_s_to_target != null && ` When the target came first it took ~${fmtS(r.median_s_to_target)} (median).`}
                </p>
                <p className="plan-note">
                  A win nets about +{r.net_win_pct.toFixed(0)}% after Papertrade's 0.2 bp deadband and 2% win fee (their impact haircut
                  isn't in this yet, so real wins are a bit smaller). Per trade, that averages{" "}
                  <b className={r.ev_pct >= 0 ? "up" : ""}>{r.ev_pct >= 0 ? "+" : "−"}{Math.abs(r.ev_pct).toFixed(1)}%</b> of margin with no edge on direction.
                  {r.ev_pct > 0 && " A positive number here usually means the past day drifted your way, not a lasting edge."}
                </p>
              </section>
            )}

            <section className="plan-sec">
              <h3>The tape right now</h3>
              <p className="plan-note">
                {now.vol_ratio != null
                  ? `The last 5 minutes moved ${now.vol_ratio.toFixed(1)}× as much as usual${now.vol_ratio >= 1.3 ? " (the clock runs faster)" : now.vol_ratio <= 0.75 ? " (quieter than usual)" : ""}. `
                  : ""}
                {live.spread_bps != null && `Spread ${live.spread_bps.toFixed(2)} bps (usually ${live.usual_spread_bps?.toFixed(2)}). `}
                {live.imbalance != null && `The book leans ${Math.abs(live.imbalance) < 0.15 ? "neither way" : live.imbalance > 0 ? "to bids (support below)" : "to asks (supply above)"}. `}
                {live.binance_bps != null && `Binance ${live.binance_bps >= 0 ? "+" : ""}${live.binance_bps.toFixed(1)} bps in the last 5s, Hyperliquid ${live.hl_bps >= 0 ? "+" : ""}${(live.hl_bps ?? 0).toFixed(1)}.`}
              </p>
              {live.warnings?.map((w) => <p key={w} className="plan-note hc-warn">{w}</p>)}
            </section>

            <footer className="plan-foot muted">
              Measured, not modelled: every number comes from the real best bid and ask, second by second. With no bust price
              entered it assumes the bust sits a full 1/{L} away; Papertrade sets it a little closer, so enter the one it shows for
              exact numbers. The past day isn't a promise about the next minute. Only use money you can lose.
            </footer>
          </div>
        )}
      </aside>
    </div>
  );
}

function position(d, side) {
  const p = d.position;
  if (!p) return null;
  return (
    <section className="plan-sec">
      <h3>Your position now</h3>
      {p.busted ? (
        <p className="plan-note"><b className="down">Past the bust price.</b></p>
      ) : (
        <>
          <p className="plan-note">
            <b className={p.pnl_pct >= 0 ? "up" : "down"}>{p.pnl_pct >= 0 ? "+" : "−"}{Math.abs(p.pnl_pct).toFixed(1)}%</b> on margin ·{" "}
            {p.dist_now_bps.toFixed(1)} bps to the bust ({side === "long" ? "best bid" : "best ask"} {fmtPrice(side === "long" ? d.live?.bid : d.live?.ask)}).
          </p>
          <table className="plan-table">
            <thead><tr><th>{p.like_now ? "From here, like now" : "From here"}</th>{p.survival.map((c) => <th key={c.horizon}>{hLabel(c.horizon)}</th>)}</tr></thead>
            <tbody>
              <tr>
                <td>Still in</td>
                {p.survival.map((c) => <td key={c.horizon} className={band(c.survive)}>{c.survive == null ? "-" : chance(c.survive)}</td>)}
              </tr>
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
