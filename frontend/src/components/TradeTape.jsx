import { fmtPrice, fmtSize, fmtTime, fmtUsd } from "../format.js";

export default function TradeTape({ trades }) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Trade tape</h2>
        <span className="panel-sub">taker side · sweeps in bold</span>
      </div>
      <div className="panel-body scroll">
        {trades.length === 0 ? (
          <div className="empty">No trades yet.</div>
        ) : (
          <table>
            <thead>
              <tr><th>Time</th><th>Side</th><th>Price</th><th>Size</th><th>Notional</th></tr>
            </thead>
            <tbody>
              {trades.map((t, i) => {
                const buy = t.side === "B";
                return (
                  <tr key={`${t.ts}-${i}`} className={t.sweep ? "sweep" : ""}>
                    <td className="muted">{fmtTime(t.ts)}</td>
                    <td>
                      <span className="side">
                        {buy ? "Buy" : "Sell"}
                        {t.sweep && <span className="chip">SWEEP</span>}
                      </span>
                    </td>
                    <td>{fmtPrice(t.px)}</td>
                    <td>{fmtSize(t.sz)}</td>
                    <td>{fmtUsd(t.notional)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
