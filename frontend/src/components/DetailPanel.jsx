import { fmtTime } from "../format.js";

function Gauge({ score }) {
  const w = Math.min(1, Math.abs(score)) * 50;
  const up = score >= 0;
  return (
    <div className="gauge" role="img" aria-label={`score ${score.toFixed(2)}`}>
      <div className="gauge-mid" />
      {w > 0.5 && (
        <div className={`gauge-fill ${up ? "up" : "down"}`} style={up ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }} />
      )}
    </div>
  );
}

function OutlookLine({ o }) {
  // "Next 1m: coin flip · tilt up 51.7% · normal swing ±0.08% — why" → the call, its details, the reasons
  const [head, ...rest] = o.line.split(" — ");
  const [call, ...details] = head.split(" · ");
  const flip = o.lean === "neutral";
  const callHelp = flip
    ? "Coin flip: the signals don't agree strongly enough to call a direction. " +
      "The tilt is which way they faintly point, with the model's odds — too close to 50% to act on."
    : o.odds_source === "earned"
      ? "Lean: the signals strongly agree. The odds are earned — how often this window's past leans were actually right."
      : "Lean: the signals strongly agree. The odds are the model's estimate until enough past leans have been scored.";
  const trackHelp =
    "Leans are sampled several times per period, so neighbouring samples share most of their price action. " +
    "The count is how many separate periods they cover, and the hit rate stays hidden until there are enough. " +
    "Inside range: how often the price ended within the stated normal swing (about 68% if the range is honest).";
  return (
    <div className={`outlook ${flip ? "flip" : ""}`} title="Heuristic estimate from the current book, flow, sweeps, momentum and funding. Not financial advice.">
      <span className={`outlook-lean ${o.lean}`}>{o.lean === "up" ? "▲" : o.lean === "down" ? "▼" : "•"}</span>
      <span>
        <span className="outlook-head" title={callHelp}>{call}</span>
        {details.map((d, i) => (
          <span key={i} className={d === "no tilt" || d.startsWith("tilt ") ? "outlook-tilt" : ""}> · {d}</span>
        ))}
        {rest.length > 0 && <span className="ink2"> — {rest.join(" — ")}</span>}
        {o.track && <span className="muted" title={trackHelp}> · {o.track}</span>}
      </span>
    </div>
  );
}

export default function DetailPanel({ ex, pinned, onUnpin }) {
  if (!ex) {
    return (
      <div className="panel">
        <div className="panel-head"><h2 className="panel-title">Why</h2></div>
        <div className="empty">The engine needs a few seconds of data.</div>
      </div>
    );
  }
  const order = ex.drivers.map((d) => d.name);
  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Why · {ex.window}</h2>
        <span className="panel-sub num">
          {fmtTime(ex.start_ms)} → {fmtTime(ex.end_ms)}
        </span>
      </div>
      <div className="panel-body">
        {pinned && (
          <div className="pin-note">
            <span className="chip">PINNED</span> Significant move at {fmtTime(pinned.peak_ms)}
            <button className="link-btn" onClick={onUnpin}>back to live</button>
          </div>
        )}
        <p className="detail-headline">{ex.headline}</p>
        {ex.outlook && <OutlookLine o={ex.outlook} />}
        <ul className="narrative">
          {ex.narrative.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
        <div>
          {order.map((name) => {
            const s = ex.signals[name];
            if (!s) return null;
            return (
              <div className="signal" key={name}>
                <div className="signal-row">
                  <span className="signal-name">{s.label}</span>
                  <Gauge score={s.score} />
                  <span className="signal-score num">{s.score >= 0 ? "+" : "−"}{Math.abs(s.score).toFixed(2)}</span>
                </div>
                <div className="signal-summary">{s.summary}</div>
              </div>
            );
          })}
          <div className="gauge-scale">
            <span>▼ pushes down</span>
            <span>pushes up ▲</span>
          </div>
        </div>
      </div>
    </div>
  );
}
