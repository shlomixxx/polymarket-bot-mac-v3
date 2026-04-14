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

  const layoutDashboard =
    streamParam === "4" ||
    streamParam === "dashboard" ||
    search.get("layout") === "dashboard" ||
    path === "/stream/dashboard" ||
    path.endsWith("/stream/dashboard");

  const layoutSpectatorV2 =
    !layoutDashboard &&
    (streamParam === "5" ||
      streamParam === "spectator-v2" ||
      search.get("layout") === "spectator-v2" ||
      path === "/stream/spectator-v2" ||
      path.endsWith("/stream/spectator-v2"));

  const layoutBroadcast =
    !layoutDashboard &&
    !layoutSpectatorV2 &&
    (streamParam === "7" ||
      streamParam === "broadcast" ||
      search.get("layout") === "broadcast" ||
      path === "/stream/broadcast" ||
      path.endsWith("/stream/broadcast"));

  const layoutPro =
    !layoutDashboard &&
    !layoutSpectatorV2 &&
    !layoutBroadcast &&
    (streamParam === "6" ||
      streamParam === "pro" ||
      search.get("layout") === "pro" ||
      path === "/stream/pro" ||
      path.endsWith("/stream/pro"));

  const layoutSpectator =
    !layoutDashboard &&
    !layoutSpectatorV2 &&
    !layoutPro &&
    (streamParam === "3" ||
      streamParam === "spectator" ||
      search.get("layout") === "spectator" ||
      path === "/stream/spectator" ||
      path.endsWith("/stream/spectator"));

  const layoutShowcase =
    !layoutDashboard &&
    !layoutSpectatorV2 &&
    !layoutPro &&
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
    streamParam === "4" ||
    streamParam === "5" ||
    streamParam === "6" ||
    streamParam === "7" ||
    streamParam === "showcase" ||
    streamParam === "spectator" ||
    streamParam === "spectator-v2" ||
    streamParam === "dashboard" ||
    streamParam === "pro" ||
    streamParam === "broadcast" ||
    path === "/stream" ||
    path.startsWith("/stream/");

  const layout: StreamViewerLayout = layoutBroadcast ? "broadcast" : layoutPro ? "pro" : layoutDashboard ? "dashboard" : layoutSpectatorV2 ? "spectator-v2" : layoutSpectator ? "spectator" : layoutShowcase ? "showcase" : "classic";
  return { stream, layout };
}

const { stream, layout } = resolveStreamEntry();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {stream ? <LiveStreamTrade layout={layout} /> : <App />}
  </React.StrictMode>
);
