import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import LiveStreamTrade from "./LiveStreamTrade";
import type { StreamViewerLayout } from "./streamViewerTypes";
import "./index.css";

function resolveStreamEntry(): { stream: boolean; layout: StreamViewerLayout } {
  if (typeof window === "undefined") {
    return { stream: false, layout: "classic" };
  }
  const path = (window.location.pathname || "/").replace(/\/+$/, "") || "/";
  const search = new URLSearchParams(window.location.search);
  const streamParam = search.get("stream");

  const layoutSpectator =
    streamParam === "3" ||
    streamParam === "spectator" ||
    search.get("layout") === "spectator" ||
    path === "/stream/spectator" ||
    path.endsWith("/stream/spectator");

  const layoutShowcase =
    !layoutSpectator &&
    (streamParam === "2" ||
      streamParam === "showcase" ||
      search.get("layout") === "showcase" ||
      path === "/stream/showcase" ||
      path.endsWith("/stream/showcase"));

  const stream =
    streamParam === "1" ||
    streamParam === "2" ||
    streamParam === "3" ||
    streamParam === "showcase" ||
    streamParam === "spectator" ||
    path === "/stream" ||
    path.startsWith("/stream/");

  const layout: StreamViewerLayout = layoutSpectator ? "spectator" : layoutShowcase ? "showcase" : "classic";
  return { stream, layout };
}

const { stream, layout } = resolveStreamEntry();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {stream ? <LiveStreamTrade layout={layout} /> : <App />}
  </React.StrictMode>
);
