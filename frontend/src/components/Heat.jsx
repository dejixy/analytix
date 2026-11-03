import { Head } from "./TermPanel.jsx";

/* HEAT: when each market usually moves. A row per market, a column per hour of the day in your time; each cell is the
   market's average hourly move at that hour over the last 30 days, shaded against its own busiest hour (so oil's
   US open and the yen's Tokyo morning both stand out). The current hour is outlined; the last column is what this
   hour usually brings. */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY" };
const ORDER = ["CL", "BRENTOIL", "GOLD", "SILVER", "JPY", "EUR", "DXY", "GBP"];
const tk = (n) => n.split(":").pop();

/** The grid alone (the BOOK panel's Hours tab, or inside its own panel). */
export function HeatBody({ d, now }) {
  const a = d.activity;
  if (!a || !a.names.length) return <p className="plan-note muted term-pad">Needs a few days of hourly history.</p>;
  const offset = Math.round(-new Date(now).getTimezoneOffset() / 60);   // your hours ahead of UTC
  const local = (utcH) => (utcH + offset + 24) % 24;
  const hourNow = new Date(now).getHours();
  const rows = a.names.map((n, i) => ({ n, v: a.m[i] })).sort((p, q) => ORDER.indexOf(tk(p.n)) - ORDER.indexOf(tk(q.n)));
  return (
    <div className="hm">
      <div className="hm-grid" style={{ gridTemplateColumns: `64px repeat(24, minmax(0, 1fr)) 44px` }}>
        <span />
        {Array.from({ length: 24 }, (_, h) => (
          <span key={h} className={`hm-h ${h === hourNow ? "now" : ""}`}>{h % 3 === 0 ? String(h).padStart(2, "0") : ""}</span>
        ))}
        <span className="hm-h right">now</span>
        {rows.map(({ n, v }) => {
          const byLocal = Array(24).fill(null);
          v.forEach((x, h) => { byLocal[local(h)] = x; });
          const top = Math.max(...byLocal.filter((x) => x != null), 1);
          return [
            <span key={`${n}-l`} className="hm-name">{SHORT[tk(n)] || tk(n)}</span>,
            ...byLocal.map((x, h) => {
              const k = x == null ? 0 : x / top;
              return (
                <span key={`${n}-${h}`} className={`hm-c ${h === hourNow ? "now" : ""}`}
                  style={{ background: x == null ? undefined : `rgba(76, 196, 204, ${(0.05 + 0.8 * k * k).toFixed(3)})` }}
                  title={`${SHORT[tk(n)] || tk(n)} ${String(h).padStart(2, "0")}:00: usually ${x == null ? "-" : `${x} bps`} in the hour`} />
              );
            }),
            <span key={`${n}-n`} className="hm-now num">{byLocal[hourNow] == null ? "-" : `${Math.round(byLocal[hourNow])}`}</span>,
          ];
        })}
      </div>
      <div className="hm-scale">
        <span className="muted">quiet</span><i /><span className="muted">its busiest hour</span>
        <span className="muted hm-unit">now: usual move this hour, bps</span>
      </div>
    </div>
  );
}

export default function Heat({ d, now }) {
  return (
    <section id="p-HEAT" className="panel">
      <Head code="HEAT" title="When markets move" meta={[["Data", `${d.activity?.days ?? 30} days`], ["Hours", "yours"]]} />
      <HeatBody d={d} now={now} />
    </section>
  );
}
