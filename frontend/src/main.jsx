import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import Terminal from "./Terminal.jsx";
import "./styles.css";

// Two pages from one build: the dashboard at / and the edge terminal at /terminal
const page = window.location.pathname.replace(/\/+$/, "") === "/terminal" ? <Terminal /> : <App />;

createRoot(document.getElementById("root")).render(<StrictMode>{page}</StrictMode>);
