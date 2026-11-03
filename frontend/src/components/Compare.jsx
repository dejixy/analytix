import { useEffect, useRef, useState } from "react";

/* The shared pieces of the terminal's lower panels: the live orders (for the world map's flow rings), a box that
   reports its real width, and the Links grid (which markets moved together, from hourly moves over the week). */

const SHORT = { CL: "WTI", BRENTOIL: "BRENT", JPY: "USD/JPY", EUR: "EUR/USD", DXY: "DXY", GBP: "GBP/USD" };
const tk = (name) => name.split(":").pop();
const label = (name) => SHORT[tk(name)] || tk(name);
const POLL_MS = 2000;

/* The orders, kept in the page: the last hour on open, then whatever is new every 2 seconds. */
export function useOrders(enabled) {
  const [orders, setOrders] = useState([]);
  const [info, setInfo] = useState(null);
  const last = useRef(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let live = true;
    let timer;
    const load = async () => {
      try {
        const r = await fetch(`/api/edges/trades${last.current ? `?since=${last.current}` : ""}`);
        const j = await r.json();
        if (!live) return;
        if (r.ok) {
          setInfo(j);
          if (j.orders.length) last.current = j.orders.at(-1).t;
          setOrders((old) => {
            const seen = new Set(old.map((o) => o.id));
            const keep = old.filter((o) => j.now_ms - o.t <= j.keep_ms);
            return [...keep, ...j.orders.filter((o) => !seen.has(o.id)).map((o) => ({ ...o, seen_ms: Date.now() }))];
          });
        }
      } catch {
        /* the next poll tries again */
      }
      if (live) timer = setTimeout(load, POLL_MS);          // after the reply, never on top of it
    };
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [enabled]);
  return { orders, info };
}

/* Draws at the box's real width (one unit, one pixel), so text and bubbles keep their size on any screen. */
export function Measured({ children }) {
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

export function Links({ c }) {
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
