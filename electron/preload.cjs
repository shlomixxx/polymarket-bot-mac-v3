const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  saveSecret: (key, value) => ipcRenderer.invoke("secrets:save", key, value),
  loadSecret: (b64) => ipcRenderer.invoke("secrets:load", b64),
});
