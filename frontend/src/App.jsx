import { useState } from "react";
import { useSnapshot } from "./useSnapshot.js";
import Header from "./components/Header.jsx";
import ExplanationCard from "./components/ExplanationCard.jsx";
import PriceChart from "./components/PriceChart.jsx";
import DetailPanel from "./components/DetailPanel.jsx";
import OrderBook from "./components/OrderBook.jsx";
import TradeTape from "./components/TradeTape.jsx";
import Positioning from "./components/Positioning.jsx";
import EventLog from "./components/EventLog.jsx";

export default function App() {
  const [coin, setCoin] = useState(null); // null = the server's default coin
  const { snap, conn } = useSnapshot(coin);
  const [selected, setSelected] = useState("1m");
  const [pinned, setPinned] = useState(null); // a MoveEvent object, kept even after it scrolls out of the feed

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

      <section className="cards" aria-label="Explanations by timeframe">
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
      </section>

      <section className="main">
        <PriceChart
          series={snap.series}
          events={snap.events}
          nowMs={snap.now_ms}
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

      <EventLog events={snap.events} pinned={pinned} onPick={pin} />
    </div>
  );
}
