import { useEffect, useMemo, useRef, useState } from "react";
import { fmtTime, fmtUsd } from "../format.js";

/* Prediction markets on Hyperliquid (HIP-4): every market from Outcome, trade.xyz and anyone else listing on
   Hyperliquid's books. A contract pays $1 if it happens, so a price of 0.62 reads as 62%. */

const KINDS = [
  ["all", "All"],
  ["price", "Price"],
  ["sports", "Sports"],
  ["other", "Other"],
];
const SORTS = [
  ["volume", "Most traded"],
  ["closing", "Closing soon"],
  ["edge", "Gap to fair odds"],
  ["move", "Biggest 24h move"],
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

/** The short read on one market for the list. */
function marketNote(r) {
  if (r.settled) return "settled";
  if (r.edge != null && Math.abs(r.edge) >= 5) {
    return r.edge > 0
      ? `Yes looks cheap: fair odds ${pct(r.fair)}`
      : `Yes looks rich: fair odds ${pct(r.fair)}`;
  }
  if (r.bid == null && r.ask == null) return "no orders yet";
  if (r.spread != null && r.spread > 10) return "wide spread";
  if (!r.volume_24h) return "no trades today";
  return null;
}

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

/** The list: every market, grouped by question, filterable by venue and kind. */
function MarketList({ onOpen }) {
  const { data, error } = useJson("/api/outcomes", 5000);
  const [venue, setVenue] = useState("all");
  const [kind, setKind] = useState("all");
  const [sort, setSort] = useState("volume");
  const [q, setQ] = useState("");

  const groups = useMemo(() => {
    if (!data?.markets) return [];
    const term = q.trim().toLowerCase();
    const keep = (r) =>
      (venue === "all" || r.venue === venue) && (kind === "all" || r.kind === kind) &&
      (!term || r.title.toLowerCase().includes(term));
    const byId = Object.fromEntries(data.markets.map((r) => [r.id, r]));
    const out = [];
    const inQuestion = new Set();
    for (const qu of data.questions) {
      const rows = qu.outcomes.map((id) => byId[id]).filter(Boolean);
      rows.forEach((r) => inQuestion.add(r.id));
      if (rows.some(keep) || (term && qu.title.toLowerCase().includes(term) && rows.length)) out.push({ q: qu, rows });
    }
    for (const r of data.markets) if (!inQuestion.has(r.id) && keep(r)) out.push({ q: null, rows: [r] });
    const now = data.now_ms;
    const vol = (g) => g.rows.reduce((s, r) => s + (r.volume_24h || 0), 0);
    const close = (g) => {
      const e = Math.min(...g.rows.map((r) => r.expiry_ms || Infinity));
      return e < now ? Infinity : e;
    };
    const edge = (g) => Math.max(g.q?.sum?.locked ? g.q.sum.locked * 100 : 0, ...g.rows.map((r) => Math.abs(r.edge ?? 0)));
    const move = (g) => Math.max(...g.rows.map((r) => Math.abs(r.chg_24h ?? 0)));
    const key = { volume: (g) => -vol(g), closing: close, edge: (g) => -edge(g), move: (g) => -move(g) }[sort];
    out.sort((a, b) => key(a) - key(b));
    for (const g of out) if (g.q) g.rows.sort((a, b) => (b.odds ?? -1) - (a.odds ?? -1));
    return out;
  }, [data, venue, kind, sort, q]);

  const now = data?.now_ms || Date.now();
  const row = (r, inQ) => {
    const note = marketNote(r);
    return (
      <tr key={r.id} className={inQ ? "oc-child" : ""} onClick={() => onOpen(r.id)} title="Open this market">
        <td className="left oc-name">
          {inQ ? r.label : <b>{r.title}</b>}
          {!inQ && <VenueTag name={r.venue_name} />}
          {!r.followed && !r.settled && <span className="scan-tag" title="Past the live limit: the price refreshes when you open it">not live</span>}
        </td>
        <td className="num"><b>{pct(r.odds)}</b></td>
        <td className="num">{cents(r.bid)} / {cents(r.ask)}</td>
        <td className={`num ${cls(r.chg_24h)}`}>{pts(r.chg_24h)}</td>
        <td className="num">{fmtUsd(r.volume_24h)}</td>
        <td className="num">{fmtUsd(r.open)}</td>
        <td className="num">{inQ ? "" : until(r.expiry_ms, now)}</td>
        <td className="num">
          {r.fair == null ? "-" : pct(r.fair)}
          {r.edge != null && <span className={`sub ${cls(r.edge)}`}>{pts(r.edge, 0)}</span>}
        </td>
        <td className="left scan-why">{note && <span className="scan-reason">{note}</span>}</td>
      </tr>
    );
  };

  return (
    <section className="panel scan">
      <div className="panel-head scan-head">
        <h2 className="panel-title">Prediction markets</h2>
        <span className="panel-sub">
          {data?.enabled
            ? <>
                {data.total} markets on Hyperliquid
                {data.venues?.length > 0 && ` (${data.venues.map((v) => `${v.count} ${v.name}`).join(", ")})`}
                {" · "}a contract pays $1 if it happens, so its price is the market's odds
              </>
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
          <div className="scan-tools">
            <div className="oc-filters">
              <div className="plan-holds" role="group" aria-label="Venue">
                {[{ id: "all", name: "All" }, ...data.venues].map((v) => (
                  <button key={v.id} className={`plan-chip ${venue === v.id ? "on" : ""}`} onClick={() => setVenue(v.id)}
                    aria-pressed={venue === v.id}>
                    {v.name}
                  </button>
                ))}
              </div>
              <div className="plan-holds" role="group" aria-label="Kind">
                {KINDS.map(([id, label]) => (
                  <button key={id} className={`plan-chip ${kind === id ? "on" : ""}`} onClick={() => setKind(id)}
                    aria-pressed={kind === id}>
                    {label}
                  </button>
                ))}
              </div>
              <div className="plan-holds" role="group" aria-label="Sort">
                {SORTS.map(([id, label]) => (
                  <button key={id} className={`plan-chip ${sort === id ? "on" : ""}`} onClick={() => setSort(id)}
                    aria-pressed={sort === id}>
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <span className="plan-input scan-search">
              <input className="left" placeholder="find a market" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find a market" />
            </span>
          </div>
          {data.error && <p className="plan-note" style={{ padding: "0 12px" }}>{data.error}</p>}
          <div className="scan-scroll">
            <table className="scan-table">
              <thead>
                <tr>
                  <th className="left">Market</th>
                  <th title="The market's odds for Yes: the middle of the best bid and ask, or the last trade when the spread is wide">Yes</th>
                  <th title="Best bid and ask for Yes, in cents. No is the mirror: No's ask is 100 minus Yes's bid">Bid / Ask</th>
                  <th title="Change in the odds over 24 hours, in percentage points">24h</th>
                  <th title="Contracts traded in the last 24 hours. Each pays $1, so this is also the dollars both sides put in">Volume 24h</th>
                  <th title="Yes contracts people hold right now (each pays $1 if Yes)">Held</th>
                  <th>Closes</th>
                  <th title="Price markets only: the chance the price ends past the line, from the perp's live price and how much it has been moving. The gap is fair odds minus the market's odds">Fair odds</th>
                  <th className="left">Notes</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) =>
                  g.q ? (
                    [
                      <tr key={`q${g.q.id}`} className="oc-q">
                        <td className="left" colSpan={6}>
                          <b>{g.q.title}</b>
                          <VenueTag name={g.q.venue_name} />
                          {g.q.sub && <span className="sub">{g.q.sub}</span>}
                        </td>
                        <td className="num">{until(g.q.expiry_ms, now)}</td>
                        <td />
                        <td className="left scan-why">
                          <span className={`scan-reason ${g.q.sum.way ? "oc-edge" : ""}`} title="Exactly one outcome ends Yes, so the Yes odds should add up to 100%">
                            {g.q.sum.note}
                          </span>
                        </td>
                      </tr>,
                      ...g.rows.map((r) => row(r, true)),
                    ]
                  ) : (
                    row(g.rows[0], false)
                  ),
                )}
              </tbody>
            </table>
            {groups.length === 0 && <div className="empty">No markets match.</div>}
          </div>
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

/** One market's page. */
function MarketPage({ id, onBack, onOpen }) {
  const [rng, setRng] = useState("12h");
  const { data: d, error } = useJson(`/api/outcomes/${id}?range=${rng}`, 2000);
  if (!d) {
    return (
      <section className="panel">
        <div className="extra-bar"><button className="plan-chip" onClick={onBack}>‹ All markets</button></div>
        <div className="empty">{error || "Loading…"}</div>
      </section>
    );
  }
  const r = d.row;
  const how = settles(r);
  return (
    <>
      <div className="extra-bar">
        <button className="plan-chip" onClick={onBack}>‹ All markets</button>
        <span><b>{r.title}</b></span>
        <VenueTag name={r.venue_name} />
        <span className="muted">{r.settled ? "settled" : `closes ${until(r.expiry_ms, d.now_ms)}`}</span>
      </div>
      {error && <div className="plan-error">{error}</div>}
      <section className="panel">
        <div className="panel-body">
          <div className="oc-tiles">
            <div className="tile"><div className="tile-label">{r.sides[0]} odds</div><div className="tile-value">{pct(r.odds)}</div>
              <div className="tile-sub">{r.sides[1]} {pct(r.odds == null ? null : 1 - r.odds)}</div></div>
            <div className="tile"><div className="tile-label">Bid / Ask</div><div className="tile-value">{cents(r.bid)} / {cents(r.ask)}</div>
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
                    <td className="num">{cents(o.bid)} / {cents(o.ask)}</td>
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

export default function Outcomes() {
  const [open, setOpen] = useState(null);
  return open == null ? <MarketList onOpen={setOpen} /> : <MarketPage id={open} onBack={() => setOpen(null)} onOpen={setOpen} />;
}
