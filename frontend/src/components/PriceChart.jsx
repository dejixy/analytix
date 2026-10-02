import { useEffect, useMemo, useRef, useState } from "react";
import { arrow, fmtPct, fmtPrice, fmtTime, fmtUsd, headlineBody } from "../format.js";

const HEIGHT = 420;
const M = { top: 12, right: 78, bottom: 26, left: 10 };
const TICK_MINUTES = [1, 2, 3, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440];
const SNAP_PX = 28; // the cursor snaps to a move marker within this distance — no need to hit the dot exactly

function tickLabel(t, tickMs) {
  const d = new Date(t);
  if (tickMs >= 86_400_000) return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
  if (tickMs >= 6 * 3_600_000) return d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", hour12: false });
  return fmtTime(t).slice(0, 5);
}

function niceStep(range, target = 5) {
  const raw = range / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n >= 5 ? 10 : n >= 2 ? 5 : n >= 1 ? 2 : 1) * mag;
}

function nearest(points, t) {
  let lo = 0;
  let hi = points.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] < t) lo = mid;
    else hi = mid;
  }
  return Math.abs(points[lo][0] - t) <= Math.abs(points[hi][0] - t) ? points[lo] : points[hi];
}

export default function PriceChart({ series, barSeries, events, levels = [], nowMs, tickSpanSeconds, windowSeconds, windowLabel, pinned, onPick, onUnpin }) {
  // ≤60m windows: tick-level mids over the last hour. Longer windows: minute-bar closes,
  // spanning 1.5× the window so the shaded window sits in some context.
  const long = windowSeconds > tickSpanSeconds;
  const SPAN_MS = (long ? Math.min(windowSeconds * 1.5, 8 * 86_400) : tickSpanSeconds) * 1000;
  const source = long ? barSeries : series;
  const wrapRef = useRef(null);
  const [width, setWidth] = useState(800);
  const [hover, setHover] = useState(null);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(320, entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const x0 = nowMs - SPAN_MS;
  const pts = useMemo(() => source.filter(([t]) => t >= x0), [source, x0]);

  const geo = useMemo(() => {
    if (pts.length < 2) return null;
    let lo = Infinity;
    let hi = -Infinity;
    for (const [, p] of pts) {
      lo = Math.min(lo, p);
      hi = Math.max(hi, p);
    }
    const pad = Math.max((hi - lo) * 0.1, hi * 0.0002);
    lo -= pad;
    hi += pad;
    const iw = width - M.left - M.right;
    const ih = HEIGHT - M.top - M.bottom;
    const xs = (t) => M.left + ((t - x0) / SPAN_MS) * iw;
    const ys = (p) => M.top + (1 - (p - lo) / (hi - lo)) * ih;
    const step = niceStep(hi - lo);
    const yTicks = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) yTicks.push(v);
    const xTicks = [];
    const tickMs = (TICK_MINUTES.find((m) => m * 60000 * 6 >= SPAN_MS) || 60) * 60000;
    for (let t = Math.ceil(x0 / tickMs) * tickMs; t <= nowMs; t += tickMs) xTicks.push(t);
    const xLabel = (t) => tickLabel(t, tickMs);
    const d = pts.map(([t, p], i) => `${i ? "L" : "M"}${xs(t).toFixed(1)},${ys(p).toFixed(1)}`).join("");
    return { xs, ys, yTicks, xTicks, xLabel, d, iw, ih, stepDigits: step < 1 ? 2 : step < 10 ? 1 : 0 };
  }, [pts, width, x0, nowMs, SPAN_MS]);

  const shownEvents = events.filter((e) => e.window === windowLabel && e.peak_ms >= x0);
  if (pinned && pinned.peak_ms >= x0 && !shownEvents.some((e) => e.id === pinned.id)) shownEvents.push(pinned);

  const markers = geo
    ? shownEvents.map((ev) => {
        const p = nearest(pts, ev.peak_ms);
        return { ev, x: geo.xs(p[0]), y: geo.ys(p[1]) };
      })
    : [];

  const onMove = (e) => {
    if (!geo) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = M.left + (e.clientX - rect.left);
    const my = M.top + (e.clientY - rect.top);
    let snap = null;
    let best = SNAP_PX;
    for (const m of markers) {
      const d = Math.hypot(m.x - mx, m.y - my);
      if (d <= best) {
        best = d;
        snap = m.ev;
      }
    }
    if (snap) {
      const p = nearest(pts, snap.peak_ms);
      setHover({ t: p[0], p: p[1], marker: snap });
      return;
    }
    const t = x0 + ((mx - M.left) / geo.iw) * SPAN_MS;
    const p = nearest(pts, t);
    setHover({ t: p[0], p: p[1], marker: null });
  };
  const onClick = () => {
    if (hover?.marker) onPick(hover.marker);
  };

  const winStart = nowMs - windowSeconds * 1000;
  const startPx = geo && pts.length ? nearest(pts, winStart)[1] : null;
  const last = pts[pts.length - 1];

  return (
    <div className="panel">
      <div className="panel-head">
        <h2 className="panel-title">Mid price · {long ? `${windowLabel} view` : `${Math.round(tickSpanSeconds / 60)}m`}</h2>
        {pinned ? (
          <span className="pinned-bar">
            <span className="chip">PINNED</span>
            <span className="num">{fmtTime(pinned.peak_ms)} · {pinned.window} {arrow(pinned.explanation.move.direction)} {fmtPct(pinned.explanation.move.move_pct)}</span>
            <button className="unpin-btn" onClick={onUnpin} title="Back to the live explanation (Esc)">✕ Unpin</button>
          </span>
        ) : (
          <span className="panel-sub">shaded: {windowLabel} window · markers: {windowLabel} significant moves</span>
        )}
      </div>
      <div className="chart-wrap" ref={wrapRef}>
        {!geo ? (
          <div className="empty">{long ? "Loading long-window history…" : "Collecting price history…"}</div>
        ) : (
          <svg height={HEIGHT} role="img" aria-label={`Mid price over the last 15 minutes, last ${fmtPrice(last?.[1])}`}>
            {/* selected window */}
            <rect x={geo.xs(winStart)} y={M.top} width={geo.xs(nowMs) - geo.xs(winStart)} height={geo.ih} fill="var(--surface-2)" />
            {startPx != null && (
              <line x1={geo.xs(winStart)} x2={geo.xs(nowMs)} y1={geo.ys(startPx)} y2={geo.ys(startPx)} stroke="var(--axis)" strokeWidth="1" />
            )}
            {/* grid + axes */}
            {geo.yTicks.map((v) => (
              <g key={v}>
                <line x1={M.left} x2={width - M.right} y1={geo.ys(v)} y2={geo.ys(v)} stroke="var(--grid)" strokeWidth="1" />
                <text x={width - M.right + 8} y={geo.ys(v) + 4} fontSize="11" fill="var(--muted)" className="num">
                  {v.toFixed(geo.stepDigits)}
                </text>
              </g>
            ))}
            {geo.xTicks.map((t) => (
              <text key={t} x={geo.xs(t)} y={HEIGHT - 8} fontSize="11" fill="var(--muted)" textAnchor="middle" className="num">
                {geo.xLabel(t)}
              </text>
            ))}
            <line x1={M.left} x2={width - M.right} y1={M.top + geo.ih} y2={M.top + geo.ih} stroke="var(--axis)" strokeWidth="1" />

            {/* defended levels: where passive orders absorbed the aggression and price held */}
            {levels
              .filter((lv) => geo.ys(lv.price) >= M.top && geo.ys(lv.price) <= M.top + geo.ih)
              .map((lv) => {
                const y = geo.ys(lv.price);
                const color = lv.side === "bid" ? "var(--up)" : "var(--grey-bar)";
                return (
                  <g key={`${lv.side}-${lv.price}`} pointerEvents="none">
                    <line x1={M.left} x2={width - M.right} y1={y} y2={y} stroke={color} strokeWidth="1" strokeDasharray="5 4" opacity="0.8" />
                    <text x={M.left + 6} y={lv.side === "bid" ? y + 13 : y - 5} fontSize="10.5" fill={color} className="num">
                      {lv.side === "bid" ? "bids held" : "offers held"} {fmtPrice(lv.price)} · {fmtUsd(lv.absorbed)} absorbed
                    </text>
                  </g>
                );
              })}

            {/* price */}
            <path d={geo.d} fill="none" stroke="var(--ink-2)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
            {last && (
              <g>
                <circle cx={geo.xs(last[0])} cy={geo.ys(last[1])} r="4" fill="var(--up-bright)" stroke="var(--surface)" strokeWidth="2" />
                <rect x={width - M.right + 2} y={geo.ys(last[1]) - 10} width={M.right - 4} height="20" rx="2" fill="var(--up-deep)" stroke="var(--up)" strokeWidth="1" />
                <text x={width - M.right + M.right / 2} y={geo.ys(last[1]) + 4} fontSize="10.5" fontWeight="700" fill="var(--ink)" textAnchor="middle" className="num">
                  {fmtPrice(last[1])}
                </text>
              </g>
            )}

            {/* pinned move: a guide line so it's obvious which marker is pinned */}
            {markers.filter((m) => pinned && m.ev.id === pinned.id).map((m) => (
              <line key="pin-guide" x1={m.x} x2={m.x} y1={M.top} y2={M.top + geo.ih} stroke="var(--up)" strokeWidth="1" strokeDasharray="3 3" opacity="0.7" pointerEvents="none" />
            ))}

            {/* significant-move markers: drawn under the hover layer; the layer snaps to the nearest one */}
            {markers.map(({ ev, x, y }) => {
              const dir = ev.explanation.move.direction;
              const isPinned = pinned && pinned.id === ev.id;
              const isHot = hover?.marker?.id === ev.id;
              return (
                <g key={ev.id} pointerEvents="none">
                  {(isPinned || isHot) && <circle cx={x} cy={y} r={isHot ? 11 : 9} fill="none" stroke={isPinned ? "var(--up-bright)" : "var(--ink-2)"} strokeWidth="2" />}
                  <circle cx={x} cy={y} r={isHot ? 7 : 5.5} fill={dir === "down" ? "var(--down)" : "var(--up)"} stroke="var(--surface)" strokeWidth="2" />
                </g>
              );
            })}

            {/* hover layer: crosshair, marker snapping and clicks */}
            {hover && !hover.marker && (
              <g pointerEvents="none">
                <line x1={geo.xs(hover.t)} x2={geo.xs(hover.t)} y1={M.top} y2={M.top + geo.ih} stroke="var(--axis)" strokeWidth="1" />
                <circle cx={geo.xs(hover.t)} cy={geo.ys(hover.p)} r="4" fill="var(--ink-2)" stroke="var(--surface)" strokeWidth="2" />
              </g>
            )}
            <rect
              x={M.left} y={M.top} width={geo.iw} height={geo.ih} fill="transparent"
              style={{ cursor: hover?.marker ? "pointer" : "crosshair" }}
              onMouseMove={onMove} onMouseLeave={() => setHover(null)} onClick={onClick}
            />
          </svg>
        )}
        {hover && geo && hover.marker && (
          <div className={`tooltip marker-tip ${geo.ys(hover.p) < 150 ? "below" : ""}`} style={{ left: geo.xs(hover.t), top: geo.ys(hover.p) }}>
            <div className="num" style={{ fontWeight: 600 }}>
              {fmtTime(hover.marker.peak_ms)} · {hover.marker.window} {arrow(hover.marker.explanation.move.direction)}{" "}
              {fmtPct(hover.marker.explanation.move.move_pct)}
            </div>
            <div className="marker-tip-body">{headlineBody(hover.marker.explanation.headline)}</div>
            <div className="muted">{pinned && pinned.id === hover.marker.id ? "click to unpin" : "click to pin its explanation"}</div>
          </div>
        )}
        {hover && geo && !hover.marker && (
          <div className="tooltip" style={{ left: geo.xs(hover.t), top: geo.ys(hover.p) }}>
            <div className="num" style={{ fontWeight: 600 }}>{fmtPrice(hover.p)}</div>
            <div className="muted num">
              {long ? new Date(hover.t).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : fmtTime(hover.t)}
              {" · "}{fmtPct((last[1] / hover.p - 1) * 100)} since
            </div>
          </div>
        )}
      </div>
      <div className="legend-row">
        <span className="legend-item"><span className="dot" style={{ background: "var(--up)" }} /> up move</span>
        <span className="legend-item"><span className="dot" style={{ background: "var(--down)" }} /> down move</span>
        {levels.length > 0 && (
          <span className="legend-item"><span className="dash" /> defended level (breaks are logged below)</span>
        )}
        <span className="legend-item muted">{pinned ? "click the pinned marker, ✕ Unpin or Esc to go back to live" : "hover near a marker and click to pin its explanation"}</span>
      </div>
    </div>
  );
}
