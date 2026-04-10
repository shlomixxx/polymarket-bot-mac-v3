import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import LiveStreamTrade from "./LiveStreamTrade";
import "./index.css";

const stream =
  typeof window !== "undefined" &&
  (new URLSearchParams(window.location.search).get("stream") === "1" ||
    window.location.pathname.endsWith("/stream"));

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>{stream ? <LiveStreamTrade /> : <App />}</React.StrictMode>
);
