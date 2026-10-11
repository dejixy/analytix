import { useEffect, useMemo, useRef, useState } from "react";
import { fmtPct, fmtPrice, fmtUsd } from "../format.js";

const FILTERS = [
  ["all", "All"],
  ["move", "Price moves"],
  ["volume", "Volume"],
  ["positions", "Open interest"],
  ["funding", "Funding"],
  ["index", "vs index"],
  ["trades", "Big orders & liquidations"],
];
const SORTS = {
  score: (r) => r.score,
  chg_1h: (r) => Math.abs(r.chg_1h ?? 0),
  chg_24h: (r) => Math.abs(r.chg_24h ?? 0),
  volume_24h: (r) => r.volume_24h,
  oi: (r) => Math.abs(r.oi_chg_1h ?? 0),
  funding: (r) => Math.abs(r.funding_apr),
};

/** Up to three reasons: the filtered kind first (strongest first), then the rest; "thin trading" always last. */
function reasonsFor(r, filter) {
  const notes = r.notes.filter((n) => n.kind !== "thin");
  const first = filter === "all" ? [] : notes.filter((n) => n.kind === filter);
  const picked = [...first, ...notes.filter((n) => !first.includes(n))].slice(0, 3).map((n) => n.text);
  const thin = r.notes.find((n) => n.kind === "thin");
  return thin ? [...picked, thin.text] : picked;
}

const ago = (ms) => {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  return s < 90 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
};
const cls = (x) => (x == null || Math.abs(x) < 0.005 ? "" : x > 0 ? "up" : "down");

function Th({ id, sort, setSort, children, title }) {
  return (
    <th className={`sortable ${sort === id ? "on" : ""}`} onClick={() => setSort(id)} title={title}>
      {children}{sort === id ? " ▾" : ""}
    </th>
  );
}

/** More coins: every Hyperliquid perp, most unusual first. Click a coin to open its dashboard. */
export default function Scanner({ onOpen }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("score");
  const [q, setQ] = useState("");
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    const load = async () => {
      try {
        const r = await fetch("/api/scanner");
        const d = await r.json();
        if (!alive.current) return;
        setData(d);
        setError(r.ok ? null : "Couldn't load the scanner.");
      } catch {
        if (alive.current) setError("Couldn't reach the backend.");
      }
    };
    load();
    const t = setInterval(load, 10_000);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, []);

  const rows = useMemo(() => {
    if (!data?.rows) return [];
    const term = q.trim().toUpperCase();
    const out = data.rows.filter((r) => (filter === "all" || r.flags.includes(filter)) && (!term || r.coin.includes(term)));
    // the default order: overall score, or with a filter on, the strongest reading of that kind
    const key = sort !== "score" ? SORTS[sort] : filter === "all" ? SORTS.score : (r) => r.kind_scores?.[filter] ?? 0;
    out.sort((a, b) => key(b) - key(a));
    return out;
  }, [data, filter, sort, q]);

  return (
    <section className="panel scan">
      <div className="panel-head scan-head">
        <h2 className="panel-title">More coins</h2>
        <span className="panel-sub">
          {data?.enabled
            ? <>
                {data.coins} Hyperliquid coins, most unusual first · trade details for the {data.followed} most traded
                {data.learned < data.coins && ` · learning each coin's normal: ${data.learned} of ${data.coins}`}
                {data.updated_ms && ` · updated ${ago(data.updated_ms)}`}
              </>
            : "every Hyperliquid coin, ranked by what's unusual right now"}
        </span>
      </div>
      {error && <div className="plan-error">{error}</div>}
      {data && !data.enabled ? (
        <div className="empty">The scanner reads every Hyperliquid coin live, so it only runs in live mode.</div>
      ) : !data ? (
        <div className="empty">Loading…</div>
      ) : (
        <>
          <div className="scan-tools">
            <div className="plan-holds" role="group" aria-label="Show">
              {FILTERS.map(([id, label]) => (
                <button key={id} className={`plan-chip ${filter === id ? "on" : ""}`} onClick={() => setFilter(id)}
                  aria-pressed={filter === id}>
                  {label}
                </button>
              ))}
            </div>
            <span className="plan-input scan-search">
              <input className="left" placeholder="find a coin" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find a coin" />
            </span>
          </div>
          {data.error && <p className="plan-note" style={{ padding: "0 12px" }}>{data.error}</p>}
          <div className="scan-scroll">
            <table className="scan-table">
              <thead>
                <tr>
                  <th className="left">Coin</th>
                  <th>Price</th>
                  <th>15m</th>
                  <Th id="chg_1h" sort={sort} setSort={setSort}>1h</Th>
                  <Th id="chg_24h" sort={sort} setSort={setSort}>24h</Th>
                  <Th id="volume_24h" sort={sort} setSort={setSort} title="Traded in the last 24 hours, and how that compares with the coin's average day">Volume 24h</Th>
                  <Th id="oi" sort={sort} setSort={setSort} title="The total size of open positions, and its change over the last hour">Open interest</Th>
                  <Th id="funding" sort={sort} setSort={setSort} title="Funding as a yearly rate: positive means longs pay shorts">Funding / yr</Th>
                  <th title="The perp's price compared with the spot index from other exchanges">vs index</th>
                  <Th id="score" sort={sort} setSort={setSort}>
                    <span className="left-label">What's unusual</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.coin} onClick={() => onOpen(r.coin)}
                    title={r.main ? `Open the ${r.coin} tab` : `Open ${r.coin}'s dashboard`}>
                    <td className="left">
                      <b>{r.coin}</b>
                      {r.main && <span className="scan-tag">tab</span>}
                      {!r.main && r.open && <span className="scan-tag">open</span>}
                    </td>
                    <td className="num">{fmtPrice(r.price)}</td>
                    <td className={`num ${cls(r.chg_15m)}`}>{fmtPct(r.chg_15m)}</td>
                    <td className={`num ${cls(r.chg_1h)}`}>{fmtPct(r.chg_1h)}</td>
                    <td className={`num ${cls(r.chg_24h)}`}>{fmtPct(r.chg_24h)}</td>
                    <td className="num">
                      {fmtUsd(r.volume_24h)}
                      {r.volume_x != null && <span className="sub">{r.volume_x.toFixed(1)}× usual</span>}
                    </td>
                    <td className="num">
                      {fmtUsd(r.oi_usd)}
                      {r.oi_chg_1h != null && <span className={`sub ${cls(r.oi_chg_1h)}`}>{fmtPct(r.oi_chg_1h, 1)} 1h</span>}
                    </td>
                    <td className="num">{fmtPct(r.funding_apr, 1)}</td>
                    <td className="num">{r.premium_pct == null ? "-" : fmtPct(r.premium_pct, 2)}</td>
                    <td className="left scan-why">
                      {r.notes?.length
                        ? reasonsFor(r, filter).map((x) => <span key={x} className="scan-reason">{x}</span>)
                        : <span className="muted">{r.learning ? "learning its normal" : "nothing unusual"}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 && <div className="empty">No coins match.</div>}
          </div>
        </>
      )}
    </section>
  );
}
