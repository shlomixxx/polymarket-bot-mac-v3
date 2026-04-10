import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import LiveStreamTrade, { type StreamViewerLayout } from "./LiveStreamTrade";
import "./index.css";

function resolveStreamEntry(): { stream: boolean; layout: StreamViewerLayout } {
  if (typeof window === "undefined") {
    return { stream: false, layout: "classic" };
  }
  const path = (window.location.pathname || "/").replace(/\/+$/, "") || "/";
  const search = new URLSearchParams(window.location.search);
  const streamParam = search.get("stream");

  const layoutShowcase =
    streamParam === "2" ||
    streamParam === "showcase" ||
    search.get("layout") === "showcase" ||
    path === "/stream/showcase" ||
    path.endsWith("/stream/showcase");

  const stream =
    streamParam === "1" ||
    streamParam === "2" ||
    streamParam === "showcase" ||
    path === "/stream" ||
    path.startsWith("/stream/");

  return { stream, layout: layoutShowcase ? "showcase" : "classic" };
}

const { stream, layout } = resolveStreamEntry();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {stream ? <LiveStreamTrade layout={layout} /> : <App />}
  </React.StrictMode>
);
