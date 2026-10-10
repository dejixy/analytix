import { arrow, fmtPct, fmtTime, headlineBody } from "../format.js";

const KIND_CHIP = { level_break: "LEVEL", cascade: "CASCADE" };

export default function EventLog({ events, marketEvents = [], pinned, onPick }) {
  const rows = [
    ...events.map((ev) => ({ kind: "move", ts: ev.peak_ms, ev })),
    ...marketEvents.map((ev) => ({ kind: ev.kind, ts: ev.ts, ev })),
  ].sort((a, b) => b.ts - a.ts);

  return (
    <section className="panel" aria-label="Event log">
      <div className="panel-head" style={{ paddingBottom: 10 }}>
        <h2 className="panel-title">Event log</h2>
        <span className="panel-sub">big moves, broken levels, cascades · click a move to see why it happened</span>
      </div>
      {rows.length === 0 ? (
        <div className="empty">Nothing yet. The log fills when price moves 2.5× more than normal, a price that buyers or sellers defended breaks, or a run of big orders hits.</div>
      ) : (
        <div className="events">
          {rows.map(({ kind, ev }) =>
            kind === "move" ? (
              <MoveRow key={`m${ev.id}`} ev={ev} pinned={pinned} onPick={onPick} />
            ) : (
              <div key={ev.id} className="event static">
                <span className="num muted">{fmtTime(ev.ts)}</span>
                <span className="chip" style={{ justifySelf: "start" }}>{KIND_CHIP[ev.kind] || ev.kind.toUpperCase()}</span>
                <span className={`num ${ev.direction}`} style={{ fontWeight: 600 }}>{arrow(ev.direction)} {ev.stat || ev.window}</span>
                <span className="event-head">
                  {ev.title}
                  {ev.detail && <span className="muted"> · {ev.detail}</span>}
                </span>
                <span />
              </div>
            ),
          )}
        </div>
      )}
    </section>
  );
}

function MoveRow({ ev, pinned, onPick }) {
  const ex = ev.explanation;
  const dir = ex.move.direction;
  return (
    <button className={`event ${pinned && pinned.id === ev.id ? "pinned" : ""}`} onClick={() => onPick(ev)}>
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
}
