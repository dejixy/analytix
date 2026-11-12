import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import Terminal from "./Terminal.jsx";
import Analyzer from "./Analyzer.jsx";
import "./styles.css";

// Three pages from one build: the dashboard at /, the edge terminal at /terminal and the Analyzer at /analyzer
const path = window.location.pathname.replace(/\/+$/, "");
const page = path === "/terminal" ? <Terminal /> : path === "/analyzer" ? <Analyzer /> : <App />;

createRoot(document.getElementById("root")).render(<StrictMode>{page}</StrictMode>);
