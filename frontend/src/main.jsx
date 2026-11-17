import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

// Four pages from one build: the dashboard at /, the edge terminal at /terminal, the Analyzer and the bets suite.
// Each page is its own download, so opening one doesn't fetch the other three.
const path = window.location.pathname.replace(/\/+$/, "");
const load = path === "/terminal" ? import("./Terminal.jsx") : path === "/analyzer" ? import("./Analyzer.jsx")
  : path === "/bets" ? import("./Bets.jsx") : import("./App.jsx");

load.then(({ default: Page }) => createRoot(document.getElementById("root")).render(<StrictMode><Page /></StrictMode>));
