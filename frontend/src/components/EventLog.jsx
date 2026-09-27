import { arrow, fmtPct, fmtTime, headlineBody } from "../format.js";

export default function EventLog({ events, pinned, onPick }) {
  return (
    <section className="panel" aria-label="Significant moves">
      <div className="panel-head" style={{ paddingBottom: 10 }}>
        <h2 className="panel-title">Significant moves</h2>
        <span className="panel-sub">captured at their peak · click to pin the explanation</span>
      </div>
      {events.length === 0 ? (
        <div className="empty">Nothing significant yet — the log fills when a move exceeds 2.5× its normal size.</div>
      ) : (
        <div className="events">
          {events.map((ev) => {
            const ex = ev.explanation;
            const dir = ex.move.direction;
            return (
              <button
                key={ev.id}
                className={`event ${pinned && pinned.id === ev.id ? "pinned" : ""}`}
                onClick={() => onPick(ev)}
              >
                <span className="num muted">{fmtTime(ev.peak_ms)}</span>
                <span className="window-chip" style={{ justifySelf: "start" }}>{ev.window}</span>
                <span className={`num ${dir}`} style={{ fontWeight: 600 }}>
                  {arrow(dir)} {fmtPct(ex.move.move_pct)}
                </span>
                <span className="event-head">
                  {headlineBody(ex.headline)}
                  {ev.active && <span className="chip" style={{ marginLeft: 8 }}>ONGOING</span>}
                </span>
                <span className="num muted event-conf" style={{ textAlign: "right" }}>{Math.round(ex.confidence * 100)}%</span>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}
