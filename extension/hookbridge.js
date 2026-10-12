// Jownloader — carries the manifest text hook.js copied in the page world to the background worker.
// Runs at document_start in every frame (about:blank / srcdoc included) so nothing the page requests early
// is missed. The text arrives over the private MessageChannel hook.js hands over once, as the first message
// of the window; a later window message (a page forging one) is ignored. The background still treats every
// copy as untrusted: it binds it to this tab and checks it against requests the tab made.
(() => {
  let taken = false;
  const MAX = 2 * 1024 * 1024;
  addEventListener("message", (e) => {
    if (taken || e.source !== window || !e.data || e.data.__jownInit !== 1 || !e.ports || e.ports.length !== 1) return;
    taken = true;
    e.ports[0].onmessage = (m) => {
      const d = m.data;
      if (!d || typeof d.url !== "string" || typeof d.text !== "string" || d.text.length > MAX) return;
      try { chrome.runtime.sendMessage({ type: "manifestText", url: d.url, text: d.text }).catch(() => {}); } catch {}
    };
  });
})();
