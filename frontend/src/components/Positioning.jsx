import { fmtPct, fmtSigned, fmtUsd } from "../format.js";

export default function Positioning({ context, explanations }) {
  const rows = Object.entries(explanations).map(([w, ex]) => {
    const f = ex.signals.funding;
    return { w, oi: f?.metrics?.oi_change_pct, phrase: f?.phrase || "—", move: ex.move.move_pct };
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
                    <td>{r.oi == null ? "—" : fmtPct(r.oi)}</td>
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
