import { useMemo } from "react";
import { COLS, ROWS, STEP, TOP } from "../worldDots.js";
import { Head, Pill } from "./TermPanel.jsx";

/* WRLD: where the edge markets live, and what they're doing right now.
     land      dots from Natural Earth; night shaded from the sun's position (it moves as you watch)
     hubs      where each market's reference trades (WTI at Cushing, Brent and gold in London, silver and the dollar
               in New York, the euro in Frankfurt, the yen in Tokyo): lit cyan while that market is open, grey when
               shut; the ring grows with 15 minutes of dollar flow and is green or red by which side is winning;
               a big order makes it ripple
     cards     each hub's markets: price, day's move, 15-minute net flow
     pins      the next scheduled events, where they come from
   The footer carries the market clock: open or shut, the countdown, the week's hours and what's next. */

const RAD = Math.PI / 180;
const H = ROWS.length;
const x = (lon) => (lon + 180) / STEP;
const y = (lat) => (TOP - lat) / STEP;

const HUBS = [
  { key: "CHI", name: "Cushing · NYMEX", lat: 35.98, lon: -96.77, markets: ["CL"], card: { dx: -2, dy: 3, align: "right" }, tz: "America/Chicago" },
  { key: "NY", name: "New York", lat: 40.71, lon: -74.0, markets: ["SILVER", "DXY"], card: { dx: 2, dy: 4, align: "left" }, tz: "America/New_York" },
  { key: "LDN", name: "London · ICE", lat: 51.51, lon: -0.13, markets: ["BRENTOIL", "GOLD"], card: { dx: -3, dy: -3, align: "right", up: true }, tz: "Europe/London" },
  { key: "FRA", name: "Frankfurt", lat: 50.11, lon: 8.68, markets: ["EUR"], card: { dx: 3, dy: -3, align: "left", up: true }, tz: "Europe/Berlin" },
  { key: "TYO", name: "Tokyo", lat: 35.68, lon: 139.69, markets: ["JPY"], card: { dx: -2, dy: 3, align: "right" }, tz: "Asia/Tokyo" },
];
const PLACES = {
  US: { lat: 38.9, lon: -77.04, name: "Washington" },
  JP: { lat: 35.68, lon: 139.69, name: "Tokyo" },
  crypto: { lat: 25.2, lon: 55.27, name: "Dubai" },
  session: { lat: 41.88, lon: -87.63, name: "Chicago" },
};
const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY" };
const tk = (n) => n.split(":").pop();
const usd = (v) => {
  const a = Math.abs(v);
  const s = a >= 1e6 ? `$${(a / 1e6).toFixed(1)}M` : a >= 1e3 ? `$${Math.round(a / 1e3)}k` : `$${Math.round(a)}`;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${s}`;
};
const px = (p) => (p == null ? "-" : p.toLocaleString(undefined, { maximumFractionDigits: p >= 1000 ? 1 : p >= 10 ? 2 : 4, minimumFractionDigits: p >= 1000 ? 1 : p >= 10 ? 2 : 4 }));
const pct = (v) => (v == null ? "-" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}%`);
const clockIn = (tz, now) => new Date(now).toLocaleTimeString("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit" });
const pad2 = (n) => String(n).padStart(2, "0");
const hms = (ms) => {
  const t = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(t / 86400);
  const core = `${pad2(Math.floor((t % 86400) / 3600))}:${pad2(Math.floor((t % 3600) / 60))}:${pad2(t % 60)}`;
  return d ? `${d}d ${core}` : core;
};
const dayTime = (ms) => new Date(ms).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

/** The night side as a polygon, from the sun's position at t (declination and the hour angle; good to a degree). */
function night(t) {
  const d = new Date(t);
  const start = Date.UTC(d.getUTCFullYear(), 0, 0);
  const n = (t - start) / 86_400_000;
  let dec = -23.44 * Math.cos((2 * Math.PI / 365) * (n + 10));
  if (Math.abs(dec) < 0.2) dec = dec < 0 ? -0.2 : 0.2;
  const hours = d.getUTCHours() + d.getUTCMinutes() / 60 + d.getUTCSeconds() / 3600;
  const sunLon = -15 * (hours - 12);
  const pts = [];
  for (let lon = -180; lon <= 180; lon += 2) {
    const lat = Math.atan(-Math.cos((lon - sunLon) * RAD) / Math.tan(dec * RAD)) / RAD;
    pts.push([x(lon), y(Math.max(-60, Math.min(80, lat)))]);
  }
  const edge = dec > 0 ? y(-62) : y(80);                   // the dark pole: south in northern summer
  const poly = [...pts, [x(180), edge], [x(-180), edge]];
  return { poly: poly.map((p) => p.map((v) => v.toFixed(2)).join(",")).join(" "), line: pts.map((p) => p.join(",")).join(" "), sun: [x(sunLon > 180 ? sunLon - 360 : sunLon < -180 ? sunLon + 360 : sunLon), y(dec)] };
}

function hubOpen(hub, mk) {
  const s = hub.markets.map((m) => mk[m]?.session).find(Boolean);
  if (s) return s.open;
  const fx = hub.markets.map((m) => mk[m]).find(Boolean);
  if (!fx) return null;
  const wd = new Date().getUTCDay();                        // currencies: shut from Friday night to Sunday night
  const h = new Date().getUTCHours();
  return !((wd === 5 && h >= 22) || wd === 6 || (wd === 0 && h < 22));
}

export default function World({ d, now, orders, info }) {
  const dots = useMemo(() => {
    let path = "";
    ROWS.forEach((runs, r) => {
      for (let k = 0; k < runs.length; k += 2) {
        for (let c = runs[k]; c < runs[k] + runs[k + 1]; c++) path += `M${c + 0.5} ${r}h0`;
      }
    });
    return path;
  }, []);
  const minute = Math.floor(now / 60_000);
  const nt = useMemo(() => night(minute * 60_000), [minute]);
  const mk = Object.fromEntries([...d.markets, ...d.fx].map((m) => [m.ticker, m]));
  const flow = info?.flow || {};
  const cut = info?.cut?.[15] || 0;
  const hubFlow = HUBS.map((h) => {
    let net = 0, gross = 0;
    for (const t of h.markets) {
      const name = Object.keys(flow).find((n) => tk(n) === t);
      const f = name && flow[name]?.[15];
      if (f) { net += f.net; gross += f.buy + f.sell; }
    }
    const fresh = orders.some((o) => h.markets.includes(tk(o.coin)) && o.usd >= cut && Date.now() - o.seen_ms < 3500);
    return { net, gross, fresh };
  });
  const maxGross = Math.max(1, ...hubFlow.map((f) => f.gross));
  const events = d.events.filter((e) => !e.all_day && e.t_ms > now).slice(0, 3).map((e) => {
    const where = e.group === "crypto" ? PLACES.crypto : e.group === "session" ? PLACES.session
      : /japan/i.test(e.note || "") ? PLACES.JP : PLACES.US;
    return { ...e, where };
  });
  const cl = mk.CL?.session;
  const target = cl ? (cl.open ? cl.next_long_close_ms : cl.next_change_ms) : null;
  const brk = cl && !cl.open && cl.why === "break";
  const W = COLS, VB = `-1 -1.5 ${W + 2} ${H + 2}`;
  const pos = (lat, lon) => ({ left: `${((x(lon) + 1) / (W + 2)) * 100}%`, top: `${((y(lat) + 1.5) / (H + 2)) * 100}%` });
  const { from_ms: a, to_ms: b, open } = d.hours;
  const pctW = (t) => Math.min(100, Math.max(0, ((t - a) / (b - a)) * 100));

  return (
    <section id="p-WRLD" className="panel wm">
      <Head code="WRLD" title="Markets around the world"
        meta={[["Flow", "15 min"], ["Night", "from the sun"], ["Times", "local"]]}
        pills={[cl ? <Pill key="s" kind={cl.open ? "state" : "mute"}>{cl.open ? "CME open" : brk ? "CME break" : "CME shut"}</Pill> : null,
          info?.last_ms && now - info.last_ms < 60_000 ? <Pill key="l" kind="live">Live</Pill> : <Pill key="l">Waiting</Pill>]} />
      <div className="wm-stage">
        <svg viewBox={VB} preserveAspectRatio="xMidYMid meet" className="wm-svg" role="img" aria-label="World map of the edge markets' hubs">
          <defs>
            <radialGradient id="wm-glow"><stop offset="0" stopColor="#4cc4cc" stopOpacity="0.55" /><stop offset="1" stopColor="#4cc4cc" stopOpacity="0" /></radialGradient>
          </defs>
          {[-60, -30, 0, 30, 60].map((lat) => <line key={lat} x1={0} x2={W} y1={y(lat)} y2={y(lat)} className="wm-grat" />)}
          {[-120, -60, 0, 60, 120].map((lon) => <line key={lon} x1={x(lon)} x2={x(lon)} y1={0} y2={H} className="wm-grat" />)}
          <path d={dots} className="wm-land" />
          <polygon points={nt.poly} className="wm-night" />
          <polyline points={nt.line} className="wm-terminator" />
          <circle cx={nt.sun[0]} cy={nt.sun[1]} r="1.1" className="wm-sun"><title>Where the sun is overhead</title></circle>
          {events.map((e, i) => (
            <g key={`${e.t_ms}-${e.title}`} className={`wm-pin ${i === 0 ? "next" : ""}`} transform={`translate(${x(e.where.lon) + (i - 1) * 1.6} ${y(e.where.lat) + 2.4})`}>
              <path d="M0 -1.4 L1.1 0 L0 1.4 L-1.1 0 Z" />
              <title>{`${e.title}: ${dayTime(e.t_ms)} (${e.where.name})`}</title>
            </g>
          ))}
          {HUBS.map((h, i) => {
            const on = hubOpen(h, mk);
            const f = hubFlow[i];
            const r = 1.4 + 4.2 * Math.sqrt(f.gross / maxGross);
            const side = f.net > 0 ? "buy" : f.net < 0 ? "sell" : "";
            return (
              <g key={h.key} transform={`translate(${x(h.lon)} ${y(h.lat)})`}>
                {on && <circle r={r + 5} fill="url(#wm-glow)" />}
                {f.gross > 0 && <circle r={r} className={`wm-flow ${side}`} />}
                {f.fresh && <circle r={r} className={`wm-ripple ${side}`} />}
                <circle r="0.9" className={`wm-hub ${on ? "on" : ""}`} />
              </g>
            );
          })}
        </svg>
        {HUBS.map((h, i) => {
          const on = hubOpen(h, mk);
          const f = hubFlow[i];
          return (
            <div key={h.key} className={`wm-card ${h.card.align} ${h.card.up ? "up" : ""}`} style={pos(h.lat + h.card.dy * -1, h.lon + h.card.dx)}>
              <div className="wm-card-top">
                <b>{h.key}</b>
                <span className="num">{clockIn(h.tz, now)}</span>
                <span className={`wm-light ${on ? "on" : ""}`}>{on ? "open" : on === false ? "shut" : "-"}</span>
              </div>
              {h.markets.map((t) => {
                const m = mk[t];
                const name = Object.keys(flow).find((n) => tk(n) === t);
                const net = name ? flow[name]?.[15]?.net : null;
                return (
                  <div key={t} className="wm-row">
                    <span className="wm-tk">{SHORT[t] || t}</span>
                    <span className="num">{px(m?.price)}</span>
                    <span className={`num ${m?.change_24h_pct > 0 ? "up" : m?.change_24h_pct < 0 ? "down" : ""}`}>{pct(m?.change_24h_pct)}</span>
                    <span className={`num wm-net ${net > 0 ? "up" : net < 0 ? "down" : "muted"}`} title="Net flow over 15 minutes: bought minus sold">{net ? usd(net) : "-"}</span>
                  </div>
                );
              })}
              {f.gross > 0 && <div className="wm-gross">{usd(f.gross).replace("+", "")} traded · 15 min</div>}
            </div>
          );
        })}
        <div className="wm-legend">
          <span><i className="wm-k hub" />open</span>
          <span><i className="wm-k shut" />shut</span>
          <span><i className="wm-k buy" />buyers ahead</span>
          <span><i className="wm-k sell" />sellers ahead</span>
          <span><i className="wm-k pin" />next events</span>
        </div>
      </div>
      <div className="wm-list">
        {HUBS.map((h, i) => (
          <div key={h.key}><b>{h.key}</b> {h.markets.map((t) => SHORT[t] || t).join(", ")} <span className={hubFlow[i].net > 0 ? "up" : hubFlow[i].net < 0 ? "down" : "muted"}>{hubFlow[i].gross ? usd(hubFlow[i].net) : "-"}</span></div>
        ))}
      </div>
      <div className="wm-foot">
        <div className="wm-clock">
          <span className={`wm-state ${cl?.open ? "on" : ""}`}>{cl ? (cl.open ? "Open" : brk ? "Daily break" : "Shut") : "-"}</span>
          <span className="muted">{cl?.open ? "shuts for the weekend in" : "reopens in"}</span>
          <b className="num">{target ? hms(target - now) : "-"}</b>
          {cl && !cl.open && !brk && <Pill kind="state">Hyperliquid alone</Pill>}
        </div>
        <div className="wm-week" title="This week's real-market hours (WTI, gold, silver): filled when open">
          {open.map(([s, e]) => <span key={s} className="wm-week-open" style={{ left: `${pctW(s)}%`, width: `${pctW(e) - pctW(s)}%` }} />)}
          <span className="wm-week-now" style={{ left: `${pctW(now)}%` }} />
        </div>
        <div className="wm-next">
          {events[0] ? <><span className="muted">Next</span> <b>{events[0].title}</b> <span className="muted">in</span> <b className="num">{hms(events[0].t_ms - now)}</b></> : <span className="muted">Nothing scheduled</span>}
        </div>
      </div>
    </section>
  );
}
