import { useEffect, useState } from "react";

/* News: the US releases coming up (their times are known days ahead) and the latest crypto headlines and releases,
   each with how the coins it concerns actually moved and traded around it, from the server's own minute bars.
   Free feeds run minutes behind the market: read "before" as the move that came first. */

const day = (ms) => new Date(ms).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const ago = (ms, now) => {
  const m = Math.max(0, (now - ms) / 60000);
  return m < 60 ? `${Math.round(m)} min ago` : m < 1440 ? `${(m / 60).toFixed(1)} h ago` : day(ms);
};
const until = (ms, now) => {
  const m = (ms - now) / 60000;
  return m < 60 ? `in ${Math.round(m)} min` : m < 1440 ? `in ${(m / 60).toFixed(1)} h` : `${day(ms)}, ${clock(ms)}`;
};
const bps = (x) => (x == null ? "-" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(0)} bps`);
const tone = (x) => (x == null || Math.abs(x) < 0.5 ? "" : x > 0 ? "up" : "down");
const CATEGORY = {
  fed: "Fed", inflation: "Inflation", jobs: "Jobs", growth: "Growth", etf: "ETF", regulation: "Regulation",
  hack: "Hack", listing: "Listing", unlock: "Unlock", whales: "Whales", exchange: "Exchange", macro: "Macro",
  other: "Other",
};

function Reaction({ r }) {
  const coins = Object.entries(r || {});
  if (!coins.length) return null;
  return (
    <div className="news-react">
      {coins.map(([c, x]) => (
        <span key={c} title={`${c}: ${bps(x.before_bps)} in the 10 minutes before, ${bps(x.after_bps)} in the ${x.done ? "30 minutes" : "minutes so far"} after, trading ${x.activity_x ?? "-"}× its usual pace`}>
          <b>{c}</b> <span className="muted">{bps(x.before_bps)} →</span> <span className={tone(x.after_bps)}>{bps(x.after_bps)}</span>
          {x.activity_x != null && <span className={x.activity_x >= 2 ? "" : "muted"}> · {x.activity_x}×</span>}
          {!x.done && <span className="muted"> · live</span>}
        </span>
      ))}
    </div>
  );
}

export default function News({ coins, onClose }) {
  const [coin, setCoin] = useState(null);
  const [hours, setHours] = useState(24);
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const r = await fetch(`/api/news?hours=${hours}${coin ? `&coin=${coin}` : ""}`);
        const j = await r.json();
        if (!live) return;
        if (r.ok) {
          setD(j);
          setError(null);
        } else setError(j.detail || "Couldn't read the news.");
      } catch {
        if (live) setError("Couldn't reach the server.");
      }
    };
    load();
    const t = setInterval(load, 30000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [coin, hours]);

  const down = d && Object.entries(d.status || {}).filter(([, s]) => !s.ok);
  return (
    <div className="planner-scrim" onClick={onClose}>
      <aside className="planner news" role="dialog" aria-label="News" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2 className="panel-title">News</h2>
          <button className="plan-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <section className="plan-sec">
          <div className="plan-holds" role="group" aria-label="Coin">
            {[null, ...(coins || [])].map((c) => (
              <button key={c || "all"} className={`plan-chip ${coin === c ? "on" : ""}`} onClick={() => setCoin(c)}>{c || "All"}</button>
            ))}
            <span className="news-gap" />
            {[[24, "24 h"], [72, "3 days"], [168, "7 days"]].map(([h, label]) => (
              <button key={h} className={`plan-chip ${hours === h ? "on" : ""}`} onClick={() => setHours(h)}>{label}</button>
            ))}
          </div>
        </section>
        {error && <div className="plan-error">{error}</div>}
        {!d ? (
          <p className="plan-note muted" style={{ padding: 12 }}>{error ? "" : "Loading…"}</p>
        ) : (
          <>
            {!d.enabled && <p className="plan-note muted" style={{ padding: "0 12px" }}>News is read in live mode only.</p>}
            <section className="plan-sec">
              <h3>Coming up · US releases</h3>
              {!d.upcoming.length ? (
                <p className="plan-note muted" style={{ margin: 0 }}>Nothing High or Medium impact in the next 7 days (or the calendar hasn't loaded yet).</p>
              ) : (
                <div className="news-list">
                  {d.upcoming.map((it) => (
                    <div key={it.id} className="news-item">
                      <span className="muted news-when">{until(it.t_ms, d.now_ms)}</span>
                      <span>
                        <b className={it.impact === "High" ? "" : "muted"}>{it.title}</b>
                        <span className="muted"> · {it.impact}{it.forecast ? ` · forecast ${it.forecast}` : ""}{it.previous ? ` · previous ${it.previous}` : ""}</span>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </section>
            <section className="plan-sec">
              <h3>Latest, with how price reacted</h3>
              {!d.recent.length ? (
                <p className="plan-note muted" style={{ margin: 0 }}>No news in this range yet.</p>
              ) : (
                <div className="news-list">
                  {d.recent.map((it) => (
                    <div key={it.id} className="news-item">
                      <span className="muted news-when" title={`${day(it.t_ms)} ${clock(it.t_ms)}`}>{ago(it.t_ms, d.now_ms)}</span>
                      <span>
                        {it.url ? <a href={it.url} target="_blank" rel="noreferrer">{it.title}</a> : <b>{it.title}</b>}
                        <span className="muted"> · {it.source} · {CATEGORY[it.category] || it.category}{it.coins?.length ? ` · ${it.coins.join(", ")}` : ""}</span>
                        <Reaction r={it.reaction} />
                      </span>
                    </div>
                  ))}
                </div>
              )}
              <p className="plan-note muted">
                Before → after: the move in the 10 minutes before the item and in the 30 minutes after; × is trading against its usual pace. Free feeds publish minutes after the market moves, so a big "before" means the market had it first.
              </p>
            </section>
            {down && down.length > 0 && (
              <p className="plan-note muted" style={{ padding: "0 12px 12px" }}>
                Not reachable right now: {down.map(([s]) => s).join(", ")}.
              </p>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
