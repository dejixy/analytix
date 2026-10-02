import { arrow, fmtPct, headlineBody, impactHelp, impactText } from "../format.js";

const ALIGN_ICON = { supports: "✓", opposes: "✕", neutral: "–" };
const ALIGN_TEXT = { supports: "supports the move", opposes: "pushed against the move", neutral: "not a factor" };

export function DriverBars({ ex, limit = 4 }) {
  return (
    <div className="drivers">
      {ex.drivers.slice(0, limit).map((d) => {
        const dir = ex.signals[d.name]?.direction || "neutral";
        const cls = d.alignment === "neutral" ? "neutral" : dir;
        return (
          <div className="driver" key={d.name} title={`${d.label}: ${ALIGN_TEXT[d.alignment]} — ${d.summary}`}>
            <span className="driver-label">{d.label}</span>
            <span className="driver-stat">{ex.signals[d.name]?.stat || "—"}</span>
            <div className="track">
              <div className={`fill ${cls}`} style={{ width: `${Math.max(2, d.strength * 100)}%` }} />
            </div>
            <span className="align-icon" aria-label={ALIGN_TEXT[d.alignment]}>{ALIGN_ICON[d.alignment]}</span>
          </div>
        );
      })}
    </div>
  );
}

export default function ExplanationCard({ ex, window, selected, onSelect }) {
  if (!ex) {
    return (
      <div className="panel card" data-window={window}>
        <div className="card-top"><span className="window-chip">{window}</span></div>
        <div className="headline muted">waiting for data…</div>
      </div>
    );
  }
  const { move } = ex;
  const dirCls = move.significance === "quiet" ? "" : move.direction;
  return (
    <button className={`panel card ${selected ? "selected" : ""}`} onClick={onSelect} aria-pressed={selected} data-window={window}>
      <div className="card-top">
        <span className="window-chip">{window}</span>
        <span className={`move num ${dirCls}`}>
          {move.significance !== "quiet" && <span aria-hidden="true">{arrow(move.direction)} </span>}
          {fmtPct(move.move_pct)}
        </span>
        <span className="sig" title={`${Math.abs(move.z).toFixed(1)}× the typical ${window} move (±${move.expected_bps.toFixed(0)} bps)`}>
          <span className={`sig-dot ${move.significance}`} />
          {move.significance.toUpperCase()} · {Math.abs(move.z).toFixed(1)}σ
        </span>
      </div>
      <div className="headline">{headlineBody(ex.headline)}</div>
      <DriverBars ex={ex} />
      <div className="card-foot">
        <span>
          {move.significance === "quiet" ? "no move to explain" : `confidence ${Math.round(ex.confidence * 100)}%`}
          {ex.shape_label && <span className="shape-tag"> · {ex.shape_label}</span>}
        </span>
        <span>
          {ex.coverage < 0.95
            ? `warming up · ${Math.round(ex.coverage * 100)}% of window`
            : ex.flow_coverage < 0.95
              ? `flow data: ${Math.round(ex.flow_coverage * 100)}% of window`
              : (
                <>
                  {ex.impact && <span className="impact" title={impactHelp(ex.impact, window)}>{impactText(ex.impact)} · </span>}
                  {`range ${(((move.high / move.low) - 1) * 100).toFixed(2)}%`}
                </>
              )}
        </span>
      </div>
    </button>
  );
}
