// Jownloader — page-world hook (runs in the page's own JS world, before the page's scripts).
// Some sites sign a stream's manifest URL for ONE fetch: the page's own player takes it, and any later
// request for the same URL (ours included) is refused. So this copies the manifest text the page itself
// receives — fetch() and XMLHttpRequest, playlists (.m3u8) and MPDs only — and hands it to the extension
// (hookbridge.js), which saves from that copy instead of asking the server a second time. It changes
// nothing the page sees: responses are cloned, never consumed or altered.
(() => {
  if (window.__jownHook) return;
  window.__jownHook = true;
  const MAX = 2 * 1024 * 1024;   // a manifest is a few KB; anything bigger is not one
  const URL_RE = /\.(m3u8|mpd)(\?|#|$)/i, TYPE_RE = /mpegurl|dash\+xml/i;
  const looksLikeManifest = (t) => /^\s*#EXTM3U/.test(t) || /<MPD[\s>]/i.test(t.slice(0, 4096));
  function give(url, text) {
    try {
      if (!text || text.length > MAX || !looksLikeManifest(text)) return;
      window.postMessage({ __jownManifest: 1, url: new URL(url, location.href).href, text }, location.origin === "null" ? "*" : location.origin);
    } catch {}
  }
  const of = window.fetch;
  if (of) window.fetch = function (input, init) {
    const p = of.apply(this, arguments);
    try {
      const url = typeof input === "string" ? input : input && input.url ? input.url : String(input);
      p.then((res) => {
        try {
          if (res.ok && (URL_RE.test(url) || TYPE_RE.test(res.headers.get("content-type") || ""))) res.clone().text().then((t) => give(res.url || url, t)).catch(() => {});
        } catch {}
      }, () => {});
    } catch {}
    return p;
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    try {
      const u = String(url);
      this.addEventListener("load", () => {
        try {
          if (this.status < 200 || this.status >= 300) return;
          const rt = this.responseType, ru = this.responseURL || u;
          if (!(URL_RE.test(ru) || TYPE_RE.test(this.getResponseHeader("content-type") || ""))) return;
          if (rt === "" || rt === "text") give(ru, this.responseText);
          else if (rt === "arraybuffer" && this.response && this.response.byteLength <= MAX) give(ru, new TextDecoder().decode(this.response));
        } catch {}
      });
    } catch {}
    return open.apply(this, arguments);
  };
})();
