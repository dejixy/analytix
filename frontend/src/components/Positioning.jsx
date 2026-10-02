import { fmtPct, fmtSigned, fmtUsd } from "../format.js";

function ordinal(p) {
  const n = Math.min(99, Math.max(1, Math.round(p * 100)));
  const s = n % 100 >= 10 && n % 100 <= 20 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th";
  return `${n}${s}`;
}

export default function Positioning({ context, explanations }) {
  const rows = Object.entries(explanations).map(([w, ex]) => {
    const f = ex.signals.funding;
    return { w, oi: f?.metrics?.oi_change_pct, oiPct: f?.metrics?.oi_pct, phrase: f?.phrase || "—", move: ex.move.move_pct };
  });
  const crowded = context && Math.abs(context.funding_apr) >= 20 ? (context.funding_apr > 0 ? "Longs" : "Shorts") : null;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Positioning</h2>
        <span className="panel-sub">funding & open interest</span>
      </div>
      <div className="panel-body">
        {!context ? (
          <div className="empty">No funding data yet.</div>
        ) : (
          <>
            <div className="tiles">
              <div className="tile">
                <div className="tile-label">Funding, annualised</div>
                <div className="tile-value num">{fmtPct(context.funding_apr, 1)}</div>
                <div className="tile-sub num">
                  {(context.funding_hourly * 100).toFixed(4)}%/h · {context.funding_apr >= 0 ? "longs pay" : "shorts pay"}
                  {context.funding_pct != null && (
                    <span title="Where today's funding sits among the past week's hourly readings for this coin">
                      {" "}· {ordinal(context.funding_pct)} pct (7d)
                    </span>
                  )}
                </div>
              </div>
              <div className="tile">
                <div className="tile-label">Open interest</div>
                <div className="tile-value num">{fmtUsd(context.open_interest_usd)}</div>
                <div className="tile-sub num">{Math.round(context.open_interest).toLocaleString()} coins</div>
              </div>
              <div className="tile">
                <div className="tile-label">Crowded side</div>
                <div className="tile-value">{crowded || "None"}</div>
                <div className="tile-sub">{crowded ? "funding above ±20% APR" : "funding within ±20% APR"}</div>
              </div>
              <div className="tile">
                <div className="tile-label">Mark − oracle</div>
                <div className="tile-value num">{fmtSigned(context.premium_bps, 1)} bps</div>
                <div className="tile-sub">perp {context.premium_bps >= 0 ? "premium" : "discount"}</div>
              </div>
            </div>
            <table className="oi-table">
              <thead>
                <tr><th>Window</th><th>Price</th><th>OI Δ</th><th style={{ textAlign: "left" }}>Read</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.w}>
                    <td>{r.w}</td>
                    <td>{fmtPct(r.move)}</td>
                    <td title={r.oiPct == null ? "Not enough history to rank this yet" : `Bigger than ${Math.round(r.oiPct * 100)}% of ${r.w} OI moves on record`}>
                      {r.oi == null ? "—" : fmtPct(r.oi)}
                      {r.oiPct != null && r.oiPct >= 0.9 && <span className="rank-tag">top {Math.max(1, Math.round((1 - r.oiPct) * 100))}%</span>}
                    </td>
                    <td style={{ textAlign: "left" }} className="ink2">{r.phrase.replace(/ \(OI.*\)$/, "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}
