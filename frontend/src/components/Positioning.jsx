import { fmtPct, fmtUsd } from "../format.js";

export default function Positioning({ context, explanations }) {
  const rows = Object.entries(explanations).map(([w, ex]) => {
    const f = ex.signals.funding;
    return { w, oi: f?.metrics?.oi_change_pct, oiPct: f?.metrics?.oi_pct, phrase: f?.phrase || "-", move: ex.move.move_pct };
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
                <div className="tile-label">Funding, per year</div>
                <div className="tile-value num">{fmtPct(context.funding_apr, 1)}</div>
                <div className="tile-sub num">
                  {(context.funding_hourly * 100).toFixed(4)}% an hour · {context.funding_apr >= 0 ? "longs pay" : "shorts pay"}
                  {context.funding_pct != null && (
                    <span title="Where today's funding sits among the past week's hourly readings for this coin">
                      {" "}· higher than {Math.round(context.funding_pct * 100)}% of the past week
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
                <div className="tile-sub">{crowded ? `${crowded.toLowerCase()} pay over 20% a year` : "funding under 20% a year"}</div>
              </div>
              <div className="tile">
                <div className="tile-label" title="Hyperliquid's price compared with the index price from other big exchanges">Price vs index</div>
                <div className="tile-value num">{fmtPct(context.premium_bps / 100, 3)}</div>
                <div className="tile-sub">{context.premium_bps >= 0 ? "above" : "below"} other exchanges</div>
              </div>
            </div>
            <table className="oi-table">
              <thead>
                <tr><th>Timeframe</th><th>Price</th><th>Open interest</th><th style={{ textAlign: "left" }}>What it means</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.w}>
                    <td>{r.w}</td>
                    <td>{fmtPct(r.move)}</td>
                    <td title={r.oiPct == null ? "Not enough history to rank this yet" : `Bigger than ${Math.round(r.oiPct * 100)}% of ${r.w} open interest changes on record`}>
                      {r.oi == null ? "-" : fmtPct(r.oi)}
                      {r.oiPct != null && r.oiPct >= 0.9 && <span className="rank-tag">top {Math.max(1, Math.round((1 - r.oiPct) * 100))}%</span>}
                    </td>
                    <td style={{ textAlign: "left" }} className="ink2">{r.phrase.replace(/ \(open interest.*\)$/, "")}</td>
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
