import { useEffect, useRef, useState } from "react";
import { Head, Pill, age } from "./TermPanel.jsx";
import { Bubbles, Links, Measured } from "./Compare.jsx";
import { HeatBody } from "./Heat.jsx";

/* BOOK: the order book over time for one market, the way a liquidity heatmap reads. Time runs left to right, price up
   the side; every resting order level is a cell, brighter the more dollars wait there, so walls show as bright bands
   and pulled orders as bands that stop. The best bid (green) and best ask (red) run through it, taker orders land on
   top as bubbles (green bought, red sold), and the right edge is the book right now. Hover any cell or bubble.
   The panel's other tabs: every market's trades as bubble lanes, how the markets moved together, and their hours. */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY", GBP: "GBP/USD" };
const ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "JPY", "EUR", "DXY", "GBP"];
const tk = (n) => n.split(":").pop();
const label = (n) => SHORT[tk(n)] || tk(n);
const usd = (x) => {
  const a = Math.abs(x);
  return a >= 1e6 ? `$${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `$${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k` : `$${Math.round(a)}`;
};
const fmtPx = (p, ref) => p.toLocaleString(undefined, { maximumFractionDigits: ref >= 1000 ? 1 : ref >= 10 ? 2 : 4, minimumFractionDigits: ref >= 1000 ? 1 : ref >= 10 ? 2 : 4 });
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const POLL_MS = 2500;
const NEAR_BPS = 10;                                       // "near the price" for the depth figures
const WINDOWS = [[10, "10m"], [20, "20m"], [45, "45m"]];
// thin → wall: deep teal to cyan to near white (direction stays green/red; size isn't direction)
const STOPS = [[0, [7, 20, 18]], [0.25, [9, 48, 48]], [0.5, [18, 104, 106]], [0.72, [62, 178, 184]], [0.88, [138, 222, 222]], [1, [214, 248, 244]]];
function heat(k) {
  const x = Math.max(0, Math.min(1, k));
  for (let i = 1; i < STOPS.length; i++) {
    if (x <= STOPS[i][0]) {
      const [a, ca] = STOPS[i - 1];
      const [b, cb] = STOPS[i];
      const f = (x - a) / (b - a);
      return `rgb(${ca.map((v, j) => Math.round(v + (cb[j] - v) * f)).join(",")})`;
    }
  }
  return "rgb(214,248,244)";
}
function niceTicks(lo, hi, n = 5) {
  const raw = (hi - lo) / n;
  const p = 10 ** Math.floor(Math.log10(raw || 1));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

/* Each book level arrives as [price, dollars resting]. The selected market's book, best bid/offer and orders: the window on open, then what's new every 2.5 s (after
   each reply, never on top of it). */
function useBook(coin, minutes) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!coin) return undefined;
    let live = true;
    let timer;
    let since = null;
    setData(null);
    const load = async () => {
      try {
        const q = new URLSearchParams({ coin, minutes: String(minutes) });
        if (since) q.set("since", String(since));
        const r = await fetch(`/api/edges/book?${q}`);
        const j = await r.json();
        if (live && r.ok) {
          const keep = minutes * 60_000;
          const mergeBy = (key) => (a = [], b = []) => {
            const seen = new Set(a.map(key));
            return [...a, ...b.filter((x) => !seen.has(key(x)))].sort((p, q2) => p[0] - q2[0]).filter((x) => j.now_ms - x[0] <= keep);
          };
          const byT = mergeBy((x) => x[0]);
          setData((old) => ({
            now_ms: j.now_ms, every_ms: j.every_ms, enabled: j.enabled, markets: j.markets, band: j.band,
            cols: byT(old?.cols, j.cols), bbo: byT(old?.bbo, j.bbo), trades: mergeBy((x) => `${x[0]}:${x[1]}:${x[2]}`)(old?.trades, j.trades),
          }));
          since = j.now_ms - 15_000;                      // overlap a little: late arrivals aren't lost (repeats dropped)
        }
      } catch {
        /* the next poll tries again */
      }
      if (live) timer = setTimeout(load, POLL_MS);
    };
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [coin, minutes]);
  return data;
}

/* Every column's cells as [low edge, high edge, dollars, side]. A grouped book's bid level covers [price, price + band)
   and its ask level (price − band, price] (bids round down, asks up), so the band both touch is one cell, summed. */
function cellsOf(b, a, band, tick) {
  if (!band) {
    const h = (Number.isFinite(tick) ? tick : 0) / 2;
    return [...b.map(([p, z]) => [p - h, p + h, z, "bid"]), ...a.map(([p, z]) => [p - h, p + h, z, "ask"])];
  }
  const m = new Map();
  const put = (k, z, side) => {
    const c = m.get(k);
    if (c) { c[2] += z; if (c[3] !== side) c[3] = "both"; } else m.set(k, [k * band, (k + 1) * band, z, side]);
  };
  for (const [p, z] of b) put(Math.round(p / band), z, "bid");
  for (const [p, z] of a) put(Math.round(p / band) - 1, z, "ask");
  return [...m.values()];
}

function view(data, minutes, W, H, zoom) {
  const AX = 66, PROF = W < 640 ? 0 : 120, B = 18;
  const plotW = W - AX - PROF, plotH = H - B;
  const t1 = data.now_ms, t0 = t1 - minutes * 60_000;
  let tick = Infinity;
  for (const [, b, a] of data.cols.slice(-5)) {
    for (const side of [b, a]) for (let i = 1; i < side.length; i++) tick = Math.min(tick, Math.abs(side[i][0] - side[i - 1][0]) || Infinity);
  }
  const band = data.band || null;
  const cols = data.cols.map(([t, b, a]) => [t, cellsOf(b, a, band, tick)]);
  const sizes = [];
  for (const [, cs] of cols) for (const c of cs) sizes.push(c[2]);
  const q = (arr, p) => {
    const s = [...arr].sort((x, y) => x - y);
    return s[Math.max(0, Math.min(s.length - 1, Math.floor(s.length * p)))];
  };
  // Centred on the price now. "Near": tall enough for the window's price path with room around it (at least a few
  // bands each side). "Book": the whole 20 levels a side. History that drifted further scrolls off the edges.
  const [, lb, la] = data.cols.at(-1);
  const lastQ = data.bbo.at(-1);
  const midNow = lastQ && lastQ[1] != null && lastQ[2] != null ? (lastQ[1] + lastQ[2]) / 2
    : lb.length && la.length ? (lb[0][0] + la[0][0]) / 2 : null;
  const reach = Math.max(la.length ? la.at(-1)[0] - midNow : 0, lb.length ? midNow - lb.at(-1)[0] : 0) || midNow * 0.002;
  let path = 0;
  for (const [t, bb, aa] of data.bbo) {
    if (t < t0) continue;
    if (bb != null) path = Math.max(path, midNow - bb);
    if (aa != null) path = Math.max(path, aa - midNow);
  }
  const step = band || (Number.isFinite(tick) ? tick : midNow * 1e-4);
  const half = zoom === "book" ? reach * 1.08 : Math.min(reach * 1.08, Math.max(path * 1.5, step * 6, midNow * 0.0012));
  const lo = midNow - half, hi = midNow + half;
  const lo20 = Math.log(q(sizes, 0.2) || 1), hi99 = Math.log(q(sizes, 0.99) || 2);
  const shade = (u) => Math.pow(Math.max(0, Math.min(1, (Math.log(Math.max(u, 1)) - lo20) / (hi99 - lo20 || 1))), 1.6);
  const X = (t) => ((t - t0) / (t1 - t0)) * plotW;
  const Y = (p) => ((hi - p) / (hi - lo)) * plotH;
  return { AX, PROF, B, plotW, plotH, t0, t1, lo, hi, shade, X, Y, cols, midNow };
}

function BookMap({ data, minutes, W, coin, zoom }) {
  const canvas = useRef(null);
  const [hover, setHover] = useState(null);
  const H = W < 640 ? 260 : 340;
  const ready = data && data.cols.length >= 2;
  const v = ready ? view(data, minutes, W, H, zoom) : null;
  const ref = data?.cols.at(-1)?.[1]?.[0]?.[0] || data?.cols.at(-1)?.[2]?.[0]?.[0] || 1;

  useEffect(() => {
    const el = canvas.current;
    if (!el || !v) return;
    const dpr = window.devicePixelRatio || 1;
    el.width = Math.round(W * dpr);
    el.height = Math.round(H * dpr);
    const g = el.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.fillStyle = "#030706";
    g.fillRect(0, 0, v.plotW, v.plotH);
    // the book over time (kept inside the plot: a band half off the edge doesn't spill onto the axes)
    g.save();
    g.beginPath(); g.rect(0, 0, v.plotW, v.plotH); g.clip();
    const cols = v.cols;
    for (let i = 0; i < cols.length; i++) {
      const [t, cs] = cols[i];
      if (t < v.t0) continue;
      const x0 = v.X(t);
      const x1 = i + 1 < cols.length ? v.X(cols[i + 1][0]) : Math.min(v.plotW, v.X(t + (data.every_ms || 3000) * 2));
      const w = Math.max(1, x1 - x0 + 0.6);
      for (const [p0, p1, sz] of cs) {
        const yTop = v.Y(p1), yBot = v.Y(p0);
        if (yBot < 0 || yTop > v.plotH) continue;
        g.fillStyle = heat(v.shade(sz));
        g.fillRect(x0, yTop, w, Math.max(1.2, yBot - yTop - (yBot - yTop > 6 ? 1 : 0)));
      }
    }
    g.restore();
    // grid and price scale
    g.font = "10px 'JetBrains Mono', monospace";
    g.textBaseline = "middle";
    const tagY = v.Y(v.midNow);
    for (const p of niceTicks(v.lo, v.hi, 6)) {
      const y = v.Y(p);
      g.strokeStyle = "rgba(160, 200, 190, 0.06)";
      g.beginPath(); g.moveTo(0, y); g.lineTo(v.plotW, y); g.stroke();
      if (Math.abs(y - tagY) < 16) continue;
      g.fillStyle = "#5a6a64";
      g.fillText(fmtPx(p, ref), v.plotW + 6, y);
    }
    const stepMin = minutes <= 10 ? 2 : minutes <= 20 ? 5 : 10;
    g.textBaseline = "alphabetic";
    for (let m = stepMin; m < minutes; m += stepMin) {
      const x = v.X(v.t1 - m * 60_000);
      g.strokeStyle = "rgba(160, 200, 190, 0.06)";
      g.beginPath(); g.moveTo(x, 0); g.lineTo(x, v.plotH); g.stroke();
      g.fillStyle = "#5a6a64";
      g.fillText(`−${m}m`, x - 12, H - 4);
    }
    // best bid and best ask
    const bbo = data.bbo.length ? data.bbo : data.cols.map(([t, b, a]) => [t, b[0]?.[0], a[0]?.[0]]);
    for (const [k, color] of [[1, "#2fc493"], [2, "#e0545e"]]) {
      g.strokeStyle = color;
      g.lineWidth = 1.3;
      g.beginPath();
      let started = false, lastY = null;
      for (const row of bbo) {
        if (row[k] == null || row[0] < v.t0) continue;
        const x = v.X(row[0]), y = v.Y(row[k]);
        if (!started) { g.moveTo(x, y); started = true; } else { g.lineTo(x, lastY); g.lineTo(x, y); }
        lastY = y;
      }
      if (started) g.lineTo(v.plotW, lastY);
      g.stroke();
    }
    g.lineWidth = 1;
    // taker orders
    const maxU = Math.max(1, ...data.trades.map((x) => x[2]));
    for (const [t, side, u, px] of data.trades) {
      if (t < v.t0) continue;
      const r = 1.6 + 9 * Math.sqrt(u / maxU);
      g.beginPath();
      g.arc(v.X(t), v.Y(px), r, 0, Math.PI * 2);
      g.fillStyle = side > 0 ? "rgba(47, 196, 147, 0.5)" : "rgba(224, 84, 94, 0.5)";
      g.strokeStyle = side > 0 ? "#2fc493" : "#e0545e";
      g.fill(); g.stroke();
    }
    // the last price
    const last = bbo.at(-1);
    const mid = last && last[1] != null && last[2] != null ? (last[1] + last[2]) / 2 : null;
    if (mid != null) {
      const y = v.Y(mid);
      g.setLineDash([3, 3]);
      g.strokeStyle = "rgba(228, 236, 232, 0.5)";
      g.beginPath(); g.moveTo(0, y); g.lineTo(v.plotW, y); g.stroke();
      g.setLineDash([]);
      g.fillStyle = "#1b2723";
      g.fillRect(v.plotW + 1, y - 8, v.AX - 2, 16);
      g.fillStyle = "#eaf5f1";
      g.textBaseline = "middle";
      g.fillText(fmtPx(mid, ref), v.plotW + 6, y);
    }
    // the book right now
    if (v.PROF) {
      const cs = cols.at(-1)[1];
      const x0 = W - v.PROF + 6;
      const room = v.PROF - 12;
      const top = Math.max(1, ...cs.map((c) => c[2]));
      g.fillStyle = "#060a09";
      g.fillRect(W - v.PROF, 0, v.PROF, v.plotH);
      g.save();
      g.beginPath(); g.rect(W - v.PROF, 0, v.PROF, v.plotH); g.clip();
      const tone = { bid: "rgba(47, 196, 147, 0.7)", ask: "rgba(224, 84, 94, 0.7)", both: "rgba(160, 180, 175, 0.6)" };
      for (const [p0, p1, z, side] of cs) {
        const yTop = v.Y(p1), yBot = v.Y(p0);
        if (yBot < 0 || yTop > v.plotH) continue;
        g.fillStyle = tone[side];
        g.fillRect(x0, yTop + 0.5, room * (z / top), Math.max(1.2, yBot - yTop - 1));
      }
      g.restore();
      g.fillStyle = "#5a6a64";
      g.textBaseline = "alphabetic";
      g.fillText("book now", x0, H - 4);
    }
  });

  if (!ready) {
    return (
      <div className="bk-empty muted" style={{ height: H }}>
        {data && !data.enabled ? "The book is read in live mode only." : `Collecting ${label(coin)}'s book… (a snapshot every few seconds)`}
      </div>
    );
  }
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    if (mx > v.plotW || my > v.plotH) return setHover(null);
    const t = v.t0 + (mx / v.plotW) * (v.t1 - v.t0);
    const p = v.hi - (my / v.plotH) * (v.hi - v.lo);
    let col = null;
    for (const c of v.cols) if (c[0] <= t) col = c;
    let level = null;
    if (col) {
      for (const [p0, p1, z, side] of col[1]) if (p >= p0 && p <= p1) level = { side, p0, p1, usd: z };
    }
    let trade = null;
    for (const tr of data.trades) {
      if (Math.hypot(v.X(tr[0]) - mx, v.Y(tr[3]) - my) < 8) trade = tr;
    }
    setHover({ mx, my, t, p, level, trade });
  };
  return (
    <div className="bk-stage" style={{ height: H }} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
      <canvas ref={canvas} style={{ width: W, height: H }} />
      {hover && (
        <>
          <i className="bk-cross-x" style={{ left: hover.mx, height: v.plotH }} />
          <i className="bk-cross-y" style={{ top: hover.my, width: v.plotW }} />
          <div className="bk-tip" style={{ left: hover.mx, top: hover.my, transform: `translate(${hover.mx > v.plotW * 0.6 ? "-105%" : "12px"}, ${hover.my > v.plotH * 0.6 ? "-105%" : "12px"})` }}>
            <div className="muted">{clock(hover.t)} · {fmtPx(hover.p, ref)}</div>
            {hover.trade ? (
              <div><span className={hover.trade[1] > 0 ? "up" : "down"}>{hover.trade[1] > 0 ? "Bought" : "Sold"} {usd(hover.trade[2])}</span> at {fmtPx(hover.trade[3], ref)}</div>
            ) : hover.level ? (
              <div>{usd(hover.level.usd)} resting {hover.level.side === "both" ? "(bids and asks)" : `on the ${hover.level.side}`} at {data.band ? `${fmtPx(hover.level.p0, ref)}–${fmtPx(hover.level.p1, ref)}` : fmtPx((hover.level.p0 + hover.level.p1) / 2, ref)}</div>
            ) : <div className="muted">No resting orders here</div>}
          </div>
        </>
      )}
    </div>
  );
}

function stats(data) {
  const col = data?.cols.at(-1);
  if (!col) return null;
  const [, b, a] = col;
  if (!b.length || !a.length) return null;
  const q = data.bbo.at(-1);
  const exact = q && q[1] != null && q[2] != null;
  const mid = exact ? (q[1] + q[2]) / 2 : (b[0][0] + a[0][0]) / 2;
  const near = (rows) => rows.filter(([p]) => Math.abs(p / mid - 1) * 1e4 <= NEAR_BPS).reduce((s2, [, z]) => s2 + z, 0);
  const bidN = near(b), askN = near(a);
  const walls = [...b.map(([p, z]) => ["bid", p, z]), ...a.map(([p, z]) => ["ask", p, z])].sort((x, y) => y[2] - x[2]);
  return {
    mid, spread: exact ? ((q[2] - q[1]) / mid) * 1e4 : null, bidN, askN,
    imb: bidN + askN ? (bidN - askN) / (bidN + askN) : 0, wall: walls[0],
    bidAll: b.reduce((s2, [, z]) => s2 + z, 0), askAll: a.reduce((s2, [, z]) => s2 + z, 0),
  };
}

export default function Depth({ d, now, orders, info }) {
  const [tab, setTab] = useState("book");
  const names = [...d.markets, ...d.fx].map((m) => m.name).sort((a, b) => ORDER.indexOf(tk(a)) - ORDER.indexOf(tk(b)));
  const [coin, setCoin] = useState(names[0]);
  const [minutes, setMinutes] = useState(20);
  const [zoom, setZoom] = useState("near");
  const data = useBook(tab === "book" ? coin : null, minutes);
  useEffect(() => {
    const onView = (e) => { if (e.detail?.view) setTab(e.detail.view); };
    window.addEventListener("term-view", onView);
    return () => window.removeEventListener("term-view", onView);
  }, []);
  const s = stats(data);
  const lastCol = data?.cols.at(-1)?.[0];
  const ref = s?.mid || 1;
  const live = lastCol != null && now - lastCol < 30_000;
  const titles = { book: "Liquidity", trades: "Live trades", links: "Links", hours: "When markets move" };
  const meta = tab === "book"
    ? [["Market", label(coin)], ["Levels", data?.band ? `20 a side, every ${fmtPx(data.band, data.band)}` : "20 a side"],
      ["Updated", lastCol ? age(now - lastCol) : "-"]]
    : tab === "trades" ? [["Feed", info?.source || "starting"], ["Last trade", info?.last_ms ? age(now - info.last_ms) : "-"]]
    : tab === "links" ? [["Data", "hourly moves"], ["Range", "7 days"]] : [["Data", `${d.activity?.days ?? 30} days`], ["Hours", "yours"]];
  return (
    <section id="p-BOOK" className="panel term-wide">
      <Head code="BOOK" title={titles[tab]} meta={meta}
        pills={[tab === "book" ? (live ? <Pill key="l" kind="live">Live</Pill> : <Pill key="l">{data?.enabled === false ? "Live mode only" : "Waiting"}</Pill>) : null]}>
        <span className="tp-tabs" role="tablist">
          {[["book", "Liquidity"], ["trades", "All trades"], ["links", "Links"], ["hours", "Hours"]].map(([k, t]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{t}</button>
          ))}
        </span>
      </Head>
      {tab === "book" && (
        <>
          <div className="term-comp-legend bk-bar">
            {names.map((n) => (
              <button key={n} className={`plan-chip ${coin === n ? "on" : ""}`} onClick={() => setCoin(n)}>{label(n)}</button>
            ))}
            <span className="news-gap" />
            {[["near", "Near price"], ["book", "Whole book"]].map(([k, t]) => (
              <button key={k} className={`plan-chip ${zoom === k ? "on" : ""}`} onClick={() => setZoom(k)}
                title={k === "near" ? "The price path and the levels around it" : "All 20 levels a side"}>{t}</button>
            ))}
            <span className="bk-sep" />
            {WINDOWS.map(([k, t]) => (
              <button key={k} className={`plan-chip ${minutes === k ? "on" : ""}`} onClick={() => setMinutes(k)}>{t}</button>
            ))}
            <span className="muted term-comp-note bk-key">
              <i className="bk-ramp" />thin to wall <i className="bk-line bid" />best bid <i className="bk-line ask" />best ask
              <i className="term-dotkey buy" />bought <i className="term-dotkey sell" />sold
            </span>
          </div>
          <Measured>{(w) => <BookMap data={data} minutes={minutes} W={w} coin={coin} zoom={zoom} />}</Measured>
          <div className="bk-stats">
            <div title="From the exact best bid and offer"><dt>Spread</dt><dd className="num">{s?.spread != null ? `${s.spread.toFixed(1)} bps` : "-"}</dd></div>
            <div><dt>Within ±10 bps</dt><dd className="num"><span className="up">{s ? usd(s.bidN) : "-"}</span> / <span className="down">{s ? usd(s.askN) : "-"}</span></dd></div>
            <div title="Bids minus asks within ±10 bps, over both"><dt>Lean</dt><dd className={`num ${s?.imb > 0.1 ? "up" : s?.imb < -0.1 ? "down" : ""}`}>{s ? `${s.imb > 0 ? "+" : ""}${Math.round(s.imb * 100)}% ${s.imb > 0.1 ? "bids" : s.imb < -0.1 ? "asks" : "even"}` : "-"}</dd></div>
            <div><dt>Biggest wall</dt><dd className="num">{s?.wall ? <><span className={s.wall[0] === "bid" ? "up" : "down"}>{usd(s.wall[2])} {s.wall[0]}</span> at {fmtPx(s.wall[1], ref)} <span className="muted">({((s.wall[1] / s.mid - 1) * 1e4).toFixed(0)} bps)</span></> : "-"}</dd></div>
            <div><dt>All 20 levels</dt><dd className="num"><span className="up">{s ? usd(s.bidAll) : "-"}</span> / <span className="down">{s ? usd(s.askAll) : "-"}</span></dd></div>
          </div>
        </>
      )}
      {tab === "trades" && <Measured>{(w) => <Bubbles d={d} now={now} W={w} orders={orders} info={info} />}</Measured>}
      {tab === "links" && (d.compare && Object.keys(d.compare.series).length ? <Links c={d.compare} /> : <p className="plan-note muted term-pad">Links need a few days of hourly history.</p>)}
      {tab === "hours" && <HeatBody d={d} now={now} />}
    </section>
  );
}
