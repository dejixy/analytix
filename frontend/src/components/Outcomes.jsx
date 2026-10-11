import { useEffect, useMemo, useRef, useState } from "react";
import { fmtPrice, fmtTime, fmtUsd } from "../format.js";

/* Prediction markets on Hyperliquid (HIP-4): every market from Outcome, trade.xyz and anyone else listing on
   Hyperliquid's books. A contract pays $1 if it happens, so a price of 0.62 reads as 62%. */

const KINDS = [
  ["all", "All"],
  ["price", "Price"],
  ["sports", "Sports"],
  ["other", "Other"],
];
const SORTS = [
  ["volume", "Most traded", "Most traded in the last 24 hours"],
  ["closing", "Closing soon", "Closest to closing first"],
  ["move", "Biggest 24h move", "The biggest change in odds over 24 hours"],
  ["edge", "Edges", "Price markets furthest from fair odds; events with a locked-in edge"],
];

export const pct = (x) => (x == null ? "-" : `${(x * 100).toFixed(x < 0.1 || x > 0.9 ? 1 : 0)}%`);
const cents = (x) => (x == null ? "-" : `${(x * 100).toFixed(1)}¢`);
const pts = (x, d = 1) => (x == null ? "-" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(d)} pts`);
const cls = (x) => (x == null || Math.abs(x) < 0.5 ? "" : x > 0 ? "up" : "down");
const num = (x) => (x == null ? "-" : x >= 1000 ? `${(x / 1000).toFixed(1)}K` : x.toFixed(0));

function until(ms, now) {
  if (!ms) return "-";
  const s = (ms - now) / 1000;
  if (s <= 0) return "closed";
  if (s < 3600) return `in ${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `in ${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  return `in ${Math.round(s / 86400)}d`;
}

/** The short read on one market, for a card's footer. */
function marketNote(r) {
  if (r.settled) return "Settled.";
  if (r.edge != null && Math.abs(r.edge) >= 5) {
    return `Yes looks ${r.edge > 0 ? "cheap" : "rich"}: fair odds ${pct(r.fair)} vs the market's ${pct(r.odds)}.`;
  }
  if (r.bid == null && r.ask == null) return "Nobody is quoting it yet.";
  if (r.spread != null && r.spread > 10) return `Wide spread (${r.spread.toFixed(0)} pts): the odds come from the last trade.`;
  return null;
}

/** "22.8¢ / 22.9¢": one decimal, or two when one would hide the spread. */
export function bidAsk(bid, ask) {
  if (bid == null && ask == null) return "no orders";
  const d = bid != null && ask != null && (bid * 100).toFixed(1) === (ask * 100).toFixed(1) ? 2 : 1;
  const c = (x) => (x == null ? "-" : `${(x * 100).toFixed(d)}¢`);
  return `${c(bid)} / ${c(ask)}`;
}

const isClosed = (r, now) => r.settled || (r.expiry_ms != null && r.expiry_ms <= now);
const sumBy = (rows, f) => rows.reduce((s, r) => s + (f(r) || 0), 0);
const lineText = (r) => `${r.kind === "touch" ? "touches" : "above"} ${r.threshold?.toLocaleString(undefined, { maximumFractionDigits: 6 })}`;

function useJson(url, everyMs) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const load = async () => {
      try {
        const r = await fetch(url);
        const d = await r.json();
        if (!alive.current) return;
        if (r.ok) {
          setData(d);
          setError(null);
        } else setError(d.detail || "Couldn't load that.");
      } catch {
        if (alive.current) setError("Couldn't reach the backend.");
      }
    };
    load();
    const t = setInterval(load, everyMs);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [url, everyMs]);
  return { data, error };
}

const VenueTag = ({ name }) => <span className="scan-tag">{name}</span>;

/** One outcome as a line: name, odds bar, odds, 24h change. */
function OddsRow({ r, label, odds = r.odds, chg = r.chg_24h, onOpen, wrap = false }) {
  const priced = odds != null;
  return (
    <button className={`oc-row ${priced ? "" : "dim"} ${wrap ? "wrap" : ""}`} onClick={(e) => { e.stopPropagation(); onOpen(r.id); }}
      title={`${r.title}\nBid / ask for Yes: ${bidAsk(r.bid, r.ask)}${r.volume_24h ? `\nTraded today: ${fmtUsd(r.volume_24h)}` : ""}`}>
      <span className="oc-row-name">{label}</span>
      <span className="oc-bar" aria-hidden="true"><span style={{ width: `${priced ? Math.max(0.5, odds * 100) : 0}%` }} /></span>
      <span className="oc-row-odds num">{priced ? pct(odds) : "-"}</span>
      <span className={`oc-row-chg num ${cls(chg)}`}>{chg == null || Math.abs(chg) < 0.05 ? "" : pts(chg)}</span>
    </button>
  );
}

const SHOWN = 5;

/** A question (several outcomes, one ends Yes) or a single market with its two sides. */
function EventCard({ ev, now, onOpen, onEvent }) {
  const [all, setAll] = useState(false);
  const r0 = ev.rows[0];
  const vol = sumBy(ev.rows, (r) => r.volume_24h);
  const sub = [ev.sub, until(ev.expiry_ms, now) === "-" ? null : `closes ${until(ev.expiry_ms, now)}`,
    vol ? `${fmtUsd(vol)} traded today` : "no trades today"].filter(Boolean).join(" · ");
  const note = ev.q ? ev.q.sum.note : marketNote(r0);
  const edge = ev.q ? !!ev.q.sum.way : r0.edge != null && Math.abs(r0.edge) >= 5;
  const shown = all ? ev.rows : ev.rows.slice(0, SHOWN);
  const openCard = () => (ev.q ? onEvent(ev.q.id) : onOpen(r0.id));
  return (
    <article className="oc-card oc-click" onClick={openCard} role="button" tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && openCard()}
      title={ev.q ? "Open the event: every outcome, and their odds on one chart" : "Open this market"}>
      <header className="oc-card-head">
        <h3>{ev.title}</h3>
        <VenueTag name={ev.venue_name} />
      </header>
      <div className="oc-card-sub">{sub}</div>
      <div className="oc-rows">
        {ev.q ? (
          shown.map((r) => <OddsRow key={r.id} r={r} label={r.label} onOpen={onOpen} />)
        ) : (
          <>
            <OddsRow r={r0} label={r0.sides[0]} onOpen={onOpen} wrap />
            <OddsRow r={r0} label={r0.sides[1]} odds={r0.odds == null ? null : 1 - r0.odds}
              chg={r0.chg_24h == null ? null : -r0.chg_24h} onOpen={onOpen} wrap />
          </>
        )}
      </div>
      {ev.q && ev.rows.length > SHOWN && (
        <button className="oc-more" onClick={(e) => { e.stopPropagation(); setAll(!all); }}>
          {all ? "Show fewer" : `+${ev.rows.length - SHOWN} more`}
        </button>
      )}
      <div className="oc-card-foot">
        <span className={`oc-card-note ${edge ? "oc-edge" : ""}`}>{note}</span>
        <span className="oc-open">{ev.q ? "chart ›" : "open ›"}</span>
      </div>
    </article>
  );
}

/** Every price market on one asset, as a ladder: when it closes, the line, the market's odds and the fair odds. */
function PriceCard({ g, now, sort, onOpen }) {
  const [all, setAll] = useState(false);
  const rows = [...g.rows].sort(sort === "edge"
    ? (a, b) => Math.abs(b.edge ?? 0) - Math.abs(a.edge ?? 0)
    : (a, b) => (a.expiry_ms - b.expiry_ms) || (a.threshold - b.threshold));
  const shown = all ? rows : rows.slice(0, 8);
  const venues = [...new Set(g.rows.map((r) => r.venue_name))];
  const vol = sumBy(g.rows, (r) => r.volume_24h);
  return (
    <article className="oc-card">
      <header className="oc-card-head">
        <h3>{g.asset} <span className="oc-spot num">{g.spot ? fmtPrice(g.spot) : ""}</span></h3>
        <span>{venues.map((v) => <VenueTag key={v} name={v} />)}</span>
      </header>
      <div className="oc-card-sub">
        {g.rows.length} market{g.rows.length === 1 ? "" : "s"} · {vol ? `${fmtUsd(vol)} traded today` : "no trades today"}
      </div>
      <table className="oc-ladder">
        <thead>
          <tr>
            <th>Closes</th>
            <th title="Yes if the price ends at or above the line (or touches it, for touch markets)">Line</th>
            <th title="The market's odds for Yes">Market</th>
            <th title="The chance from the perp's live price and how much it has been moving">Fair</th>
            <th title="Fair minus market, in points. Positive: Yes looks cheap. Negative: Yes looks rich">Gap</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr key={r.id} onClick={() => onOpen(r.id)} title={`${r.title}\nBid / ask for Yes: ${bidAsk(r.bid, r.ask)}`}
              className={r.odds == null ? "dim" : ""}>
              <td>{until(r.expiry_ms, now).replace("in ", "")}</td>
              <td>{lineText(r)}</td>
              <td className="num"><b>{pct(r.odds)}</b></td>
              <td className="num">{pct(r.fair)}</td>
              <td className={`num ${cls(r.edge)}`}>{r.edge == null ? "-" : `${r.edge >= 0 ? "+" : "−"}${Math.abs(r.edge).toFixed(0)}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 8 && (
        <button className="oc-more" onClick={() => setAll(!all)}>{all ? "Show fewer" : `+${rows.length - 8} more`}</button>
      )}
    </article>
  );
}

const PAGE = 48;

/** Every market: price markets as a ladder per asset, everything else as one card per event. */
function MarketList({ onOpen, onEvent }) {
  const { data, error } = useJson("/api/outcomes", 5000);
  const [venue, setVenue] = useState("all");
  const [kind, setKind] = useState("all");
  const [sort, setSort] = useState("volume");
  const [q, setQ] = useState("");
  const [showClosed, setShowClosed] = useState(false);
  const [limit, setLimit] = useState(PAGE);

  const { prices, events, nPrice } = useMemo(() => {
    if (!data?.markets) return { prices: [], events: [], nPrice: 0 };
    const now = data.now_ms;
    const term = q.trim().toLowerCase();
    const open = (r) => showClosed || !isClosed(r, now);
    const inVenue = (r) => venue === "all" || r.venue === venue;
    const byId = Object.fromEntries(data.markets.map((r) => [r.id, r]));

    // price markets, grouped by the asset they settle on
    const groups = {};
    let nPrice = 0;
    if (kind === "all" || kind === "price") {
      for (const r of data.markets) {
        if ((r.kind !== "price" && r.kind !== "touch") || !open(r) || !inVenue(r)) continue;
        if (term && !r.title.toLowerCase().includes(term)) continue;
        const asset = (r.underlying || "?").split(":").pop();
        (groups[asset] ||= { asset, spot: r.spot, rows: [] }).rows.push(r);
        nPrice += 1;
      }
    }
    const prices = Object.values(groups).sort((a, b) => sumBy(b.rows, (r) => r.volume_24h) - sumBy(a.rows, (r) => r.volume_24h)
      || b.rows.length - a.rows.length);

    // events: questions, then single markets that aren't price markets
    const evs = [];
    const inQ = new Set();
    for (const qu of data.questions) {
      const rows = qu.outcomes.map((id) => byId[id]).filter(Boolean);
      rows.forEach((r) => inQ.add(r.id));
      if (!rows.length || !inVenue(rows[0]) || (kind !== "all" && rows[0].kind !== kind)) continue;
      if (!showClosed && rows.every((r) => isClosed(r, now))) continue;
      if (term && !qu.title.toLowerCase().includes(term) && !rows.some((r) => r.label.toLowerCase().includes(term))) continue;
      rows.sort((a, b) => (b.odds ?? -1) - (a.odds ?? -1));
      evs.push({ key: `q${qu.id}`, q: qu, title: qu.title, sub: qu.sub, venue_name: qu.venue_name, rows,
                 expiry_ms: qu.expiry_ms ?? Math.min(...rows.map((r) => r.expiry_ms ?? Infinity)) });
    }
    for (const r of data.markets) {
      if (inQ.has(r.id) || r.kind === "price" || r.kind === "touch" || !open(r) || !inVenue(r)) continue;
      if (kind !== "all" && r.kind !== kind) continue;
      if (term && !r.title.toLowerCase().includes(term)) continue;
      evs.push({ key: `m${r.id}`, q: null, title: r.title, sub: r.sub, venue_name: r.venue_name, rows: [r], expiry_ms: r.expiry_ms });
    }
    const vol = (e) => sumBy(e.rows, (r) => r.volume_24h);
    const move = (e) => Math.max(0, ...e.rows.map((r) => Math.abs(r.chg_24h ?? 0)));
    const edge = (e) => (e.q ? (e.q.sum.locked ?? 0) * 100 : Math.abs(e.rows[0].edge ?? 0));
    const soon = (e) => (e.expiry_ms && e.expiry_ms > now ? e.expiry_ms : Infinity);
    const key = { volume: (e) => -vol(e), closing: soon, move: (e) => -move(e), edge: (e) => -edge(e) }[sort];
    evs.sort((a, b) => key(a) - key(b) || vol(b) - vol(a));
    return { prices, events: evs, nPrice };
  }, [data, venue, kind, sort, q, showClosed]);

  const now = data?.now_ms || Date.now();
  const chip = (on, set, id, label, title) => (
    <button key={id} className={`plan-chip ${on === id ? "on" : ""}`} onClick={() => set(id)} aria-pressed={on === id} title={title}>
      {label}
    </button>
  );

  return (
    <section className="panel scan">
      <div className="panel-head scan-head">
        <h2 className="panel-title">Prediction markets</h2>
        <span className="panel-sub">
          {data?.enabled
            ? [`${data.total} markets on Hyperliquid`, ...data.venues.map((v) => `${v.count} ${v.name}`)].join(" · ")
            : "Outcome, trade.xyz and every other prediction market on Hyperliquid"}
        </span>
      </div>
      {error && <div className="plan-error">{error}</div>}
      {data && !data.enabled ? (
        <div className="empty">Prediction markets are read live, so they only show in live mode.</div>
      ) : !data ? (
        <div className="empty">Loading…</div>
      ) : (
        <>
          <div className="scan-tools oc-tools">
            <div className="oc-filters">
              <div className="plan-holds" role="group" aria-label="Venue">
                {[{ id: "all", name: "All venues" }, ...data.venues].map((v) => chip(venue, setVenue, v.id, v.name))}
              </div>
              <div className="plan-holds" role="group" aria-label="Kind">
                {KINDS.map(([id, label]) => chip(kind, setKind, id, label))}
              </div>
              <div className="plan-holds" role="group" aria-label="Sort">
                {SORTS.map(([id, label, title]) => chip(sort, setSort, id, label, title))}
              </div>
              <button className={`plan-chip ${showClosed ? "on" : ""}`} onClick={() => setShowClosed(!showClosed)}
                aria-pressed={showClosed} title="Markets past their closing time that haven't settled yet">
                Closed
              </button>
            </div>
            <span className="plan-input scan-search">
              <input className="left" placeholder="find a market" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find a market" />
            </span>
          </div>
          <p className="oc-explain muted">
            A contract pays $1 if it happens, so its price is the market's odds. Click any line for its chart, book, trades
            and who is trading.
          </p>
          {data.error && <p className="plan-note" style={{ padding: "0 12px" }}>{data.error}</p>}
          {data.feed && !data.feed.connected && (
            <p className="plan-note" style={{ padding: "0 12px" }}>Live prices are reconnecting to Hyperliquid. Opening a market still reads its book.</p>
          )}

          {prices.length > 0 && (
            <>
              <h3 className="oc-section">Price markets <span className="muted">{nPrice} on {prices.length} assets</span></h3>
              <div className="oc-grid oc-grid-wide">
                {prices.map((g) => <PriceCard key={g.asset} g={g} now={now} sort={sort} onOpen={onOpen} />)}
              </div>
            </>
          )}
          {events.length > 0 && (
            <>
              <h3 className="oc-section">Events <span className="muted">{events.length}</span></h3>
              <div className="oc-grid">
                {events.slice(0, limit).map((ev) => <EventCard key={ev.key} ev={ev} now={now} onOpen={onOpen} onEvent={onEvent} />)}
              </div>
              {events.length > limit && (
                <button className="oc-more oc-more-page" onClick={() => setLimit(limit + PAGE)}>
                  Show {Math.min(PAGE, events.length - limit)} more of {events.length - limit}
                </button>
              )}
            </>
          )}
          {!prices.length && !events.length && <div className="empty">No markets match.</div>}
        </>
      )}
    </section>
  );
}

/** Odds over time, 0 to 100%, with the fair-odds line for price markets. */
function OddsChart({ odds, fair }) {
  const box = useRef(null);
  const [W, setW] = useState(800);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const H = W < 600 ? 180 : 230, L = 38, R = 12, T = 10, B = 22;
  if (!odds || odds.length < 2) return <div ref={box} className="empty">Not enough trading yet to draw the odds.</div>;
  const all = [...odds, ...(fair || [])];
  const t0 = Math.min(...all.map((p) => p[0]));
  const t1 = Math.max(...all.map((p) => p[0]));
  const x = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = (p) => T + (1 - p) * (H - T - B);
  const path = (s) => s.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const long = t1 - t0 > 2 * 86_400_000;
  const tick = (t) =>
    long ? new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" })
      : new Date(t).toLocaleTimeString(undefined, { hour12: false, hour: "2-digit", minute: "2-digit" });
  const last = odds[odds.length - 1];
  return (
    <div ref={box}>
    <svg className="oc-chart" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Odds over time">
      {[0, 0.25, 0.5, 0.75, 1].map((p) => (
        <g key={p}>
          <line x1={L} x2={W - R} y1={y(p)} y2={y(p)} stroke="var(--grid)" />
          <text x={L - 6} y={y(p) + 3} textAnchor="end" fontSize="10" fill="var(--muted)">{p * 100}%</text>
        </g>
      ))}
      {[0, 1, 2, 3].map((i) => {
        const t = t0 + ((t1 - t0) * i) / 3;
        return (
          <text key={i} x={x(t)} y={H - 6} textAnchor={i === 0 ? "start" : i === 3 ? "end" : "middle"} fontSize="10" fill="var(--muted)">
            {tick(t)}
          </text>
        );
      })}
      {fair?.length > 1 && <path d={path(fair)} fill="none" stroke="var(--ink-2)" strokeWidth="1.2" strokeDasharray="4 3" />}
      <path d={path(odds)} fill="none" stroke="var(--up-bright)" strokeWidth="1.6" />
      <circle cx={x(last[0])} cy={y(last[1])} r="3" fill="var(--up-bright)" />
    </svg>
    </div>
  );
}

function Book({ book }) {
  const asks = (book.asks || []).slice(0, 8).reverse();
  const bids = (book.bids || []).slice(0, 8);
  const maxSz = Math.max(1, ...asks.map((l) => l[1]), ...bids.map((l) => l[1]));
  const line = (l, side) => (
    <tr key={`${side}${l[0]}`} className="book-row">
      <td className={side === "bid" ? "up" : "down"}>{cents(l[0])}</td>
      <td>{num(l[1])}</td>
      <td>
        {fmtUsd(l[0] * l[1])}
        <span className={`depth-bar ${side}`} style={{ width: `${(l[1] / maxSz) * 100}%` }} />
      </td>
    </tr>
  );
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Book (Yes)</h2>
        <span className="panel-sub">No is the mirror: 100 minus these</span>
      </div>
      <div className="panel-body">
        {!asks.length && !bids.length ? (
          <div className="empty">Nobody is quoting this market right now.</div>
        ) : (
          <table>
            <thead><tr><th>Price</th><th>Contracts</th><th>Dollars</th></tr></thead>
            <tbody>
              {asks.map((l) => line(l, "ask"))}
              <tr className="spread-row">
                <td colSpan={3}>
                  {bids.length && asks.length ? `spread ${((book.asks[0][0] - book.bids[0][0]) * 100).toFixed(1)} pts` : "one side empty"}
                </td>
              </tr>
              {bids.map((l) => line(l, "bid"))}
            </tbody>
          </table>
        )}
        <div className="oc-push muted">
          To push Yes up 5 points: {book.up5 == null ? "the asks don't go that deep" : `${fmtUsd(book.up5)} of buying`}.
          {" "}Down 5 points: {book.down5 == null ? "the bids don't go that deep" : `${fmtUsd(book.down5)} of selling`}.
        </div>
      </div>
    </div>
  );
}

function Trades({ trades }) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Trades</h2>
        <span className="panel-sub">who crossed the spread, and which side they backed</span>
      </div>
      <div className="panel-body scroll">
        {!trades.length ? (
          <div className="empty">No trades yet.</div>
        ) : (
          <table>
            <thead><tr><th>Time</th><th>Backed</th><th>Yes price</th><th>Contracts</th><th>Wallet</th></tr></thead>
            <tbody>
              {trades.map((t) => {
                const yes = t.side === "B";
                const w = yes ? t.buyer : t.seller;
                return (
                  <tr key={t.tid ?? `${t.t}${t.px}`}>
                    <td className="muted">{fmtTime(t.t)}</td>
                    <td className={yes ? "up" : "down"}>{yes ? "Yes" : "No"}</td>
                    <td>{cents(t.px)}</td>
                    <td>{num(t.sz)}</td>
                    <td className="muted" title={w || ""}>{w ? `${w.slice(0, 6)}…${w.slice(-4)}` : "-"}</td>
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

function Who({ who }) {
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Who is trading</h2>
        <span className="panel-sub">{who.trades ? `last ${who.trades} trades, ${who.count} wallets` : "no trades yet"}</span>
      </div>
      <div className="panel-body">
        {who.notes.map((n) => <p key={n} className="oc-note">{n}</p>)}
        {who.wallets.length > 0 && (
          <table>
            <thead><tr><th>Wallet</th><th title="Yes contracts bought">Yes</th><th title="No contracts bought (Yes sold)">No</th><th>Net</th><th>Share</th></tr></thead>
            <tbody>
              {who.wallets.map((w) => (
                <tr key={w.wallet}>
                  <td title={w.wallet}>{w.short}</td>
                  <td>{num(w.yes)}</td>
                  <td>{num(w.no)}</td>
                  <td className={w.net > 0 ? "up" : w.net < 0 ? "down" : ""}>{w.net > 0 ? "Yes" : w.net < 0 ? "No" : "flat"} {num(Math.abs(w.net))}</td>
                  <td>{(w.share * 100).toFixed(0)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function settles(r) {
  const a = (r.underlying || "").split(":").pop();
  if (r.kind === "price") return `Yes if ${a}'s price on Hyperliquid (${r.underlying} perp) is at or above ${r.threshold} at the close.`;
  if (r.kind === "touch") return `Yes if ${a}'s price on Hyperliquid (${r.underlying} perp) reaches ${r.threshold} before the close.`;
  return null;
}

/* One colour per charted outcome, in this order (checked for colour-blind separation on the dark surface).
   Colour follows the outcome's place in the listing, not its rank, so lines keep their colour as odds move. */
const SERIES = ["#3987e5", "#d95926", "#199e70", "#9085e9", "#d55181"];

const valueAt = (pts, t) => {
  let lo = 0, hi = pts.length - 1, ans = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid][0] <= t) { ans = pts[mid][1]; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
};

/** Several outcomes' odds over time on one chart, with a hover readout. */
function MultiOddsChart({ series, colors }) {
  const box = useRef(null);
  const [W, setW] = useState(800);
  const [hover, setHover] = useState(null);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const drawn = series.filter((s) => s.points.length >= 2);
  if (!drawn.length) return <div ref={box} className="empty">Not enough trading yet to draw the odds.</div>;
  const wide = W >= 640;
  const H = wide ? 260 : 200, L = 38, R = wide ? 150 : 12, T = 10, B = 22;
  const all = drawn.flatMap((s) => s.points);
  const t0 = Math.min(...all.map((p) => p[0]));
  const t1 = Math.max(...all.map((p) => p[0]));
  const top = Math.min(1, Math.max(0.1, Math.ceil((Math.max(...all.map((p) => p[1])) + 0.03) * 10) / 10));
  const step = top <= 0.3 ? 0.05 : top <= 0.6 ? 0.1 : 0.25;
  const grid = [];
  for (let v = 0; v <= top + 1e-9; v += step) grid.push(+v.toFixed(2));
  const x = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = (p) => T + (1 - p / top) * (H - T - B);
  const path = (s) => s.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const long = t1 - t0 > 2 * 86_400_000;
  const tick = (t) =>
    long ? new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" })
      : new Date(t).toLocaleTimeString(undefined, { hour12: false, hour: "2-digit", minute: "2-digit" });

  // labels at the right end of each line, nudged apart so they never overlap
  const ends = drawn.map((s) => ({ s, v: s.points[s.points.length - 1][1] })).sort((a, b) => b.v - a.v);
  let last = -Infinity;
  for (const e of ends) {
    e.ly = Math.max(y(e.v), last + 13);
    last = e.ly;
  }
  const over = ends.length ? ends[ends.length - 1].ly - (H - B) : 0;
  if (over > 0) ends.forEach((e) => { e.ly -= over; });

  const onMove = (ev) => {
    const rect = ev.currentTarget.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    if (px < L || px > W - R) return setHover(null);
    setHover(t0 + ((px - L) / (W - L - R)) * (t1 - t0));
  };
  const readout = hover == null ? null
    : drawn.map((s) => ({ s, v: valueAt(s.points, hover) })).filter((r) => r.v != null).sort((a, b) => b.v - a.v);

  return (
    <div ref={box} className="oc-multi">
      <svg className="oc-chart" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Odds over time"
        onPointerMove={onMove} onPointerDown={onMove} onPointerLeave={() => setHover(null)} style={{ touchAction: "pan-y" }}>
        {grid.map((p) => (
          <g key={p}>
            <line x1={L} x2={W - R} y1={y(p)} y2={y(p)} stroke="var(--grid)" />
            <text x={L - 6} y={y(p) + 3} textAnchor="end" fontSize="10" fill="var(--muted)">{Math.round(p * 100)}%</text>
          </g>
        ))}
        {[0, 1, 2, 3].map((i) => {
          const t = t0 + ((t1 - t0) * i) / 3;
          return (
            <text key={i} x={x(t)} y={H - 6} textAnchor={i === 0 ? "start" : i === 3 ? "end" : "middle"} fontSize="10" fill="var(--muted)">
              {tick(t)}
            </text>
          );
        })}
        {drawn.map((s) => (
          <path key={s.id} d={path(s.points)} fill="none" stroke={colors[s.id]} strokeWidth="2" strokeLinejoin="round" />
        ))}
        {ends.map((e) => (
          <g key={e.s.id}>
            <circle cx={x(e.s.points[e.s.points.length - 1][0])} cy={y(e.v)} r="4" fill={colors[e.s.id]} stroke="var(--surface)" strokeWidth="2" />
            {wide && (
              <text x={W - R + 10} y={e.ly + 3.5} fontSize="10.5" fill="var(--ink-2)">
                {e.s.label.length > 14 ? `${e.s.label.slice(0, 13)}…` : e.s.label} {pct(e.v)}
              </text>
            )}
          </g>
        ))}
        {hover != null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} stroke="var(--axis)" />
            {readout.map((r) => (
              <circle key={r.s.id} cx={x(hover)} cy={y(r.v)} r="4" fill={colors[r.s.id]} stroke="var(--surface)" strokeWidth="2" />
            ))}
          </g>
        )}
      </svg>
      {hover != null && readout.length > 0 && (
        <div className="oc-tip" style={{ left: Math.min(x(hover) + 12, W - 200), top: 8 }}>
          <div className="muted">{new Date(hover).toLocaleString(undefined, { hour12: false, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</div>
          {readout.map((r) => (
            <div key={r.s.id} className="oc-tip-row">
              <span className="oc-swatch" style={{ background: colors[r.s.id] }} />
              <span className="oc-tip-name">{r.s.label}</span>
              <b className="num">{pct(r.v)}</b>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** One event: every outcome, the likeliest five on one chart, and the add-up check. */
function EventPage({ id, onBack, onOpen }) {
  const [rng, setRng] = useState("12h");
  const { data: d, error } = useJson(`/api/outcomes/questions/${id}?range=${rng}`, 4000);
  if (!d) {
    return (
      <section className="panel">
        <div className="extra-bar"><button className="plan-chip" onClick={onBack}>‹ Back</button></div>
        <div className="empty">{error || "Loading…"}</div>
      </section>
    );
  }
  const q = d.question;
  const colors = Object.fromEntries(d.series.map((s, i) => [s.id, SERIES[i % SERIES.length]]));
  const rows = [...d.rows].sort((a, b) => (b.odds ?? -1) - (a.odds ?? -1));
  const sub = [q.sub, q.expiry_ms ? `closes ${until(q.expiry_ms, d.now_ms)}` : null,
    d.volume_24h ? `${fmtUsd(d.volume_24h)} traded today` : "no trades today"].filter(Boolean).join(" · ");
  return (
    <>
      <div className="extra-bar">
        <button className="plan-chip" onClick={onBack}>‹ Back</button>
        <span><b>{q.title}</b></span>
        <VenueTag name={q.venue_name} />
        <span className="muted">{sub}</span>
      </div>
      {error && <div className="plan-error">{error}</div>}
      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Odds</h2>
          <span className="panel-sub">
            {d.series.length > 1 ? `the ${d.series.length} likeliest outcomes` : d.series.length ? "the only priced outcome" : "no prices yet"}
            {" · point at the chart for the odds at any moment"}
          </span>
          <div className="plan-holds">
            {[["12h", "12h"], ["2w", "2 weeks"]].map(([k, label]) => (
              <button key={k} className={`plan-chip ${rng === k ? "on" : ""}`} onClick={() => setRng(k)}>{label}</button>
            ))}
          </div>
        </div>
        <div className="panel-body">
          {d.series.length > 1 && (
            <div className="oc-legend">
              {d.series.map((s) => (
                <button key={s.id} className="oc-legend-item" onClick={() => onOpen(s.id)} title={`Open ${s.label}`}>
                  <span className="oc-swatch" style={{ background: colors[s.id] }} />
                  {s.label} <b className="num">{pct(s.odds)}</b>
                </button>
              ))}
            </div>
          )}
          <MultiOddsChart series={d.series} colors={colors} />
        </div>
      </section>
      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Outcomes</h2>
          <span className="panel-sub">{rows.length} · click one for its book, trades and who is trading</span>
        </div>
        <div className="panel-body">
          <p className={`oc-note ${q.sum.way ? "oc-edge" : ""}`}>{q.sum.note}</p>
          <div className="scan-scroll">
            <table className="scan-table">
              <thead>
                <tr>
                  <th className="left">Outcome</th>
                  <th>Yes</th>
                  <th title="Best bid and ask for Yes">Bid / Ask</th>
                  <th title="Change in the odds over 24 hours, in points">24h</th>
                  <th>Volume 24h</th>
                  <th title="Yes contracts people hold (each pays $1 if it happens)">Held</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} onClick={() => onOpen(r.id)} className={r.odds == null ? "oc-dim" : ""}>
                    <td className="left">
                      <span className="oc-swatch" style={{ background: colors[r.id] || "transparent" }} />
                      {r.label}
                    </td>
                    <td className="num"><b>{pct(r.odds)}</b></td>
                    <td className="num">{bidAsk(r.bid, r.ask)}</td>
                    <td className={`num ${cls(r.chg_24h)}`}>{pts(r.chg_24h)}</td>
                    <td className="num">{fmtUsd(r.volume_24h)}</td>
                    <td className="num">{fmtUsd(r.open)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </>
  );
}

/** One market's page. */
function MarketPage({ id, onBack, onOpen, onEvent }) {
  const [rng, setRng] = useState("12h");
  const { data: d, error } = useJson(`/api/outcomes/${id}?range=${rng}`, 2000);
  if (!d) {
    return (
      <section className="panel">
        <div className="extra-bar"><button className="plan-chip" onClick={onBack}>‹ Back</button></div>
        <div className="empty">{error || "Loading…"}</div>
      </section>
    );
  }
  const r = d.row;
  const how = settles(r);
  return (
    <>
      <div className="extra-bar">
        <button className="plan-chip" onClick={onBack}>‹ Back</button>
        <span><b>{r.title}</b></span>
        <VenueTag name={r.venue_name} />
        <span className="muted">{[r.sub, r.settled ? "settled" : `closes ${until(r.expiry_ms, d.now_ms)}`].filter(Boolean).join(" · ")}</span>
      </div>
      {error && <div className="plan-error">{error}</div>}
      <section className="panel">
        <div className="panel-body">
          <div className="oc-tiles">
            <div className="tile"><div className="tile-label">{r.sides[0]} odds</div><div className="tile-value">{pct(r.odds)}</div>
              <div className="tile-sub">{r.sides[1]} {pct(r.odds == null ? null : 1 - r.odds)}</div></div>
            <div className="tile"><div className="tile-label">Bid / Ask</div><div className="tile-value">{bidAsk(r.bid, r.ask)}</div>
              <div className="tile-sub">spread {r.spread == null ? "-" : `${r.spread.toFixed(1)} pts`}</div></div>
            <div className="tile"><div className="tile-label">24h</div><div className={`tile-value ${cls(r.chg_24h)}`}>{pts(r.chg_24h)}</div>
              <div className="tile-sub">1h {pts(r.chg_1h)}</div></div>
            <div className="tile"><div className="tile-label">Volume 24h</div><div className="tile-value">{fmtUsd(r.volume_24h)}</div>
              <div className="tile-sub">held {fmtUsd(r.open)}</div></div>
            {d.fair && (
              <div className="tile" title={`If ${(r.underlying || "").split(":").pop()} keeps moving like it has over the last 12 hours (about ${d.fair.sigma_day_pct?.toFixed(1)}% a day), with no trend. A simple estimate to compare against, not advice.`}>
                <div className="tile-label">Fair odds</div><div className="tile-value">{pct(d.fair.odds)}</div>
                <div className={`tile-sub ${cls(r.edge)}`}>
                  {r.edge == null ? "" : r.edge >= 0 ? `Yes ${Math.abs(r.edge).toFixed(0)} pts cheap` : `Yes ${Math.abs(r.edge).toFixed(0)} pts rich`}
                  {d.fair.spot ? ` · now ${d.fair.spot.toLocaleString()}` : ""}
                </div>
              </div>
            )}
          </div>
          {d.why && <p className="oc-why">{d.why}</p>}
          {how && <p className="oc-how muted">{how}</p>}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Odds</h2>
          <span className="panel-sub">
            <span className="up">━</span> market{d.chart.fair.length > 1 && <> · <span className="ink2">┅</span> fair odds</>}
          </span>
          <div className="plan-holds">
            {[["12h", "12h"], ["2w", "2 weeks"]].map(([k, label]) => (
              <button key={k} className={`plan-chip ${rng === k ? "on" : ""}`} onClick={() => setRng(k)}>{label}</button>
            ))}
          </div>
        </div>
        <div className="panel-body"><OddsChart odds={d.chart.odds} fair={d.chart.fair} /></div>
      </section>

      <section className="lower">
        <Book book={d.book} />
        <Trades trades={d.trades} />
        <Who who={d.who} />
      </section>

      {d.question && (
        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">{d.question.title}</h2>
            <span className="panel-sub">{d.question.sub}</span>
            <button className="plan-chip" onClick={() => onEvent(d.question.id)}>All outcomes on one chart ›</button>
          </div>
          <div className="panel-body">
            <p className={`oc-note ${d.question.sum.way ? "oc-edge" : ""}`}>{d.question.sum.note}</p>
            <table className="scan-table">
              <thead><tr><th className="left">Outcome</th><th>Yes</th><th>Bid / Ask</th><th>24h</th><th>Volume 24h</th></tr></thead>
              <tbody>
                {d.question.outcomes.map((o) => (
                  <tr key={o.id} onClick={() => onOpen(o.id)} className={o.id === r.id ? "oc-here" : ""}>
                    <td className="left">{o.label}</td>
                    <td className="num"><b>{pct(o.odds)}</b></td>
                    <td className="num">{bidAsk(o.bid, o.ask)}</td>
                    <td className={`num ${cls(o.chg_24h)}`}>{pts(o.chg_24h)}</td>
                    <td className="num">{fmtUsd(o.volume_24h)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}

/** The list, plus whatever was opened from it (an event or a market), with Back returning step by step. The list
    stays mounted underneath, so its filters, expanded cards and scroll position survive a visit. */
export default function Outcomes() {
  const [stack, setStack] = useState([]);
  const listY = useRef(0);
  const top = stack[stack.length - 1];
  const push = (view) => {
    if (!stack.length) listY.current = window.scrollY;
    setStack((s) => [...s, view]);
    window.scrollTo(0, 0);
  };
  const back = () => {
    const toList = stack.length === 1;
    setStack((s) => s.slice(0, -1));
    if (toList) requestAnimationFrame(() => window.scrollTo(0, listY.current));
    else window.scrollTo(0, 0);
  };
  const openMarket = (id) => push({ type: "market", id });
  const openEvent = (id) => push({ type: "event", id });
  return (
    <>
      <div style={{ display: top ? "none" : "contents" }}>
        <MarketList onOpen={openMarket} onEvent={openEvent} />
      </div>
      {top?.type === "event" && <EventPage key={`e${top.id}`} id={top.id} onBack={back} onOpen={openMarket} />}
      {top?.type === "market" && <MarketPage key={`m${top.id}`} id={top.id} onBack={back} onOpen={openMarket} onEvent={openEvent} />}
    </>
  );
}
