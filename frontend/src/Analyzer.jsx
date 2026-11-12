import { useEffect, useState } from "react";
import { Head, Pill } from "./components/TermPanel.jsx";
import { Seg } from "./components/River.jsx";
import { fmtTime, fmtUsd } from "./format.js";

/* The Analyzer (/analyzer, /api/analyzer): where crypto's money is being made right now. The money map ranks every
   category by the fees people are paying in it against its own normal, and splits it by chain; the profit boards show
   who is actually making money where wallets' profit is public. Fees show where the action is and what the venues
   earn; they are not traders' profit, which is why the two are kept apart. */

const EVERY_MS = 60_000;
const x1 = (r) => (r == null ? "-" : `${r.toFixed(2)}×`);
const pct = (v, d = 0) => (v == null ? "-" : `${(v * 100).toFixed(d)}%`);
const tone = (v) => (v == null || Math.abs(v) < 1e-9 ? "" : v > 0 ? "up" : "down");
const short = (a) => (a && a.length > 14 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a || "-");
const ago = (ms) => {
  if (ms == null) return "-";
  const m = Math.max(0, Math.round(ms / 60000));
  return m < 1 ? "now" : m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ${m % 60}m ago`;
};
const FLAG = { hot: ["good", "Heating up"], cooling: ["bad", "Cooling"] };
const flagPill = (f) => (FLAG[f] ? <Pill kind={FLAG[f][0]}>{FLAG[f][1]}</Pill> : null);
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

function MoneyMap({ map, pick, setPick }) {
  const max = Math.max(...map.categories.filter((c) => !c.infra).map((c) => c.d1), 1);
  const firstInfra = map.categories.findIndex((c) => c.infra);
  return (
    <section id="p-MAP" className="panel">
      <Head code="MAP" title="Where the money is"
        meta={[["Fees, 24 h", fmtUsd(map.total.d1)], ["7 days", fmtUsd(map.total.d7)]]}
        pills={[<Pill key="h" kind="good">{map.categories.filter((c) => !c.infra && c.flag === "hot").length} heating up</Pill>,
          <Pill key="c" kind="bad">{map.categories.filter((c) => !c.infra && c.flag === "cooling").length} cooling</Pill>]} />
      <div className="az-scroll"><table className="az-table az-map">
        <thead><tr><th>Category</th><th className="r">Fees, 24 h</th><th /><th className="r" title="The last 24 hours against this category's own daily average over 30 days">vs normal</th><th className="r">7 days</th><th>Biggest right now</th></tr></thead>
        <tbody>
          {map.categories.map((c, i) => (
            <tr key={c.key} className={`${pick === c.key ? "on" : ""} ${c.infra ? "az-infra" : ""} ${i === firstInfra ? "az-first-infra" : ""}`}
              onClick={() => setPick(c.key)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setPick(c.key)}
              title={c.infra ? "Infrastructure: what the rails earn, not a way to make money trading" : undefined}>
              <td><b>{c.name}</b> {c.infra ? null : flagPill(c.flag)}</td>
              <td className="r num">{fmtUsd(c.d1)}</td>
              <td className="az-bar">{!c.infra && <i style={{ width: `${(c.d1 / max) * 100}%` }} />}</td>
              <td className={`r num ${c.ratio == null ? "" : c.ratio >= 1.25 ? "up" : c.ratio <= 0.75 ? "down" : ""}`}>{x1(c.ratio)}</td>
              <td className="r num muted">{fmtUsd(c.d7)}</td>
              <td className="muted az-names">{c.protocols.slice(0, 3).map((p) => p.name).join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      <p className="term-pad plan-note muted">
        Fees are what people pay to take part (trading fees, launch fees, interest, gas), so they show where the action is and
        what the venues earn. They are not traders' profit: that is on the profit boards. "Heating up" is 24 hours at
        1.25× the category's 30-day daily average or more, with the week above it too; "cooling" is 0.75× or less. Stablecoins,
        gas and bridges sit at the bottom as infrastructure and are left out of the totals.
      </p>
    </section>
  );
}

function Detail({ map, pick }) {
  const c = map.categories.find((x) => x.key === pick) || map.categories[0];
  const chains = Object.entries(map.grid[c.key] || {}).sort((a, b) => b[1] - a[1]);
  return (
    <section id="p-CAT" className="panel">
      <Head code="CAT" title={c.name} meta={[["Protocols", c.n], ["vs normal", x1(c.ratio)]]} pills={[flagPill(c.flag)]} />
      <div className="az-scroll"><table className="az-table">
        <thead><tr><th>Protocol</th><th className="r">Fees, 24 h</th><th className="r">vs normal</th><th className="r">7 days</th><th>Chains</th></tr></thead>
        <tbody>
          {c.protocols.map((p) => (
            <tr key={p.name}>
              <td><b>{p.name}</b> {flagPill(p.flag)}</td>
              <td className="r num">{fmtUsd(p.d1)}</td>
              <td className={`r num ${p.ratio == null ? "" : p.ratio >= 1.25 ? "up" : p.ratio <= 0.75 ? "down" : ""}`}>{x1(p.ratio)}</td>
              <td className="r num muted">{fmtUsd(p.d7)}</td>
              <td className="muted">{p.chains.join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      {chains.length > 0 && (
        <p className="term-pad plan-note muted">By chain, 24 h: {chains.map(([n, v]) => `${n} ${fmtUsd(v)}`).join(" · ")}</p>
      )}
    </section>
  );
}

function Grid({ map, pick, setPick }) {
  const cats = map.categories.filter((c) => Object.keys(map.grid[c.key] || {}).length);
  const max = Math.max(...cats.flatMap((c) => Object.values(map.grid[c.key])), 1);
  return (
    <section id="p-GRID" className="panel term-wide">
      <Head code="GRID" title="Category by chain" meta={[["Shows", "fees, 24 h"], ["Chains", map.chains.length]]} />
      <div className="az-scroll">
        <table className="az-table az-grid">
          <thead><tr><th>Category</th>{map.chains.map((c) => <th key={c.name} className="r">{c.name}</th>)}</tr></thead>
          <tbody>
            {cats.map((c) => (
              <tr key={c.key} className={pick === c.key ? "on" : ""} onClick={() => setPick(c.key)}>
                <td><b>{c.name}</b></td>
                {map.chains.map((ch) => {
                  const v = map.grid[c.key][ch.name];
                  return (
                    <td key={ch.name} className="r num az-cell" style={v ? { background: `rgba(76, 196, 204, ${0.06 + 0.5 * Math.sqrt(v / max)})` } : undefined}>
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

function Perps({ hl }) {
  const [win, setWin] = useState("week");
  const w = hl.windows[win];
  const c = w.crowd;
  return (
    <section id="p-PERP" className="panel">
      <Head code="PERP" title="Perps traders: who made money" meta={[["Venue", "Hyperliquid"], ["Traders", c.traders.toLocaleString()]]}
        pills={[<Pill key="p" kind={c.in_profit >= 0.5 ? "good" : "bad"}>{pct(c.in_profit)} in profit</Pill>]}>
        <Seg value={win} set={setWin} opts={[["day", "Day"], ["week", "Week"], ["month", "Month"]]} />
      </Head>
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

function Rows({ rows, chain, extra }) {
  return (
    <div className="az-scroll az-rows">
      <table className="az-table">
        <thead><tr><th className="r">#</th><th>Wallet</th><th className="r">Profit</th>{extra && <><th className="r">Return</th><th className="r">Volume</th><th className="r">Account</th></>}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.wallet}>
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

function Board({ b, labels }) {
  return (
    <section className="panel">
      <Head code="PNL" title={b.title} meta={[["Category", labels[b.category] || b.category], ["Source", b.source]]}
        pills={[<Pill key="t" kind="good">Top {b.rows.length}: {fmtUsd(b.total)}</Pill>]} />
      {b.rows.length ? <Rows rows={b.rows} chain={b.chain} /> : <p className="term-pad plan-note muted">No rows in the latest reply.</p>}
    </section>
  );
}

function Flags({ map }) {
  const hot = map.categories.filter((c) => !c.infra).flatMap((c) => c.protocols.filter((p) => p.flag === "hot" && p.d1 >= 100_000).map((p) => ({ ...p, cat: c.name })))
    .sort((a, b) => b.ratio - a.ratio).slice(0, 12);
  return (
    <section id="p-HOT" className="panel">
      <Head code="HOT" title="Heating up right now" meta={[["Rule", "24 h at 1.25× normal or more"]]} pills={[<Pill key="n">{hot.length} protocols</Pill>]} />
      {hot.length ? (
        <div className="az-scroll"><table className="az-table">
          <thead><tr><th>Protocol</th><th>Category</th><th className="r">Fees, 24 h</th><th className="r">vs normal</th></tr></thead>
          <tbody>
            {hot.map((p) => (
              <tr key={p.cat + p.name}><td><b>{p.name}</b></td><td className="muted">{p.cat}</td><td className="r num">{fmtUsd(p.d1)}</td><td className="r num up">{x1(p.ratio)}</td></tr>
            ))}
          </tbody>
        </table></div>
      ) : <p className="term-pad plan-note muted">Nothing with $100K+ of daily fees is running hot against its own month.</p>}
    </section>
  );
}

function Sources({ status, now }) {
  return (
    <section id="p-SRC" className="panel term-wide">
      <Head code="SRC" title="Sources" />
      <div className="az-src">
        {Object.entries(status).map(([name, s]) => (
          <div key={name}>
            <Pill kind={s.ok ? "good" : s.ok === false && !s.setup ? "bad" : "mute"}>
              {s.ok ? "OK" : s.ok === false ? (s.setup ? "Not set up" : "Down") : "Waiting"}
            </Pill>
            <b>{name}</b>
            <span className="muted" title={s.error || ""}>{s.ok ? ago(now - s.at) : s.error || "first read pending"}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

export default function Analyzer() {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [pick, setPick] = useState(null);
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
  const map = d?.map;
  const sel = pick || map?.categories?.find((c) => !c.infra)?.key;
  return (
    <div className="app term">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">ANALYTIX<span className="cursor" aria-hidden="true" /></span>
          <span className="brand-tag">analyzer · where the money is</span>
        </div>
        <div className="header-right">
          <a className="alerts-open term-link" href="/terminal">‹ Terminal</a>
          <a className="alerts-open term-link" href="/">‹ Dashboard</a>
          <span className="badge" title={error || ""}>
            <span className={`dot ${error ? "critical" : d?.enabled ? "good pulse" : "info"}`} />
            {error ? "Server offline" : d?.enabled ? "Live" : "Replay"}
          </span>
          <span className="clock num" title="Your local time">{fmtTime(now)}</span>
        </div>
      </header>
      {!d && <div className="term-boot" role="status"><div>&gt; {error || "reading the money map"}<span className="cursor" aria-hidden="true" /></div></div>}
      {d && (
        <div className="term-grid">
          {!map && <p className="plan-note muted term-wide">{d.enabled ? "The first read of the fee data is on its way (about a minute after the server starts)." : "The Analyzer reads its sources in live mode only."}</p>}
          {map && <div className="tw-7"><MoneyMap map={map} pick={sel} setPick={setPick} /></div>}
          {map && <div className="tw-5 term-col"><Detail map={map} pick={sel} /><Flags map={map} /></div>}
          {map && <Grid map={map} pick={sel} setPick={setPick} />}
          {d.hyperliquid && <div className={d.boards.length ? "tw-7" : "term-wide"}><Perps hl={d.hyperliquid} /></div>}
          {d.boards.length > 0 && <div className="tw-5 term-col">{d.boards.map((b) => <Board key={b.title} b={b} labels={d.labels} />)}</div>}
          <Sources status={d.status} now={now} />
        </div>
      )}
    </div>
  );
}
