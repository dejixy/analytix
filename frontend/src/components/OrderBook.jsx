import { fmtPrice, fmtSize, fmtUsd } from "../format.js";

const ROWS = 10;

function withCumulative(levels) {
  let cum = 0;
  return levels.slice(0, ROWS).map(([px, sz, n]) => {
    cum += sz;
    return { px, sz, n, cum };
  });
}

export default function OrderBook({ book, price, walls }) {
  if (!book) {
    return (
      <div className="panel">
        <div className="panel-head"><h2 className="panel-title">Order book</h2></div>
        <div className="empty">No book yet.</div>
      </div>
    );
  }
  const bids = withCumulative(book.bids);
  const asks = withCumulative(book.asks);
  const max = Math.max(bids.at(-1)?.cum || 0, asks.at(-1)?.cum || 0) || 1;
  const bidShare = book.bid_notional + book.ask_notional > 0 ? book.bid_notional / (book.bid_notional + book.ask_notional) : 0.5;

  const wallAt = new Map((walls?.active || []).map((w) => [`${w.side}${w.price}`, w]));
  const stats = walls?.stats;
  const row = (l, side) => {
    const wall = wallAt.get(`${side}${l.px}`);
    return (
    <tr className={`book-row ${wall ? "wall" : ""}`} key={`${side}${l.px}`}>
      <td className={side === "bid" ? "up" : "down"}>{fmtPrice(l.px)}</td>
      <td>
        {fmtSize(l.sz)}
        {wall && (
          <span className="wall-tag" title={`Wall: ${fmtUsd(wall.notional)} resting for ${Math.round(wall.age_s)}s · ${fmtUsd(wall.traded)} traded into it so far`}>
            wall
          </span>
        )}
      </td>
      <td className="muted">{fmtSize(l.cum)}</td>
      <td style={{ position: "static", padding: 0, width: 0 }}>
        <span className={`depth-bar ${side}`} style={{ width: `${(l.cum / max) * 100}%` }} />
      </td>
    </tr>
    );
  };
  const wallLine = (side, label) => {
    const real = stats.eaten[side] + stats.held[side];
    const pulled = stats.pulled_near[side];
    return real + pulled === 0 ? `${label}: none yet` : `${label}: ${real} real · ${pulled} pulled`;
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Order book</h2>
        <span className="panel-sub">top {ROWS} levels</span>
      </div>
      <div className="panel-body">
        <table>
          <thead>
            <tr><th>Price</th><th>Size</th><th>Total</th><th /></tr>
          </thead>
          <tbody>
            {[...asks].reverse().map((l) => row(l, "ask"))}
            <tr className="spread-row">
              <td colSpan={4}>
                Spread {price ? `${(price.best_ask - price.best_bid).toFixed(2)} · ${price.spread_bps.toFixed(2)} bps` : "—"}
              </td>
            </tr>
            {bids.map((l) => row(l, "bid"))}
          </tbody>
        </table>
        <div className="meter" role="img" aria-label={`Bids are ${Math.round(bidShare * 100)}% of near-touch depth`}>
          <div className="meter-fill" style={{ width: `${bidShare * 100}%` }} />
        </div>
        <div className="panel-sub num" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>Bids {fmtUsd(book.bid_notional)} · {Math.round(bidShare * 100)}%</span>
          <span>{Math.round((1 - bidShare) * 100)}% · {fmtUsd(book.ask_notional)} Asks</span>
        </div>
        {stats && (
          <div
            className="panel-sub num wall-stats"
            title="Big resting orders (4× a normal level) over the last 30 minutes. Real = traded into, or standing after absorbing 20%+ of its size. Pulled = vanished unfilled as price came within 10 bps: often bait."
          >
            <span>{wallLine("bid", "Bid walls")}</span>
            <span>{wallLine("ask", "Ask walls")}</span>
          </div>
        )}
      </div>
    </div>
  );
}
