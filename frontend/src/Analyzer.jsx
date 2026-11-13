import { useEffect, useState } from "react";
import { fmtTime, fmtUsd } from "./format.js";

/* The Analyzer (/analyzer, /api/analyzer): where crypto's money is being made right now. The money map ranks every
   category by fees, revenue or trading volume against its own normal and splits it by chain. Everything drills
   down: a category opens its protocols, a protocol its 90-day history and chains, a chain what is driving it (its
   biggest protocols and its busiest pools). The profit boards show who is actually making money where wallets'
   profit is public. Colours: green and red for data, white for everything else. */

const EVERY_MS = 60_000;
const METRIC = {
  fees: ["Fees", "what people pay to take part: trading fees, launch fees, interest, gas"],
  revenue: ["Revenue", "the part of the fees the protocol keeps"],
  volume: ["Volume", "dollars traded on spot, perps and options venues"],
};
const x1 = (r) => (r == null ? "-" : `${r.toFixed(2)}×`);
const pct = (v, d = 0) => (v == null ? "-" : `${(v * 100).toFixed(d)}%`);
const tone = (v) => (v == null || Math.abs(v) < 1e-9 ? "" : v > 0 ? "up" : "down");
const heatTone = (r) => (r == null ? "" : r >= 1.25 ? "up" : r <= 0.75 ? "down" : "");
const short = (a) => (a && a.length > 14 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a || "-");
const ago = (ms) => {
  if (ms == null) return "-";
  const m = Math.max(0, Math.round(ms / 60000));
  return m < 1 ? "now" : m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ${m % 60}m ago`;
};
const Flag = ({ f }) => (f === "hot" ? <span className="az-flag up">▲ heating up</span> : f === "cooling" ? <span className="az-flag down">▼ cooling</span> : null);
const EXPLORER = {
  Hyperliquid: (a) => `https://hypurrscan.io/address/${a}`,
  Solana: (a) => `https://solscan.io/account/${a}`,
  Polygon: (a) => `https://polymarket.com/profile/${a}`,
  Ethereum: (a) => `https://etherscan.io/address/${a}`,
  Base: (a) => `https://basescan.org/address/${a}`,
};
const Wallet = ({ a, name, chain }) => {
  const url = EXPLORER[chain]?.(a);
  const text = name && !name.startsWith("0x") ? name : short(a);
  return url ? <a href={url} target="_blank" rel="noreferrer" title={a}>{text}</a> : <span title={a}>{text}</span>;
};
const Seg = ({ value, set, opts }) => (
  <span className="az-seg" role="group">
    {opts.map(([k, t, tip]) => <button key={k} className={value === k ? "on" : ""} aria-pressed={value === k} onClick={() => set(k)} title={tip}>{t}</button>)}
  </span>
);
/* A panel's top line: a title, plain facts on the right, and whatever control it needs. */
const Top = ({ title, facts = [], children, back }) => (
  <div className="az-top">
    {back}
    <h2>{title}</h2>
    {children}
    <span className="az-facts">{facts.filter(Boolean).map((f, i) => <span key={i}>{f}</span>)}</span>
  </div>
);
const Go = ({ onClick, children, title }) => <button className="az-go" onClick={onClick} title={title}>{children}</button>;

function useFetch(url) {
  const [state, setState] = useState({ url: null, data: null });
  useEffect(() => {
    if (!url) return undefined;
    let live = true;
    fetch(url).then((r) => r.json()).then((j) => live && setState({ url, data: j })).catch(() => live && setState({ url, data: { error: "Can't reach the server" } }));
    return () => {
      live = false;
    };
  }, [url]);
  return state.url === url ? state.data : null;
}

function MoneyMap({ map, metric, setMetric, have, sel, open }) {
  const cats = map.categories;
  const max = Math.max(...cats.filter((c) => !c.infra).map((c) => c.d1), 1);
  const firstInfra = cats.findIndex((c) => c.infra);
  const word = METRIC[metric][0].toLowerCase();
  return (
    <section className="panel">
      <Top title="Where the money is"
        facts={[`24 h ${fmtUsd(map.total.d1)}`, `7 days ${fmtUsd(map.total.d7)}`,
          <span key="h" className="up">{cats.filter((c) => !c.infra && c.flag === "hot").length} heating up</span>,
          <span key="c" className="down">{cats.filter((c) => !c.infra && c.flag === "cooling").length} cooling</span>]}>
        <Seg value={metric} set={setMetric} opts={Object.entries(METRIC).filter(([k]) => have.includes(k)).map(([k, [t, tip]]) => [k, t, tip])} />
      </Top>
      <div className="az-scroll"><table className="az-table az-click">
        <thead><tr><th>Category</th><th className="r">{METRIC[metric][0]}, 24 h</th><th /><th className="r" title="The last 24 hours against this category's own daily average over 30 days">vs normal</th><th className="r">7 days</th><th>Biggest right now</th></tr></thead>
        <tbody>
          {cats.map((c, i) => (
            <tr key={c.key} className={`${sel.cat === c.key ? "on" : ""} ${c.infra ? "az-infra" : ""} ${i === firstInfra ? "az-first-infra" : ""}`}
              onClick={() => open({ cat: c.key })} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && open({ cat: c.key })}
              title={c.infra ? "Infrastructure: what the rails earn, not a way to make money trading" : undefined}>
              <td><b>{c.name}</b>{!c.infra && <Flag f={c.flag} />}</td>
              <td className="r num">{fmtUsd(c.d1)}</td>
              <td className="az-bar">{!c.infra && <i style={{ width: `${(c.d1 / max) * 100}%` }} />}</td>
              <td className={`r num ${heatTone(c.ratio)}`}>{x1(c.ratio)}</td>
              <td className="r num muted">{fmtUsd(c.d7)}</td>
              <td className="muted az-names">{c.protocols.slice(0, 3).map((p) => p.name).join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      <p className="az-note">
        {METRIC[metric][0]}: {METRIC[metric][1]}. It shows where the action is{metric === "volume" ? "" : " and what the venues earn"}; it is not
        traders' profit, which is on the profit boards. "Heating up" is 24 hours of {word} at 1.25× the 30-day daily average or
        more, with the week above it too; "cooling" is 0.75× or less. Click any row to open it.
      </p>
    </section>
  );
}

function Spark({ rows }) {
  if (!rows?.length) return null;
  const W = 520, H = 120, pad = 4;
  const vals = rows.map((r) => r[1]);
  const max = Math.max(...vals, 1);
  const bw = (W - pad * 2) / rows.length;
  const avg = vals.slice(-30).reduce((a, b) => a + b, 0) / Math.min(30, vals.length);
  const y = (v) => H - pad - (v / max) * (H - pad * 2);
  const day = (ms) => new Date(ms).toLocaleDateString([], { day: "numeric", month: "short" });
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="az-spark" role="img" aria-label="Daily history">
      {rows.map((r, i) => (
        <rect key={r[0]} x={pad + i * bw + 0.5} y={y(r[1])} width={Math.max(bw - 1, 1)} height={H - pad - y(r[1])}
          className={i >= rows.length - 1 ? "last" : ""}><title>{`${day(r[0])}: ${fmtUsd(r[1])}`}</title></rect>
      ))}
      <line x1={pad} x2={W - pad} y1={y(avg)} y2={y(avg)} />
    </svg>
  );
}

function Category({ map, sel, open, metric }) {
  const c = map.categories.find((x) => x.key === sel.cat) || map.categories.find((x) => !x.infra) || map.categories[0];
  const chains = Object.entries(map.grid[c.key] || {}).sort((a, b) => b[1] - a[1]);
  return (
    <section className="panel">
      <Top title={c.name} facts={[`${c.n} protocols`, `${x1(c.ratio)} normal`, <Flag key="f" f={c.flag} />]} />
      <div className="az-scroll az-tall"><table className="az-table az-click">
        <thead><tr><th>Protocol</th><th className="r">{METRIC[metric][0]}, 24 h</th><th className="r">vs normal</th><th className="r">7 days</th><th>Chains</th></tr></thead>
        <tbody>
          {c.protocols.map((p) => (
            <tr key={p.name} onClick={() => open({ cat: c.key, protocol: p.name })} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && open({ cat: c.key, protocol: p.name })}>
              <td><b>{p.name}</b><Flag f={p.flag} /></td>
              <td className="r num">{fmtUsd(p.d1)}</td>
              <td className={`r num ${heatTone(p.ratio)}`}>{x1(p.ratio)}</td>
              <td className="r num muted">{fmtUsd(p.d7)}</td>
              <td className="muted">{p.chains.join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      {chains.length > 0 && (
        <p className="az-note">By chain: {chains.map(([n, v]) => <Go key={n} onClick={() => open({ cat: c.key, chain: n })} title={`What's driving ${n}`}>{n} {fmtUsd(v)}</Go>)}</p>
      )}
    </section>
  );
}

function Protocol({ map, sel, open, metric }) {
  const c = map.categories.find((x) => x.key === sel.cat);
  const p = c?.protocols.find((x) => x.name === sel.protocol);
  const hist = useFetch(p?.slug ? `/api/analyzer/protocol?slug=${encodeURIComponent(p.slug)}&metric=${metric}` : null);
  if (!p) return <Category map={map} sel={{ cat: sel.cat }} open={open} metric={metric} />;
  const chains = Object.entries(p.by_chain || {});
  return (
    <section className="panel">
      <Top title={p.name} back={<Go onClick={() => open({ cat: c.key })}>‹ {c.name}</Go>}
        facts={[`${x1(p.ratio)} normal`, <Flag key="f" f={p.flag} />]} />
      <div className="az-kpis">
        <div><span className="muted">{METRIC[metric][0]}, 24 h</span><b className="num">{fmtUsd(p.d1)}</b></div>
        <div><span className="muted">7 days</span><b className="num">{fmtUsd(p.d7)}</b></div>
        <div><span className="muted">30 days</span><b className="num">{fmtUsd(p.d30)}</b></div>
        <div><span className="muted">Daily average, 30 days</span><b className="num">{fmtUsd(p.d30 / 30)}</b></div>
      </div>
      <div className="az-pad">
        <div className="muted az-cap">{METRIC[metric][0]} a day, last 90 days (the line is the 30-day average)</div>
        {!hist && <div className="muted">Loading the history…</div>}
        {hist && hist.rows?.length > 0 && <Spark rows={hist.rows} />}
        {hist && !hist.rows?.length && <div className="muted">No daily history for this one{hist.error ? ` (${hist.error})` : ""}.</div>}
      </div>
      {chains.length > 0 && (
        <table className="az-table az-click">
          <thead><tr><th>On chain</th><th className="r">{METRIC[metric][0]}, 24 h</th><th className="r">Share</th><th /></tr></thead>
          <tbody>
            {chains.map(([n, v]) => (
              <tr key={n} onClick={() => open({ cat: c.key, protocol: p.name, chain: n })} tabIndex={0}>
                <td><b>{n}</b></td><td className="r num">{fmtUsd(v)}</td><td className="r num muted">{pct(v / (p.d1 || 1))}</td>
                <td className="muted">what's driving {n} ›</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Chain({ map, sel, open, metric }) {
  const d = useFetch(`/api/analyzer/chain?name=${encodeURIComponent(sel.chain)}&metric=${metric}`);
  const backTo = sel.protocol ? { cat: sel.cat, protocol: sel.protocol } : { cat: sel.cat };
  const catOf = (k) => map.categories.find((c) => c.key === k);
  const pools = d?.pools;
  return (
    <section className="panel">
      <Top title={`What's driving ${sel.chain}`} back={<Go onClick={() => open(backTo)}>‹ {sel.protocol || catOf(sel.cat)?.name || "Back"}</Go>}
        facts={[d?.total != null ? `${METRIC[metric][0].toLowerCase()}, 24 h ${fmtUsd(d.total)}` : null]} />
      {!d && <p className="az-note">Reading {sel.chain}…</p>}
      {d && (
        <>
          <div className="muted az-cap az-pad">Biggest protocols on {sel.chain}, by {METRIC[metric][0].toLowerCase()}</div>
          {d.protocols.length ? (
            <div className="az-scroll az-mid"><table className="az-table az-click">
              <thead><tr><th>Protocol</th><th>Category</th><th className="r">{METRIC[metric][0]}, 24 h</th><th className="r">vs normal</th></tr></thead>
              <tbody>
                {d.protocols.map((p) => (
                  <tr key={p.name} onClick={() => open({ cat: p.category, protocol: p.name })} tabIndex={0}>
                    <td><b>{p.name}</b></td><td className="muted">{catOf(p.category)?.name || p.category}</td>
                    <td className="r num">{fmtUsd(p.d1)}</td><td className={`r num ${heatTone(p.ratio)}`}>{x1(p.ratio)}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          ) : <p className="az-note">No protocol reports {METRIC[metric][0].toLowerCase()} on {sel.chain}.</p>}
          <div className="muted az-cap az-pad">Busiest pools on {sel.chain}, by 24 h volume (GeckoTerminal)</div>
          {pools?.rows?.length ? (
            <div className="az-scroll az-mid"><table className="az-table">
              <thead><tr><th>Pool</th><th>Venue</th><th className="r">Volume, 24 h</th><th className="r">Price, 24 h</th><th className="r">Trades</th><th className="r">Buyers</th><th className="r">Liquidity</th></tr></thead>
              <tbody>
                {pools.rows.map((p) => (
                  <tr key={p.address || p.name}>
                    <td><a href={`https://www.geckoterminal.com/${pools.network}/pools/${p.address}`} target="_blank" rel="noreferrer">{p.name}</a></td>
                    <td className="muted">{p.dex}</td>
                    <td className="r num">{fmtUsd(p.vol)}</td>
                    <td className={`r num ${tone(p.chg)}`}>{p.chg == null ? "-" : `${p.chg > 0 ? "+" : ""}${p.chg.toFixed(1)}%`}</td>
                    <td className="r num muted">{p.trades.toLocaleString()}</td>
                    <td className="r num muted">{p.buyers ? p.buyers.toLocaleString() : "-"}</td>
                    <td className="r num muted">{fmtUsd(p.liquidity)}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          ) : <p className="az-note">No pool list for {sel.chain}{pools?.error ? ` (${pools.error})` : ""}. Chains without token pools (Bitcoin, Hyperliquid's own order book) have none.</p>}
        </>
      )}
    </section>
  );
}

function Grid({ map, sel, open, metric }) {
  const cats = map.categories.filter((c) => Object.keys(map.grid[c.key] || {}).length);
  const max = Math.max(...cats.filter((c) => !c.infra).flatMap((c) => Object.values(map.grid[c.key])), 1);
  return (
    <section className="panel term-wide">
      <Top title="Category by chain" facts={[`${METRIC[metric][0].toLowerCase()}, 24 h`, "click a chain to see what's driving it"]} />
      <div className="az-scroll">
        <table className="az-table az-grid">
          <thead><tr><th>Category</th>{map.chains.map((c) => <th key={c.name} className="r"><Go onClick={() => open({ cat: sel.cat, chain: c.name })}>{c.name}</Go></th>)}</tr></thead>
          <tbody>
            {cats.map((c) => (
              <tr key={c.key} className={`${sel.cat === c.key ? "on" : ""} ${c.infra ? "az-infra" : ""}`}>
                <td className="az-pick" onClick={() => open({ cat: c.key })}><b>{c.name}</b></td>
                {map.chains.map((ch) => {
                  const v = map.grid[c.key][ch.name];
                  return (
                    <td key={ch.name} className="r num az-cell" onClick={() => v && open({ cat: c.key, chain: ch.name })}
                      style={v && !c.infra ? { background: `rgba(31, 166, 122, ${0.05 + 0.45 * Math.sqrt(Math.min(1, v / max))})`, cursor: "pointer" } : undefined}>
                      {v ? fmtUsd(v) : <span className="muted">·</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
            <tr className="az-total"><td className="muted">All categories</td>{map.chains.map((ch) => <td key={ch.name} className="r num">{fmtUsd(ch.d1)}</td>)}</tr>
          </tbody>
        </table>
      </div>
    </section>
  );
}

const SCREEN_COLS = [
  ["name", "Protocol", "", null],
  ["rev30", "Revenue a day", "30-day average of what the protocol keeps", (r) => fmtUsd(r.rev30 / 30)],
  ["share", "Share of category", "Its share of its category's fees this week", (r) => pct(r.share, 1)],
  ["share_mom", "Share trend", "This week's share against its share over the month: above 1 is gaining", (r) => x1(r.share_mom)],
  ["earn30", "After incentives", "Revenue minus token rewards paid out, 30 days: negative means it pays more than it earns", (r) => fmtUsd(r.earn30)],
  ["holders", "To holders", "The share of revenue that reaches token holders: buybacks, burns, staking payouts", (r) => pct(r.holders)],
  ["take", "Take rate", "Revenue for every $10,000 traded (venues with volume)", (r) => (r.take == null ? "-" : `${(r.take * 1e4).toFixed(1)} bps`)],
  ["yield_tvl", "Revenue / TVL", "Yearly revenue as a share of the money deposited: how hard the capital works", (r) => pct(r.yield_tvl)],
  ["mcap", "Market cap", "", (r) => fmtUsd(r.mcap)],
  ["ps", "Price / sales", "Market cap divided by yearly revenue: lower is cheaper", (r) => (r.ps == null ? "-" : `${r.ps.toFixed(1)}×`)],
  ["ps_vs", "vs category", "Its price/sales against the median of its category: under 1 is cheaper than its peers", (r) => x1(r.ps_vs)],
];
const screenTone = (k, r) => (k === "share_mom" ? (r.share_mom == null ? "" : r.share_mom >= 1.1 ? "up" : r.share_mom <= 0.9 ? "down" : "")
  : k === "earn30" ? tone(r.earn30) : k === "ps_vs" ? (r.ps_vs == null ? "" : r.ps_vs <= 1 ? "up" : r.ps_vs >= 2 ? "down" : "") : "");

function Screener({ screen, days, labels, open }) {
  const [cat, setCat] = useState("all");
  const [only, setOnly] = useState(false);
  const [sort, setSort] = useState(["setup", -1]);
  const cats = [...new Set(screen.rows.map((r) => r.category))];
  const rows = screen.rows.filter((r) => (cat === "all" || r.category === cat) && (!only || r.setup));
  const [key, dir] = sort;
  const val = (r) => (key === "setup" ? (r.setup ? 1 : 0) * 1e6 + (r.share_mom || 0) : key === "name" ? r.name.toLowerCase() : r[key]);
  const sorted = [...rows].sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null) return 1;
    if (y == null) return -1;
    return (x > y ? 1 : x < y ? -1 : 0) * dir;
  });
  const setups = screen.rows.filter((r) => r.setup).length;
  return (
    <section className="panel term-wide">
      <Top title="Screener: is the money real, and is it priced in?"
        facts={[`${screen.rows.length} protocols earning $10K+ a day`, <span key="s" className="up">{setups} setups</span>,
          `${days} day${days === 1 ? "" : "s"} logged`]}>
        <select className="az-select" value={cat} onChange={(e) => setCat(e.target.value)} aria-label="Category">
          <option value="all">All categories</option>
          {cats.map((k) => <option key={k} value={k}>{labels[k] || k}</option>)}
        </select>
        <label className="az-check"><input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} /> Setups only</label>
      </Top>
      <div className="az-scroll az-tall"><table className="az-table az-click az-screen">
        <thead><tr>
          {SCREEN_COLS.map(([k, t, tip], i) => (
            <th key={k} className={i ? "r" : ""} title={tip}><button className={`az-sort ${key === k ? "on" : ""}`} onClick={() => setSort([k, key === k ? -dir : k === "name" || k === "ps" || k === "ps_vs" ? 1 : -1])}>{t}{key === k ? (dir > 0 ? " ↑" : " ↓") : ""}</button></th>
          ))}
        </tr></thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.category + r.name} className={r.setup ? "az-setup" : ""} tabIndex={0}
              onClick={() => { open({ cat: r.category, protocol: r.name }); window.scrollTo({ top: 0, behavior: "smooth" }); }}>
              <td><b>{r.name}</b>{r.symbol && <span className="muted az-sym">{r.symbol}</span>}{r.setup && <span className="az-flag up">● setup</span>}
                <span className="muted az-cat">{labels[r.category] || r.category}</span></td>
              {SCREEN_COLS.slice(1).map(([k, , , f]) => <td key={k} className={`r num ${screenTone(k, r)}`}>{f(r)}</td>)}
            </tr>
          ))}
        </tbody>
      </table></div>
      <p className="az-note">
        A <span className="up">setup</span> is the classic shape, fixed in advance: gaining share of its category (this week's share 10%+ above
        the month's), earnings after incentives not negative, and a price/sales at or below its category's median. It is a list of
        candidates, not a signal: every day's screen is logged so the setups can be scored against the rest once enough days have passed.
        {!screen.priced && " Market caps and TVL couldn't be read, so nothing can be a setup right now."}
        {!screen.has_earnings && " Earnings after incentives aren't served by the free source, so that column is empty and doesn't count against a setup."}
        {" "}A dash means the source doesn't publish that number for the protocol (no token, no TVL, or no volume).
      </p>
    </section>
  );
}

function Capital({ rows, open }) {
  const chg = (v, p) => (v == null ? "-" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtUsd(Math.abs(v))}${p == null ? "" : ` (${p > 0 ? "+" : ""}${(p * 100).toFixed(1)}%)`}`);
  return (
    <section className="panel term-wide">
      <Top title="Where capital is moving, and who keeps the money" facts={["stablecoin supply by chain", "apps against the chain itself"]} />
      <div className="az-scroll"><table className="az-table az-click">
        <thead><tr><th>Chain</th><th className="r">Stablecoins</th><th className="r" title="Dollars of stablecoins that arrived or left in 7 days">7 days</th><th className="r">30 days</th>
          <th className="r" title="What the apps on this chain kept, 24 h">Apps' revenue, 24 h</th><th className="r" title="What the chain itself earned in gas, 24 h">Chain's own, 24 h</th>
          <th className="r" title="Apps' revenue for every dollar the chain earns: above 1, the apps are capturing more than the chain">Apps ÷ chain</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.chain} onClick={() => { open((s) => ({ cat: s.cat, chain: r.chain })); window.scrollTo({ top: 0, behavior: "smooth" }); }} tabIndex={0}>
              <td><b>{r.chain}</b></td>
              <td className="r num">{fmtUsd(r.st_supply)}</td>
              <td className={`r num ${tone(r.st_d7)}`}>{chg(r.st_d7, r.st_d7_pct)}</td>
              <td className={`r num ${tone(r.st_d30)}`}>{chg(r.st_d30, r.st_d30_pct)}</td>
              <td className="r num">{fmtUsd(r.apps)}</td>
              <td className="r num muted">{r.gas ? fmtUsd(r.gas) : "-"}</td>
              <td className="r num">{r.capture == null ? "-" : `${r.capture.toFixed(1)}×`}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      <p className="az-note">Stablecoins arriving on a chain are dry powder: money that came to be used there. Apps ÷ chain shows who is winning the fees: the applications or the chain underneath them.</p>
    </section>
  );
}

function Hot({ map, open, metric }) {
  const hot = map.categories.filter((c) => !c.infra).flatMap((c) => c.protocols.filter((p) => p.flag === "hot" && p.d1 >= 100_000).map((p) => ({ ...p, cat: c })))
    .sort((a, b) => b.ratio - a.ratio).slice(0, 12);
  return (
    <section className="panel">
      <Top title="Heating up right now" facts={[`${hot.length} protocols`, "24 h at 1.25× normal or more"]} />
      {hot.length ? (
        <div className="az-scroll"><table className="az-table az-click">
          <thead><tr><th>Protocol</th><th>Category</th><th className="r">{METRIC[metric][0]}, 24 h</th><th className="r">vs normal</th></tr></thead>
          <tbody>
            {hot.map((p) => (
              <tr key={p.cat.key + p.name} onClick={() => open({ cat: p.cat.key, protocol: p.name })} tabIndex={0}>
                <td><b>{p.name}</b></td><td className="muted">{p.cat.name}</td><td className="r num">{fmtUsd(p.d1)}</td><td className="r num up">{x1(p.ratio)}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ) : <p className="az-note">Nothing with $100K+ a day is running hot against its own month.</p>}
    </section>
  );
}

function Rows({ rows, chain, extra }) {
  return (
    <div className="az-scroll az-tall">
      <table className="az-table">
        <thead><tr><th className="r">#</th><th>Wallet</th><th className="r">Profit</th>{extra && <><th className="r">Return</th><th className="r">Volume</th><th className="r">Account</th></>}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.wallet + i}>
              <td className="r num muted">{i + 1}</td>
              <td><Wallet a={r.wallet} name={r.name} chain={chain} /></td>
              <td className={`r num ${tone(r.pnl)}`}>{fmtUsd(r.pnl)}</td>
              {extra && <><td className={`r num ${tone(r.roi)}`}>{pct(r.roi, 1)}</td><td className="r num muted">{fmtUsd(r.vlm)}</td><td className="r num muted">{fmtUsd(r.value)}</td></>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Perps({ hl }) {
  const [win, setWin] = useState("week");
  const w = hl.windows[win];
  const c = w.crowd;
  return (
    <section className="panel">
      <Top title="Perps traders: who made money"
        facts={["Hyperliquid", `${c.traders.toLocaleString()} traders`, <span key="p" className={c.in_profit >= 0.5 ? "up" : "down"}>{pct(c.in_profit)} in profit</span>]}>
        <Seg value={win} set={setWin} opts={[["day", "Day"], ["week", "Week"], ["month", "Month"]]} />
      </Top>
      <div className="az-kpis">
        <div><span className="muted">Winners made</span><b className="num up">{fmtUsd(c.won)}</b></div>
        <div><span className="muted">Losers lost</span><b className="num down">{fmtUsd(c.lost)}</b></div>
        <div><span className="muted">Everyone, net</span><b className={`num ${tone(c.net)}`}>{fmtUsd(c.net)}</b></div>
        <div><span className="muted">Top 10's share of winnings</span><b className="num">{pct(c.top10_share)}</b></div>
        <div><span className="muted">Volume</span><b className="num">{fmtUsd(c.volume)}</b></div>
      </div>
      <Rows rows={w.top} chain="Hyperliquid" extra />
    </section>
  );
}

function Board({ b, labels }) {
  return (
    <section className="panel">
      <Top title={b.title} facts={[labels[b.category] || b.category, b.source, <span key="t" className="up">top {b.rows.length}: {fmtUsd(b.total)}</span>]} />
      {b.rows.length ? <Rows rows={b.rows} chain={b.chain} /> : <p className="az-note">No rows in the latest reply.</p>}
    </section>
  );
}

function Sources({ status, now }) {
  return (
    <section className="panel term-wide">
      <Top title="Sources" />
      <div className="az-src">
        {Object.entries(status).map(([name, s]) => (
          <div key={name}>
            <i className={`az-dot ${s.ok ? "up" : s.ok === false && !s.setup ? "down" : ""}`} />
            <b>{name}</b>
            <span className="muted" title={s.error || ""}>{s.ok ? ago(now - s.at) : s.ok === false ? (s.setup ? `not set up: ${s.error}` : `down: ${s.error}`) : "first read pending"}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

export default function Analyzer() {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [metric, setMetric] = useState("fees");
  const [sel, setSel] = useState({});                      // {cat, protocol?, chain?}: how far down the page has drilled
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    document.title = "Analytix · Analyzer";
    let live = true;
    let timer;
    const load = async () => {
      try {
        const r = await fetch("/api/analyzer");
        const j = await r.json();
        if (live && r.ok) {
          setD(j);
          setError(null);
        }
      } catch {
        if (live) setError("Can't reach the Analytix server");
      }
      if (live) timer = setTimeout(load, EVERY_MS);
    };
    load();
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      live = false;
      clearTimeout(timer);
      clearInterval(tick);
    };
  }, []);
  const maps = d?.maps && Object.keys(d.maps).length ? d.maps : d?.map ? { fees: d.map } : null;
  const have = maps ? Object.keys(maps) : [];
  const m = have.includes(metric) ? metric : "fees";
  const map = maps?.[m];
  const cur = { ...sel, cat: sel.cat || map?.categories?.find((c) => !c.infra)?.key };
  const side = { map, sel: cur, open: setSel, metric: m };
  return (
    <div className="app term az">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">ANALYTIX<span className="cursor" aria-hidden="true" /></span>
          <span className="brand-tag">analyzer · where the money is</span>
        </div>
        <div className="header-right">
          <a className="alerts-open term-link" href="/terminal">‹ Terminal</a>
          <a className="alerts-open term-link" href="/">‹ Dashboard</a>
          <span className="badge" title={error || ""}>
            <span className={`dot ${error ? "critical" : "good pulse"}`} />
            {error ? "Server offline" : d?.enabled === false ? "Replay" : "Live"}
          </span>
          <span className="clock num" title="Your local time">{fmtTime(now)}</span>
        </div>
      </header>
      {!d && <div className="term-boot" role="status"><div>&gt; {error || "reading the money map"}<span className="cursor" aria-hidden="true" /></div></div>}
      {d && (
        <div className="term-grid">
          {!map && <p className="az-note term-wide">{d.enabled ? "The first read of the data is on its way (about a minute after the server starts)." : "The Analyzer reads its sources in live mode only."}</p>}
          {map && <div className="tw-7"><MoneyMap map={map} metric={m} setMetric={setMetric} have={have} sel={cur} open={setSel} /></div>}
          {map && (
            <div className="tw-5 term-col">
              {cur.chain ? <Chain {...side} /> : cur.protocol ? <Protocol {...side} /> : <Category {...side} />}
              <Hot map={map} open={setSel} metric={m} />
            </div>
          )}
          {map && <Grid map={map} sel={cur} open={setSel} metric={m} />}
          {d.screen?.rows?.length > 0 && <Screener screen={d.screen} days={d.screen_days || 0} labels={d.labels} open={setSel} />}
          {d.capital?.length > 0 && <Capital rows={d.capital} open={setSel} />}
          {d.hyperliquid && <div className={d.boards.length ? "tw-7" : "term-wide"}><Perps hl={d.hyperliquid} /></div>}
          {d.boards.length > 0 && <div className="tw-5 term-col">{d.boards.map((b) => <Board key={b.title} b={b} labels={d.labels} />)}</div>}
          <Sources status={d.status} now={now} />
        </div>
      )}
    </div>
  );
}
