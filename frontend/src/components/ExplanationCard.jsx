import { arrow, fmtPct, headlineBody } from "../format.js";

const ALIGN_ICON = { supports: "✓", opposes: "✕", neutral: "·" };
const ALIGN_TEXT = { supports: "supports the move", opposes: "pushed against the move", neutral: "not a factor" };

export function DriverBars({ ex, limit = 4 }) {
  return (
    <div className="drivers">
      {ex.drivers.slice(0, limit).map((d) => {
        const dir = ex.signals[d.name]?.direction || "neutral";
        const cls = d.alignment === "neutral" ? "neutral" : dir;
        return (
          <div className="driver" key={d.name} title={`${d.label}: ${ALIGN_TEXT[d.alignment]}. ${d.summary}`}>
            <span className="driver-label">{d.label}</span>
            <span className="driver-stat">{ex.signals[d.name]?.stat || ""}</span>
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

// Verdicts worth catching the eye: they change how you'd trade the timeframe.
const HOT = new Set([
  "TWAP", "whale", "price held", "big reaction", "far above", "far below", "thin", "fake walls?", "one trader",
  "crowded longs", "crowded shorts", "choppy", "uptrend", "downtrend", "short squeeze", "long squeeze",
  "rose anyway", "fell anyway", "liquidations",
]);

/** The card's rows: one per metric chosen for this timeframe (engine/summary.py). Hover a row for the working. */
export function SummaryRows({ ex }) {
  const warming = ex.coverage < 0.95;
  return (
    <div className="metrics">
      {ex.summary.map((m) => {
        const partial = m.partial || warming;
        const tip =
          `${m.label}: ${m.value}${m.tag ? ` (${m.tag})` : ""}${partial ? ". Only part of the window has data so far." : ""}` +
          (m.detail ? `\n\n${m.detail}` : "") +
          `\n\nHow it's worked out: ${m.help}`;
        return (
          <div className={`metric ${partial ? "partial" : ""}`} key={m.key} title={tip}>
            <span className="metric-label">{m.label}</span>
            <span className="metric-value">{m.value}</span>
            <div className="track mtrack">
              {m.kind === "position" ? (
                <span className="pos-mark" style={{ left: `calc(${Math.min(100, Math.max(0, m.bar * 100))}% - 1px)` }} />
              ) : (
                <div className={`fill ${m.lean}`} style={{ width: `${Math.max(2, m.bar * 100)}%` }} />
              )}
            </div>
            <span className={`metric-tag ${m.lean} ${HOT.has(m.tag) ? "hot" : ""}`}>{m.tag}</span>
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
        <span className="sig" title={`This move is ${Math.abs(move.z).toFixed(1)}× the size of a normal ${window} move (about ±${(move.expected_bps / 100).toFixed(2)}%)`}>
          <span className={`sig-dot ${move.significance}`} />
          {move.significance.toUpperCase()} · {Math.abs(move.z).toFixed(1)}×
        </span>
      </div>
      <div className="headline">{headlineBody(ex.headline)}</div>
      {ex.summary?.length ? <SummaryRows ex={ex} /> : <DriverBars ex={ex} />}
      <div className="card-foot">
        <span>
          {move.significance === "quiet" ? "no move to explain" : `confidence ${Math.round(ex.confidence * 100)}%`}
          {ex.shape_label && <span className="shape-tag"> · {ex.shape_label}</span>}
        </span>
        <span>
          {ex.coverage < 0.95
            ? `warming up · ${Math.round(ex.coverage * 100)}% of window`
            : ex.flow_coverage < 0.95
              ? `trade data: ${Math.round(ex.flow_coverage * 100)}% of window`
              : (
                <span
                  className="range-note"
                  title="The window's high-to-low range, compared with the usual range for this timeframe (from the last week of price history)."
                >
                  range {(((move.high / move.low) - 1) * 100).toFixed(2)}%
                  {ex.range_ratio != null && ` · ${ex.range_ratio.toFixed(1)}× usual`}
                </span>
              )}
        </span>
      </div>
    </button>
  );
}
