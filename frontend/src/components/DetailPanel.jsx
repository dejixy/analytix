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
            <span className="chip">PINNED</span>
            {pinned.custom ? `${pinned.window} up to ${fmtTime(pinned.peak_ms)}` : `Significant move at ${fmtTime(pinned.peak_ms)}`}
            <button className="unpin-btn" onClick={onUnpin} title="Back to the live explanation (Esc)">✕ Unpin</button>
          </div>
        )}
        {pinned?.note && <p className="pin-fallback muted">{pinned.note}</p>}
        <p className="detail-headline">{ex.headline}</p>
        <ul className="narrative">
          {ex.narrative.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
        {ex.summary?.length > 0 && (
          <div className="snapshot">
            <div className="snapshot-head">At a glance · {ex.window}</div>
            {ex.summary.map((m) => (
              <div className={`snap-row ${m.partial || ex.coverage < 0.95 ? "partial" : ""}`} key={m.key}>
                <div className="snap-line">
                  <span className="snap-label" title={m.help}>{m.label}</span>
                  <span className="snap-value">{m.value}</span>
                  <span className={`metric-tag ${m.lean}`}>{m.tag}</span>
                </div>
                {m.detail && <div className="signal-summary">{m.detail}</div>}
              </div>
            ))}
          </div>
        )}
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
