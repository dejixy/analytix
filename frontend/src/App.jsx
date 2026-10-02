import { useRef, useState } from "react";
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

export default function App() {
  const [coin, setCoin] = useState(null); // null = the server's default coin
  const { snap, conn } = useSnapshot(coin);
  const [selected, setSelected] = useState("1m");
  const [pinned, setPinned] = useState(null); // a MoveEvent object, kept even after it scrolls out of the feed
  const railRef = useRef(null);

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
  const current = snap.explanations[selected];
  const detail = pinned ? pinned.explanation : current;

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
      />

      <section className="rail-wrap" aria-label="Explanations by timeframe">
        <div className="rail-head">
          <nav className="tf-tabs" aria-label="Timeframe">
            {windows.map(([w]) => (
              <button key={w} className={`tf-tab ${!pinned && selected === w ? "active" : ""}`} onClick={() => jumpTo(w)}>
                {w}
              </button>
            ))}
          </nav>
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
        />
        <DetailPanel ex={detail} pinned={pinned} onUnpin={() => setPinned(null)} />
      </section>

      <section className="lower">
        <OrderBook book={snap.book} price={snap.price} />
        <TradeTape trades={snap.trades} />
        <Positioning context={snap.context} explanations={snap.explanations} />
      </section>

      <EventLog events={snap.events} marketEvents={snap.market_events || []} pinned={pinned} onPick={pin} />
      <Toasts coin={snap.coin} marketEvents={snap.market_events || []} />
    </div>
  );
}
