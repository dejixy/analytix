import { fmtPct, fmtPrice, fmtTime } from "../format.js";

function CoinTabs({ coins, tickers, active, onCoin }) {
  if (!coins || coins.length < 2) return null;
  return (
    <nav className="coins" aria-label="Market">
      {coins.map((c) => {
        const t = tickers?.[c] || {};
        const mv = t.move_1m_pct;
        return (
          <button key={c} className={`coin-tab ${c === active ? "active" : ""}`} onClick={() => onCoin(c)} aria-pressed={c === active}>
            <span className="coin-name">{c}</span>
            <span className="coin-meta">
              {fmtPrice(t.mid)}{" "}
              {mv != null && <span className={mv >= 0 ? "up" : "down"}>{fmtPct(mv)}</span>}
            </span>
          </button>
        );
      })}
    </nav>
  );
}

function FeedBadge({ feed, conn }) {
  if (conn !== "open") {
    return (
      <span className="badge" title="The dashboard lost its connection to the Analytix backend">
        <span className="dot critical" /> Backend offline
      </span>
    );
  }
  if (feed.mode === "replay") {
    const sample = (feed.source || "").endsWith("sampleSession.jsonl");
    return (
      <span className="badge" title={feed.source}>
        <span className={`dot info ${feed.finished ? "" : "pulse"}`} />
        {feed.finished ? "Replay finished" : "Replay"}
        {sample ? " · synthetic sample" : ""}
      </span>
    );
  }
  if (!feed.connected) {
    return (
      <span className="badge" title={feed.last_error || ""}>
        <span className="dot critical" /> Reconnecting
      </span>
    );
  }
  if (feed.stale) {
    return (
      <span className="badge" title="Connected, but the exchange hasn't sent anything for a while">
        <span className="dot warning" /> No updates for {Math.round(feed.silence_s)}s
      </span>
    );
  }
  return (
    <span className="badge">
      <span className="dot good pulse" /> Live
    </span>
  );
}

export default function Header({ snap, conn, onCoin, onPlan, onAlerts }) {
  const { price, context, coin, feed } = snap;
  const chg = context?.change_24h_pct;
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-name">ANALYTIX<span className="cursor" aria-hidden="true" /></span>
        <span className="brand-tag">why did this price move?</span>
      </div>

      <CoinTabs coins={snap.coins} tickers={snap.tickers} active={coin} onCoin={onCoin} />

      <div className="market">
        <span className="market-sym">{coin}-PERP</span>
        <span className="market-price">{fmtPrice(price?.mid)}</span>
        {chg != null && <span className={`num ${chg >= 0 ? "up" : "down"}`}>{chg >= 0 ? "▲" : "▼"} {fmtPct(chg)} 24h</span>}
      </div>

      <div className="stats">
        <div>
          <div className="stat-label">Spread</div>
          <div className="stat-value num">{price ? `${(price.spread_bps / 100).toFixed(3)}%` : "-"}</div>
        </div>
        <div>
          <div className="stat-label">Funding / year</div>
          <div className="stat-value num">{context ? fmtPct(context.funding_apr, 1) : "-"}</div>
        </div>
        <div>
          <div className="stat-label" title="Hyperliquid's price compared with the index price from other big exchanges">vs index</div>
          <div className="stat-value num">{context ? fmtPct(context.premium_bps / 100, 3) : "-"}</div>
        </div>
      </div>

      <div className="header-right">
        <button className="alerts-open" onClick={onAlerts} title="Telegram and browser alerts: big liquidations, steady sellers or buyers (TWAPs), broken price levels, your own price alerts">
          Alerts
        </button>
        <button className="plan-open" onClick={onPlan} title="Before you take a trade: your chance of liquidation, where price may go, and what it costs">
          Plan a trade
        </button>
        <FeedBadge feed={feed} conn={conn} />
        <span className="clock num" title="Exchange clock (your local time zone)">{fmtTime(snap.now_ms)}</span>
      </div>
    </header>
  );
}
