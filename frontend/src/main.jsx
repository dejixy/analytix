import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import Terminal from "./Terminal.jsx";
import Analyzer from "./Analyzer.jsx";
import Bets from "./Bets.jsx";
import "./styles.css";

// Four pages from one build: the dashboard at /, the edge terminal at /terminal, the Analyzer and the bets suite
const path = window.location.pathname.replace(/\/+$/, "");
const page = path === "/terminal" ? <Terminal /> : path === "/analyzer" ? <Analyzer /> : path === "/bets" ? <Bets /> : <App />;

createRoot(document.getElementById("root")).render(<StrictMode>{page}</StrictMode>);
