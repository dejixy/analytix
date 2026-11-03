/* The terminal's panel chrome, the same on every panel: a header bar with the panel's command code, its title, what
   the data is and how fresh (key / value segments), and status pills. Colour carries meaning: green and red for
   direction, cyan for system state (live, active, selected), grey for everything else. */

export function Pill({ kind = "mute", children, title }) {
  return (
    <span className={`tp-pill ${kind}`} title={title}>
      {kind === "live" && <i aria-hidden="true" />}
      {children}
    </span>
  );
}

export function Head({ code, title, meta = [], pills = [], children }) {
  return (
    <div className="panel-head tp-head">
      <h2 className="panel-title"><span className="term-code">{code}</span>{title}</h2>
      {meta.filter(Boolean).length > 0 && (
        <dl className="tp-meta">
          {meta.filter(Boolean).map(([k, v]) => (
            <div key={k} className="tp-seg"><dt>{k}</dt><dd>{v}</dd></div>
          ))}
        </dl>
      )}
      {children}
      {pills.filter(Boolean).length > 0 && <span className="tp-pills">{pills.filter(Boolean)}</span>}
    </div>
  );
}

export const age = (ms) => (ms == null ? "-" : ms < 60_000 ? `${Math.max(0, Math.round(ms / 1000))}s` : ms < 3_600_000 ? `${Math.round(ms / 60000)}m` : `${(ms / 3_600_000).toFixed(1)}h`);

/** How fresh a source is: {age, state: "live" | "stale" | "down" | "none"}, against how often it should update. */
export function fresh(d, src, now, okWithin) {
  const s = d?.status?.[src];
  if (!s) return { age: "-", state: "none" };
  if (!s.ok) return { age: s.last_ms ? age(now - s.last_ms) : "-", state: "down", error: s.error };
  const a = s.last_ms ? now - s.last_ms : null;
  return { age: age(a), state: a != null && a <= okWithin ? "live" : "stale" };
}

/** The status pill for a source: LIVE, STALE or DOWN. */
export function SourcePill({ f, label = "Live" }) {
  if (f.state === "live") return <Pill kind="live">{label}</Pill>;
  if (f.state === "down") return <Pill kind="bad" title={f.error}>Down</Pill>;
  if (f.state === "stale") return <Pill kind="warn">Stale</Pill>;
  return <Pill>Waiting</Pill>;
}
