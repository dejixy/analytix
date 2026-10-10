import { useEffect, useRef, useState } from "react";
import { useSnapshot } from "./useSnapshot.js";
import Header from "./components/Header.jsx";
import ExplanationCard from "./components/ExplanationCard.jsx";
import PriceChart from "./components/PriceChart.jsx";
import DetailPanel from "./components/DetailPanel.jsx";
import OrderBook from "./components/OrderBook.jsx";
import TradeTape from "./components/TradeTape.jsx";
import Positioning from "./components/Positioning.jsx";
import EventLog from "./components/EventLog.jsx";
import Toasts from "./components/Toasts.jsx";
import Planner from "./components/Planner.jsx";
import AlertsPanel, { notifyEnabled } from "./components/AlertsPanel.jsx";

const FLOW_MIN_STRENGTH = 0.25;

/** Which way order flow leans on a timeframe: "up" | "down" | "neutral" (too weak or too little live data). */
function flowDir(ex) {
  const f = ex?.signals?.volume_imbalance;
  if (!f || f.strength < FLOW_MIN_STRENGTH || Math.min(ex.coverage ?? 1, ex.flow_coverage ?? 1) < 0.5) return "neutral";
  return f.direction;
}

/** One line on whether short- and longer-term flow agree, from 1m, 10m and 60m. */
function agreement(explanations) {
  const [a, b, c] = ["1m", "10m", "60m"].map((w) => flowDir(explanations[w]));
  const glyph = (d) => (d === "up" ? "▲" : "▼");
  if (a !== "neutral" && a === b && b === c) return { text: `Flow aligned ${glyph(a)} 1m–60m`, cls: a };
  if (c !== "neutral" && a !== "neutral" && b !== "neutral" && a === b && a !== c)
    return { text: `Short-term flow against the 60m ${glyph(c)}`, cls: "mixed" };
  if (b !== "neutral" && b === c && a !== "neutral" && a !== b)
    return { text: `1m pushing against 10m–60m ${glyph(b)}`, cls: "mixed" };
  return { text: "Flow mixed across timeframes", cls: "neutral" };
}

export default function App() {
  const [coin, setCoin] = useState(null); // null = the server's default coin
  const { snap, conn } = useSnapshot(coin);
  const [selected, setSelected] = useState("1m");
  const [pinned, setPinned] = useState(null); // a MoveEvent object, kept even after it scrolls out of the feed
  const [notice, setNotice] = useState(null); // a short message under the chart, e.g. "that moment is outside the data"
  const [planning, setPlanning] = useState(false);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const seenAlerts = useRef(null); // ids already shown, so only new alerts notify

  // Desktop notifications for new alerts while the dashboard sits in a background tab
  useEffect(() => {
    const list = snap?.alerts || [];
    if (seenAlerts.current === null) {
      if (snap) seenAlerts.current = new Set(list.map((a) => a.id));
      return;
    }
    const fresh = list.filter((a) => !seenAlerts.current.has(a.id));
    fresh.forEach((a) => seenAlerts.current.add(a.id));
    if (!fresh.length || !document.hidden || !notifyEnabled()) return;
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    fresh.slice(-3).forEach((a) => {
      try {
        new Notification(a.title, { body: a.lines.join("\n"), tag: a.id });
      } catch {
        /* some browsers only allow notifications from a service worker */
      }
    });
  }, [snap]);
  const railRef = useRef(null);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(timer);
  }, [notice]);

  // Esc unpins: the quickest way back to the live explanation.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") setPinned(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!snap) {
    return (
      <div className="loading">
        <div>
          <div style={{ fontWeight: 700, letterSpacing: "0.12em", marginBottom: 6 }}>ANALYTIX<span className="cursor" /></div>
          {conn === "closed" ? "Waiting for the backend on :8000…" : "Connecting…"}
        </div>
      </div>
    );
  }

  const windows = Object.entries(snap.windows); // [["1m", 60], ...]
  const agree = agreement(snap.explanations);
  const current = snap.explanations[selected];
  const detail = pinned ? pinned.explanation : current;

  // Click anywhere on the chart: explain the selected timeframe's move up to that moment, and pin it.
  const explainAt = async (t) => {
    try {
      const q = new URLSearchParams({ t: String(Math.round(t)), window: selected, coin: snap.coin });
      const r = await fetch(`/api/explain_at?${q}`);
      const d = await r.json();
      if (!r.ok) {
        setNotice(d.detail || "Nothing to explain there.");
        return;
      }
      setNotice(null);
      setPinned({ id: `at-${d.at_ms}`, window: d.window, peak_ms: d.at_ms, explanation: d.explanation, custom: true, note: d.note });
    } catch {
      setNotice("Couldn't reach the backend.");
    }
  };

  const pin = (ev) => {
    setPinned(pinned && pinned.id === ev.id ? null : ev);
    setSelected(ev.window);
  };

  // Swipe/scroll the card rail by one card; the tab strip jumps straight to a card.
  const scrollRail = (dir) => {
    const el = railRef.current;
    if (el) el.scrollBy({ left: dir * (el.firstElementChild?.offsetWidth || 300), behavior: "smooth" });
  };
  const jumpTo = (w) => {
    setPinned(null);
    setSelected(w);
    const card = railRef.current?.querySelector(`[data-window="${w}"]`);
    if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
  };

  return (
    <div className="app">
      <Header
        snap={snap}
        conn={conn}
        onCoin={(c) => {
          setPinned(null);
          setCoin(c);
        }}
        onPlan={() => setPlanning(true)}
        onAlerts={() => setAlertsOpen(true)}
      />

      <section className="rail-wrap" aria-label="Explanations by timeframe">
        <div className="rail-head">
          <nav className="tf-tabs" aria-label="Timeframe">
            {windows.map(([w]) => {
              const ex = snap.explanations[w];
              const d = flowDir(ex);
              const f = ex?.signals?.volume_imbalance;
              return (
                <button
                  key={w}
                  className={`tf-tab ${!pinned && selected === w ? "active" : ""}`}
                  onClick={() => jumpTo(w)}
                  title={f?.stat && f.stat !== "—" ? `${w} order flow: ${f.stat}` : `${w}: no flow read yet`}
                >
                  {w}
                  <span className={`tf-flow ${d}`} aria-hidden="true">{d === "up" ? "▲" : d === "down" ? "▼" : "·"}</span>
                </button>
              );
            })}
          </nav>
          <span
            className={`tf-agree ${agree.cls}`}
            title="Which way aggressive order flow leans on 1m, 10m and 60m. Aligned = every timeframe agrees; against = the short term is pushing the other way."
          >
            {agree.text}
          </span>
          <div className="rail-arrows">
            <button className="rail-arrow" onClick={() => scrollRail(-1)} aria-label="Shorter timeframes">‹</button>
            <button className="rail-arrow" onClick={() => scrollRail(1)} aria-label="Longer timeframes">›</button>
          </div>
        </div>
        <div className="cards" ref={railRef}>
          {windows.map(([w]) => (
            <ExplanationCard
              key={w}
              ex={snap.explanations[w]}
              window={w}
              selected={!pinned && selected === w}
              onSelect={() => {
                setPinned(null);
                setSelected(w);
              }}
            />
          ))}
        </div>
      </section>

      <section className="main">
        <PriceChart
          series={snap.series}
          barSeries={snap.bar_series || []}
          events={snap.events}
          levels={snap.levels || []}
          nowMs={snap.now_ms}
          tickSpanSeconds={snap.chart_span_s || 3600}
          windowSeconds={snap.windows[selected]}
          windowLabel={selected}
          pinned={pinned}
          onPick={pin}
          onUnpin={() => setPinned(null)}
          onExplainAt={explainAt}
          notice={notice}
        />
        <DetailPanel ex={detail} pinned={pinned} onUnpin={() => setPinned(null)} />
      </section>

      <section className="lower">
        <OrderBook book={snap.book} price={snap.price} walls={snap.walls} />
        <TradeTape trades={snap.trades} />
        <Positioning context={snap.context} explanations={snap.explanations} />
      </section>

      <EventLog events={snap.events} marketEvents={snap.market_events || []} pinned={pinned} onPick={pin} />
      <Toasts coin={snap.coin} nowMs={snap.now_ms} marketEvents={snap.market_events || []} />
      {alertsOpen && <AlertsPanel coin={snap.coin} onClose={() => setAlertsOpen(false)} />}
      {planning && <Planner coin={snap.coin} mid={snap.price?.mid} explanations={snap.explanations} onClose={() => setPlanning(false)} />}
    </div>
  );
}
