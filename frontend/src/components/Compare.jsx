import { useEffect, useId, useRef, useState } from "react";
import { Head, Pill, age } from "./TermPanel.jsx";

/* FLOW: the edge markets' taker orders as they happen, one lane per market. A bubble is one order (its fills merged):
   size by dollars, green bought from the asks, red sold into the bids, fading with age; the newest ripple. Each lane
   ends with its net flow over the window. "Key" orders: Auto keeps the biggest fifth in the window, or pick a floor.
   The Links view shows which markets moved together, from hourly moves over the week. */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY", GBP: "GBP/USD" };
const ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "JPY", "EUR", "DXY", "GBP"];
const tk = (name) => name.split(":").pop();
const label = (name) => SHORT[tk(name)] || tk(name);
const usd = (x) => {
  const a = Math.abs(x);
  const s = a >= 1e6 ? `$${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `$${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k` : `$${Math.round(a)}`;
  return x < 0 ? `−${s}` : s;
};
const px = (p) => p.toLocaleString(undefined, { maximumFractionDigits: p >= 1000 ? 1 : p >= 10 ? 2 : 4, minimumFractionDigits: p >= 1000 ? 1 : p >= 10 ? 2 : 4 });
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const WINDOWS = [[5, "5m"], [15, "15m"], [60, "60m"]];
const FLOORS = [["auto", "Auto"], [10_000, "$10k+"], [50_000, "$50k+"], [250_000, "$250k+"]];
const POLL_MS = 2000;
// A steady sideways scatter per order, so bubbles at the same moment don't stack exactly
const jitter = (id) => {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return (((h % 1000) + 1000) % 1000) / 1000;              // 0 to 1 (h can be negative)
};

/* The orders, kept in the page: the last hour on open, then whatever is new every 2 seconds. */
function useOrders(enabled) {
  const [orders, setOrders] = useState([]);
  const [info, setInfo] = useState(null);
  const last = useRef(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let live = true;
    const load = async () => {
      try {
        const r = await fetch(`/api/edges/trades${last.current ? `?since=${last.current}` : ""}`);
        const j = await r.json();
        if (!live || !r.ok) return;
        setInfo(j);
        if (j.orders.length) last.current = j.orders.at(-1).t;
        setOrders((old) => {
          const seen = new Set(old.map((o) => o.id));
          const keep = old.filter((o) => j.now_ms - o.t <= j.keep_ms);
          return [...keep, ...j.orders.filter((o) => !seen.has(o.id)).map((o) => ({ ...o, seen_ms: Date.now() }))];
        });
      } catch {
        /* the next poll tries again */
      }
    };
    load();
    const t = setInterval(load, POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [enabled]);
  return { orders, info };
}

function Bubbles({ d, now, W, orders, info }) {
  const clip = useId();
  const [mins, setMins] = useState(15);
  const [floor, setFloor] = useState("auto");
  const [hover, setHover] = useState(null);
  const names = [...d.markets, ...d.fx].map((m) => m.name).sort((a, b) => ORDER.indexOf(tk(a)) - ORDER.indexOf(tk(b)));
  const shut = Object.fromEntries(d.markets.map((m) => [m.name, m.session && !m.session.open && m.session.why !== "break"]));
  const span = mins * 60_000;
  const inWin = orders.filter((o) => now - o.t <= span && o.t <= now + 5000);
  const cut = floor === "auto" ? info?.cut?.[mins] ?? 0 : floor;      // the server sees every order, the page the big ones
  const shown = inWin.filter((o) => o.usd >= cut);
  const max = Math.max(1, ...shown.map((o) => o.usd));
  const top3 = new Set([...shown].sort((a, b) => b.usd - a.usd).slice(0, 3).map((o) => o.id));
  const flow = Object.fromEntries(names.map((n) => [n, info?.flow?.[n]?.[mins] || { net: 0, buy: 0, sell: 0, n: 0 }]));

  const lane = W < 600 ? 30 : 36, L = W < 600 ? 70 : 92, R = W < 600 ? 62 : 86, T = 6, B = 22;
  const H = T + lane * names.length + B;
  const x = (t) => L + (1 - (now - t) / span) * (W - L - R);
  const ticks = [];
  const every = mins <= 5 ? 1 : mins <= 15 ? 3 : 10;
  for (let k = every; k < mins; k += every) ticks.push(k);
  const biggest = [...inWin].sort((a, b) => b.usd - a.usd)[0];
  const h = hover && shown.find((o) => o.id === hover);

  return (
    <>
      <div className="term-comp-legend">
        {WINDOWS.map(([k, t]) => (
          <button key={k} className={`plan-chip ${mins === k ? "on" : ""}`} onClick={() => setMins(k)}>{t}</button>
        ))}
        <span className="news-gap" />
        {FLOORS.map(([k, t]) => (
          <button key={k} className={`plan-chip ${floor === k ? "on" : ""}`} onClick={() => setFloor(k)}
            title={k === "auto" ? "The biggest fifth of orders in the window" : `Orders of ${t.replace("+", "")} or more`}>{t}</button>
        ))}
        <span className="muted term-comp-note">
          <i className="term-dotkey buy" />bought <i className="term-dotkey sell" />sold · size = dollars
          {floor === "auto" && cut ? ` · showing ${usd(cut)}+` : ""}
        </span>
      </div>
      <div className="term-bub-wrap">
        <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label={`Taker orders over the last ${mins} minutes`}>
          {names.map((n, i) => {
            const f = flow[n];
            const y0 = T + i * lane;
            return (
              <g key={n}>
                <rect x={L} y={y0} width={W - L - R} height={lane} className={i % 2 ? "term-bub-lane alt" : "term-bub-lane"} />
                <text x={8} y={y0 + lane / 2 - 2} className="term-bub-name">{label(n)}</text>
                <text x={8} y={y0 + lane / 2 + 10} className={shut[n] ? "term-bub-sub hl" : "term-bub-sub"}>
                  {shut[n] ? "HL only" : `${f.n} orders`}
                </text>
                <text x={W - R + 8} y={y0 + lane / 2 + 4} className={`term-bub-net ${f.net > 0 ? "up" : f.net < 0 ? "down" : ""}`}>
                  {f.n ? `${f.net > 0 ? "+" : ""}${usd(f.net)}` : "-"}
                </text>
              </g>
            );
          })}
          {ticks.map((k) => (
            <g key={k}>
              <line x1={x(now - k * 60_000)} x2={x(now - k * 60_000)} y1={T} y2={H - B} className="term-bub-tick" />
              <text x={x(now - k * 60_000)} y={H - 7} textAnchor="middle" className="term-comp-ax">−{k}m</text>
            </g>
          ))}
          <line x1={x(now)} x2={x(now)} y1={T} y2={H - B} className="term-bub-now" />
          <text x={x(now)} y={H - 7} textAnchor="middle" className="term-comp-ax">now</text>
          <clipPath id={clip}><rect x={L} y={T} width={W - L - R} height={lane * names.length} /></clipPath>
          <g clipPath={`url(#${clip})`}>
          {shown.map((o) => {
            const i = names.indexOf(o.coin);
            if (i < 0) return null;
            const r = Math.min(lane / 2 - 1.5, 2.2 + 12 * Math.sqrt(o.usd / max) * (lane / 36));
            const y0 = T + i * lane;
            const room = lane / 2 - r - 1;                     // how far it can sit off the lane's middle and stay inside
            const cy = y0 + lane / 2 + (jitter(o.id) - 0.5) * 2 * room;
            const age = (now - o.t) / span;
            const fresh = Date.now() - o.seen_ms < 4000;
            return (
              <g key={o.id} onPointerEnter={() => setHover(o.id)} onPointerLeave={() => setHover(null)}>
                {fresh && <circle cx={x(o.t)} cy={cy} r={r} className={`term-bub-ring ${o.side > 0 ? "buy" : "sell"}`} />}
                <circle cx={x(o.t)} cy={cy} r={r} className={`term-bub ${o.side > 0 ? "buy" : "sell"} ${hover === o.id ? "on" : ""}`}
                  style={{ opacity: Math.max(0.28, 0.95 - 0.65 * age) }} />
                {top3.has(o.id) && r > 7 && (
                  <text x={x(o.t) - r - 4} y={cy + 3} textAnchor="end" className="term-bub-label">{usd(o.usd)}</text>
                )}
              </g>
            );
          })}
          </g>
        </svg>
        {h && (
          <div className="term-comp-tip" style={{ left: `${(x(h.t) / W) * 100}%`, top: 8, transform: `translateX(${x(h.t) > W * 0.6 ? "-104%" : "6%"})` }}>
            <div className="muted">{clock(h.t)}</div>
            <div><b>{label(h.coin)}</b> · <span className={h.side > 0 ? "up" : "down"}>{h.side > 0 ? "bought" : "sold"} {usd(h.usd)}</span></div>
            <div className="muted">at {px(h.px)} · {h.fills} fill{h.fills > 1 ? "s" : ""}</div>
          </div>
        )}
      </div>
      <p className="plan-note muted term-pad term-bub-foot">
        {!info?.enabled ? "Live trades come in live mode only." :
          !orders.length ? "Waiting for the first trades…" :
          <>
            Last {mins} min: {Object.values(flow).reduce((a, f) => a + f.n, 0)} orders of $1k+
            {biggest && <> · biggest <b className={biggest.side > 0 ? "up" : "down"}>{label(biggest.coin)} {biggest.side > 0 ? "buy" : "sell"} {usd(biggest.usd)}</b> at {clock(biggest.t)}</>}
            {" "}· net flow = bought minus sold, by market. Fills from one order in one block count as one order.
          </>}
      </p>
    </>
  );
}

/* Draws at the box's real width (one unit, one pixel), so text and bubbles keep their size on any screen. */
function Measured({ children }) {
  const box = useRef(null);
  const [w, setW] = useState(760);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(300, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return <div className="term-comp-chart" ref={box}>{children(w)}</div>;
}

function Links({ c }) {
  const names = c.corr.names;
  const m = c.corr.m;
  const pairs = [];
  names.forEach((a, i) => names.forEach((b, j) => { if (j > i && m[i][j] != null) pairs.push([a, b, m[i][j]]); }));
  const top = pairs.sort((p, q) => Math.abs(q[2]) - Math.abs(p[2])).slice(0, 3);
  const cell = (r) => (r == null ? undefined : r > 0 ? `rgba(31, 166, 122, ${0.08 + 0.6 * r})` : `rgba(229, 83, 95, ${0.08 + 0.55 * -r})`);
  return (
    <div className="term-pad">
      <div className="term-corr" style={{ gridTemplateColumns: `72px repeat(${names.length}, minmax(0, 1fr))` }}>
        <span />
        {names.map((n) => <span key={n} className="term-corr-h">{label(n)}</span>)}
        {names.map((a, i) => [
          <span key={`${a}-h`} className="term-corr-h left">{label(a)}</span>,
          ...names.map((b, j) => (
            <span key={`${a}-${b}`} className={`term-corr-c ${i === j ? "self" : ""}`} style={{ background: i === j ? undefined : cell(m[i][j]) }}
              title={i === j ? label(a) : `${label(a)} and ${label(b)}: ${m[i][j] == null ? "not enough shared hours" : m[i][j]}`}>
              {i === j ? "" : m[i][j] == null ? "-" : m[i][j].toFixed(2)}
            </span>
          )),
        ])}
      </div>
      {top.length > 0 && (
        <p className="plan-note">
          Strongest links this week:{" "}
          {top.map(([a, b, r], k) => (
            <span key={`${a}${b}`}>{k ? " · " : ""}<b>{label(a)}</b> {r > 0 ? "with" : "against"} <b>{label(b)}</b> <span className={r > 0 ? "up" : "down"}>{r.toFixed(2)}</span></span>
          ))}
        </p>
      )}
      <p className="plan-note muted">
        Each cell compares two markets' hourly moves over the last week: 1 moves exactly together, −1 exactly opposite,
        0 no link. USD/JPY rising means the yen falling.
      </p>
    </div>
  );
}

export default function Compare({ d, now }) {
  const [view, setView] = useState("flow");
  const { orders, info } = useOrders(Boolean(d));
  if (!d) return null;
  const last = info?.last_ms;
  const live = last != null && now - last < 60_000;
  return (
    <section id="p-COMP" className="panel term-wide">
      <Head code="FLOW" title={view === "flow" ? "Live trades" : "Links"}
        meta={view === "flow"
          ? [["Feed", info?.source || "starting"], ["Last trade", last ? age(now - last) : "-"], ["Kept", "1 hour"]]
          : [["Data", "hourly moves"], ["Range", "7 days"]]}
        pills={[view === "flow" ? (live ? <Pill key="l" kind="live">Live</Pill> : <Pill key="l">{info?.enabled === false ? "Live mode only" : "Waiting"}</Pill>) : null]}>
        <span className="tp-tabs" role="tablist">
          {[["flow", "Live trades"], ["links", "Links"]].map(([k, t]) => (
            <button key={k} role="tab" aria-selected={view === k} className={view === k ? "on" : ""} onClick={() => setView(k)}>{t}</button>
          ))}
        </span>
      </Head>
      {view === "flow" ? (
        <Measured>{(w) => <Bubbles d={d} now={now} W={w} orders={orders} info={info} />}</Measured>
      ) : d.compare && Object.keys(d.compare.series).length ? (
        <Links c={d.compare} />
      ) : (
        <p className="plan-note muted term-pad">Links need a few days of hourly history.</p>
      )}
    </section>
  );
}
