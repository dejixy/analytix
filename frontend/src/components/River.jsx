import { useEffect, useMemo, useState } from "react";
import { Head, Pill, age } from "./TermPanel.jsx";
import { Links, Measured } from "./Compare.jsx";
import { HeatBody } from "./Heat.jsx";

/* FLOW: the flow river. Time runs left to right; above the centre line, the dollars takers bought each minute, below
   it the dollars they sold, every market a band of its own (stacked out from the centre in a fixed order), so a
   market's band swelling is money arriving there. "vs normal" redraws each band against that market's usual pace
   for the weekday and hour, which is what turns a busy minute into a trigger. Scheduled events sit on the river as
   markers, so the flow around an EIA report or a reopen reads at a glance. The list beside it: each market's pace over
   the last 15 minutes and its net flow in the window. The panel's other tabs: how the markets moved together, and
   their hours. */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY", GBP: "GBP/USD" };
const ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "JPY", "EUR", "DXY", "GBP"];
const tk = (n) => n.split(":").pop();
const label = (n) => SHORT[tk(n)] || tk(n);
const usd = (x) => {
  const a = Math.abs(x);
  return a >= 1e6 ? `$${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M` : a >= 1e3 ? `$${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k` : `$${Math.round(a)}`;
};
const signed = (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${usd(x)}`;
const hhmm = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const WINDOWS = [[60, "1h"], [240, "4h"], [720, "12h"]];
// bands from the centre out: brightest at the core, darker toward the banks (green bought, red sold)
const BUY = ["#3ad6a2", "#2cb689", "#239870", "#1c7c5c", "#16644a", "#114f3b", "#0d3d2e", "#0a3025"];
const SELL = ["#ee6b75", "#d05661", "#b14750", "#933b43", "#772f36", "#5f262b", "#4a1e22", "#3a181b"];
const STEP_NAME = { 60000: "1 min", 300000: "5 min", 900000: "15 min" };

/* d3's curveBasis: smooth, never overshoots, and stacked edges smoothed the same way never cross. */
function basis(pts, start) {
  let d = "", x0 = NaN, y0 = NaN, x1 = NaN, y1 = NaN, k = 0;
  const f = (v) => v.toFixed(1);
  const bez = (x, y) => {
    d += `C${f((2 * x0 + x1) / 3)},${f((2 * y0 + y1) / 3)},${f((x0 + 2 * x1) / 3)},${f((y0 + 2 * y1) / 3)},${f((x0 + 4 * x1 + x) / 6)},${f((y0 + 4 * y1 + y) / 6)}`;
  };
  for (const [x, y] of pts) {
    if (k === 0) { d += `${start}${f(x)},${f(y)}`; k = 1; }
    else if (k === 1) k = 2;
    else {
      if (k === 2) { d += `L${f((5 * x0 + x1) / 6)},${f((5 * y0 + y1) / 6)}`; k = 3; }
      bez(x, y);
    }
    x0 = x1; x1 = x; y0 = y1; y1 = y;
  }
  if (k === 3) { bez(x1, y1); d += `L${f(x1)},${f(y1)}`; } else if (k === 2) d += `L${f(x1)},${f(y1)}`;
  return d;
}
const band = (lo, hi) => basis(hi, "M") + basis([...lo].reverse(), "L") + "Z";

function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw || 1));
  return [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw) || raw;
}

/* The river's data, one request at a time (the next waits for this reply). */
function useFlow(minutes, on) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!on) return undefined;
    let live = true;
    let timer;
    const every = minutes <= 60 ? 5000 : 15000;
    const load = async () => {
      try {
        const r = await fetch(`/api/edges/flow?minutes=${minutes}`);
        const j = await r.json();
        if (live && r.ok) setData(j);
      } catch {
        /* the next poll tries again */
      }
      if (live) timer = setTimeout(load, every);
    };
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [minutes, on]);
  return data;
}

/* Each market's bought and sold per point, in dollars a minute or as a multiple of its normal pace. */
function series(data, mode) {
  const mk = [...data.markets].sort((a, b) => ORDER.indexOf(tk(a.coin)) - ORDER.indexOf(tk(b.coin)));
  return mk.map((m, i) => {
    let up = m.b, dn = m.s;
    if (mode === "pace") {
      const seen = m.b.map((b, k) => b + m.s[k]).filter((x) => x > 0).sort((a, b) => a - b);
      const fallback = seen.length ? seen[Math.floor(seen.length / 2)] : 1;            // no candles yet: the window's median
      const n = m.norm.map((x) => x || fallback);
      up = m.b.map((b, k) => b / n[k]);
      dn = m.s.map((s, k) => s / n[k]);
    }
    return { ...m, i, up, dn };
  });
}

function River({ data, mode, W, hi, setHi }) {
  const [hover, setHover] = useState(null);
  const H = W < 640 ? 240 : 320;
  const PADT = 22, PADB = 20, PADL = 4, PADR = W < 640 ? 40 : 58;
  const plotW = W - PADL - PADR, plotH = H - PADT - PADB;
  const mid = PADT + plotH / 2;
  const t0 = data.from_ms, t1 = data.now_ms;
  const X = (ms) => PADL + ((ms - t0) / (t1 - t0)) * plotW;
  // from the first step the recordings cover in full (a half-covered step would read as a quiet one)
  const k0 = Math.max(0, data.data_from_ms == null ? 0 : data.t.findIndex((ms, k) => k === data.t.length - 1 || ms - data.step_ms / 2 >= data.data_from_ms));
  const mk = useMemo(() => series(data, mode).map((m) => ({ ...m, b: m.b.slice(k0), s: m.s.slice(k0), norm: m.norm.slice(k0), up: m.up.slice(k0), dn: m.dn.slice(k0) })), [data, mode, k0]);
  const t = data.t.slice(k0);
  const n = t.length;
  const totUp = t.map((_, k) => mk.reduce((a, m) => a + m.up[k], 0));
  const totDn = t.map((_, k) => mk.reduce((a, m) => a + m.dn[k], 0));
  // the scale: the busiest point, unless one spike would flatten everything else (then it's clipped and marked)
  const peaks = [...totUp, ...totDn].filter((x) => x > 0).sort((a, b) => a - b);
  const top = peaks.at(-1) || 1;
  const q90 = peaks[Math.floor(peaks.length * 0.9)] || top;
  const ymax = top > 3.2 * q90 ? 3.2 * q90 : top;
  const S = (v) => (Math.min(v, ymax) / ymax) * (plotH / 2 - 2);
  const xs = t.map((ms) => X(ms));

  const layers = [];
  const cumU = new Array(n).fill(0), cumD = new Array(n).fill(0);
  for (const m of mk) {
    const loU = cumU.map((c, k) => [xs[k], mid - S(c)]);
    for (let k = 0; k < n; k++) cumU[k] += m.up[k];
    const hiU = cumU.map((c, k) => [xs[k], mid - S(c)]);
    const loD = cumD.map((c, k) => [xs[k], mid + S(c)]);
    for (let k = 0; k < n; k++) cumD[k] += m.dn[k];
    const hiD = cumD.map((c, k) => [xs[k], mid + S(c)]);
    // where the band is thickest, for its name
    let best = -1, bk = 0, side = 1;
    for (let k = 1; k < n - 1; k++) {
      const a = loU[k][1] - hiU[k][1], b = hiD[k][1] - loD[k][1];
      if (xs[k] < PADL + 26 || xs[k] > PADL + plotW - 40) continue;
      if (a > best) { best = a; bk = k; side = 1; }
      if (b > best) { best = b; bk = k; side = -1; }
    }
    const tag = best >= 13 ? { x: xs[bk], y: side > 0 ? (loU[bk][1] + hiU[bk][1]) / 2 : (loD[bk][1] + hiD[bk][1]) / 2,
      v: side > 0 ? m.up[bk] : m.dn[bk] } : null;
    const w = (label(m.coin).length + (mode === "pace" ? 5 : 0)) * 6.5;
    const clash = tag && layers.some((l) => l.tag && Math.abs(l.tag.x - tag.x) < (l.tag.w + w) / 2 + 6 && Math.abs(l.tag.y - tag.y) < 12);
    layers.push({ m, up: band(loU, hiU), dn: band(loD, hiD), tag: tag && !clash ? { ...tag, w } : null });
  }
  const edgeU = basis(cumU.map((c, k) => [xs[k], mid - S(c)]), "M");
  const edgeD = basis(cumD.map((c, k) => [xs[k], mid + S(c)]), "M");
  const net = mode === "usd" ? basis(t.map((_, k) => {
    const v = totUp[k] - totDn[k];
    return [xs[k], mid - Math.sign(v) * S(Math.abs(v))];
  }), "M") : null;
  const clipped = t.map((_, k) => [totUp[k] > ymax, totDn[k] > ymax]);

  // gridlines and labels
  const yStep = niceStep(ymax / 3);
  const yTicks = [];
  for (let v = yStep; v <= ymax * 1.001; v += yStep) yTicks.push(v);
  const span = t1 - t0;
  const tStep = span <= 90 * 60_000 ? 10 * 60_000 : span <= 6 * 3600_000 ? 30 * 60_000 : 2 * 3600_000;
  const off = new Date().getTimezoneOffset() * 60_000;         // ticks on round local times
  const tTicks = [];
  for (let ms = Math.ceil((t0 - off) / tStep) * tStep + off; ms < t1; ms += tStep) {
    if (X(ms) > PADL + 18 && X(ms) < PADL + plotW - 34) tTicks.push(ms);
  }
  const gap = k0 > 0 || (data.data_from_ms != null && data.data_from_ms > t0 + 60_000) ? t[0] : null;
  const evs = [];
  let lastX = -1e9;
  for (const e of data.events || []) {
    const x = X(e.t_ms);
    evs.push({ ...e, x, show: x - lastX > 96 && x < PADL + plotW - 40 });
    if (x - lastX > 96) lastX = x;
  }

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - r.left;
    if (mx < PADL || mx > PADL + plotW) return setHover(null);
    let k = 0;
    for (let j = 1; j < n; j++) if (Math.abs(xs[j] - mx) < Math.abs(xs[k] - mx)) k = j;
    setHover({ k, mx: xs[k], my: e.clientY - r.top });
  };
  const hk = hover?.k;
  const near = hover ? (data.events || []).filter((ev) => Math.abs(ev.t_ms - t[hk]) <= Math.max(data.step_ms, 5 * 60_000)) : [];
  const fmtV = (v) => (mode === "pace" ? `${v.toFixed(v >= 10 ? 0 : 1)}×` : usd(v));
  return (
    <div className="rv-stage" style={{ height: H }} onPointerMove={onMove} onPointerLeave={() => { setHover(null); }}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label="Dollars bought (above) and sold (below) by takers in each market, over time">
        <defs>
          <pattern id="rv-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" className="rv-hatch" />
          </pattern>
          <linearGradient id="rv-fade" x1="0" x2="1">
            <stop offset="0" stopColor="#060a09" stopOpacity="0.9" />
            <stop offset="0.04" stopColor="#060a09" stopOpacity="0" />
          </linearGradient>
        </defs>
        {yTicks.map((v) => (
          <g key={v}>
            <line className="rv-gl" x1={PADL} x2={PADL + plotW} y1={mid - S(v)} y2={mid - S(v)} />
            <line className="rv-gl" x1={PADL} x2={PADL + plotW} y1={mid + S(v)} y2={mid + S(v)} />
            {mode === "usd" && <text className="rv-ax" x={PADL + plotW + 6} y={mid - S(v) + 3}>{usd(v)}</text>}
            {mode === "usd" && <text className="rv-ax" x={PADL + plotW + 6} y={mid + S(v) + 3}>{usd(v)}</text>}
          </g>
        ))}
        {tTicks.map((ms) => (
          <g key={ms}>
            <line className="rv-gl" x1={X(ms)} x2={X(ms)} y1={PADT} y2={PADT + plotH} />
            <text className="rv-ax" x={X(ms)} y={H - 5} textAnchor="middle">{hhmm(ms)}</text>
          </g>
        ))}
        <text className="rv-ax rv-now" x={PADL + plotW} y={H - 5} textAnchor="end">now</text>
        {mode === "usd" && <text className="rv-ax" x={PADL + plotW + 6} y={PADT - 8}>a min</text>}
        <text className="rv-side up" x={PADL + 6} y={PADT + 11}>BOUGHT</text>
        <text className="rv-side down" x={PADL + 6} y={PADT + plotH - 4}>SOLD</text>
        {gap != null && gap > t0 && (
          <g>
            <rect x={PADL} y={PADT} width={Math.max(0, X(gap) - PADL)} height={plotH} fill="url(#rv-hatch)" />
            {X(gap) - PADL > 120 && <text className="rv-ax" x={(PADL + X(gap)) / 2} y={mid - 6} textAnchor="middle">not recorded</text>}
          </g>
        )}
        {layers.map(({ m, up, dn }) => {
          const dim = hi && hi !== m.coin;
          return (
            <g key={m.coin} className={`rv-layer ${dim ? "dim" : ""} ${hi === m.coin ? "on" : ""}`}
              onPointerEnter={() => setHi(m.coin)} onPointerLeave={() => setHi(null)}>
              <path d={up} fill={BUY[m.i % BUY.length]} />
              <path d={dn} fill={SELL[m.i % SELL.length]} />
            </g>
          );
        })}
        <path d={edgeU} className="rv-edge up" />
        <path d={edgeD} className="rv-edge down" />
        <line className="rv-mid" x1={PADL} x2={PADL + plotW} y1={mid} y2={mid} />
        {net && <path d={net} className="rv-net" />}
        {net && <text className="rv-netlab" x={PADL + plotW - 4} y={mid - Math.sign(totUp[n - 1] - totDn[n - 1]) * S(Math.abs(totUp[n - 1] - totDn[n - 1])) - 5} textAnchor="end">net</text>}
        {clipped.map(([u, d2], k) => (
          <g key={k}>
            {u && <path className="rv-clip" d={`M${xs[k] - 4},${PADT + 5}L${xs[k]},${PADT}L${xs[k] + 4},${PADT + 5}`} />}
            {d2 && <path className="rv-clip" d={`M${xs[k] - 4},${PADT + plotH - 5}L${xs[k]},${PADT + plotH}L${xs[k] + 4},${PADT + plotH - 5}`} />}
          </g>
        ))}
        {layers.map(({ m, tag }) => tag && (!hi || hi === m.coin) && (
          <text key={m.coin} className="rv-tag" x={tag.x} y={tag.y + 3.5} textAnchor="middle">
            {label(m.coin)}{mode === "pace" ? ` ${fmtV(tag.v)}` : ""}
          </text>
        ))}
        {evs.map((e) => (
          <g key={`${e.t_ms}-${e.title}`} className="rv-ev">
            <line x1={e.x} x2={e.x} y1={PADT - 6} y2={PADT + plotH} />
            <path d={`M${e.x - 3.5},${PADT - 10}L${e.x + 3.5},${PADT - 10}L${e.x},${PADT - 5}Z`} />
            {e.show && <text x={e.x + 6} y={PADT - 6}>{e.title}</text>}
          </g>
        ))}
        <rect x={PADL} y={PADT} width={plotW} height={plotH} fill="url(#rv-fade)" pointerEvents="none" />
        {hover && <line className="rv-cross" x1={hover.mx} x2={hover.mx} y1={PADT} y2={PADT + plotH} />}
      </svg>
      {hover && (
        <div className="rv-tip" style={{ left: hover.mx, top: Math.min(hover.my, H - 40),
          transform: `translate(${hover.mx > W * 0.62 ? "calc(-100% - 12px)" : "12px"}, -50%)` }}>
          <div className="rv-tip-h">
            {hk === n - 1 ? `last ${STEP_NAME[data.step_ms] || "step"} to now` : `${hhmm(t[hk] - data.step_ms / 2)}–${hhmm(t[hk] + data.step_ms / 2)}`}
            <span className="muted"> · {mode === "pace" ? "vs normal" : "a minute"}</span>
          </div>
          <table>
            <tbody>
              {mk.filter((m) => m.b[hk] + m.s[hk] > 0).map((m) => (
                <tr key={m.coin} className={hi === m.coin ? "on" : ""}>
                  <td>{label(m.coin)}</td>
                  <td className="num up">{fmtV(m.up[hk])}</td>
                  <td className="num down">{fmtV(m.dn[hk])}</td>
                  <td className="num muted">{m.norm[hk] ? `${((m.b[hk] + m.s[hk]) / m.norm[hk]).toFixed(1)}×` : ""}</td>
                </tr>
              ))}
              {mode === "usd" && (
                <tr className="rv-tip-tot">
                  <td>All</td><td className="num up">{usd(totUp[hk])}</td><td className="num down">{usd(totDn[hk])}</td>
                  <td className={`num ${totUp[hk] >= totDn[hk] ? "up" : "down"}`}>{signed(totUp[hk] - totDn[hk])}</td>
                </tr>
              )}
            </tbody>
          </table>
          {!mk.some((m) => m.b[hk] + m.s[hk] > 0) && <div className="muted">No trades</div>}
          {near.map((ev) => <div key={ev.t_ms + ev.title} className="rv-tip-ev">{hhmm(ev.t_ms)} {ev.title}</div>)}
        </div>
      )}
    </div>
  );
}

function Gauge({ x }) {
  const v = x == null ? 0 : Math.min(x, 4) / 4;
  return (
    <span className={`rv-gauge ${x >= 2 ? "hot" : ""}`} aria-hidden="true">
      <i style={{ width: `${v * 100}%` }} />
      <b style={{ left: "25%" }} />
    </span>
  );
}

function Pace({ data, hi, setHi, pinned, setPinned }) {
  const mk = series(data, "usd");
  const step = data.step_ms / 60_000;
  return (
    <div className="rv-list">
      <div className="rv-row rv-row-h muted">
        <span>Market</span><span title="The last 15 minutes against this market's normal for this weekday and hour">Pace 15m</span>
        <span className="num">Net</span>
      </div>
      {mk.map((m) => {
        const netW = m.bought - m.sold;
        const tot = m.bought + m.sold;
        return (
          <button key={m.coin} className={`rv-row ${hi === m.coin ? "on" : ""} ${pinned === m.coin ? "pinned" : ""}`} data-mk={tk(m.coin)}
            onPointerEnter={() => setHi(m.coin)} onPointerLeave={() => setHi(null)}
            onClick={() => setPinned(pinned === m.coin ? null : m.coin)}
            title={`${label(m.coin)}: bought ${usd(m.bought)}, sold ${usd(m.sold)} in the window${m.pace != null ? `; the last 15 minutes ran at ${m.pace}× its normal pace` : ""}`}>
            <span className="rv-name">
              <i className="rv-sw" style={{ background: `linear-gradient(${BUY[m.i % BUY.length]} 50%, ${SELL[m.i % SELL.length]} 50%)` }} />
              {label(m.coin)}
            </span>
            <span className="rv-pace">
              <Gauge x={m.pace} />
              <b className={`num ${m.pace >= 2 ? "hot" : ""}`}>{m.pace == null ? "–" : `${m.pace.toFixed(1)}×`}</b>
            </span>
            <span className="rv-net">
              <b className={`num ${netW > 0 ? "up" : netW < 0 ? "down" : "muted"}`}>{tot ? signed(netW) : "–"}</b>
              <span className="rv-split" aria-hidden="true"><i style={{ width: `${tot ? (m.bought / tot) * 100 : 50}%` }} /></span>
            </span>
          </button>
        );
      })}
      <p className="plan-note muted rv-note">
        Takers' dollars, {STEP_NAME[data.step_ms] || `${step} min`} steps. Pace: the last 15 minutes against the market's
        normal for this weekday and hour (median of 8 weeks of hourly volume); 2× and up is lit.
      </p>
    </div>
  );
}

export default function Flow({ d, now }) {
  const [tab, setTab] = useState("river");
  const [minutes, setMinutes] = useState(60);
  const [mode, setMode] = useState("usd");
  const [hiHover, setHiHover] = useState(null);
  const [pinned, setPinned] = useState(null);
  const data = useFlow(minutes, tab === "river");
  useEffect(() => {
    const onView = (e) => { if (e.detail?.view) setTab(e.detail.view); };
    window.addEventListener("term-view", onView);
    return () => window.removeEventListener("term-view", onView);
  }, []);
  const hi = hiHover || pinned;
  const live = data?.last_ms != null && now - data.last_ms < 120_000;
  const titles = { river: "Flow river", links: "Links", hours: "When markets move" };
  const since = data?.data_from_ms && data.data_from_ms > data.from_ms + 60_000 ? hhmm(data.data_from_ms) : null;
  const meta = tab === "river"
    ? [["Feed", data?.source || "starting"], ["Step", data ? STEP_NAME[data.step_ms] : "-"], ...(since ? [["Since", since]] : []),
      ["Last trade", data?.last_ms ? age(now - data.last_ms) : "-"]]
    : tab === "links" ? [["Data", "hourly moves"], ["Range", "7 days"]] : [["Data", `${d.activity?.days ?? 30} days`], ["Hours", "yours"]];
  const empty = data && !data.markets.some((m) => m.b.some((x) => x) || m.s.some((x) => x));
  return (
    <section id="p-FLOW" className="panel term-wide">
      <Head code="FLOW" title={titles[tab]} meta={meta}
        pills={[tab === "river" ? (live ? <Pill key="l" kind="live">Live</Pill> : <Pill key="l">{data?.enabled === false ? "Live mode only" : "Waiting"}</Pill>) : null]}>
        <span className="tp-tabs" role="tablist">
          {[["river", "River"], ["links", "Links"], ["hours", "Hours"]].map(([k, t]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{t}</button>
          ))}
        </span>
      </Head>
      {tab === "river" && (
        <>
          <div className="term-comp-legend rv-bar">
            {WINDOWS.map(([k, t]) => (
              <button key={k} className={`plan-chip ${minutes === k ? "on" : ""}`} onClick={() => setMinutes(k)}>{t}</button>
            ))}
            <span className="rv-sep" />
            {[["usd", "Dollars"], ["pace", "vs normal"]].map(([k, t]) => (
              <button key={k} className={`plan-chip ${mode === k ? "on" : ""}`} onClick={() => setMode(k)}
                title={k === "pace" ? "Each band against that market's usual pace for this weekday and hour" : "Dollars a minute"}>{t}</button>
            ))}
            <span className="news-gap" />
            <span className="muted term-comp-note rv-key">
              <i className="rv-k up" />bought <i className="rv-k down" />sold <i className="rv-k net" />net <i className="rv-k ev" />scheduled event
            </span>
          </div>
          <div className="rv-body">
            {!data ? <div className="rv-empty muted">Loading the river…</div>
              : empty ? <div className="rv-empty muted">{data.enabled ? "No trades yet: the river fills as they come in." : "The river runs in live mode only."}</div>
              : <Measured>{(w) => <River data={data} mode={mode} W={w} hi={hi} setHi={setHiHover} />}</Measured>}
            {data && <Pace data={data} hi={hi} setHi={setHiHover} pinned={pinned} setPinned={setPinned} />}
          </div>
        </>
      )}
      {tab === "links" && (d.compare && Object.keys(d.compare.series).length ? <Links c={d.compare} /> : <p className="plan-note muted term-pad">Links need a few days of hourly history.</p>)}
      {tab === "hours" && <HeatBody d={d} now={now} />}
    </section>
  );
}
