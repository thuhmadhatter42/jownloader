// Jownloader — page-world hook (runs in the page's own JS world, before the page's scripts).
// Some sites sign a stream's manifest URL for ONE fetch: the page's own player takes it, and any later
// request for the same URL (ours included) is refused. So this copies the manifest text the page itself
// receives — fetch() and XMLHttpRequest, playlists (.m3u8) and MPDs only — and hands it to the extension
// (hookbridge.js), which saves from that copy instead of asking the server a second time. Responses are
// cloned, never consumed or altered; the wrapped functions are proxies of the originals (same name, length
// and native toString), a failing fetch still raises exactly one unhandledrejection, and the text travels
// over a private MessageChannel — the page's window never receives a message from us after the single
// port hand-off at document start.
(() => {
  if (Object.prototype.hasOwnProperty.call(window, "__jownHook")) return;
  try { Object.defineProperty(window, "__jownHook", { value: true }); } catch { return; }
  const MAX = 2 * 1024 * 1024;   // a manifest is a few KB; anything bigger is not one
  const URL_RE = /\.(m3u8|mpd)(\?|#|$)/i, TYPE_RE = /mpegurl|dash\+xml/i;
  const looksLikeManifest = (t) => /^\s*#EXTM3U/.test(t) || /<MPD[\s>]/i.test(t.slice(0, 4096));
  // the bridge (isolated world) gets port2 once, before any page script has a listener; it keeps the first
  // port it is handed. A sandboxed frame has an opaque origin, so the target is "*" (the message is our own).
  let port = null;
  try {
    const ch = new MessageChannel();
    port = ch.port1;
    window.postMessage({ __jownInit: 1 }, "*", [ch.port2]);
  } catch {}
  function give(url, text) {
    try {
      if (!port || !text || text.length > MAX || !looksLikeManifest(text)) return;
      port.postMessage({ url: new URL(url, location.href).href, text });
    } catch {}
  }
  const of = window.fetch;
  if (of) window.fetch = new Proxy(of, {
    apply(target, thisArg, args) {
      const p = Reflect.apply(target, thisArg, args);
      try {
        const input = args[0];
        const url = typeof input === "string" ? input : input instanceof URL ? input.href : input instanceof Request ? input.url : "";
        // derived promise returned to the page: a rejection reaches the page's handlers exactly as before
        return p.then((res) => {
          try {
            if (res.ok && (URL_RE.test(res.url || url) || TYPE_RE.test(res.headers.get("content-type") || ""))) res.clone().text().then((t) => give(res.url || url, t)).catch(() => {});
          } catch {}
          return res;
        });
      } catch {}
      return p;
    },
  });
  const urls = new WeakMap();   // xhr -> url of its latest open(); one load listener per object
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = new Proxy(open, {
    apply(target, thisArg, args) {
      try {
        const first = !urls.has(thisArg);
        urls.set(thisArg, String(args[1]));
        if (first) thisArg.addEventListener("load", function () {
          try {
            if (this.status < 200 || this.status >= 300) return;
            const rt = this.responseType, ru = this.responseURL || urls.get(this) || "";
            if (!(URL_RE.test(ru) || TYPE_RE.test(this.getResponseHeader("content-type") || ""))) return;
            if (rt === "" || rt === "text") give(ru, this.responseText);
            else if (rt === "arraybuffer" && this.response && this.response.byteLength <= MAX) give(ru, new TextDecoder().decode(this.response));
          } catch {}
        });
      } catch {}
      return Reflect.apply(target, thisArg, args);
    },
  });
})();
