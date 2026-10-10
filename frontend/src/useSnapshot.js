import { useEffect, useState } from "react";

/**
 * Subscribes to the backend's /ws push for one coin. The server sends a full
 * snapshot on connect and then twice a second; we reconnect with backoff if it
 * drops. Changing `coin` opens a fresh socket for that coin.
 */
export function useSnapshot(coin) {
  const [snap, setSnap] = useState(null);
  const [conn, setConn] = useState("connecting");

  useEffect(() => {
    let ws;
    let timer;
    let retry = 0;
    let disposed = false;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const q = coin ? `?coin=${encodeURIComponent(coin)}` : "";
      ws = new WebSocket(`${proto}://${window.location.host}/ws${q}`);
      ws.onopen = () => {
        retry = 0;
        setConn("open");
      };
      // The long-window chart series only rides along every few pushes: keep the last one.
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        setSnap((prev) => (msg.bar_series || !prev || prev.coin !== msg.coin ? msg : { ...msg, bar_series: prev.bar_series }));
      };
      ws.onclose = () => {
        setConn("closed");
        if (!disposed) timer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      disposed = true;
      clearTimeout(timer);
      if (ws) ws.close();
    };
  }, [coin]);

  return { snap, conn };
}
