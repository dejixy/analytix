import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

// Five pages from one build: the dashboard at /, the edge terminal at /terminal, the Analyzer, the bets suite and
// Sentiment Check.
// Each page is its own download, so opening one doesn't fetch the others.
const path = window.location.pathname.replace(/\/+$/, "");
const load = path === "/terminal" ? import("./Terminal.jsx") : path === "/analyzer" ? import("./Analyzer.jsx")
  : path === "/bets" ? import("./Bets.jsx") : path === "/sentiment" ? import("./Sentiment.jsx") : import("./App.jsx");

load.then(({ default: Page }) => createRoot(document.getElementById("root")).render(<StrictMode><Page /></StrictMode>));
