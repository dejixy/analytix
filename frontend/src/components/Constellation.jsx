import { useMemo, useState } from "react";
import { Head } from "./TermPanel.jsx";
import { Measured } from "./Compare.jsx";
import { Seg } from "./River.jsx";

/* Two views of the same moment, in green and red only.

   CNST, the market constellation: every market is a star, placed so markets that moved together over the week sit
   close (classical scaling of the hourly-return correlations) and joined by a line: green when they move together,
   red when they move opposite, thicker the stronger. A star's size is its pace against normal over the last 15
   minutes, its colour who is winning there: green when takers bought more, red when they sold more.

   PRES, the pressure radar: one spoke per market. The green shape is buying against that market's normal pace, the
   red shape selling, so a shape reaching out on one spoke is pressure building there. Rings at ½×, 1× (normal), 2×
   and 4×. */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY", GBP: "GBP/USD" };
const ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "JPY", "EUR", "DXY", "GBP"];
const tk = (n) => n.split(":").pop();
const label = (n) => SHORT[tk(n)] || tk(n);
const G = "#2fc493", R = "#e0545e";
const usd = (x) => {
  const a = Math.abs(x);
  return a >= 1e6 ? `$${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M` : a >= 1e3 ? `$${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k` : `$${Math.round(a)}`;
};
const signed = (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${usd(x)}`;
const pct = (x) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(2)}%`);
const byOrder = (a, b) => ORDER.indexOf(tk(a)) - ORDER.indexOf(tk(b));
const LINK_MIN = 0.35;                                        // weaker links aren't drawn (they'd only add clutter)

/* Seeded scatter for the background stars, so they stay put between renders. */
function stars(n, W, H, seed = 7) {
  let s = seed;
  const rnd = () => ((s = (s * 16807) % 2147483647) / 2147483647);
  return Array.from({ length: n }, () => ({ x: rnd() * W, y: rnd() * H, r: 0.5 + rnd() * 0.9, o: 0.06 + rnd() * 0.22, tw: rnd() < 0.18 }));
}

/* Classical multidimensional scaling of correlation distances (√(2(1 − r))) to two dimensions, then nudged apart so no
   two stars overlap. Signs are fixed (WTI on the left, gold above) so the picture doesn't flip between refreshes. */
function place(names, m, W, H) {
  const n = names.length;
  const r = (i, j) => (i === j ? 1 : m?.[i]?.[j] ?? 0);
  const D2 = names.map((_, i) => names.map((__, j) => 2 * (1 - r(i, j))));
  const rowM = D2.map((row) => row.reduce((a, b) => a + b, 0) / n);
  const all = rowM.reduce((a, b) => a + b, 0) / n;
  const B = D2.map((row, i) => row.map((v, j) => -0.5 * (v - rowM[i] - rowM[j] + all)));
  const vecs = [];
  for (let k = 0; k < 2; k++) {
    let v = names.map((_, i) => Math.sin(i * 1.7 + k * 2.3) + 0.5);
    let lam = 0;
    for (let it = 0; it < 120; it++) {
      let w = B.map((row) => row.reduce((a, b, j) => a + b * v[j], 0));
      for (const [u] of vecs) {                                 // deflate what's already found
        const d = w.reduce((a, b, i) => a + b * u[i], 0);
        w = w.map((x, i) => x - d * u[i]);
      }
      const norm = Math.hypot(...w) || 1;
      lam = norm;
      v = w.map((x) => x / norm);
    }
    vecs.push([v, lam]);
  }
  let pts = names.map((_, i) => [vecs[0][0][i] * Math.sqrt(vecs[0][1]), vecs[1][0][i] * Math.sqrt(vecs[1][1])]);
  const iW = names.findIndex((x) => tk(x) === "CL"), iG = names.findIndex((x) => tk(x) === "GOLD");
  if (iW >= 0 && pts[iW][0] > 0) pts = pts.map(([x, y]) => [-x, y]);
  if (iG >= 0 && pts[iG][1] > 0) pts = pts.map(([x, y]) => [x, -y]);
  const spread = Math.max(...pts.map(([x, y]) => Math.hypot(x, y)));
  if (!Number.isFinite(spread) || spread < 1e-6) {           // no links yet: a ring
    pts = names.map((_, i) => [Math.cos((i / n) * 2 * Math.PI - Math.PI / 2), Math.sin((i / n) * 2 * Math.PI - Math.PI / 2)]);
  }
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const PX = Math.min(90, W * 0.13), PY = 46;
  let out = pts.map(([x, y]) => [PX + ((x - x0) / (x1 - x0 || 1)) * (W - 2 * PX), PY + ((y - y0) / (y1 - y0 || 1)) * (H - 2 * PY)]);
  // a star and its label take about 120 × 64 px: keep every pair at least that far apart (in that ellipse)
  const AX = Math.min(124, W / 4.6), AY = 66;
  for (let it = 0; it < 140; it++) {
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = out[j][0] - out[i][0], dy = out[j][1] - out[i][1];
        const e = Math.hypot(dx / AX, dy / AY) || 0.01;
        if (e < 1) {
          const k = (1 - e) / 2 / e;
          out[i] = [out[i][0] - dx * k, out[i][1] - dy * k];
          out[j] = [out[j][0] + dx * k, out[j][1] + dy * k];
        }
      }
    }
    out = out.map(([x, y]) => [Math.max(64, Math.min(W - 64, x)), Math.max(PY * 0.7, Math.min(H - PY * 1.2, y))]);   // labels stay inside
  }
  return out;
}

function Sky({ W, H, names, m, info, hi, setHi }) {
  const P = useMemo(() => place(names, m, W, H), [names.join(), JSON.stringify(m), W, H]);
  const bg = useMemo(() => stars(Math.round((W * H) / 5200), W, H), [W, H]);
  const links = [];
  names.forEach((a, i) => names.forEach((b, j) => {
    const r = m?.[i]?.[j];
    if (j > i && r != null && Math.abs(r) >= LINK_MIN) links.push({ a, b, i, j, r });
  }));
  links.sort((x, y) => Math.abs(x.r) - Math.abs(y.r));        // strongest drawn last, on top
  const near = (n) => hi && (n === hi);
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="cn-sky" role="img"
      aria-label="Markets placed by how they moved together this week; lines green together, red opposite">
      <defs>
        <radialGradient id="cn-g"><stop offset="0" stopColor={G} stopOpacity="0.55" /><stop offset="1" stopColor={G} stopOpacity="0" /></radialGradient>
        <radialGradient id="cn-r"><stop offset="0" stopColor={R} stopOpacity="0.5" /><stop offset="1" stopColor={R} stopOpacity="0" /></radialGradient>
      </defs>
      {bg.map((s, k) => <circle key={k} cx={s.x} cy={s.y} r={s.r} fill={G} opacity={s.o} className={s.tw ? "cn-tw" : ""}
        style={s.tw ? { animationDelay: `${(k % 9) * 0.7}s` } : undefined} />)}
      {links.map(({ a, b, i, j, r }) => {
        const lit = !hi || near(a) || near(b);
        return (
          <g key={`${a}-${b}`} opacity={lit ? 1 : 0.12}>
            <line x1={P[i][0]} y1={P[i][1]} x2={P[j][0]} y2={P[j][1]} stroke={r > 0 ? G : R}
              strokeWidth={0.7 + 2.6 * Math.abs(r)} strokeOpacity={0.14 + 0.62 * Math.abs(r)} strokeDasharray={r < 0 ? "5 4" : undefined}
              strokeLinecap="round" />
            {(Math.abs(r) >= 0.6 || (hi && lit)) && (
              <text x={(P[i][0] + P[j][0]) / 2} y={(P[i][1] + P[j][1]) / 2 - 5} className="cn-r" fill={r > 0 ? G : R} textAnchor="middle">
                {r > 0 ? "+" : "−"}{Math.abs(r).toFixed(2)}
              </text>
            )}
          </g>
        );
      })}
      {names.map((n, i) => {
        const x = info[n] || {};
        const buy = (x.share ?? 0.5) >= 0.5;
        const pace = x.pace ?? 0.6;
        const core = 4 + 3.4 * Math.sqrt(Math.min(pace, 6));
        const tilt = Math.min(1, Math.abs((x.share ?? 0.5) - 0.5) * 2);
        const c = buy ? G : R;
        const lit = !hi || hi === n;
        const [px, py] = P[i];

        return (
          <g key={n} className="cn-star" opacity={lit ? 1 : 0.28} onPointerEnter={() => setHi(n)} onPointerLeave={() => setHi(null)}>
            <circle cx={px} cy={py} r={core * 3.6} fill={`url(#cn-${buy ? "g" : "r"})`} opacity={0.45 + 0.55 * Math.min(1, pace / 2)} />
            {pace >= 2 && <circle cx={px} cy={py} r={core + 4} className="cn-pulse" stroke={c} />}
            <circle cx={px} cy={py} r={core} fill={c} fillOpacity={0.55 + 0.45 * tilt} stroke={c} strokeWidth="1.2" />
            <circle cx={px} cy={py} r={Math.max(1.6, core * 0.32)} fill="#f3fbf8" opacity="0.85" />
            <text x={px} y={py + core + 17} textAnchor="middle" className="cn-name">{label(n)}</text>
            <text x={px} y={py + core + 31} textAnchor="middle" className="cn-sub">
              <tspan fill={x.chg > 0 ? G : x.chg < 0 ? R : undefined}>{pct(x.chg)}</tspan>
              <tspan className="cn-dim"> 1h · {x.pace == null ? "–" : `${x.pace.toFixed(1)}×`}</tspan>
            </text>
            <circle cx={px} cy={py} r={Math.max(core * 2.4, 22)} fill="transparent" />
          </g>
        );
      })}
    </svg>
  );
}

export default function Constellation({ d, flow }) {
  const [hi, setHi] = useState(null);
  const c = d.compare?.corr;
  const names = c ? [...c.names].sort(byOrder) : [];
  const idx = c ? names.map((n) => c.names.indexOf(n)) : [];
  const m = c ? idx.map((i) => idx.map((j) => c.m[i][j])) : null;
  const rows = Object.fromEntries([...d.markets, ...d.fx].map((x) => [x.name, x]));
  const fm = Object.fromEntries((flow?.markets || []).map((x) => [x.coin, x]));
  const info = Object.fromEntries(names.map((n) => {
    const h = fm[n]?.horizons?.[15];
    const share = h && h.b + h.s ? h.b / (h.b + h.s) : null;
    return [n, { chg: rows[n]?.change_1h_pct, pace: fm[n]?.pace ?? null, share, net: h ? h.b - h.s : null }];
  }));
  const pairs = [];
  names.forEach((a, i) => names.forEach((b, j) => { if (j > i && m[i][j] != null) pairs.push([a, b, m[i][j]]); }));
  const together = [...pairs].sort((p, q) => q[2] - p[2])[0];
  const apart = [...pairs].sort((p, q) => p[2] - q[2])[0];
  const h = hi && info[hi];
  return (
    <section id="p-CNST" className="panel cn-panel">
      <Head code="CNST" title="Market constellation" meta={[["Links", "7 days, hourly"], ["Stars", "last 15 min"]]}
        pills={[<span key="n" className="cn-badge">{pairs.filter((p) => Math.abs(p[2]) >= LINK_MIN).length} links</span>]} />
      <div className="cn-bar">
        {together && <span>Move together <b>{label(together[0])}–{label(together[1])}</b> <em className="up num">+{together[2].toFixed(2)}</em></span>}
        {apart && apart[2] < 0 && <span>Move opposite <b>{label(apart[0])}–{label(apart[1])}</b> <em className="down num">−{Math.abs(apart[2]).toFixed(2)}</em></span>}
        {h && <span className="cn-hover"><b>{label(hi)}</b> {h.net != null && <em className={`num ${h.net >= 0 ? "up" : "down"}`}>{signed(h.net)}</em>} net 15m
          {h.share != null && <> · {Math.round(h.share * 100)}% bought</>}</span>}
      </div>
      {!names.length ? <p className="plan-note muted term-pad">The constellation needs a few days of hourly history.</p>
        : <Measured>{(w) => <div className="cn-stage"><Sky W={w - 22} H={w < 640 ? 320 : 372} names={names} m={m} info={info} hi={hi} setHi={setHi} /></div>}</Measured>}
      <p className="cn-key">
        <span><i className="cn-k g" />move together</span><span><i className="cn-k r dash" />move opposite</span>
        <span><i className="cn-dot g" />net bought</span><span><i className="cn-dot r" />net sold</span><span>bigger = busier than normal</span>
      </p>
    </section>
  );
}

/* ── the pressure radar ───────────────────────────────────────────────── */
const RINGS = [0.5, 1, 2, 4];
const MAXX = 4;
const rad = (x, R) => R * Math.sqrt(Math.min(Math.max(x || 0, 0), MAXX) / MAXX);   // area-true: √ scale

function Scope({ W, H, rows, hz }) {
  const cx = W / 2, cy = H / 2 + 4;
  const RAD = Math.min(W / 2 - (W < 520 ? 94 : 72), H / 2 - 40);
  const n = rows.length;
  const ang = (i) => -Math.PI / 2 + (i / n) * 2 * Math.PI;
  const at = (i, r) => [cx + Math.cos(ang(i)) * r, cy + Math.sin(ang(i)) * r];
  const poly = (key) => rows.map((x, i) => at(i, rad(x[key], RAD)).map((v) => v.toFixed(1)).join(",")).join(" ");
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="pr-scope" role="img"
      aria-label={`Buying (green) and selling (red) against each market's normal pace over ${hz}`}>
      <defs>
        <radialGradient id="pr-bg"><stop offset="0" stopColor={G} stopOpacity="0.07" /><stop offset="1" stopColor={G} stopOpacity="0" /></radialGradient>
        <linearGradient id="pr-sw" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={G} stopOpacity="0" /><stop offset="1" stopColor={G} stopOpacity="0.14" />
        </linearGradient>
      </defs>
      <circle cx={cx} cy={cy} r={RAD + 6} fill="url(#pr-bg)" />
      {RINGS.map((x) => (
        <g key={x}>
          <circle cx={cx} cy={cy} r={rad(x, RAD)} className={`pr-ring ${x === 1 ? "norm" : ""}`} />
          <text x={cx + Math.cos(-Math.PI / 2 + Math.PI / n) * rad(x, RAD) + 3} y={cy + Math.sin(-Math.PI / 2 + Math.PI / n) * rad(x, RAD) - 3}
            className="pr-rl">{x === 1 ? "1× normal" : `${x}×`}</text>
        </g>
      ))}
      {rows.map((_, i) => {
        const [x, y] = at(i, RAD);
        return <line key={i} x1={cx} y1={cy} x2={x} y2={y} className="pr-spoke" />;
      })}
      <g className="pr-sweep" style={{ transformOrigin: `${cx}px ${cy}px` }}>
        <path d={`M${cx},${cy} L${cx + RAD},${cy} A${RAD},${RAD} 0 0,0 ${cx + RAD * Math.cos(-0.55)},${cy + RAD * Math.sin(-0.55)} Z`} fill="url(#pr-sw)" />
        <line x1={cx} y1={cy} x2={cx + RAD} y2={cy} stroke={G} strokeOpacity="0.55" strokeWidth="1" />
      </g>
      <polygon points={poly("sell")} className="pr-poly sell" />
      <polygon points={poly("buy")} className="pr-poly buy" />
      {rows.map((x, i) => {
        const [bx, by] = at(i, rad(x.buy, RAD));
        const [sx, sy] = at(i, rad(x.sell, RAD));
        const hot = Math.max(x.buy || 0, x.sell || 0) >= 2;
        const lead = (x.buy || 0) >= (x.sell || 0);
        return (
          <g key={x.coin}>
            <circle cx={sx} cy={sy} r="2.6" fill={R} />
            <circle cx={bx} cy={by} r="2.6" fill={G} />
            {hot && <circle cx={lead ? bx : sx} cy={lead ? by : sy} r="5" className="pr-blip" stroke={lead ? G : R} />}
          </g>
        );
      })}
      {rows.map((x, i) => {
        const [lx, ly] = at(i, RAD + 22);
        const a = Math.cos(ang(i));
        const anchor = Math.abs(a) < 0.25 ? "middle" : a > 0 ? "start" : "end";
        const net = x.b - x.s;
        return (
          <g key={`l-${x.coin}`}>
            <text x={lx} y={ly - 2} textAnchor={anchor} className="pr-name">{label(x.coin)}</text>
            <text x={lx} y={ly + 12} textAnchor={anchor} className="pr-net" fill={net > 0 ? G : net < 0 ? R : undefined}>
              {x.b + x.s ? signed(net) : "–"}
            </text>
          </g>
        );
      })}
      <circle cx={cx} cy={cy} r="2.2" fill={G} />
    </svg>
  );
}

export function Radar({ flow }) {
  const [hz, setHz] = useState(15);
  const rows = (flow?.markets || []).map((x) => {
    const h = x.horizons?.[hz] || {};
    return { coin: x.coin, buy: h.buy_x, sell: h.sell_x, b: h.b || 0, s: h.s || 0 };
  }).sort((a, b) => byOrder(a.coin, b.coin));
  const known = rows.filter((x) => x.buy != null);
  const topB = [...known].sort((a, b) => b.buy - a.buy)[0];
  const topS = [...known].sort((a, b) => b.sell - a.sell)[0];
  const hot = known.filter((x) => Math.max(x.buy, x.sell) >= 2).length;
  const hzName = { 5: "5 min", 15: "15 min", 60: "1 hour" }[hz];
  return (
    <section id="p-PRES" className="panel pr-panel">
      <Head code="PRES" title="Pressure radar" meta={[["Against", "normal for this hour"], ["Over", hzName]]}
        pills={[<span key="h" className={`cn-badge ${hot ? "hot" : ""}`}>{hot ? `${hot} hot` : "calm"}</span>]} />
      <div className="cn-bar pr-bar">
        <span>Most buying <b>{topB ? label(topB.coin) : "–"}</b> {topB && <em className="up num">{topB.buy.toFixed(1)}×</em>}</span>
        <span>Most selling <b>{topS ? label(topS.coin) : "–"}</b> {topS && <em className="down num">{topS.sell.toFixed(1)}×</em>}</span>
        <span className="news-gap" />
        <Seg value={hz} set={setHz} tone="g" opts={[[5, "5m"], [15, "15m"], [60, "1h"]]} />
      </div>
      {!flow ? <div className="rv-empty muted">Loading…</div>
        : !known.length ? <p className="plan-note muted term-pad">The radar needs each market's normal pace (the hourly candles) and some trades.</p>
        : <Measured>{(w) => <div className="cn-stage"><Scope W={w - 22} H={w < 520 ? 330 : 372} rows={rows} hz={hzName} /></div>}</Measured>}
      <p className="cn-key">
        <span><i className="cn-k g" />buying</span><span><i className="cn-k r" />selling</span><span>each against its market's normal pace for this weekday and hour</span>
      </p>
    </section>
  );
}
