// Jownloader — carries the manifest text hook.js copied in the page world to the background worker.
// Runs at document_start in every frame so nothing the page requests early is missed.
addEventListener("message", (e) => {
  const d = e.data;
  if (e.source !== window || !d || d.__jownManifest !== 1 || typeof d.url !== "string" || typeof d.text !== "string") return;
  try { chrome.runtime.sendMessage({ type: "manifestText", url: d.url, text: d.text }).catch(() => {}); } catch {}
});
