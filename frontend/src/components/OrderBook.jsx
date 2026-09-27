import { fmtPrice, fmtSize, fmtUsd } from "../format.js";

const ROWS = 10;

function withCumulative(levels) {
  let cum = 0;
  return levels.slice(0, ROWS).map(([px, sz, n]) => {
    cum += sz;
    return { px, sz, n, cum };
  });
}

export default function OrderBook({ book, price }) {
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

  const row = (l, side) => (
    <tr className="book-row" key={`${side}${l.px}`}>
      <td className={side === "bid" ? "up" : "down"}>{fmtPrice(l.px)}</td>
      <td>{fmtSize(l.sz)}</td>
      <td className="muted">{fmtSize(l.cum)}</td>
      <td style={{ position: "static", padding: 0, width: 0 }}>
        <span className={`depth-bar ${side}`} style={{ width: `${(l.cum / max) * 100}%` }} />
      </td>
    </tr>
  );

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
      </div>
    </div>
  );
}
