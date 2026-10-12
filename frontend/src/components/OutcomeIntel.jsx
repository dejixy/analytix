import { useState } from "react";
import { fmtUsd } from "../format.js";

/* The independent reads for prediction markets: edges against options, the live game and the final score;
   sharp wallets; news; calibration. Kept apart from Outcomes.jsx so the list and pages stay readable. */

export const SOURCES = {
  options: "Options",
  final: "Final score",
  espn: "ESPN live model",
  goals: "Goals model",
  book: "Sportsbook",
  moves: "Perp moves",
};
const RECORD = { beats: "beats market", worse: "worse than market", unproven: "unproven" };

function recordText(r) {
  if (!r) return "unproven";
  return `${RECORD[r.verdict] || r.verdict} · ${r.markets}`;
}
function recordTitle(r) {
  if (!r || !r.markets) return "No settled markets scored yet for this source.";
  const skill = r.skill == null ? "" : `Its chances have been ${Math.abs(r.skill * 100).toFixed(0)}% ${r.skill >= 0 ? "closer to" : "further from"} what happened than the market's own odds. `;
  const ret = r.edge_return == null ? "" : `Its ${r.edges} edges returned ${r.edge_return >= 0 ? "+" : "−"}${Math.abs(r.edge_return * 100).toFixed(0)}% per $1.`;
  return `${r.markets} settled markets. ${skill}${ret}`;
}
const pctOf = (x, d = 0) => (x == null ? "-" : `${(x * 100).toFixed(d)}%`);
const signed = (x, d = 0) => (x == null ? "-" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(d)}%`);
const centsOf = (x) => (x == null ? "-" : `${(x * 100).toFixed(1)}¢`);

function until(ms, now) {
  if (!ms) return "-";
  const s = (ms - now) / 1000;
  if (s <= 0) return "closed";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  return `${Math.round(s / 86400)}d`;
}

const ago = (ms) => {
  if (!ms) return "";
  const m = Math.max(0, Math.round((Date.now() - ms) / 60000));
  return m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
};

/** LIVE Q3 4:12 PIT 21–13 IND · FINAL 27–13 */
export function GameChip({ g }) {
  if (!g || g.state === "pre" || !g.state) return null;
  const score = g.score_a != null ? `${g.a || ""} ${g.score_a}–${g.score_b} ${g.b || ""}`.trim() : "";
  return (
    <span className={`oc-chip ${g.state === "in" ? "live" : ""}`} title={g.detail}>
      {g.state === "in" ? `LIVE ${g.detail || ""}` : "FINAL"} {score}
    </span>
  );
}

export function NewsChip({ n }) {
  if (!n) return null;
  return (
    <span className="oc-chip" title={`${n.recent} articles in the last 2 hours; usually about ${Math.round(n.usual ?? 0)}`}>
      news {n.ratio.toFixed(0)}× normal
    </span>
  );
}

export function SharpChip({ s }) {
  if (!s || Math.abs(s.net) < 1) return null;
  return (
    <span className="oc-chip" title={`Wallets with a record of being right have bought ${Math.abs(s.net).toFixed(0)} net ${s.net > 0 ? "Yes" : "No"} contracts in the last 6 hours`}>
      sharp: {s.net > 0 ? "Yes" : "No"}
    </span>
  );
}

/** The best trades right now: an independent chance against a price you can actually get. */
export function EdgeBoard({ edges, now, onOpen, intel, hidden }) {
  return (
    <section className="oc-board">
      <h3 className="oc-section">
        Edges right now <span className="muted">independent estimate vs the price you can get · at least +3% expected value</span>
      </h3>
      {!edges?.length ? (
        <p className="oc-explain muted">
          No edge right now. Hyperliquid's prices sit within 3% of the independent estimates (the options market, the live
          game, the final score), or there's too little size at those prices.
        </p>
      ) : (
        <div className="scan-scroll oc-board-scroll">
          <table className="scan-table">
            <thead>
              <tr>
                <th className="left">Market</th>
                <th className="left">Buy</th>
                <th title="The price you'd pay per contract (it pays $1 if right)">Price</th>
                <th title="The independent chance this side is right">Estimate</th>
                <th className="left">From</th>
                <th className="left" title="How this source has done on settled markets, against the market's own odds">Record</th>
                <th title="Expected profit per $1 staked, if the estimate is right">EV</th>
                <th title="A quarter of the Kelly bet size, as a share of your bankroll. Full Kelly assumes the estimate is exact; a quarter allows for it not being">¼ Kelly</th>
                <th title="What you can buy at that price right now">Available</th>
                <th title="Profit if right, and that pace over a year">If right</th>
                <th>Closes</th>
              </tr>
            </thead>
            <tbody>
              {edges.map((e) => (
                <tr key={`${e.id}${e.side}`} onClick={() => onOpen(e.id)} title={e.label}>
                  <td className="left oc-name"><b>{e.title}</b><span className="scan-tag">{e.venue_name}</span></td>
                  <td className={`left ${e.side === "Yes" ? "up" : "down"}`}>{e.side}</td>
                  <td className="num">{centsOf(e.price)}</td>
                  <td className="num"><b>{pctOf(e.p)}</b></td>
                  <td className="left muted">{SOURCES[e.source] || e.source}</td>
                  <td className={`left ${e.record?.verdict === "beats" ? "" : "muted"}`} title={recordTitle(e.record)}>{recordText(e.record)}</td>
                  <td className="num up"><b>{signed(e.ev)}</b></td>
                  <td className="num">{pctOf(e.quarter_kelly, 1)}</td>
                  <td className="num">{fmtUsd(e.usd)}</td>
                  <td className="num">
                    {signed(e.if_right, 1)}
                    {e.per_year != null && e.per_year < 5 && <span className="sub">{signed(e.per_year)} a year</span>}
                  </td>
                  <td className="num">{until(e.expiry_ms, now)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {hidden > 0 && <p className="oc-explain muted">{hidden} more hidden: their source has been less accurate than the market.</p>}
      {intel && <SourcesLine intel={intel} />}
    </section>
  );
}

function SourcesLine({ intel }) {
  const mark = (s) => (s?.ok === true ? "on" : s?.ok === false ? "down" : "starting");
  const sh = intel.sharp || {};
  return (
    <p className="oc-explain muted">
      Sources: options {mark(intel.options)}
      {intel.surfaces && Object.keys(intel.surfaces).length ? ` (${Object.keys(intel.surfaces).join(", ")})` : ""} ·
      live games {mark(intel.games)} ({intel.matched_games} matched) · news {mark(intel.news)} ({intel.news_tracked} tracked) ·
      {intel.implied?.ok != null && <> implied vol {mark(intel.implied)}{Object.keys(intel.implied.indices || {}).length
        ? ` (${Object.entries(intel.implied.indices).filter(([, v]) => v != null).map(([k, v]) => `${k} ${v.toFixed(1)}`).join(", ")})` : ""} ·</>}
      sharp wallets: {sh.sharp ?? 0} found from {(sh.scored_trades ?? 0).toLocaleString()} scored trades
    </p>
  );
}

/** One market's independent reads: the estimate, the best trade, returns, the game, sharp wallets, news, rules. */
export function EdgePanel({ r, intel, now }) {
  const t = r.trade;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Edge</h2>
        <span className="panel-sub">an estimate that doesn't come from this order book</span>
      </div>
      <div className="panel-body oc-edge-body">
        {r.ref ? (
          <div className="oc-edge-grid">
            <div className="tile">
              <div className="tile-label">Independent estimate</div>
              <div className="tile-value">{pctOf(r.ref.p, 1)}</div>
              <div className="tile-sub">{SOURCES[r.ref.source] || r.ref.source}</div>
            </div>
            <div className="tile">
              <div className="tile-label">Best trade</div>
              <div className={`tile-value ${t ? (t.side === "Yes" ? "up" : "down") : ""}`}>
                {t ? `Buy ${t.side} at ${centsOf(t.price)}` : "None"}
              </div>
              <div className="tile-sub">{t ? `EV ${signed(t.ev)} per $1` : "the prices sit around the estimate"}</div>
            </div>
            <div className="tile" title="A quarter of the Kelly size. Full Kelly assumes the estimate is exact">
              <div className="tile-label">¼ Kelly stake</div>
              <div className="tile-value">{t ? pctOf(t.quarter_kelly, 1) : "-"}</div>
              <div className="tile-sub">{t ? `${fmtUsd(t.usd)} available at that price` : "of a bankroll"}</div>
            </div>
            <div className="tile" title="Profit if each side is right, at the price you can buy it now, and that pace over a year">
              <div className="tile-label">If right</div>
              <div className="tile-value">
                <span className="up">Yes {signed(r.yes_returns?.if_right, 1)}</span>
              </div>
              <div className="tile-sub">
                No {signed(r.no_returns?.if_right, 1)}
                {r.yes_returns?.per_year != null && r.yes_returns.per_year < 5 ? ` · Yes ${signed(r.yes_returns.per_year)} a year` : ""}
              </div>
            </div>
          </div>
        ) : (
          <p className="oc-note">
            No independent estimate for this market yet. Price markets on BTC, ETH and SOL use the options market; matches
            use the live game once it's matched on ESPN.
          </p>
        )}
        {r.ref && <p className="oc-how muted">{r.ref.label}. Not advice: an estimate to trade against, with its own error.</p>}
        {intel?.rules && <p className="oc-how muted">{intel.rules}</p>}
      </div>
    </section>
  );
}

export function GamePanel({ game, chip }) {
  if (!game) return null;
  const home = game.home?.team || {};
  const away = game.away?.team || {};
  const book = game.odds || {};
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">The game</h2>
        <span className="panel-sub">{game.state === "in" ? "live" : game.state === "post" ? "finished" : "not started"} · ESPN</span>
      </div>
      <div className="panel-body">
        <div className="oc-score">
          <span>{away.displayName || away.shortDisplayName}</span>
          <b className="num">{game.away_score ?? "-"}</b>
          <span className="muted">at</span>
          <b className="num">{game.home_score ?? "-"}</b>
          <span>{home.displayName || home.shortDisplayName}</span>
        </div>
        <p className="oc-note">{game.detail}{chip?.state === "pre" && game.start_ms ? ` · starts ${new Date(game.start_ms).toLocaleString()}` : ""}</p>
        {book.home_ml != null && (
          <p className="oc-how muted">
            Pre-game line{book.provider ? ` (${book.provider})` : ""}: home {book.home_ml > 0 ? "+" : ""}{book.home_ml}, away{" "}
            {book.away_ml > 0 ? "+" : ""}{book.away_ml}{book.draw_ml != null ? `, draw ${book.draw_ml > 0 ? "+" : ""}${book.draw_ml}` : ""}
          </p>
        )}
        {game.win_prob?.home != null && (
          <p className="oc-how muted">ESPN live model: home {pctOf(game.win_prob.home)}, away {pctOf(game.win_prob.away)}</p>
        )}
        {game.link && <a className="oc-link" href={game.link} target="_blank" rel="noreferrer">Game page on ESPN ›</a>}
      </div>
    </div>
  );
}

export function SharpPanel({ sharp }) {
  if (!sharp) return null;
  const s = sharp.summary || {};
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Sharp wallets</h2>
        <span className="panel-sub">wallets that keep being right</span>
      </div>
      <div className="panel-body">
        {Math.abs(sharp.sharp_net) >= 1 ? (
          <p className="oc-note">
            Last 6 hours: sharp wallets bought <b className={sharp.sharp_net > 0 ? "up" : "down"}>
              {Math.abs(sharp.sharp_net).toFixed(0)} net {sharp.sharp_net > 0 ? "Yes" : "No"}
            </b>{" "}contracts{Math.abs(sharp.fade_net) >= 1 ? `; wallets that are usually wrong bought ${Math.abs(sharp.fade_net).toFixed(0)} net ${sharp.fade_net > 0 ? "Yes" : "No"}` : ""}.
          </p>
        ) : (
          <p className="oc-note">No sharp-wallet trading here in the last 6 hours.</p>
        )}
        {sharp.known_wallets?.length > 0 && (
          <table>
            <thead><tr><th>Wallet</th><th>Record</th><th title="Trades scored">Trades</th><th title="Average points gained per contract, 2 hours after trading">Avg 2h</th><th title="How unlikely that average is to be luck (2 or more is strong)">t</th></tr></thead>
            <tbody>
              {sharp.known_wallets.map((w) => (
                <tr key={w.wallet}>
                  <td title={w.wallet}>{w.wallet.slice(0, 6)}…{w.wallet.slice(-4)}</td>
                  <td className={w.label === "sharp" ? "up" : "down"}>{w.label}</td>
                  <td>{w.trades}</td>
                  <td>{w.avg_2h > 0 ? "+" : ""}{w.avg_2h} pts</td>
                  <td>{w.t}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="oc-how muted">
          Learning across all markets: {s.sharp ?? 0} sharp and {s.fade ?? 0} usually-wrong wallets from{" "}
          {(s.scored_trades ?? 0).toLocaleString()} scored trades. A wallet needs 15+ trades, +1.5 points on average and a t of 2+.
        </p>
      </div>
    </div>
  );
}

export function NewsPanel({ news }) {
  if (!news) return null;
  const s = news.spike;
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">News pulse</h2>
        <span className="panel-sub">coverage against its own normal · GDELT</span>
      </div>
      <div className="panel-body">
        <p className="oc-note">
          {s?.ratio != null
            ? `${s.recent} articles in the last 2 hours, ${s.ratio.toFixed(1)}× the usual ${Math.round(s.usual ?? 0)}.`
            : "Measuring its normal coverage…"}
          {news.flags?.length > 0 && <> Headlines mention: {news.flags.map((f) => <span key={f} className="oc-chip">{f}</span>)}</>}
        </p>
        {news.articles == null ? (
          <p className="oc-how muted">Loading headlines…</p>
        ) : news.articles.length === 0 ? (
          <p className="oc-how muted">No headlines in the last 2 days.</p>
        ) : (
          <ul className="oc-news">
            {news.articles.slice(0, 3).map((a) => (
              <li key={a.url}>
                <a href={a.url} target="_blank" rel="noreferrer">{a.title}</a>
                <span className="muted"> · {a.domain} · {ago(a.seen_ms)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Does 90% mean 90%? Hyperliquid's odds 6 hours before the close, against how markets really ended. */
export function ScorecardPanel({ card }) {
  const [open, setOpen] = useState(false);
  if (!card) return null;
  const settled = card.reduce((n, r) => Math.max(n, r.markets), 0);
  return (
    <section className="oc-board">
      <button className="oc-more oc-section-btn" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} Scorecard: how each estimate has done ({settled} settled markets scored)
      </button>
      {open && (
        !card.length ? (
          <p className="oc-explain muted">Fills in as markets settle. Each source needs 15 settled markets for a verdict.</p>
        ) : (
          <div className="scan-scroll">
            <table className="scan-table">
              <thead>
                <tr>
                  <th className="left">Source</th><th>Markets</th>
                  <th title="How much closer to what happened than the market's own odds (Brier score)">vs market</th>
                  <th>Edges</th><th title="What buying its edges returned per $1">Return</th><th>Won</th><th className="left">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {card.map((r) => (
                  <tr key={r.source}>
                    <td className="left">{SOURCES[r.source] || r.source}</td>
                    <td className="num">{r.markets}</td>
                    <td className={`num ${r.skill > 0 ? "up" : r.skill < 0 ? "down" : ""}`}>{signed(r.skill)}</td>
                    <td className="num">{r.edges}</td>
                    <td className={`num ${r.edge_return > 0 ? "up" : r.edge_return < 0 ? "down" : ""}`}>{signed(r.edge_return)}</td>
                    <td className="num">{pctOf(r.edge_won)}</td>
                    <td className="left">{RECORD[r.verdict] || r.verdict}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </section>
  );
}

export function CalibrationPanel({ cal }) {
  const [open, setOpen] = useState(false);
  if (!cal) return null;
  return (
    <section className="oc-board">
      <button className="oc-more oc-section-btn" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} Calibration: how well Hyperliquid's odds predict ({cal.settled} settled markets so far)
      </button>
      {open && (
        cal.settled < 30 ? (
          <p className="oc-explain muted">
            Still learning: it needs about 30 settled markets with their odds noted 6 hours before the close. It fills in by
            itself as markets settle, and is kept across restarts.
          </p>
        ) : (
          <div className="scan-scroll">
            <table className="scan-table oc-calib">
              <thead><tr><th className="left">Odds 6h before</th><th>Markets</th><th>Priced at</th><th>Happened</th><th>Gap</th></tr></thead>
              <tbody>
                {cal.rows.map((r) => (
                  <tr key={r.band}>
                    <td className="left">{r.band}</td>
                    <td className="num">{r.n}</td>
                    <td className="num">{pctOf(r.priced)}</td>
                    <td className="num">{pctOf(r.happened)}</td>
                    <td className={`num ${r.happened - r.priced > 0.03 ? "up" : r.happened - r.priced < -0.03 ? "down" : ""}`}>
                      {signed(r.happened - r.priced)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </section>
  );
}

/** A price ladder at one closing time as bands: where the market and the independent estimate put the price. */
export function Distribution({ rows }) {
  const lines = rows.map((r) => r.threshold);
  const mkt = rows.map((r) => (r.bid != null && r.ask != null && r.ask - r.bid <= 0.1 ? (r.bid + r.ask) / 2 : r.odds));
  const fair = rows.map((r) => r.fair);
  if (lines.length < 3 || mkt.some((x) => x == null) || fair.some((x) => x == null)) return null;
  const bands = (ps) => {
    const pts = [1, ...ps, 0];
    return pts.slice(0, -1).map((p, i) => p - pts[i + 1]);
  };
  const bm = bands(mkt);
  const bf = bands(fair);
  const label = (i) => (i === 0 ? `below ${lines[0].toLocaleString()}` : i === lines.length ? `above ${lines.at(-1).toLocaleString()}`
    : `${lines[i - 1].toLocaleString()}–${lines[i].toLocaleString()}`);
  const max = Math.max(0.05, ...bm.map(Math.abs), ...bf);
  return (
    <div className="oc-dist" title="Each band's chance: from this ladder's prices (blue) and from the independent estimate (orange). A negative band means the ladder is out of order">
      <div className="oc-dist-head muted">
        Where it ends: <span className="oc-swatch" style={{ background: "#3987e5" }} />market{" "}
        <span className="oc-swatch" style={{ background: "#d95926" }} />estimate
      </div>
      {bm.map((p, i) => (
        <div key={i} className="oc-dist-row">
          <span className="oc-dist-label">{label(i)}</span>
          <span className="oc-dist-bars">
            <span style={{ width: `${(Math.max(0, p) / max) * 100}%`, background: "#3987e5" }} />
            <span style={{ width: `${(Math.max(0, bf[i]) / max) * 100}%`, background: "#d95926" }} />
          </span>
          <span className={`oc-dist-val num ${p < -0.005 ? "down" : ""}`}>{pctOf(p)} · {pctOf(bf[i])}</span>
        </div>
      ))}
    </div>
  );
}
