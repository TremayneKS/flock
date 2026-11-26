// Runs in every page the harness's browser opens (Playwright MCP --init-script). It changes nothing on the page: it only
// remembers the text drawn on each <canvas> so window.__kchCanvas() can report what a
// chart actually draws (labels, NaN, or nothing at all) alongside DOM snapshots. Text in SVG and HTML is already in the page snapshot.
(() => {
  if (window.__kchCanvas) return;
  const drawn = new WeakMap();
  const P = window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
  if (P) {
    for (const name of ['fillText', 'strokeText']) {
      const orig = P[name];
      if (typeof orig !== 'function') continue;
      P[name] = function (text, ...rest) {
        try {
          let list = drawn.get(this.canvas);
          if (!list) drawn.set(this.canvas, (list = []));
          list.push(String(text).slice(0, 40));
          if (list.length > 300) list.splice(0, list.length - 200);   // keep the latest drawing
        } catch (e) { /* never break the page */ }
        return orig.call(this, text, ...rest);
      };
    }
  }
  window.__kchCanvas = () => [...document.querySelectorAll('canvas')].slice(0, 10).map((c) => {
    const out = { name: c.id ? '#' + c.id : (c.className ? '.' + String(c.className).split(' ')[0] : 'canvas'),
                  w: c.width, h: c.height, shown: `${Math.round(c.clientWidth)}x${Math.round(c.clientHeight)}` };
    try {
      if (c.width && c.height) {
        // copy into a scratch canvas: reading this way never adds a context to the page's own canvas
        const t = document.createElement('canvas');
        t.width = c.width; t.height = c.height;
        const g = t.getContext('2d');
        g.drawImage(c, 0, 0);
        const d = g.getImageData(0, 0, c.width, c.height).data;
        const px = c.width * c.height, step = Math.max(1, Math.floor(px / 60000));
        const bg = [d[0], d[1], d[2], d[3]];
        const colours = new Set();
        let n = 0, total = 0;
        for (let i = 0; i < px; i += step) {
          const o = i * 4;
          total++;
          if (Math.abs(d[o] - bg[0]) + Math.abs(d[o + 1] - bg[1]) + Math.abs(d[o + 2] - bg[2]) + Math.abs(d[o + 3] - bg[3]) > 30) n++;
          if (colours.size < 40 && d[o + 3] > 0) colours.add((d[o] >> 5) + ',' + (d[o + 1] >> 5) + ',' + (d[o + 2] >> 5));
        }
        out.filled = Math.round((1000 * n) / total) / 10;
        out.colours = colours.size;
      }
    } catch (e) { out.error = String(e).slice(0, 80); }
    const texts = drawn.get(c) || [];
    out.text = [...new Set(texts.slice(-120))].slice(0, 40);
    return out;
  });
})();
