'use strict';
/* Interactive in-viewer editors for Flock (desktop + mobile):
   - Word (.docx): rich WYSIWYG editing (fonts, font sizes, colours, highlights, alignment, line spacing,
     bold/italic/underline/strikethrough/sup/sub, lists, tables, links, images) with full OOXML round-trip
     load & save preserving original drawings/TIFF images, styles, headers/footers, and page setup.
   - Spreadsheets (.xlsx/.xls/.ods and .csv/.tsv): inline editing, formula bar, cell formatting
     (font, size, bold, italic, alignment, text colour, background fill persisted in xl/styles.xml),
     add/delete rows & cols, column sort, live filter, rename/add sheets.
   - PDF (.pdf): HiDPI PDF.js rendering with Pan, Draw, Highlight, Rect, Arrow, styled Text notes
     (font, size, bold, italic, custom colour), Eraser, Undo/Redo, Clear, Zoom, and pdf-lib save.
   - PowerPoint (.pptx): slide text & style editing (font, size, bold, italic, underline, colour, align)
     patching slide XML directly inside the original .pptx archive.
   - Markdown (.md): visual WYSIWYG editing (headings, inline styles, code, lists, quotes, links)
     converting cleanly back to Markdown with YAML frontmatter preserved. */
(() => {
  const { esc, el, base, api, toast } = window.KCH;
  const h = (tag, cls, html) => el(tag, cls, html);
  const num = (n) => Number(n).toLocaleString('en-GB');
  const MB = 1048576;

  /* ---------------------------------------------------------------- shared binary, zip & style helpers */
  function b64(u8) {
    let s = '';
    for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function fromB64(str) {
    const bin = atob(str);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  async function saveBinary(path, u8) {
    await api('/api/files/upload', { path, data: b64(u8) });
  }
  async function deflateRaw(u8) {
    if (typeof CompressionStream !== 'function' || u8.length < 64) return { data: u8, method: 0 };
    try {
      const cs = new CompressionStream('deflate-raw');
      const w = cs.writable.getWriter();
      w.write(u8); w.close();
      const rd = cs.readable.getReader();
      const parts = []; let len = 0;
      for (;;) { const { value, done } = await rd.read(); if (done) break; parts.push(value); len += value.length; }
      const out = new Uint8Array(len); let o = 0;
      for (const p of parts) { out.set(p, o); o += p.length; }
      return out.length < u8.length ? { data: out, method: 8 } : { data: u8, method: 0 };
    } catch { return { data: u8, method: 0 }; }
  }
  const CRC_TBL = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1); t[n] = c >>> 0; }
    return t;
  })();
  function crc32(u8) {
    let c = 0xffffffff;
    for (let i = 0; i < u8.length; i++) c = CRC_TBL[(c ^ u8[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }
  async function makeZip(entries) {
    const enc = new TextEncoder();
    const now = new Date();
    const dosTime = ((now.getHours() & 31) << 11) | ((now.getMinutes() & 63) << 5) | ((now.getSeconds() >> 1) & 31);
    const dosDate = (((Math.max(1980, now.getFullYear()) - 1980) & 127) << 9) | (((now.getMonth() + 1) & 15) << 5) | (now.getDate() & 31);
    const locals = [];
    const centrals = [];
    let offset = 0;
    for (const [name, raw] of entries) {
      const u8 = raw instanceof Uint8Array ? raw : enc.encode(String(raw));
      const nb = enc.encode(name);
      const crc = crc32(u8);
      const { data: comp, method } = await deflateRaw(u8);
      const lh = new Uint8Array(30 + nb.length);
      const lv = new DataView(lh.buffer);
      lv.setUint32(0, 0x04034b50, true); lv.setUint16(4, 20, true); lv.setUint16(6, 0x0800, true);
      lv.setUint16(8, method, true); lv.setUint16(10, dosTime, true); lv.setUint16(12, dosDate, true);
      lv.setUint32(14, crc, true); lv.setUint32(18, comp.length, true); lv.setUint32(22, u8.length, true);
      lv.setUint16(26, nb.length, true); lv.setUint16(28, 0, true);
      lh.set(nb, 30);
      locals.push(lh, comp);

      const ch = new Uint8Array(46 + nb.length);
      const cv = new DataView(ch.buffer);
      cv.setUint32(0, 0x02014b50, true); cv.setUint16(4, 20, true); cv.setUint16(6, 20, true); cv.setUint16(8, 0x0800, true);
      cv.setUint16(10, method, true); cv.setUint16(12, dosTime, true); cv.setUint16(14, dosDate, true);
      cv.setUint32(16, crc, true); cv.setUint32(20, comp.length, true); cv.setUint32(24, u8.length, true);
      cv.setUint16(28, nb.length, true); cv.setUint16(30, 0, true); cv.setUint16(32, 0, true);
      cv.setUint16(34, 0, true); cv.setUint16(36, 0, true); cv.setUint32(38, 0, true); cv.setUint32(42, offset, true);
      ch.set(nb, 46);
      centrals.push(ch);
      offset += lh.length + comp.length;
    }
    const cdStart = offset;
    let cdSize = 0;
    for (const c of centrals) cdSize += c.length;
    const eocd = new Uint8Array(22);
    const ev = new DataView(eocd.buffer);
    ev.setUint32(0, 0x06054b50, true); ev.setUint16(4, 0, true); ev.setUint16(6, 0, true);
    ev.setUint16(8, entries.length, true); ev.setUint16(10, entries.length, true);
    ev.setUint32(12, cdSize, true); ev.setUint32(16, cdStart, true); ev.setUint16(20, 0, true);

    const total = offset + cdSize + 22;
    const out = new Uint8Array(total);
    let p = 0;
    for (const x of locals) { out.set(x, p); p += x.length; }
    for (const x of centrals) { out.set(x, p); p += x.length; }
    out.set(eocd, p);
    return out;
  }

  const xmlEsc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const sep = () => h('span', 'kv-sep');

  const FONTS = [
    ['', 'Font'],
    ['Arial', 'Arial'],
    ['Calibri', 'Calibri'],
    ['Times New Roman', 'Times New Roman'],
    ['Georgia', 'Georgia'],
    ['Verdana', 'Verdana'],
    ['Cambria', 'Cambria'],
    ['Garamond', 'Garamond'],
    ['Courier New', 'Courier New'],
    ['Consolas', 'Consolas'],
  ];
  const DOC_SIZES = [['', 'Size'], [9, '9 pt'], [10, '10 pt'], [11, '11 pt'], [12, '12 pt'], [14, '14 pt'], [16, '16 pt'], [18, '18 pt'], [20, '20 pt'], [24, '24 pt'], [28, '28 pt'], [36, '36 pt']];
  const HIGHLIGHTS = [
    ['', 'Highlight'],
    ['#fef08a', 'Yellow'],
    ['#bbf7d0', 'Green'],
    ['#bae6fd', 'Blue'],
    ['#fbcfe8', 'Pink'],
    ['#fed7aa', 'Orange'],
    ['none', 'No highlight'],
  ];
  const WORD_HL_HEX = {
    yellow: '#fef08a', green: '#bbf7d0', cyan: '#bae6fd', magenta: '#fbcfe8',
    red: '#fecaca', blue: '#bfdbfe', darkYellow: '#fde047', lightGray: '#e5e7eb',
  };

  function cssColorToHex(c) {
    if (!c) return '';
    const s = String(c).trim().toLowerCase();
    if (!s || s === 'transparent' || s === 'inherit' || s === 'initial' || s === 'none' || s === 'auto') return '';
    if (s.startsWith('#')) {
      const hex = s.slice(1);
      if (hex.length === 3) return (hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2]).toUpperCase();
      if (hex.length >= 6) return hex.slice(0, 6).toUpperCase();
      return '';
    }
    const m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)$/.exec(s);
    if (!m) return '';
    if (m[4] !== undefined && parseFloat(m[4]) === 0) return '';
    return [m[1], m[2], m[3]].map((v) => Math.max(0, Math.min(255, +v)).toString(16).padStart(2, '0')).join('').toUpperCase();
  }
  function cssSizeToPt(sz) {
    if (!sz) return 0;
    const s = String(sz).trim().toLowerCase();
    const m = /^([\d.]+)(pt|px)$/.exec(s);
    if (!m) return 0;
    const v = parseFloat(m[1]);
    return m[2] === 'pt' ? v : +(v * 0.75).toFixed(1);
  }
  function cleanFontName(f) {
    if (!f) return '';
    const first = String(f).split(',')[0].trim().replace(/^["']|["']$/g, '');
    if (/^(var\(|system-ui|-apple-system|sans-serif|serif|monospace|inherit|initial)$/i.test(first)) return '';
    return first;
  }
  function makeSelect(options, title, onChange) {
    const s = h('select', 'kv-sel');
    if (title) s.title = title;
    s.innerHTML = options.map(([v, label]) => `<option value="${esc(String(v))}">${esc(label)}</option>`).join('');
    s.onchange = () => onChange(s.value, s);
    return s;
  }
  function makeColorControl(labelHtml, title, initialHex, onPick) {
    const wrap = h('label', 'kv-color-wrap');
    wrap.title = title;
    const lbl = h('span', 'kv-color-lbl', labelHtml);
    const barEl = h('span', 'kv-color-bar');
    barEl.style.background = initialHex;
    const inp = document.createElement('input');
    inp.type = 'color';
    inp.value = initialHex;
    inp.className = 'kv-color-native';
    inp.oninput = () => { barEl.style.background = inp.value; onPick(inp.value); };
    wrap.append(lbl, barEl, inp);
    wrap.setColor = (hex) => { if (/^#[0-9a-f]{6}$/i.test(hex)) { inp.value = hex; barEl.style.background = hex; } };
    wrap.input = inp;
    return wrap;
  }
  const ALIGN_ICONS = {
    left: '<svg viewBox="0 0 16 16"><path d="M2 3h12M2 7h8M2 11h11M2 14h7"/></svg>',
    center: '<svg viewBox="0 0 16 16"><path d="M2 3h12M4 7h8M2.5 11h11M4.5 14h7"/></svg>',
    right: '<svg viewBox="0 0 16 16"><path d="M2 3h12M6 7h8M3 11h11M7 14h7"/></svg>',
    justify: '<svg viewBox="0 0 16 16"><path d="M2 3h12M2 7h12M2 11h12M2 14h12"/></svg>',
  };

  /* Selection-safe inline styling for contentEditable roots (Word & PowerPoint) */
  function createSelectionStyler(root) {
    let savedRange = null;
    function capture() {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      const r = sel.getRangeAt(0);
      if (root.contains(r.commonAncestorContainer)) savedRange = r.cloneRange();
    }
    function restore() {
      if (!savedRange) return null;
      if (!root.contains(savedRange.commonAncestorContainer)) return null;
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(savedRange);
      return savedRange;
    }
    function applyInline(stylePatch, datasetPatch) {
      root.focus();
      let range = restore();
      if (!range) {
        const sel = window.getSelection();
        if (sel && sel.rangeCount && root.contains(sel.getRangeAt(0).commonAncestorContainer)) range = sel.getRangeAt(0);
      }
      if (!range) return false;

      const applyToEl = (elNode) => {
        for (const [k, v] of Object.entries(stylePatch || {})) {
          if (v === null || v === '' || v === 'none') elNode.style[k] = '';
          else elNode.style[k] = v;
        }
        for (const [k, v] of Object.entries(datasetPatch || {})) {
          if (v === null || v === '') delete elNode.dataset[k];
          else elNode.dataset[k] = String(v);
        }
      };

      if (range.collapsed) {
        const node = range.startContainer;
        const parentSpan = (node.nodeType === 1 ? node : node.parentElement)?.closest('span');
        if (parentSpan && root.contains(parentSpan) && (parentSpan.textContent === '\u200B' || parentSpan.textContent === '')) {
          applyToEl(parentSpan);
          return true;
        }
        const sp = document.createElement('span');
        applyToEl(sp);
        const zw = document.createTextNode('\u200B');
        sp.appendChild(zw);
        range.insertNode(sp);
        const nr = document.createRange();
        nr.setStart(zw, 1);
        nr.setEnd(zw, 1);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(nr);
        savedRange = nr.cloneRange();
        return true;
      }

      // Collect all text nodes intersecting range
      const common = range.commonAncestorContainer;
      const nodes = [];
      if (common.nodeType === 3) {
        nodes.push(common);
      } else {
        const tw = document.createTreeWalker(common, NodeFilter.SHOW_TEXT);
        let cur;
        while ((cur = tw.nextNode())) {
          if (!cur.nodeValue || cur.nodeValue === '\u200B') continue;
          if (range.intersectsNode(cur)) nodes.push(cur);
        }
      }
      if (!nodes.length) return false;

      const wrapped = [];
      for (const tn of nodes) {
        let target = tn;
        const isStart = tn === range.startContainer;
        const isEnd = tn === range.endContainer;
        const startOff = isStart ? range.startOffset : 0;
        const endOff = isEnd ? range.endOffset : tn.nodeValue.length;
        if (startOff >= endOff) continue;
        if (endOff < target.nodeValue.length) target.splitText(endOff);
        if (startOff > 0) target = target.splitText(startOff);

        const par = target.parentElement;
        if (par && par.tagName === 'SPAN' && root.contains(par) && par.childNodes.length === 1 && !par.classList.contains('kv-bu')) {
          applyToEl(par);
          wrapped.push(target);
        } else {
          const sp = document.createElement('span');
          applyToEl(sp);
          target.parentNode.insertBefore(sp, target);
          sp.appendChild(target);
          wrapped.push(target);
        }
      }

      if (wrapped.length) {
        const nr = document.createRange();
        nr.setStart(wrapped[0], 0);
        const last = wrapped[wrapped.length - 1];
        nr.setEnd(last, last.nodeValue.length);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(nr);
        savedRange = nr.cloneRange();
      }
      return wrapped.length > 0;
    }
    function activeBlocks(blockSel = 'p,h1,h2,h3,h4,li,blockquote,div.kv-p') {
      const range = restore() || (() => {
        const s = window.getSelection();
        return s && s.rangeCount && root.contains(s.getRangeAt(0).commonAncestorContainer) ? s.getRangeAt(0) : null;
      })();
      if (!range) return [];
      const all = [...root.querySelectorAll(blockSel)].filter((x) => range.intersectsNode(x));
      if (all.length) return all;
      const startEl = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
      const closest = startEl?.closest(blockSel);
      return closest && root.contains(closest) ? [closest] : [];
    }
    return { capture, restore, applyInline, activeBlocks, getSaved: () => savedRange };
  }

  /* ================================================================ 1. WORD (.docx) EDITOR */
  const W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
  const R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';

  const xmlKids = (n, local) => (n ? [...n.children].filter((c) => c.localName === local) : []);
  const xmlKid = (n, local) => (n ? [...n.children].find((c) => c.localName === local) || null : null);
  const xmlAttr = (n, local) => {
    if (!n) return null;
    for (const a of n.attributes) if (a.localName === local || a.name === local) return a.value;
    return null;
  };
  const xmlOn = (n) => {
    if (!n) return false;
    const v = xmlAttr(n, 'val');
    return v === null || (v !== '0' && v !== 'false' && v !== 'none');
  };

  function buildDrawingXml(rId, idNum, cxEmu, cyEmu, name) {
    return `<w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0" ` +
      `xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">` +
      `<wp:extent cx="${cxEmu}" cy="${cyEmu}"/>` +
      `<wp:docPr id="${idNum}" name="${xmlEsc(name)}"/>` +
      `<wp:cNvGraphicFramePr><a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/></wp:cNvGraphicFramePr>` +
      `<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">` +
      `<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">` +
      `<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">` +
      `<pic:nvPicPr><pic:cNvPr id="${idNum}" name="${xmlEsc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>` +
      `<pic:blipFill><a:blip r:embed="${rId}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>` +
      `<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="${cxEmu}" cy="${cyEmu}"/></a:xfrm>` +
      `<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>` +
      `</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>`;
  }

  function extractOrigDocParts(docXmlText) {
    const parser = new DOMParser();
    const doc = parser.parseFromString(docXmlText, 'application/xml');
    const ser = new XMLSerializer();
    const root = doc.documentElement;
    const rootAttrs = [...root.attributes].map((a) => `${a.name}="${xmlEsc(a.value)}"`).join(' ');
    const hasR = /\bxmlns:r=/.test(rootAttrs);
    const fullAttrs = hasR ? rootAttrs : (rootAttrs + ` xmlns:r="${R_NS}"`).trim();
    const bodyEl = doc.getElementsByTagNameNS(W_NS, 'body')[0];
    let sectPrXml = '';
    if (bodyEl) {
      const sect = [...bodyEl.children].find((c) => c.localName === 'sectPr');
      if (sect) sectPrXml = ser.serializeToString(sect);
    }
    return { fullAttrs, sectPrXml, doc, bodyEl };
  }

  /* Rich OOXML -> HTML renderer preserving fonts, font sizes, colours, highlights, alignment, line spacing,
     strikethrough, sup/sub, tables, lists, hyperlinks, AND embedded images (including TIFFs via UTIF). */
  async function renderDocxOoxmlHtml(entries, origParts, origBySrc, H) {
    const dec = new TextDecoder('utf-8');
    const ser = new XMLSerializer();
    const parser = new DOMParser();
    const bodyEl = origParts.bodyEl;
    if (!bodyEl) return null;

    // Parse word/_rels/document.xml.rels
    const rels = new Map();
    if (entries.has('word/_rels/document.xml.rels')) {
      const rd = parser.parseFromString(dec.decode(entries.get('word/_rels/document.xml.rels')), 'application/xml');
      for (const r of rd.getElementsByTagNameNS('*', 'Relationship')) {
        rels.set(r.getAttribute('Id'), {
          type: (r.getAttribute('Type') || '').split('/').pop(),
          target: r.getAttribute('Target') || '',
          external: r.getAttribute('TargetMode') === 'External',
        });
      }
    }

    // Parse word/styles.xml for heading/title style names
    const styleNames = new Map();
    if (entries.has('word/styles.xml')) {
      const sd = parser.parseFromString(dec.decode(entries.get('word/styles.xml')), 'application/xml');
      for (const st of sd.getElementsByTagNameNS(W_NS, 'style')) {
        const id = xmlAttr(st, 'styleId');
        const nm = xmlAttr(xmlKid(st, 'name'), 'val') || id;
        if (id) styleNames.set(id, nm);
      }
    }

    // Parse word/numbering.xml completely (per numId & ilvl: numFmt, start, startOverride)
    const numDefs = new Map();
    const numCounters = new Map();
    if (entries.has('word/numbering.xml')) {
      const nd = parser.parseFromString(dec.decode(entries.get('word/numbering.xml')), 'application/xml');
      const absLvls = new Map();
      for (const an of nd.getElementsByTagNameNS(W_NS, 'abstractNum')) {
        const aid = xmlAttr(an, 'abstractNumId');
        const lvls = new Map();
        for (const lv of xmlKids(an, 'lvl')) {
          const il = +(xmlAttr(lv, 'ilvl') || 0);
          const fmt = xmlAttr(xmlKid(lv, 'numFmt'), 'val') || 'bullet';
          const st = +(xmlAttr(xmlKid(lv, 'start'), 'val') || 1);
          lvls.set(il, { fmt, start: isFinite(st) ? st : 1 });
        }
        if (aid != null) absLvls.set(aid, lvls);
      }
      for (const nm of nd.getElementsByTagNameNS(W_NS, 'num')) {
        const nid = xmlAttr(nm, 'numId');
        const aid = xmlAttr(xmlKid(nm, 'abstractNumId'), 'val');
        const overrides = new Map();
        for (const ov of xmlKids(nm, 'lvlOverride')) {
          const il = +(xmlAttr(ov, 'ilvl') || 0);
          const so = xmlAttr(xmlKid(ov, 'startOverride'), 'val');
          if (so != null && isFinite(+so)) overrides.set(il, +so);
        }
        if (nid != null && aid != null) numDefs.set(nid, { lvls: absLvls.get(aid) || new Map(), overrides });
      }
    }
    const OL_TYPE = { decimal: '1', lowerLetter: 'a', upperLetter: 'A', lowerRoman: 'i', upperRoman: 'I' };
    function nextListInfo(numPr) {
      if (!numPr) return null;
      const nid = xmlAttr(xmlKid(numPr, 'numId'), 'val');
      if (!nid || nid === '0') return null;
      const ilvl = +(xmlAttr(xmlKid(numPr, 'ilvl'), 'val') || 0);
      const def = numDefs.get(nid);
      const lvlDef = def?.lvls.get(ilvl) || def?.lvls.get(0) || { fmt: 'bullet', start: 1 };
      if (lvlDef.fmt === 'none') return null;
      if (lvlDef.fmt === 'bullet') return { listType: 'ul', numId: nid, ilvl, olType: '', num: 0 };
      let ctrs = numCounters.get(nid);
      if (!ctrs) { ctrs = new Map(); numCounters.set(nid, ctrs); }
      for (const k of [...ctrs.keys()]) if (k > ilvl) ctrs.delete(k);
      const startVal = def?.overrides.has(ilvl) ? def.overrides.get(ilvl) : lvlDef.start;
      const val = ctrs.has(ilvl) ? ctrs.get(ilvl) + 1 : startVal;
      ctrs.set(ilvl, val);
      return { listType: 'ol', numId: nid, ilvl, olType: OL_TYPE[lvlDef.fmt] || '1', num: val };
    }

    // Cache decoded image data URLs per rId
    const imgCache = new Map();
    async function imgSrcForRel(rId) {
      if (!rId) return null;
      if (imgCache.has(rId)) return imgCache.get(rId);
      const rel = rels.get(rId);
      if (!rel || rel.external) return null;
      let target = rel.target.replace(/^\/+/, '');
      if (!target.startsWith('word/')) target = 'word/' + target.replace(/^\.\//, '');
      const ib = entries.get(target);
      if (!ib) return null;
      const ct = H.sniffImgMime(ib, '');
      let src = '';
      if (ct === 'image/tiff') {
        try {
          const { canvas } = await H.decodeTiffCanvas(H.ab(ib));
          src = canvas.toDataURL('image/png');
        } catch { /* fallback below */ }
      }
      if (!src) src = `data:${ct || 'application/octet-stream'};base64,${b64(ib)}`;
      imgCache.set(rId, src);
      return src;
    }

    function rPrToStyle(rPr) {
      const css = [];
      const tags = [];
      if (!rPr) return { css, tags };
      const rFonts = xmlKid(rPr, 'rFonts');
      const font = xmlAttr(rFonts, 'ascii') || xmlAttr(rFonts, 'hAnsi') || xmlAttr(rFonts, 'cs');
      if (font) css.push(`font-family:'${font.replace(/['"]/g, '')}'`);
      const sz = xmlAttr(xmlKid(rPr, 'sz'), 'val');
      if (sz && +sz > 0) css.push(`font-size:${+(+sz / 2).toFixed(1)}pt`);
      const col = xmlAttr(xmlKid(rPr, 'color'), 'val');
      if (col && /^[0-9a-f]{6}$/i.test(col)) css.push(`color:#${col}`);
      const hl = xmlAttr(xmlKid(rPr, 'highlight'), 'val');
      const shd = xmlAttr(xmlKid(rPr, 'shd'), 'fill');
      if (hl && hl !== 'none' && WORD_HL_HEX[hl]) css.push(`background-color:${WORD_HL_HEX[hl]}`);
      else if (shd && /^[0-9a-f]{6}$/i.test(shd) && shd.toUpperCase() !== 'FFFFFF') css.push(`background-color:#${shd}`);
      if (xmlOn(xmlKid(rPr, 'b'))) tags.push('strong');
      if (xmlOn(xmlKid(rPr, 'i'))) tags.push('em');
      if (xmlOn(xmlKid(rPr, 'u'))) tags.push('u');
      if (xmlOn(xmlKid(rPr, 'strike')) || xmlOn(xmlKid(rPr, 'dstrike'))) tags.push('s');
      const va = xmlAttr(xmlKid(rPr, 'vertAlign'), 'val');
      if (va === 'superscript') tags.push('sup');
      else if (va === 'subscript') tags.push('sub');
      return { css, tags };
    }

    async function renderRun(rEl) {
      const rPr = xmlKid(rEl, 'rPr');
      const { css, tags } = rPrToStyle(rPr);
      let inner = '';
      for (const ch of rEl.children) {
        const ln = ch.localName;
        if (ln === 't' || ln === 'delText') {
          inner += esc(ch.textContent || '');
        } else if (ln === 'br' || ln === 'cr') {
          inner += '<br>';
        } else if (ln === 'tab') {
          inner += '&emsp;';
        } else if (ln === 'drawing' || ln === 'pict') {
          const blip = ch.getElementsByTagNameNS('*', 'blip')[0];
          const imgData = ch.getElementsByTagNameNS('*', 'imagedata')[0];
          const rId = blip ? (blip.getAttributeNS(R_NS, 'embed') || xmlAttr(blip, 'embed') || xmlAttr(blip, 'link'))
            : imgData ? (imgData.getAttributeNS(R_NS, 'id') || xmlAttr(imgData, 'id')) : null;
          const src = await imgSrcForRel(rId);
          if (src) {
            origBySrc.set(src, ser.serializeToString(ch));
            inner += `<img src="${src}" alt="">`;
          }
        }
      }
      if (!inner) return '';
      let out = inner;
      for (const t of tags) out = `<${t}>${out}</${t}>`;
      if (css.length) out = `<span style="${css.join(';')}">${out}</span>`;
      return out;
    }

    async function renderPara(pEl) {
      const pPr = xmlKid(pEl, 'pPr');
      const pStyleId = xmlAttr(xmlKid(pPr, 'pStyle'), 'val') || '';
      const pStyleName = (styleNames.get(pStyleId) || pStyleId).toLowerCase();
      const jc = xmlAttr(xmlKid(pPr, 'jc'), 'val');
      const spEl = xmlKid(pPr, 'spacing');
      const lineVal = xmlAttr(spEl, 'line');
      const lineRule = xmlAttr(spEl, 'lineRule') || 'auto';
      const numPr = xmlKid(pPr, 'numPr');

      const css = [];
      if (jc) {
        const map = { left: 'left', start: 'left', center: 'center', right: 'right', end: 'right', both: 'justify', distribute: 'justify' };
        if (map[jc]) css.push(`text-align:${map[jc]}`);
      }
      if (lineVal && lineRule === 'auto' && +lineVal > 0) {
        const lh = +(+lineVal / 240).toFixed(2);
        if (lh >= 0.8 && lh <= 4) css.push(`line-height:${lh}`);
      }

      let inner = '';
      for (const ch of pEl.children) {
        const ln = ch.localName;
        if (ln === 'r') {
          inner += await renderRun(ch);
        } else if (ln === 'hyperlink') {
          const rid = ch.getAttributeNS(R_NS, 'id') || xmlAttr(ch, 'id');
          const rel = rid && rels.get(rid);
          let linkInner = '';
          for (const lr of xmlKids(ch, 'r')) linkInner += await renderRun(lr);
          if (rel && rel.target) inner += `<a href="${esc(rel.target)}">${linkInner || esc(rel.target)}</a>`;
          else inner += linkInner;
        } else if (ln === 'ins') {
          for (const ir of xmlKids(ch, 'r')) inner += await renderRun(ir);
        }
      }

      let tag = 'p', cls = '';
      if (!numPr) {
        if (/^title$/.test(pStyleName)) { tag = 'h1'; cls = ' class="title"'; }
        else if (/^subtitle$/.test(pStyleName)) { tag = 'h2'; cls = ' class="subtitle"'; }
        else if (/heading\s*1|^h1$/.test(pStyleName)) tag = 'h1';
        else if (/heading\s*2|^h2$/.test(pStyleName)) tag = 'h2';
        else if (/heading\s*3|^h3$/.test(pStyleName)) tag = 'h3';
        else if (/heading\s*4|^h4$/.test(pStyleName)) tag = 'h4';
      }
      const stAttr = css.length ? ` style="${css.join(';')}"` : '';
      const listInfo = nextListInfo(numPr);
      return {
        listInfo,
        html: listInfo
          ? `<li${listInfo.listType === 'ol' ? ` value="${listInfo.num}"` : ''}${stAttr}>${inner || '<br>'}</li>`
          : `<${tag}${cls}${stAttr}>${inner || '<br>'}</${tag}>`,
      };
    }

    async function renderBlocks(container) {
      let out = '';
      let openList = null; // { tag, key }
      const closeList = () => { if (openList) { out += `</${openList.tag}>`; openList = null; } };
      for (const ch of container.children) {
        const ln = ch.localName;
        if (ln === 'p') {
          const r = await renderPara(ch);
          if (r.listInfo) {
            const li = r.listInfo;
            const key = `${li.listType}:${li.numId}:${li.ilvl}:${li.olType}`;
            if (!openList || openList.key !== key) {
              closeList();
              const pad = li.ilvl > 0 ? ` style="margin-left:${li.ilvl * 20}px"` : '';
              const attrs = li.listType === 'ol'
                ? ` start="${li.num}"${li.olType && li.olType !== '1' ? ` type="${li.olType}"` : ''}${pad}`
                : pad;
              openList = { tag: li.listType, key };
              out += `<${li.listType}${attrs}>`;
            }
            out += r.html;
          } else {
            closeList();
            out += r.html;
          }
        } else if (ln === 'tbl') {
          closeList();
          let rowsHtml = '';
          const trs = xmlKids(ch, 'tr');
          for (let ri = 0; ri < trs.length; ri++) {
            const tr = trs[ri];
            const isHeader = !!xmlKid(xmlKid(tr, 'trPr'), 'tblHeader');
            let cellsHtml = '';
            for (const tc of xmlKids(tr, 'tc')) {
              const tcPr = xmlKid(tc, 'tcPr');
              const span = +(xmlAttr(xmlKid(tcPr, 'gridSpan'), 'val') || 1);
              const shd = xmlAttr(xmlKid(tcPr, 'shd'), 'fill');
              const cellStyle = shd && /^[0-9a-f]{6}$/i.test(shd) && shd.toUpperCase() !== 'FFFFFF' ? ` style="background-color:#${shd}"` : '';
              const cTag = isHeader ? 'th' : 'td';
              const spanAttr = span > 1 ? ` colspan="${span}"` : '';
              cellsHtml += `<${cTag}${spanAttr}${cellStyle}>${await renderBlocks(tc)}</${cTag}>`;
            }
            if (cellsHtml) rowsHtml += `<tr>${cellsHtml}</tr>`;
          }
          if (rowsHtml) out += `<table><tbody>${rowsHtml}</tbody></table>`;
        } else if (ln === 'sdt') {
          const content = xmlKid(ch, 'sdtContent');
          if (content) { closeList(); out += await renderBlocks(content); }
        }
      }
      closeList();
      return out;
    }

    return renderBlocks(bodyEl);
  }

  function serializeDomToDocxBody(art, origBySrc, addNewImage, addHyperlink) {
    function runPrXml(fmt) {
      const parts = [];
      if (fmt.font) parts.push(`<w:rFonts w:ascii="${xmlEsc(fmt.font)}" w:hAnsi="${xmlEsc(fmt.font)}" w:cs="${xmlEsc(fmt.font)}"/>`);
      if (fmt.b) parts.push('<w:b/>');
      if (fmt.i) parts.push('<w:i/>');
      if (fmt.s) parts.push('<w:strike/>');
      if (fmt.color) parts.push(`<w:color w:val="${fmt.color}"/>`);
      if (fmt.sizePt && fmt.sizePt > 0) {
        const hp = Math.round(fmt.sizePt * 2);
        parts.push(`<w:sz w:val="${hp}"/><w:szCs w:val="${hp}"/>`);
      }
      if (fmt.bg) parts.push(`<w:shd w:val="clear" w:color="auto" w:fill="${fmt.bg}"/>`);
      if (fmt.u) parts.push('<w:u w:val="single"/>');
      if (fmt.sup) parts.push('<w:vertAlign w:val="superscript"/>');
      else if (fmt.sub) parts.push('<w:vertAlign w:val="subscript"/>');
      return parts.length ? `<w:rPr>${parts.join('')}</w:rPr>` : '';
    }
    function textRun(str, fmt) {
      const clean = String(str || '').replace(/\u200B/g, '');
      if (!clean) return '';
      return `<w:r>${runPrXml(fmt)}<w:t xml:space="preserve">${xmlEsc(clean)}</w:t></w:r>`;
    }
    function extractInlineFmt(node, baseFmt) {
      const next = { ...baseFmt };
      const tag = node.tagName.toLowerCase();
      if (tag === 'b' || tag === 'strong') next.b = true;
      if (tag === 'i' || tag === 'em') next.i = true;
      if (tag === 'u') next.u = true;
      if (tag === 's' || tag === 'del' || tag === 'strike') next.s = true;
      if (tag === 'sup') next.sup = true;
      if (tag === 'sub') next.sub = true;
      if (tag === 'mark') next.bg = 'FEF08A';
      if (tag === 'font') {
        const face = cleanFontName(node.getAttribute('face'));
        if (face) next.font = face;
        const col = cssColorToHex(node.getAttribute('color'));
        if (col) next.color = col;
      }
      const st = node.style;
      if (st) {
        const ff = cleanFontName(st.fontFamily);
        if (ff) next.font = ff;
        const pt = cssSizeToPt(st.fontSize);
        if (pt) next.sizePt = pt;
        const col = cssColorToHex(st.color);
        if (col) next.color = col;
        const bg = cssColorToHex(st.backgroundColor);
        if (bg) next.bg = bg;
        if (st.fontWeight === 'bold' || +st.fontWeight >= 600) next.b = true;
        else if (st.fontWeight === 'normal' || st.fontWeight === '400') next.b = false;
        if (st.fontStyle === 'italic') next.i = true;
        else if (st.fontStyle === 'normal') next.i = false;
        if (st.textDecoration && st.textDecoration.includes('underline')) next.u = true;
        if (st.textDecoration && st.textDecoration.includes('line-through')) next.s = true;
      }
      return next;
    }
    function walkInline(node, fmt) {
      if (node.nodeType === 3) return textRun(node.nodeValue, fmt);
      if (node.nodeType !== 1) return '';
      const tag = node.tagName.toLowerCase();
      if (tag === 'br') return '<w:r><w:br/></w:r>';
      if (tag === 'img') {
        const src = node.getAttribute('src') || '';
        const orig = origBySrc.get(src);
        if (orig) return `<w:r>${orig}</w:r>`;
        const added = addNewImage(node);
        return added ? `<w:r>${added}</w:r>` : '';
      }
      if (tag === 'a' && node.getAttribute('href')) {
        const href = node.getAttribute('href');
        const next = extractInlineFmt(node, { ...fmt, u: true, color: fmt.color || '1D4ED8' });
        let inner = '';
        for (const ch of node.childNodes) inner += walkInline(ch, next);
        const rId = addHyperlink ? addHyperlink(href) : '';
        return rId ? `<w:hyperlink r:id="${rId}">${inner}</w:hyperlink>` : inner;
      }
      const next = extractInlineFmt(node, fmt);
      let out = '';
      for (const ch of node.childNodes) out += walkInline(ch, next);
      return out;
    }
    function para(runsXml, pStyle, blockEl) {
      const pPrParts = [];
      if (pStyle) pPrParts.push(`<w:pStyle w:val="${pStyle}"/>`);
      if (blockEl && blockEl.style) {
        const lh = parseFloat(blockEl.style.lineHeight);
        if (isFinite(lh) && lh >= 0.8 && lh <= 4) {
          pPrParts.push(`<w:spacing w:line="${Math.round(lh * 240)}" w:lineRule="auto"/>`);
        }
        const ta = blockEl.style.textAlign;
        const jcMap = { left: 'left', center: 'center', right: 'right', justify: 'both' };
        if (ta && jcMap[ta]) pPrParts.push(`<w:jc w:val="${jcMap[ta]}"/>`);
      }
      const pPr = pPrParts.length ? `<w:pPr>${pPrParts.join('')}</w:pPr>` : '';
      return `<w:p>${pPr}${runsXml}</w:p>`;
    }
    function walkBlocks(container) {
      let out = '';
      let loose = '';
      const flushLoose = () => { if (loose) { out += para(loose, '', null); loose = ''; } };
      for (const node of container.childNodes) {
        if (node.nodeType === 3) {
          if (node.nodeValue.replace(/\u200B/g, '').trim()) loose += textRun(node.nodeValue, {});
          continue;
        }
        if (node.nodeType !== 1) continue;
        const tag = node.tagName.toLowerCase();
        if (tag === 'h1' || tag === 'h2' || tag === 'h3' || tag === 'h4') {
          flushLoose();
          const st = node.classList.contains('title') ? 'Title'
            : node.classList.contains('subtitle') ? 'Subtitle'
            : 'Heading' + tag[1];
          const baseFmt = extractInlineFmt(node, {});
          let inner = '';
          for (const ch of node.childNodes) inner += walkInline(ch, baseFmt);
          out += para(inner, st, node);
        } else if (tag === 'p' || tag === 'div' || tag === 'blockquote' || tag === 'pre') {
          flushLoose();
          const hasBlockChild = (tag === 'div' || tag === 'p') && [...node.children].some((c) => /^(p|div|h[1-6]|ul|ol|table|blockquote)$/i.test(c.tagName));
          if (hasBlockChild) out += walkBlocks(node);
          else {
            const baseFmt = extractInlineFmt(node, {});
            let inner = '';
            for (const ch of node.childNodes) {
              if (ch.nodeType === 1 && ch.tagName === 'BR' && node.childNodes.length === 1) continue;
              inner += walkInline(ch, baseFmt);
            }
            out += para(inner, '', node);
          }
        } else if (tag === 'ul' || tag === 'ol') {
          flushLoose();
          let idx = tag === 'ol' ? (parseInt(node.getAttribute('start') || '1', 10) || 1) : 1;
          for (const li of node.children) {
            if (li.tagName.toLowerCase() !== 'li') continue;
            if (tag === 'ol' && li.hasAttribute('value')) {
              const v = parseInt(li.getAttribute('value'), 10);
              if (isFinite(v)) idx = v;
            }
            const prefix = tag === 'ol' ? `${idx++}. ` : '\u2022 ';
            const baseFmt = extractInlineFmt(li, {});
            let inner = '';
            for (const ch of li.childNodes) inner += walkInline(ch, baseFmt);
            out += para(textRun(prefix, baseFmt) + inner, '', li);
          }
        } else if (tag === 'table') {
          flushLoose();
          let rowsXml = '';
          for (const tr of node.querySelectorAll(':scope > tr, :scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr')) {
            let cellsXml = '';
            for (const td of tr.children) {
              if (!/^(td|th)$/i.test(td.tagName)) continue;
              const isHead = td.tagName.toLowerCase() === 'th';
              const bgHex = cssColorToHex(td.style?.backgroundColor);
              const shdXml = bgHex ? `<w:shd w:val="clear" w:color="auto" w:fill="${bgHex}"/>` : '';
              const spanXml = td.colSpan > 1 ? `<w:gridSpan w:val="${td.colSpan}"/>` : '';
              const inner = [...td.children].some((c) => /^(p|div|ul|ol|h[1-6])$/i.test(c.tagName))
                ? walkBlocks(td)
                : para(walkInline(td, isHead ? { b: true } : {}), '', td);
              cellsXml += `<w:tc><w:tcPr>${spanXml}<w:tcBorders><w:top w:val="single" w:sz="4" w:color="CCCCCC"/><w:left w:val="single" w:sz="4" w:color="CCCCCC"/><w:bottom w:val="single" w:sz="4" w:color="CCCCCC"/><w:right w:val="single" w:sz="4" w:color="CCCCCC"/></w:tcBorders>${shdXml}</w:tcPr>${inner || '<w:p/>'}</w:tc>`;
            }
            if (cellsXml) rowsXml += `<w:tr>${cellsXml}</w:tr>`;
          }
          if (rowsXml) {
            out += `<w:tbl><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="CCCCCC"/><w:left w:val="single" w:sz="4" w:color="CCCCCC"/><w:bottom w:val="single" w:sz="4" w:color="CCCCCC"/><w:right w:val="single" w:sz="4" w:color="CCCCCC"/><w:insideH w:val="single" w:sz="4" w:color="CCCCCC"/><w:insideV w:val="single" w:sz="4" w:color="CCCCCC"/></w:tblBorders></w:tblPr>${rowsXml}</w:tbl>`;
          }
        } else {
          loose += walkInline(node, {});
        }
      }
      flushLoose();
      return out;
    }
    return walkBlocks(art);
  }

  async function docxEditor(cx, H) {
    let u8 = await cx.src.bytes(100 * MB);

    const canEdit = !!cx.src.path;
    const tools = H.bar(cx);
    tools.classList.add('kv-editbar');

    const b = H.body(cx, 'kv-scroll kv-paper');
    const art = h('article', 'prose kv-doc kv-page');
    b.appendChild(art);

    const styler = createSelectionStyler(art);
    const origBySrc = new Map();
    let origZipEntries = null;
    let origParts = null;
    let editing = false;
    let dirty = false;

    async function loadOriginalZip(bytes) {
      const z = await H.zipOpen(H.memSrc(cx.src.name, bytes));
      const map = new Map();
      for (const en of z.entries) {
        if (!en.dir) map.set(en.name, await z.read(en, 64 * MB));
      }
      const docBytes = map.get('word/document.xml');
      origZipEntries = map;
      origParts = docBytes
        ? extractOrigDocParts(new TextDecoder('utf-8').decode(docBytes))
        : { fullAttrs: `xmlns:w="${W_NS}" xmlns:r="${R_NS}"`, sectPrXml: '', doc: null, bodyEl: null };
    }

    async function renderFromBytes(bytes) {
      origBySrc.clear();
      await loadOriginalZip(bytes);
      let html = null;
      try {
        html = await renderDocxOoxmlHtml(origZipEntries, origParts, origBySrc, H);
      } catch (e) {
        console.warn('OOXML render fallback to mammoth', e);
      }
      if (!html) {
        await H.script(H.LIB.mammoth);
        const convertImage = window.mammoth.images.imgElement(async (image) => {
          const raw = await image.readAsArrayBuffer();
          const ib = raw instanceof Uint8Array ? raw : new Uint8Array(raw);
          const ct = H.sniffImgMime(ib, image.contentType);
          let src = '';
          if (ct === 'image/tiff') {
            try { src = (await H.decodeTiffCanvas(H.ab(ib))).canvas.toDataURL('image/png'); } catch {}
          }
          if (!src) src = `data:${ct || 'application/octet-stream'};base64,${b64(ib)}`;
          return { src };
        });
        const r = await window.mammoth.convertToHtml(
          { arrayBuffer: H.ab(bytes) },
          { styleMap: ["p[style-name='Title'] => h1.title:fresh", "p[style-name='Subtitle'] => h2.subtitle:fresh"], convertImage }
        );
        html = r.value;
      }
      art.innerHTML = H.sanitize(html, true);
      H.wireDoc(art, cx);
      updateWordInfo();
    }

    function updateWordInfo() {
      const wc = art.innerText.split(/\s+/).filter(Boolean).length;
      cx.info(`Word document · ${num(wc)} words${editing ? ' · Editing' : ''}${dirty ? ' · Unsaved' : ''}`);
    }

    function markDirty(v) {
      dirty = !!v;
      cx.setDirty?.(dirty);
      saveBtn.disabled = !dirty;
      saveBtn.classList.toggle('primary', dirty);
      updateWordInfo();
    }

    async function saveDocx() {
      if (!canEdit || !dirty) return;
      if (!origZipEntries) await loadOriginalZip(u8);
      const entries = new Map(origZipEntries);
      let relsText = entries.has('word/_rels/document.xml.rels')
        ? new TextDecoder('utf-8').decode(entries.get('word/_rels/document.xml.rels'))
        : `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>`;
      let ctText = entries.has('[Content_Types].xml')
        ? new TextDecoder('utf-8').decode(entries.get('[Content_Types].xml'))
        : `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/></Types>`;

      let newImgIdx = 1;
      let newLinkIdx = 1;
      const addedExts = new Set();
      const linkCache = new Map();

      function addHyperlink(href) {
        if (!/^https?:\/\/|^mailto:/i.test(href)) return '';
        if (linkCache.has(href)) return linkCache.get(href);
        let rId;
        do { rId = `rIdKchLink${newLinkIdx++}`; } while (relsText.includes(`"${rId}"`));
        relsText = relsText.replace(
          '</Relationships>',
          `<Relationship Id="${rId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="${xmlEsc(href)}" TargetMode="External"/></Relationships>`
        );
        linkCache.set(href, rId);
        return rId;
      }

      function addNewImage(imgEl) {
        const src = imgEl.getAttribute('src') || '';
        const m = /^data:image\/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=\s]+)$/i.exec(src);
        if (!m) return '';
        let ext = m[1].toLowerCase();
        const mime = ext === 'jpg' ? 'image/jpeg' : `image/${ext}`;
        if (ext === 'jpeg') ext = 'jpg';
        const imgBytes = fromB64(m[2].replace(/\s/g, ''));
        let name, rId;
        do { name = `kch_img_${newImgIdx}.${ext}`; rId = `rIdKch${newImgIdx}`; newImgIdx++; } while (entries.has(`word/media/${name}`) || relsText.includes(`"${rId}"`));
        entries.set(`word/media/${name}`, imgBytes);
        relsText = relsText.replace('</Relationships>', `<Relationship Id="${rId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/${name}"/></Relationships>`);
        if (!addedExts.has(ext) && !new RegExp(`Extension="${ext}"`, 'i').test(ctText)) {
          addedExts.add(ext);
          ctText = ctText.replace('</Types>', `<Default Extension="${ext}" ContentType="${mime}"/></Types>`);
        }
        const natW = imgEl.naturalWidth || 480;
        const natH = imgEl.naturalHeight || 320;
        const maxPx = 560;
        const scale = natW > maxPx ? maxPx / natW : 1;
        const cxEmu = Math.round(natW * scale * 9525);
        const cyEmu = Math.round(natH * scale * 9525);
        const xml = buildDrawingXml(rId, 9000 + newImgIdx, cxEmu, cyEmu, name);
        origBySrc.set(src, xml);
        return xml;
      }

      const bodyInner = serializeDomToDocxBody(art, origBySrc, addNewImage, addHyperlink);
      const fullDocXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document ${origParts.fullAttrs}><w:body>${bodyInner}${origParts.sectPrXml || ''}</w:body></w:document>`;
      const enc = new TextEncoder();
      entries.set('word/document.xml', enc.encode(fullDocXml));
      entries.set('word/_rels/document.xml.rels', enc.encode(relsText));
      entries.set('[Content_Types].xml', enc.encode(ctText));

      const outZip = await makeZip([...entries.entries()]);
      await saveBinary(cx.src.path, outZip);
      u8 = outZip;
      origZipEntries = entries;
      markDirty(false);
    }

    // Toolbar controls
    const editBtn = H.btn('Edit', 'Edit this Word document', () => setEditing(!editing));
    const saveBtn = H.btn('Save', 'Save Word document (Ctrl+S)', () => {
      saveDocx().then(() => toast('Saved ' + base(cx.src.path), 1200)).catch((e) => toast('Save failed: ' + e.message));
    });
    saveBtn.disabled = true;

    const fmtGroup = h('span', 'kv-grp');
    fmtGroup.hidden = true;

    const cmdBtn = (label, title, cmd, arg) => {
      const x = H.btn(label, title, () => {
        art.focus();
        styler.restore();
        document.execCommand(cmd, false, arg || null);
        styler.capture();
        markDirty(true);
        syncToolbar();
      });
      x.onmousedown = (e) => e.preventDefault();
      x.dataset.cmd = cmd;
      return x;
    };

    const blockSel = makeSelect([
      ['P', 'Paragraph'], ['TITLE', 'Title'], ['H1', 'Heading 1'], ['H2', 'Heading 2'], ['H3', 'Heading 3'],
    ], 'Paragraph style', (val) => {
      art.focus();
      styler.restore();
      if (val === 'TITLE') {
        document.execCommand('formatBlock', false, 'H1');
        styler.activeBlocks('h1').forEach((elNode) => elNode.classList.add('title'));
      } else {
        document.execCommand('formatBlock', false, val);
        styler.activeBlocks('h1,h2,h3,p').forEach((elNode) => elNode.classList.remove('title', 'subtitle'));
      }
      styler.capture();
      markDirty(true);
    });

    const fontSel = makeSelect(FONTS, 'Font family', (val) => {
      if (styler.applyInline({ fontFamily: val ? `"${val}"` : '' })) markDirty(true);
      syncToolbar();
    });

    const sizeSel = makeSelect(DOC_SIZES, 'Font size', (val) => {
      if (styler.applyInline({ fontSize: val ? `${val}pt` : '' })) markDirty(true);
      syncToolbar();
    });

    const bBtn = cmdBtn('<b>B</b>', 'Bold (Ctrl+B)', 'bold');
    const iBtn = cmdBtn('<i>I</i>', 'Italic (Ctrl+I)', 'italic');
    const uBtn = cmdBtn('<u>U</u>', 'Underline (Ctrl+U)', 'underline');
    const sBtn = cmdBtn('<s>S</s>', 'Strikethrough', 'strikeThrough');
    const supBtn = cmdBtn('x<sup>2</sup>', 'Superscript', 'superscript');
    const subBtn = cmdBtn('x<sub>2</sub>', 'Subscript', 'subscript');

    const colorCtl = makeColorControl('<b>A</b>', 'Text colour', '#111827', (hex) => {
      if (styler.applyInline({ color: hex })) markDirty(true);
    });
    const hlSel = makeSelect(HIGHLIGHTS, 'Highlight colour', (val) => {
      if (!val) return;
      if (styler.applyInline({ backgroundColor: val === 'none' ? '' : val })) markDirty(true);
      hlSel.value = '';
    });

    const alignBtns = {};
    for (const a of ['left', 'center', 'right', 'justify']) {
      const ab = H.btn(ALIGN_ICONS[a], 'Align ' + a, () => {
        const blocks = styler.activeBlocks();
        if (blocks.length) {
          blocks.forEach((bl) => { bl.style.textAlign = a; });
          markDirty(true);
          syncToolbar();
        }
      });
      ab.onmousedown = (e) => e.preventDefault();
      ab.dataset.align = a;
      alignBtns[a] = ab;
    }

    const lineSel = makeSelect([
      ['', 'Spacing'], ['1', '1.0×'], ['1.15', '1.15×'], ['1.5', '1.5×'], ['2', '2.0×'],
    ], 'Line spacing', (val) => {
      const blocks = styler.activeBlocks();
      if (blocks.length) {
        blocks.forEach((bl) => { bl.style.lineHeight = val || ''; });
        markDirty(true);
      }
    });

    const addTblBtn = H.btn('+ Table', 'Insert 3×3 table', () => {
      art.focus();
      const r = styler.restore();
      const tbl = h('table', '', '<tbody><tr><th>Header 1</th><th>Header 2</th><th>Header 3</th></tr>' +
        '<tr><td>Cell</td><td>Cell</td><td>Cell</td></tr>' +
        '<tr><td>Cell</td><td>Cell</td><td>Cell</td></tr></tbody>');
      const afterP = h('p', '', '<br>');
      let anchor = null;
      if (r && art.contains(r.endContainer)) {
        let cur = r.endContainer.nodeType === 1 ? r.endContainer : r.endContainer.parentElement;
        while (cur && cur.parentElement !== art) cur = cur.parentElement;
        if (cur && cur.parentElement === art) anchor = cur;
      }
      if (anchor) {
        anchor.after(tbl, afterP);
      } else {
        art.append(tbl, afterP);
      }
      const firstCell = tbl.rows[1]?.cells[0];
      if (firstCell) {
        const nr = document.createRange();
        nr.selectNodeContents(firstCell);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(nr);
        styler.capture();
      }
      markDirty(true);
    });
    addTblBtn.onmousedown = (e) => e.preventDefault();

    function activeTable() {
      const r = styler.getSaved();
      const n = r ? (r.startContainer.nodeType === 1 ? r.startContainer : r.startContainer.parentElement) : null;
      return n?.closest('table') || art.querySelector('table:last-of-type');
    }
    const addTblRowBtn = H.btn('+ Row', 'Add row to current table', () => {
      const tbl = activeTable();
      if (!tbl) { toast('Place cursor inside a table first', 1400); return; }
      const bodyT = tbl.tBodies[0] || tbl;
      const refRow = bodyT.rows[bodyT.rows.length - 1];
      const cols = refRow ? refRow.cells.length : 3;
      const tr = document.createElement('tr');
      for (let i = 0; i < cols; i++) tr.appendChild(h('td', '', 'Cell'));
      bodyT.appendChild(tr);
      markDirty(true);
    });
    addTblRowBtn.onmousedown = (e) => e.preventDefault();

    const addTblColBtn = H.btn('+ Col', 'Add column to current table', () => {
      const tbl = activeTable();
      if (!tbl) { toast('Place cursor inside a table first', 1400); return; }
      for (const tr of tbl.rows) {
        const isHead = tr.cells[0]?.tagName === 'TH';
        tr.appendChild(h(isHead ? 'th' : 'td', '', isHead ? 'Header' : 'Cell'));
      }
      markDirty(true);
    });
    addTblColBtn.onmousedown = (e) => e.preventDefault();

    const linkBtn = H.btn('+ Link', 'Insert hyperlink', () => {
      styler.restore();
      const url = window.prompt('Link URL (https://...)');
      if (!url) return;
      art.focus();
      styler.restore();
      const sel = window.getSelection();
      if (sel && sel.isCollapsed) {
        document.execCommand('insertHTML', false, `<a href="${esc(url)}">${esc(url)}</a>`);
      } else {
        document.execCommand('createLink', false, url);
      }
      markDirty(true);
    });
    linkBtn.onmousedown = (e) => e.preventDefault();

    const imgInput = document.createElement('input');
    imgInput.type = 'file'; imgInput.accept = 'image/png,image/jpeg,image/gif,image/webp'; imgInput.hidden = true;
    imgInput.onchange = () => {
      const f = imgInput.files?.[0];
      if (!f) return;
      const fr = new FileReader();
      fr.onload = () => {
        art.focus();
        styler.restore();
        document.execCommand('insertHTML', false, `<p><img src="${fr.result}" alt="${esc(f.name)}"></p>`);
        markDirty(true);
      };
      fr.readAsDataURL(f);
      imgInput.value = '';
    };
    const addImgBtn = H.btn('+ Image', 'Insert an image into the document', () => imgInput.click());
    addImgBtn.onmousedown = (e) => e.preventDefault();

    const clearFmtBtn = H.btn('Clear', 'Clear inline formatting on selection', () => {
      art.focus();
      styler.restore();
      document.execCommand('removeFormat', false, null);
      styler.applyInline({ fontFamily: '', fontSize: '', color: '', backgroundColor: '' });
      markDirty(true);
      syncToolbar();
    });
    clearFmtBtn.onmousedown = (e) => e.preventDefault();

    fmtGroup.append(
      blockSel, fontSel, sizeSel, sep(),
      bBtn, iBtn, uBtn, sBtn, supBtn, subBtn, sep(),
      colorCtl, hlSel, sep(),
      alignBtns.left, alignBtns.center, alignBtns.right, alignBtns.justify, lineSel, sep(),
      cmdBtn('• List', 'Bullet list', 'insertUnorderedList'),
      cmdBtn('1. List', 'Numbered list', 'insertOrderedList'),
      addTblBtn, addTblRowBtn, addTblColBtn, linkBtn, addImgBtn, clearFmtBtn, sep(),
      cmdBtn('Undo', 'Undo', 'undo'),
      cmdBtn('Redo', 'Redo', 'redo'),
      imgInput
    );

    if (canEdit) {
      tools.append(editBtn, fmtGroup, H.spacer(), saveBtn);
    } else {
      tools.hidden = true;
    }

    function syncToolbar() {
      if (!editing) return;
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount) return;
      const n = sel.anchorNode;
      if (!n || !art.contains(n)) return;
      const elNode = n.nodeType === 1 ? n : n.parentElement;
      if (!elNode) return;
      const cs = window.getComputedStyle(elNode);
      bBtn.classList.toggle('on', document.queryCommandState('bold') || +cs.fontWeight >= 600);
      iBtn.classList.toggle('on', document.queryCommandState('italic') || cs.fontStyle === 'italic');
      uBtn.classList.toggle('on', document.queryCommandState('underline'));
      sBtn.classList.toggle('on', document.queryCommandState('strikeThrough'));

      const blk = elNode.closest('h1,h2,h3,p,li');
      if (blk) {
        const tn = blk.tagName;
        blockSel.value = tn === 'H1' && blk.classList.contains('title') ? 'TITLE'
          : (tn === 'H1' || tn === 'H2' || tn === 'H3') ? tn : 'P';
        const ta = blk.style.textAlign || 'left';
        for (const [k, btnEl] of Object.entries(alignBtns)) btnEl.classList.toggle('on', ta === k);
        const lh = blk.style.lineHeight || '';
        lineSel.value = ['1', '1.15', '1.5', '2'].includes(lh) ? lh : '';
      }
      // Inline font / size reflection
      const spanOrEl = elNode.closest('[style*="font-family"]') || elNode;
      const ff = cleanFontName(spanOrEl.style?.fontFamily);
      fontSel.value = FONTS.some(([v]) => v === ff) ? ff : '';
      const szEl = elNode.closest('[style*="font-size"]') || elNode;
      const pt = Math.round(cssSizeToPt(szEl.style?.fontSize));
      sizeSel.value = DOC_SIZES.some(([v]) => +v === pt) ? String(pt) : '';
      const hex = cssColorToHex(elNode.style?.color);
      if (hex) colorCtl.setColor('#' + hex);
    }

    const onSelChange = () => { if (editing) { styler.capture(); syncToolbar(); } };
    document.addEventListener('selectionchange', onSelChange);
    cx.later(() => document.removeEventListener('selectionchange', onSelChange));

    function setEditing(on) {
      editing = !!on;
      art.contentEditable = editing ? 'true' : 'false';
      art.spellcheck = editing;
      art.classList.toggle('editing', editing);
      editBtn.classList.toggle('on', editing);
      editBtn.textContent = editing ? 'Done' : 'Edit';
      fmtGroup.hidden = !editing;
      if (editing) art.focus();
      updateWordInfo();
    }

    art.addEventListener('input', () => { if (editing) markDirty(true); });
    art.addEventListener('keyup', () => { if (editing) { styler.capture(); syncToolbar(); } });
    art.addEventListener('mouseup', () => { if (editing) { styler.capture(); syncToolbar(); } });
    art.addEventListener('paste', (e) => {
      if (!editing) return;
      const items = e.clipboardData?.items || [];
      for (const it of items) {
        if (it.type.startsWith('image/')) {
          e.preventDefault();
          const f = it.getAsFile();
          if (f) {
            const fr = new FileReader();
            fr.onload = () => { document.execCommand('insertHTML', false, `<img src="${fr.result}" alt="pasted image">`); markDirty(true); };
            fr.readAsDataURL(f);
          }
          return;
        }
      }
    });
    art.addEventListener('keydown', (e) => {
      if (!editing) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        saveDocx().then(() => toast('Saved ' + base(cx.src.path), 1200)).catch((err) => toast('Save failed: ' + err.message));
      }
    });

    cx.setSaver?.(saveDocx);
    await renderFromBytes(u8);
  }

  /* ================================================================ 2. SPREADSHEET EDITOR (.xlsx/.xls/.ods & .csv/.tsv) */
  function csvSerialize(rows, d) {
    const needs = (v) => v.includes(d) || v.includes('"') || v.includes('\n') || v.includes('\r');
    return rows.map((r) => r.map((v) => {
      const s = String(v ?? '');
      return needs(s) ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(d)).join('\r\n') + '\r\n';
  }

  function parseCellInput(raw) {
    const s = String(raw ?? '');
    if (s === '') return null;
    if (s.startsWith('=')) return { t: 'n', f: s.slice(1) };
    if (/^(true|false)$/i.test(s.trim())) return { t: 'b', v: s.trim().toLowerCase() === 'true' };
    if (/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(s.trim())) return { t: 'n', v: Number(s.trim()) };
    return { t: 's', v: s };
  }

  const SS_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main';
  const XLSX_SIZES = [['', 'Size'], [9, '9 pt'], [10, '10 pt'], [11, '11 pt'], [12, '12 pt'], [14, '14 pt'], [16, '16 pt'], [18, '18 pt'], [20, '20 pt'], [24, '24 pt']];

  /* Read cell styles (font, size, bold, italic, colour, fill, alignment) from xl/styles.xml + sheet XMLs */
  async function readXlsxStyles(bytes, H) {
    const out = new Map(); // sheetName -> Map(cellRef -> { font, size, b, i, color, bg, align })
    try {
      const z = await H.zipOpen(H.memSrc('book.xlsx', bytes));
      const byName = new Map(z.entries.map((e) => [e.name, e]));
      const readXml = async (p) => {
        const en = byName.get(p);
        if (!en) return null;
        return new DOMParser().parseFromString(new TextDecoder('utf-8').decode(await z.read(en, 32 * MB)), 'application/xml');
      };
      const stDoc = await readXml('xl/styles.xml');
      const wbDoc = await readXml('xl/workbook.xml');
      const relDoc = await readXml('xl/_rels/workbook.xml.rels');
      if (!stDoc || !wbDoc || !relDoc) return out;

      const rgb6 = (elNode) => {
        const rgb = elNode?.getAttribute('rgb');
        if (!rgb) return '';
        const h6 = rgb.length === 8 ? rgb.slice(2) : rgb.length === 6 ? rgb : '';
        return /^[0-9a-f]{6}$/i.test(h6) ? '#' + h6.toUpperCase() : '';
      };

      const fonts = xmlKids(xmlKid(stDoc.documentElement, 'fonts'), 'font').map((f) => ({
        b: !!xmlKid(f, 'b'),
        i: !!xmlKid(f, 'i'),
        size: +(xmlKid(f, 'sz')?.getAttribute('val') || 0),
        font: xmlKid(f, 'name')?.getAttribute('val') || '',
        color: rgb6(xmlKid(f, 'color')),
      }));
      const fills = xmlKids(xmlKid(stDoc.documentElement, 'fills'), 'fill').map((fl) => {
        const pf = xmlKid(fl, 'patternFill');
        if (!pf || pf.getAttribute('patternType') !== 'solid') return '';
        return rgb6(xmlKid(pf, 'fgColor'));
      });
      const xfs = xmlKids(xmlKid(stDoc.documentElement, 'cellXfs'), 'xf').map((xf) => {
        const fi = +(xf.getAttribute('fontId') || 0);
        const fli = +(xf.getAttribute('fillId') || 0);
        const al = xmlKid(xf, 'alignment')?.getAttribute('horizontal') || '';
        const f = fonts[fi] || {};
        const bg = fills[fli] || '';
        const st = {};
        if (f.b) st.b = true;
        if (f.i) st.i = true;
        if (f.size && f.size !== 11) st.size = f.size;
        if (f.font && f.font !== 'Calibri') st.font = f.font;
        if (f.color && f.color !== '#000000') st.color = f.color;
        if (bg) st.bg = bg;
        if (['left', 'center', 'right'].includes(al)) st.align = al;
        return Object.keys(st).length ? st : null;
      });

      const relMap = new Map();
      for (const r of relDoc.getElementsByTagNameNS('*', 'Relationship')) {
        let t = (r.getAttribute('Target') || '').replace(/^\/+/, '');
        if (!t.startsWith('xl/')) t = 'xl/' + t.replace(/^\.\//, '');
        relMap.set(r.getAttribute('Id'), t);
      }

      for (const sh of wbDoc.getElementsByTagNameNS('*', 'sheet')) {
        const name = sh.getAttribute('name');
        const rId = sh.getAttributeNS(R_NS, 'id') || xmlAttr(sh, 'id');
        const path = relMap.get(rId);
        if (!name || !path) continue;
        const sDoc = await readXml(path);
        if (!sDoc) continue;
        const sm = new Map();
        for (const c of sDoc.getElementsByTagNameNS('*', 'c')) {
          const si = c.getAttribute('s');
          if (si == null) continue;
          const st = xfs[+si];
          const ref = c.getAttribute('r');
          if (st && ref) sm.set(ref, { ...st });
        }
        if (sm.size) out.set(name, sm);
      }
    } catch (e) {
      console.warn('readXlsxStyles', e);
    }
    return out;
  }

  /* Patch xl/styles.xml + xl/worksheets/sheet*.xml inside a SheetJS-generated .xlsx archive */
  async function writeXlsxStyles(xlsxBytes, sheetStyles, H) {
    let any = false;
    for (const m of sheetStyles.values()) if (m.size) { any = true; break; }
    if (!any) return xlsxBytes;

    const z = await H.zipOpen(H.memSrc('out.xlsx', xlsxBytes));
    const entries = new Map();
    for (const en of z.entries) if (!en.dir) entries.set(en.name, await z.read(en, 64 * MB));
    if (!entries.has('xl/styles.xml') || !entries.has('xl/workbook.xml') || !entries.has('xl/_rels/workbook.xml.rels')) {
      return xlsxBytes;
    }

    const dec = new TextDecoder('utf-8');
    const enc = new TextEncoder();
    const parser = new DOMParser();
    const ser = new XMLSerializer();

    const stDoc = parser.parseFromString(dec.decode(entries.get('xl/styles.xml')), 'application/xml');
    const ns = stDoc.documentElement.namespaceURI || SS_NS;
    const mk = (doc, tag, attrs) => {
      const n = doc.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
      return n;
    };

    let fontsEl = xmlKid(stDoc.documentElement, 'fonts');
    if (!fontsEl) { fontsEl = mk(stDoc, 'fonts', { count: 0 }); stDoc.documentElement.appendChild(fontsEl); }
    let fillsEl = xmlKid(stDoc.documentElement, 'fills');
    if (!fillsEl) { fillsEl = mk(stDoc, 'fills', { count: 0 }); stDoc.documentElement.appendChild(fillsEl); }
    let xfsEl = xmlKid(stDoc.documentElement, 'cellXfs');
    if (!xfsEl) { xfsEl = mk(stDoc, 'cellXfs', { count: 0 }); stDoc.documentElement.appendChild(xfsEl); }

    let fontCount = xmlKids(fontsEl, 'font').length;
    let fillCount = xmlKids(fillsEl, 'fill').length;
    let xfCount = xmlKids(xfsEl, 'xf').length;
    // Excel reserves fills 0 (none) and 1 (gray125)
    while (fillCount < 2) {
      const fl = mk(stDoc, 'fill');
      fl.appendChild(mk(stDoc, 'patternFill', { patternType: fillCount === 0 ? 'none' : 'gray125' }));
      fillsEl.appendChild(fl);
      fillCount++;
    }

    const fontIds = new Map();
    const fillIds = new Map();
    const xfIds = new Map();

    function getFontId(st) {
      const key = `${st.font || 'Calibri'}|${st.size || 11}|${st.b ? 1 : 0}|${st.i ? 1 : 0}|${st.color || ''}`;
      if (fontIds.has(key)) return fontIds.get(key);
      const f = mk(stDoc, 'font');
      if (st.b) f.appendChild(mk(stDoc, 'b'));
      if (st.i) f.appendChild(mk(stDoc, 'i'));
      f.appendChild(mk(stDoc, 'sz', { val: st.size || 11 }));
      const hex = cssColorToHex(st.color);
      if (hex) f.appendChild(mk(stDoc, 'color', { rgb: 'FF' + hex }));
      f.appendChild(mk(stDoc, 'name', { val: st.font || 'Calibri' }));
      fontsEl.appendChild(f);
      const id = fontCount++;
      fontIds.set(key, id);
      return id;
    }
    function getFillId(st) {
      const hex = cssColorToHex(st.bg);
      if (!hex) return 0;
      if (fillIds.has(hex)) return fillIds.get(hex);
      const fl = mk(stDoc, 'fill');
      const pf = mk(stDoc, 'patternFill', { patternType: 'solid' });
      pf.appendChild(mk(stDoc, 'fgColor', { rgb: 'FF' + hex }));
      pf.appendChild(mk(stDoc, 'bgColor', { indexed: 64 }));
      fl.appendChild(pf);
      fillsEl.appendChild(fl);
      const id = fillCount++;
      fillIds.set(hex, id);
      return id;
    }
    function getXfId(st) {
      const key = JSON.stringify([st.font || '', st.size || 0, !!st.b, !!st.i, st.color || '', st.bg || '', st.align || '']);
      if (xfIds.has(key)) return xfIds.get(key);
      const needsFont = !!(st.font || st.size || st.b || st.i || st.color);
      const fontId = needsFont ? getFontId(st) : 0;
      const fillId = getFillId(st);
      const attrs = { numFmtId: 0, fontId, fillId, borderId: 0, xfId: 0 };
      if (needsFont) attrs.applyFont = 1;
      if (fillId) attrs.applyFill = 1;
      if (st.align) attrs.applyAlignment = 1;
      const xf = mk(stDoc, 'xf', attrs);
      if (st.align) xf.appendChild(mk(stDoc, 'alignment', { horizontal: st.align }));
      xfsEl.appendChild(xf);
      const id = xfCount++;
      xfIds.set(key, id);
      return id;
    }

    const wbDoc = parser.parseFromString(dec.decode(entries.get('xl/workbook.xml')), 'application/xml');
    const relDoc = parser.parseFromString(dec.decode(entries.get('xl/_rels/workbook.xml.rels')), 'application/xml');
    const relMap = new Map();
    for (const r of relDoc.getElementsByTagNameNS('*', 'Relationship')) {
      let t = (r.getAttribute('Target') || '').replace(/^\/+/, '');
      if (!t.startsWith('xl/')) t = 'xl/' + t.replace(/^\.\//, '');
      relMap.set(r.getAttribute('Id'), t);
    }

    for (const sh of wbDoc.getElementsByTagNameNS('*', 'sheet')) {
      const name = sh.getAttribute('name');
      const sm = sheetStyles.get(name);
      if (!sm || !sm.size) continue;
      const rId = sh.getAttributeNS(R_NS, 'id') || xmlAttr(sh, 'id');
      const path = relMap.get(rId);
      if (!path || !entries.has(path)) continue;
      const sDoc = parser.parseFromString(dec.decode(entries.get(path)), 'application/xml');
      for (const c of sDoc.getElementsByTagNameNS('*', 'c')) {
        const ref = c.getAttribute('r');
        const st = ref && sm.get(ref);
        if (st && Object.keys(st).length) c.setAttribute('s', String(getXfId(st)));
      }
      entries.set(path, enc.encode(ser.serializeToString(sDoc)));
    }

    fontsEl.setAttribute('count', String(fontCount));
    fillsEl.setAttribute('count', String(fillCount));
    xfsEl.setAttribute('count', String(xfCount));
    entries.set('xl/styles.xml', enc.encode(ser.serializeToString(stDoc)));
    return makeZip([...entries.entries()]);
  }

  function applyCellCss(td, st) {
    td.style.fontFamily = st?.font ? `"${st.font}", var(--sans)` : '';
    td.style.fontSize = st?.size ? `${st.size}pt` : '';
    td.style.fontWeight = st?.b ? '700' : '';
    td.style.fontStyle = st?.i ? 'italic' : '';
    td.style.color = st?.color || '';
    td.style.backgroundColor = st?.bg || '';
    td.style.textAlign = st?.align || '';
  }

  function buildEditableGrid(cx, H, opts) {
    const {
      canEdit, getSheet, setCell, addRow, delRow, addCol, delCol, sortCol,
      getCellStyle, setCellStyle, onSave, sheetNames, onSwitchSheet, onAddSheet, onRenameSheet,
    } = opts;
    const tools = H.bar(cx);
    tools.classList.add('kv-editbar');

    const addrEl = h('span', 'kv-cell-addr', 'A1');
    const fx = document.createElement('input');
    fx.type = 'text'; fx.className = 'kv-fx'; fx.placeholder = canEdit ? 'Select a cell to edit value or =FORMULA' : 'Read-only preview';
    fx.disabled = !canEdit;

    const filterInp = document.createElement('input');
    filterInp.type = 'search'; filterInp.className = 'kv-filter'; filterInp.placeholder = 'Filter rows…';
    filterInp.oninput = () => renderGrid();

    const saveBtn = H.btn('Save', 'Save spreadsheet (Ctrl+S)', () => {
      onSave().then(() => { markDirty(false); toast('Saved ' + base(cx.src.path), 1200); }).catch((e) => toast('Save failed: ' + e.message));
    });
    saveBtn.disabled = true;

    let styleCtrls = null;
    if (canEdit) {
      tools.append(
        addrEl, fx, filterInp, sep(),
        H.btn('+ Row', 'Add a row at the bottom', () => { addRow(); markDirty(true); renderGrid(); }),
        H.btn('- Row', 'Delete selected row', () => { if (selR >= 0 && delRow(selR)) { selR = Math.max(0, selR - 1); markDirty(true); renderGrid(); } }),
        H.btn('+ Col', 'Add a column on the right', () => { addCol(); markDirty(true); renderGrid(); }),
        ...(delCol ? [H.btn('- Col', 'Delete selected column', () => { if (selC >= 0 && delCol(selC)) { selC = Math.max(0, selC - 1); markDirty(true); renderGrid(); } })] : []),
        ...(sortCol ? [
          H.btn('A↓Z', 'Sort selected column ascending', () => { if (sortCol(selC, 1)) { markDirty(true); renderGrid(); } }),
          H.btn('Z↓A', 'Sort selected column descending', () => { if (sortCol(selC, -1)) { markDirty(true); renderGrid(); } }),
        ] : []),
        ...(onAddSheet ? [H.btn('+ Sheet', 'Add a new worksheet', () => { const nm = window.prompt('New sheet name'); if (nm && onAddSheet(nm.trim())) { markDirty(true); rebuildSheetTabs(); renderGrid(); } })] : []),
        saveBtn
      );

      if (setCellStyle) {
        const fmtBar = H.bar(cx);
        fmtBar.classList.add('kv-editbar', 'kv-subbar');
        const patchStyle = (patch) => {
          setCellStyle(selR, selC, patch);
          markDirty(true);
          const td = grid.querySelector(`td[data-r="${selR}"][data-c="${selC}"]`);
          if (td) applyCellCss(td, getCellStyle(selR, selC));
          syncStyleBar();
        };
        const fontSel = makeSelect(FONTS, 'Cell font', (v) => patchStyle({ font: v || undefined }));
        const sizeSel = makeSelect(XLSX_SIZES, 'Cell font size', (v) => patchStyle({ size: v ? +v : undefined }));
        const bBtn = H.btn('<b>B</b>', 'Bold cell', () => patchStyle({ b: !getCellStyle(selR, selC)?.b }));
        const iBtn = H.btn('<i>I</i>', 'Italic cell', () => patchStyle({ i: !getCellStyle(selR, selC)?.i }));
        const colCtl = makeColorControl('<b>A</b>', 'Cell text colour', '#111827', (hex) => patchStyle({ color: hex }));
        const bgCtl = makeColorControl('Fill', 'Cell background fill', '#fef08a', (hex) => patchStyle({ bg: hex }));
        const clearFillBtn = H.btn('No fill', 'Clear cell fill and text colour', () => patchStyle({ bg: undefined, color: undefined }));
        const alBtns = {};
        for (const a of ['left', 'center', 'right']) {
          alBtns[a] = H.btn(ALIGN_ICONS[a], 'Align ' + a, () => patchStyle({ align: a }));
        }
        fmtBar.append(fontSel, sizeSel, sep(), bBtn, iBtn, sep(), colCtl, bgCtl, clearFillBtn, sep(), alBtns.left, alBtns.center, alBtns.right);
        styleCtrls = { fontSel, sizeSel, bBtn, iBtn, colCtl, bgCtl, alBtns };
      }
    } else {
      tools.append(addrEl, fx, filterInp);
    }

    const b = H.body(cx, 'kv-split');
    const grid = h('div', 'kv-scroll kv-grow');
    const tabs = h('div', 'kv-sheets');
    b.append(grid, tabs);

    let selR = 0, selC = 0, dirty = false, curSheet = 0;

    function markDirty(v) {
      dirty = !!v;
      cx.setDirty?.(dirty);
      saveBtn.disabled = !dirty;
      saveBtn.classList.toggle('primary', dirty);
    }
    function syncStyleBar() {
      if (!styleCtrls || !getCellStyle) return;
      const st = getCellStyle(selR, selC) || {};
      styleCtrls.fontSel.value = FONTS.some(([v]) => v === (st.font || '')) ? (st.font || '') : '';
      styleCtrls.sizeSel.value = st.size ? String(st.size) : '';
      styleCtrls.bBtn.classList.toggle('on', !!st.b);
      styleCtrls.iBtn.classList.toggle('on', !!st.i);
      if (st.color) styleCtrls.colCtl.setColor(st.color);
      if (st.bg) styleCtrls.bgCtl.setColor(st.bg);
      for (const [k, btnEl] of Object.entries(styleCtrls.alBtns)) btnEl.classList.toggle('on', st.align === k);
    }

    function colLabel(c) {
      let s = '', n = c + 1;
      while (n > 0) { const rem = (n - 1) % 26; s = String.fromCharCode(65 + rem) + s; n = Math.floor((n - 1) / 26); }
      return s;
    }

    function selectCell(r, c, focusInline) {
      const sh = getSheet();
      if (!sh.rows.length || !sh.cols.length) return;
      selR = Math.max(0, Math.min(r, sh.rows.length - 1));
      selC = Math.max(0, Math.min(c, sh.cols.length - 1));
      grid.querySelectorAll('td.kv-sel').forEach((x) => x.classList.remove('kv-sel'));
      const td = grid.querySelector(`td[data-r="${selR}"][data-c="${selC}"]`);
      if (td) {
        td.classList.add('kv-sel');
        td.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      }
      addrEl.textContent = `${colLabel(selC)}${selR + (sh.rowOffset ?? 1)}`;
      const rawVal = sh.rawAt ? sh.rawAt(selR, selC) : (sh.rows[selR]?.[selC] ?? '');
      fx.value = String(rawVal ?? '');
      syncStyleBar();
      if (focusInline && canEdit && td) beginInlineEdit(td, selR, selC);
    }

    function commitCell(r, c, newVal) {
      const sh = getSheet();
      const oldRaw = sh.rawAt ? sh.rawAt(r, c) : (sh.rows[r]?.[c] ?? '');
      if (String(newVal) === String(oldRaw ?? '')) return;
      const display = setCell(r, c, newVal);
      markDirty(true);
      const td = grid.querySelector(`td[data-r="${r}"][data-c="${c}"]`);
      if (td) td.textContent = display;
      if (r === selR && c === selC) fx.value = String(newVal);
    }

    function beginInlineEdit(td, r, c) {
      if (!canEdit || td.querySelector('input')) return;
      const sh = getSheet();
      const orig = String((sh.rawAt ? sh.rawAt(r, c) : sh.rows[r]?.[c]) ?? '');
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'kv-cell-in';
      inp.value = orig;
      td.textContent = '';
      td.appendChild(inp);
      inp.focus();
      inp.select();
      let done = false;
      const finish = (apply, moveR, moveC) => {
        if (done) return;
        done = true;
        const val = inp.value;
        inp.remove();
        if (apply) commitCell(r, c, val);
        else td.textContent = String(sh.rows[r]?.[c] ?? '');
        if (moveR != null || moveC != null) selectCell(r + (moveR || 0), c + (moveC || 0), false);
      };
      inp.onblur = () => finish(true);
      inp.onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); finish(true, 1, 0); }
        else if (e.key === 'Tab') { e.preventDefault(); finish(true, 0, e.shiftKey ? -1 : 1); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      };
    }

    fx.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commitCell(selR, selC, fx.value); selectCell(selR + 1, selC, false); }
      else if (e.key === 'Escape') { const sh = getSheet(); fx.value = String((sh.rawAt ? sh.rawAt(selR, selC) : sh.rows[selR]?.[selC]) ?? ''); fx.blur(); }
    });
    fx.addEventListener('change', () => { if (canEdit) commitCell(selR, selC, fx.value); });

    function renderGrid() {
      const sh = getSheet();
      grid.innerHTML = '';
      if (!sh.rows.length && !sh.cols.length) {
        H.note(grid, 'This sheet is empty', canEdit ? 'Click + Row to start editing.' : '');
        cx.info(sh.info || 'empty');
        return;
      }
      const q = filterInp.value.trim().toLowerCase();
      const rowIndices = [];
      for (let ri = 0; ri < sh.rows.length; ri++) {
        if (!q || sh.rows[ri].some((v) => String(v ?? '').toLowerCase().includes(q))) rowIndices.push(ri);
      }

      const wrap = h('div', 'kv-tablewrap');
      const tbl = h('table', 'kv-table kv-grid-edit');
      const thead = h('thead');
      const hr = h('tr');
      hr.appendChild(h('th', 'ix', ''));
      sh.cols.forEach((col, ci) => {
        const th = h('th', '', `<span>${esc(col || colLabel(ci))}</span>`);
        if (sh.editableHeader && canEdit) {
          th.ondblclick = () => {
            const nv = window.prompt('Rename column header', col);
            if (nv !== null && nv !== col) { sh.setHeader(ci, nv); markDirty(true); renderGrid(); }
          };
          th.title = 'Double-click to rename column';
        }
        hr.appendChild(th);
      });
      thead.appendChild(hr);
      tbl.appendChild(thead);

      const tbody = h('tbody');
      const cap = Math.min(rowIndices.length, 500);
      let shown = 0;
      const appendRows = (limit) => {
        const frag = document.createDocumentFragment();
        const end = Math.min(rowIndices.length, shown + limit);
        for (let k = shown; k < end; k++) {
          const ri = rowIndices[k];
          const row = sh.rows[ri];
          const tr = h('tr');
          tr.appendChild(h('td', 'ix', String(ri + (sh.rowOffset ?? 1))));
          for (let ci = 0; ci < sh.cols.length; ci++) {
            const val = row[ci] ?? '';
            const td = h('td', '', esc(val));
            td.dataset.r = String(ri);
            td.dataset.c = String(ci);
            if (getCellStyle) applyCellCss(td, getCellStyle(ri, ci));
            if (ri === selR && ci === selC) td.classList.add('kv-sel');
            tr.appendChild(td);
          }
          frag.appendChild(tr);
        }
        shown = end;
        tbody.appendChild(frag);
      };
      appendRows(cap);
      tbl.appendChild(tbody);
      wrap.appendChild(tbl);
      if (shown < rowIndices.length) {
        const moreBtn = H.btn(`Show more rows (${num(rowIndices.length - shown)} left)`, '', () => {
          appendRows(500);
          if (shown >= rowIndices.length) moreBtn.remove();
          else moreBtn.textContent = `Show more rows (${num(rowIndices.length - shown)} left)`;
        });
        moreBtn.className = 'kv-more';
        wrap.appendChild(moreBtn);
      }
      grid.appendChild(wrap);
      const filterNote = q ? ` · ${num(rowIndices.length)} matching "${q}"` : '';
      cx.info((sh.info || `${num(sh.rows.length)} rows × ${sh.cols.length} columns`) + filterNote);
      selectCell(selR, selC, false);
    }

    grid.addEventListener('click', (e) => {
      const td = e.target.closest('td[data-r]');
      if (!td) return;
      const r = +td.dataset.r, c = +td.dataset.c;
      if (r === selR && c === selC && canEdit && e.pointerType === 'touch') beginInlineEdit(td, r, c);
      else selectCell(r, c, false);
    });
    grid.addEventListener('dblclick', (e) => {
      const td = e.target.closest('td[data-r]');
      if (td && canEdit) selectCell(+td.dataset.r, +td.dataset.c, true);
    });

    function rebuildSheetTabs() {
      const names = sheetNames ? sheetNames() : [];
      tabs.innerHTML = '';
      names.forEach((n, i) => {
        const x = h('button', i === curSheet ? 'on' : '', esc(n));
        x.onclick = () => { curSheet = i; onSwitchSheet(i); selR = 0; selC = 0; rebuildSheetTabs(); renderGrid(); };
        if (onRenameSheet && canEdit) {
          x.title = 'Double-click to rename sheet';
          x.ondblclick = () => {
            const nv = window.prompt('Rename sheet', n);
            if (nv && nv.trim() && nv.trim() !== n && onRenameSheet(i, nv.trim())) {
              markDirty(true);
              rebuildSheetTabs();
              renderGrid();
            }
          };
        }
        tabs.appendChild(x);
      });
      tabs.hidden = names.length < 1 || (names.length === 1 && !onRenameSheet);
    }

    cx.setSaver?.(async () => { await onSave(); markDirty(false); });
    rebuildSheetTabs();
    renderGrid();
  }

  async function sheetEditor(cx, H) {
    let u8 = await cx.src.bytes(100 * MB);
    await H.script(H.LIB.xlsx);
    const X = window.XLSX;
    let wb = X.read(u8, { type: 'array', cellFormula: true, cellDates: true });
    const ext = H.extOf(cx.src.name);
    const isXlsx = ext === 'xlsx' || ext === 'xlsm';
    const sheetStyles = isXlsx ? await readXlsxStyles(u8, H) : new Map();
    let sheetIdx = 0;

    function activeWs() {
      const name = wb.SheetNames[sheetIdx];
      let ws = wb.Sheets[name];
      if (!ws) { ws = {}; wb.Sheets[name] = ws; }
      return { name, ws };
    }
    function activeStyleMap() {
      const { name } = activeWs();
      if (!sheetStyles.has(name)) sheetStyles.set(name, new Map());
      return sheetStyles.get(name);
    }
    function ensureRange(ws, r, c) {
      const cur = ws['!ref'] ? X.utils.decode_range(ws['!ref']) : { s: { r: 0, c: 0 }, e: { r: 0, c: 0 } };
      cur.s.r = Math.min(cur.s.r, 0, r);
      cur.s.c = Math.min(cur.s.c, 0, c);
      cur.e.r = Math.max(cur.e.r, r);
      cur.e.c = Math.max(cur.e.c, c);
      ws['!ref'] = X.utils.encode_range(cur);
    }

    function getSheetData() {
      const { name, ws } = activeWs();
      if (!ws['!ref']) return { rows: [], cols: [], rowOffset: 1, info: `${name} · empty` };
      const rng = X.utils.decode_range(ws['!ref']);
      const nrows = Math.min(rng.e.r + 1, 2000);
      const ncols = Math.min(Math.max(rng.e.c + 1, 1), 200);
      const cols = Array.from({ length: ncols }, (_, c) => X.utils.encode_col(c));
      const rows = [];
      for (let r = 0; r < nrows; r++) {
        const line = new Array(ncols);
        for (let c = 0; c < ncols; c++) {
          const cell = ws[X.utils.encode_cell({ r, c })];
          line[c] = cell ? (cell.f && cell.v === undefined ? '=' + cell.f : X.utils.format_cell(cell)) : '';
        }
        rows.push(line);
      }
      const rawAt = (r, c) => {
        const cell = ws[X.utils.encode_cell({ r, c })];
        if (!cell) return '';
        if (cell.f) return '=' + cell.f;
        return cell.w != null ? cell.w : (cell.v != null ? String(cell.v) : '');
      };
      return {
        rows, cols, rowOffset: 1, rawAt,
        info: `${name} · ${num(rng.e.r + 1)} rows × ${rng.e.c + 1} columns${rng.e.r + 1 > 2000 ? ' · showing first 2,000 rows (all rows preserved on save)' : ''}`,
      };
    }

    buildEditableGrid(cx, H, {
      canEdit: !!cx.src.path,
      sheetNames: () => wb.SheetNames,
      onSwitchSheet: (i) => { sheetIdx = i; },
      onAddSheet: (nm) => {
        if (wb.SheetNames.includes(nm)) return false;
        const ws = X.utils.aoa_to_sheet([['']]);
        X.utils.book_append_sheet(wb, ws, nm);
        sheetIdx = wb.SheetNames.length - 1;
        return true;
      },
      onRenameSheet: (idx, newName) => {
        const oldName = wb.SheetNames[idx];
        if (!oldName || wb.SheetNames.includes(newName)) return false;
        wb.SheetNames[idx] = newName;
        wb.Sheets[newName] = wb.Sheets[oldName];
        delete wb.Sheets[oldName];
        if (sheetStyles.has(oldName)) { sheetStyles.set(newName, sheetStyles.get(oldName)); sheetStyles.delete(oldName); }
        return true;
      },
      getSheet: getSheetData,
      getCellStyle: (r, c) => activeStyleMap().get(X.utils.encode_cell({ r, c })) || null,
      setCellStyle: (r, c, patch) => {
        const { ws } = activeWs();
        const ref = X.utils.encode_cell({ r, c });
        const sm = activeStyleMap();
        const cur = { ...(sm.get(ref) || {}) };
        for (const [k, v] of Object.entries(patch)) {
          if (v === undefined || v === '' || v === false) delete cur[k];
          else cur[k] = v;
        }
        if (Object.keys(cur).length) {
          sm.set(ref, cur);
          if (!ws[ref]) { ws[ref] = { t: 's', v: '' }; ensureRange(ws, r, c); }
        } else {
          sm.delete(ref);
        }
      },
      setCell: (r, c, rawVal) => {
        const { ws } = activeWs();
        const ref = X.utils.encode_cell({ r, c });
        const parsed = parseCellInput(rawVal);
        if (!parsed) { delete ws[ref]; return ''; }
        ws[ref] = parsed;
        ensureRange(ws, r, c);
        return parsed.f ? '=' + parsed.f : X.utils.format_cell(parsed);
      },
      addRow: () => {
        const { ws } = activeWs();
        const cur = ws['!ref'] ? X.utils.decode_range(ws['!ref']) : { s: { r: 0, c: 0 }, e: { r: -1, c: 0 } };
        ensureRange(ws, cur.e.r + 1, Math.max(0, cur.e.c));
      },
      delRow: (rowIdx) => {
        const { ws } = activeWs();
        if (!ws['!ref']) return false;
        const rng = X.utils.decode_range(ws['!ref']);
        if (rowIdx > rng.e.r || rng.e.r < 0) return false;
        const sm = activeStyleMap();
        for (let r = rowIdx; r < rng.e.r; r++) {
          for (let c = 0; c <= rng.e.c; c++) {
            const dst = X.utils.encode_cell({ r, c });
            const src = X.utils.encode_cell({ r: r + 1, c });
            if (ws[src]) ws[dst] = ws[src]; else delete ws[dst];
            if (sm.has(src)) sm.set(dst, sm.get(src)); else sm.delete(dst);
          }
        }
        for (let c = 0; c <= rng.e.c; c++) {
          const ref = X.utils.encode_cell({ r: rng.e.r, c });
          delete ws[ref]; sm.delete(ref);
        }
        if (rng.e.r === 0) delete ws['!ref'];
        else { rng.e.r -= 1; ws['!ref'] = X.utils.encode_range(rng); }
        return true;
      },
      addCol: () => {
        const { ws } = activeWs();
        const cur = ws['!ref'] ? X.utils.decode_range(ws['!ref']) : { s: { r: 0, c: 0 }, e: { r: 0, c: -1 } };
        ensureRange(ws, Math.max(0, cur.e.r), cur.e.c + 1);
      },
      delCol: (colIdx) => {
        const { ws } = activeWs();
        if (!ws['!ref']) return false;
        const rng = X.utils.decode_range(ws['!ref']);
        if (colIdx > rng.e.c || rng.e.c < 0) return false;
        const sm = activeStyleMap();
        for (let r = 0; r <= rng.e.r; r++) {
          for (let c = colIdx; c < rng.e.c; c++) {
            const dst = X.utils.encode_cell({ r, c });
            const src = X.utils.encode_cell({ r, c: c + 1 });
            if (ws[src]) ws[dst] = ws[src]; else delete ws[dst];
            if (sm.has(src)) sm.set(dst, sm.get(src)); else sm.delete(dst);
          }
          const last = X.utils.encode_cell({ r, c: rng.e.c });
          delete ws[last]; sm.delete(last);
        }
        if (rng.e.c === 0) delete ws['!ref'];
        else { rng.e.c -= 1; ws['!ref'] = X.utils.encode_range(rng); }
        return true;
      },
      sortCol: (colIdx, dir) => {
        const { ws } = activeWs();
        if (!ws['!ref']) return false;
        const rng = X.utils.decode_range(ws['!ref']);
        if (rng.e.r < 2) return false; // keep row 0 as header
        const sm = activeStyleMap();
        const items = [];
        for (let r = 1; r <= rng.e.r; r++) {
          const cells = [], styles = [];
          for (let c = 0; c <= rng.e.c; c++) {
            const ref = X.utils.encode_cell({ r, c });
            cells.push(ws[ref] || null);
            styles.push(sm.get(ref) || null);
          }
          const keyCell = cells[colIdx];
          const key = keyCell ? (keyCell.v !== undefined ? keyCell.v : X.utils.format_cell(keyCell)) : '';
          items.push({ cells, styles, key });
        }
        const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
        items.sort((a, b) => {
          if (typeof a.key === 'number' && typeof b.key === 'number') return dir * (a.key - b.key);
          return dir * coll.compare(String(a.key ?? ''), String(b.key ?? ''));
        });
        items.forEach((it, idx) => {
          const r = idx + 1;
          for (let c = 0; c <= rng.e.c; c++) {
            const ref = X.utils.encode_cell({ r, c });
            if (it.cells[c]) ws[ref] = it.cells[c]; else delete ws[ref];
            if (it.styles[c]) sm.set(ref, it.styles[c]); else sm.delete(ref);
          }
        });
        return true;
      },
      onSave: async () => {
        const bookType = ext === 'ods' ? 'ods' : ext === 'xls' ? 'xls' : 'xlsx';
        const out = X.write(wb, { bookType, type: 'array' });
        let bytes = out instanceof Uint8Array ? out : new Uint8Array(out);
        if (bookType === 'xlsx') bytes = await writeXlsxStyles(bytes, sheetStyles, H);
        await saveBinary(cx.src.path, bytes);
        u8 = bytes;
      },
    });
  }

  async function csvEditor(cx, H) {
    const { text, partial } = await cx.src.text();
    const ext = H.extOf(cx.src.name);
    const d = ext === 'tsv' || ext === 'tab' ? '\t' : ext === 'psv' ? '|' : H.sniff(text);
    const { rows, more } = H.parseDelimited(text, d);
    if (partial && rows.length > 1) rows.pop();
    const head = rows.length ? rows.shift() : [];
    const canEdit = !!cx.src.path && !partial && !more;

    buildEditableGrid(cx, H, {
      canEdit,
      getSheet: () => ({
        rows, cols: head, rowOffset: 2, editableHeader: true,
        setHeader: (ci, v) => { head[ci] = v; },
        info: `${num(rows.length)}${more || partial ? '+' : ''} rows × ${head.length} columns${!canEdit && cx.src.path ? ' · too large for grid edit (use Source)' : ''}`,
      }),
      setCell: (r, c, v) => {
        while (rows[r].length <= c) rows[r].push('');
        rows[r][c] = String(v);
        return String(v);
      },
      addRow: () => { rows.push(new Array(Math.max(1, head.length)).fill('')); },
      delRow: (ri) => { if (ri < 0 || ri >= rows.length) return false; rows.splice(ri, 1); return true; },
      addCol: () => { head.push(`Col ${head.length + 1}`); rows.forEach((r) => r.push('')); },
      delCol: (ci) => {
        if (ci < 0 || ci >= head.length || head.length <= 1) return false;
        head.splice(ci, 1);
        rows.forEach((r) => { if (ci < r.length) r.splice(ci, 1); });
        return true;
      },
      sortCol: (ci, dir) => {
        if (rows.length < 2) return false;
        const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
        rows.sort((a, b) => dir * coll.compare(String(a[ci] ?? ''), String(b[ci] ?? '')));
        return true;
      },
      onSave: async () => {
        const out = csvSerialize([head, ...rows], d);
        const res = await api('/api/files/write', { path: cx.src.path, content: out, force: true });
        if (cx.tab) { cx.tab.text = out; cx.tab.mtime = res.mtime; }
      },
    });
  }

  /* ================================================================ 3. PDF VIEWER + DRAWING / SHAPE / TEXT EDITOR */
  const PDF_FONTS = [
    ['system-ui, -apple-system, Segoe UI, sans-serif', 'Sans'],
    ['Georgia, Times New Roman, serif', 'Serif'],
    ['Consolas, Courier New, monospace', 'Mono'],
    ['Arial, sans-serif', 'Arial'],
    ['Times New Roman, serif', 'Times'],
    ['Georgia, serif', 'Georgia'],
    ['Courier New, monospace', 'Courier'],
  ];
  const PDF_SIZES = [[10, '10 pt'], [12, '12 pt'], [14, '14 pt'], [16, '16 pt'], [20, '20 pt'], [24, '24 pt'], [32, '32 pt'], [48, '48 pt']];

  async function pdfEditor(cx, H) {
    if (!cx.src.path) { H.note(H.body(cx), 'PDF inside an archive', 'Extract it to open or annotate it here.'); return; }
    let origBytes = await cx.src.bytes(100 * MB);
    await H.script(H.LIB.pdfjs);
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = H.LIB.pdfjsWorker;

    const tools = H.bar(cx);
    tools.classList.add('kv-editbar');

    const scroller = H.body(cx, 'kv-scroll kv-pdf-pages');

    let tool = 'pan';         // pan | pen | hl | rect | arrow | text | erase
    let colour = '#e11d48';
    let width = 3;
    let textFont = PDF_FONTS[0][0];
    let textSize = 16;
    let textBold = true;
    let textItalic = false;
    let scale = 1.25;
    let dirty = false;
    let nativeMode = false;
    let nativeFrame = null;

    const pageAnnots = new Map();
    const pageEls = new Map();
    const history = [];
    const redoStack = [];

    const toolBtns = {};
    function setTool(t) {
      tool = t;
      Object.entries(toolBtns).forEach(([k, b]) => b.classList.toggle('on', k === t));
      scroller.dataset.tool = t;
      textOpts.hidden = t !== 'text';
      for (const pe of pageEls.values()) pe.drawCvs.style.touchAction = t === 'pan' ? 'auto' : 'none';
    }
    const mkTool = (id, label, tip) => { const x = H.btn(label, tip, () => setTool(id)); toolBtns[id] = x; return x; };

    const swatches = ['#e11d48', '#2563eb', '#111827', '#16a34a', '#facc15'].map((c) => {
      const sw = h('button', 'kv-swatch' + (c === colour ? ' on' : ''));
      sw.style.background = c;
      sw.title = c;
      sw.onclick = () => {
        colour = c;
        customCol.value = c;
        swatches.forEach((x) => x.classList.toggle('on', x === sw));
        if (tool === 'pan') setTool(c === '#facc15' ? 'hl' : 'pen');
      };
      return sw;
    });
    const customCol = document.createElement('input');
    customCol.type = 'color'; customCol.value = colour; customCol.className = 'kv-color'; customCol.title = 'Custom colour';
    customCol.oninput = () => {
      colour = customCol.value;
      swatches.forEach((x) => x.classList.remove('on'));
      if (tool === 'pan') setTool('pen');
    };

    const sizeSel = makeSelect([[2, 'Thin'], [3, 'Med'], [6, 'Thick']], 'Stroke width', (v) => { width = +v; });
    sizeSel.value = '3';

    // Text formatting sub-group (shown when Text tool is active)
    const textOpts = h('span', 'kv-grp');
    const tFontSel = makeSelect(PDF_FONTS, 'Text font', (v) => { textFont = v; });
    const tSizeSel = makeSelect(PDF_SIZES, 'Text size', (v) => { textSize = +v; });
    tSizeSel.value = '16';
    const tBoldBtn = H.btn('<b>B</b>', 'Bold text note', () => { textBold = !textBold; tBoldBtn.classList.toggle('on', textBold); });
    tBoldBtn.classList.add('on');
    const tItalicBtn = H.btn('<i>I</i>', 'Italic text note', () => { textItalic = !textItalic; tItalicBtn.classList.toggle('on', textItalic); });
    textOpts.append(tFontSel, tSizeSel, tBoldBtn, tItalicBtn);
    textOpts.hidden = true;

    const zl = h('span', 'kv-dim', '125%');
    const saveBtn = H.btn('Save PDF', 'Bake drawings and notes into the PDF file (Ctrl+S)', () => {
      savePdf().then(() => toast('Saved ' + base(cx.src.path), 1400)).catch((e) => toast('Save failed: ' + e.message));
    });
    saveBtn.disabled = true;

    const undoBtn = H.btn('Undo', 'Undo last annotation', () => {
      const last = history.pop();
      if (!last) return;
      const list = pageAnnots.get(last.pageNum) || [];
      const idx = list.lastIndexOf(last.item);
      if (idx >= 0) list.splice(idx, 1);
      redoStack.push(last);
      redrawPage(last.pageNum);
      if (!hasAnyAnnots()) markDirty(false);
    });
    const redoBtn = H.btn('Redo', 'Redo undone annotation', () => {
      const next = redoStack.pop();
      if (!next) return;
      if (!pageAnnots.has(next.pageNum)) pageAnnots.set(next.pageNum, []);
      pageAnnots.get(next.pageNum).push(next.item);
      history.push(next);
      redrawPage(next.pageNum);
      markDirty(true);
    });
    const clearBtn = H.btn('Clear', 'Clear all unsaved annotations', () => {
      if (!hasAnyAnnots()) return;
      pageAnnots.clear();
      history.length = 0;
      redoStack.length = 0;
      for (const pn of pageEls.keys()) redrawPage(pn);
      markDirty(false);
    });
    const nativeBtn = H.btn('Native PDF', 'Switch to browser native PDF viewer', async () => {
      nativeMode = !nativeMode;
      nativeBtn.classList.toggle('on', nativeMode);
      if (nativeMode) {
        scroller.hidden = true;
        const { url } = await api('/api/preview-url?path=' + encodeURIComponent(cx.src.path));
        nativeFrame = document.createElement('iframe');
        nativeFrame.className = 'kv-frame'; nativeFrame.title = 'PDF';
        nativeFrame.src = url + (url.includes('?') ? '&' : '?') + 't=' + Date.now();
        cx.root.appendChild(nativeFrame);
      } else {
        nativeFrame?.remove(); nativeFrame = null;
        scroller.hidden = false;
      }
    });

    tools.append(
      mkTool('pan', 'Pan', 'Scroll and read normally'),
      mkTool('pen', 'Draw', 'Freehand pen'),
      mkTool('hl', 'Highlight', 'Translucent highlighter'),
      mkTool('rect', 'Rect', 'Drag to draw a rectangle'),
      mkTool('arrow', 'Arrow', 'Drag to draw an arrow'),
      mkTool('text', 'Text', 'Click on page to add styled text note'),
      mkTool('erase', 'Erase', 'Click or drag over annotations to erase'),
      sep(),
      ...swatches, customCol, sizeSel, textOpts, sep(),
      H.btn('−', 'Zoom out', () => setZoom(Math.max(0.6, +(scale - 0.25).toFixed(2)))),
      zl,
      H.btn('+', 'Zoom in', () => setZoom(Math.min(3, +(scale + 0.25).toFixed(2)))),
      sep(),
      undoBtn, redoBtn, clearBtn, H.spacer(), nativeBtn, saveBtn
    );
    setTool('pan');

    function hasAnyAnnots() {
      for (const arr of pageAnnots.values()) if (arr.length) return true;
      return false;
    }
    function markDirty(v) {
      dirty = !!v;
      cx.setDirty?.(dirty);
      saveBtn.disabled = !dirty;
      saveBtn.classList.toggle('primary', dirty);
    }

    function drawItemsOnCtx(ctx, items, W, Hh) {
      ctx.clearRect(0, 0, W, Hh);
      const scaleF = W / 612;
      for (const it of items) {
        ctx.save();
        if (it.type === 'pen' || it.type === 'hl') {
          if (it.pts.length < 1) { ctx.restore(); continue; }
          ctx.strokeStyle = it.color;
          ctx.lineCap = 'round';
          ctx.lineJoin = 'round';
          ctx.globalAlpha = it.type === 'hl' ? 0.36 : 1;
          ctx.lineWidth = (it.type === 'hl' ? it.width * 4.5 : it.width) * scaleF;
          ctx.beginPath();
          ctx.moveTo(it.pts[0][0] * W, it.pts[0][1] * Hh);
          for (let i = 1; i < it.pts.length; i++) ctx.lineTo(it.pts[i][0] * W, it.pts[i][1] * Hh);
          if (it.pts.length === 1) ctx.lineTo(it.pts[0][0] * W + 0.5, it.pts[0][1] * Hh + 0.5);
          ctx.stroke();
        } else if (it.type === 'rect') {
          ctx.strokeStyle = it.color;
          ctx.lineWidth = it.width * scaleF;
          ctx.lineJoin = 'miter';
          const x = Math.min(it.x1, it.x2) * W;
          const y = Math.min(it.y1, it.y2) * Hh;
          const w = Math.abs(it.x2 - it.x1) * W;
          const h2 = Math.abs(it.y2 - it.y1) * Hh;
          ctx.strokeRect(x, y, w, h2);
        } else if (it.type === 'arrow') {
          const x1 = it.x1 * W, y1 = it.y1 * Hh, x2 = it.x2 * W, y2 = it.y2 * Hh;
          ctx.strokeStyle = it.color;
          ctx.fillStyle = it.color;
          ctx.lineWidth = it.width * scaleF;
          ctx.lineCap = 'round';
          ctx.beginPath();
          ctx.moveTo(x1, y1);
          ctx.lineTo(x2, y2);
          ctx.stroke();
          const ang = Math.atan2(y2 - y1, x2 - x1);
          const head = Math.max(10, it.width * 4.5) * scaleF;
          ctx.beginPath();
          ctx.moveTo(x2, y2);
          ctx.lineTo(x2 - head * Math.cos(ang - Math.PI / 6), y2 - head * Math.sin(ang - Math.PI / 6));
          ctx.lineTo(x2 - head * Math.cos(ang + Math.PI / 6), y2 - head * Math.sin(ang + Math.PI / 6));
          ctx.closePath();
          ctx.fill();
        } else if (it.type === 'text') {
          const px = Math.max(10, Math.round((it.size || 16) * scaleF));
          const styleStr = `${it.italic ? 'italic ' : ''}${it.bold ? '700 ' : '400 '}${px}px ${it.font || PDF_FONTS[0][0]}`;
          ctx.font = styleStr;
          ctx.fillStyle = it.color;
          ctx.textBaseline = 'top';
          const lines = String(it.text || '').split('\n');
          lines.forEach((ln, li) => ctx.fillText(ln, it.x * W, it.y * Hh + li * (px * 1.25)));
        }
        ctx.restore();
      }
    }

    function redrawPage(pageNum) {
      const pe = pageEls.get(pageNum);
      if (!pe) return;
      drawItemsOnCtx(pe.drawCvs.getContext('2d'), pageAnnots.get(pageNum) || [], pe.drawCvs.width, pe.drawCvs.height);
    }

    function distToSeg(px, py, x1, y1, x2, y2) {
      const dx = x2 - x1, dy = y2 - y1;
      const len2 = dx * dx + dy * dy;
      const t = len2 ? Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / len2)) : 0;
      return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
    }

    function eraseNear(pageNum, nx, ny) {
      const list = pageAnnots.get(pageNum);
      if (!list || !list.length) return false;
      const thr = 0.028;
      let removed = false;
      for (let i = list.length - 1; i >= 0; i--) {
        const it = list[i];
        let hit = false;
        if (it.type === 'text') {
          hit = Math.abs(it.x - nx) < 0.14 && Math.abs(it.y - ny) < 0.05;
        } else if (it.type === 'rect') {
          const minX = Math.min(it.x1, it.x2), maxX = Math.max(it.x1, it.x2);
          const minY = Math.min(it.y1, it.y2), maxY = Math.max(it.y1, it.y2);
          const onVert = (Math.abs(nx - minX) < thr || Math.abs(nx - maxX) < thr) && ny >= minY - thr && ny <= maxY + thr;
          const onHorz = (Math.abs(ny - minY) < thr || Math.abs(ny - maxY) < thr) && nx >= minX - thr && nx <= maxX + thr;
          hit = onVert || onHorz;
        } else if (it.type === 'arrow') {
          hit = distToSeg(nx, ny, it.x1, it.y1, it.x2, it.y2) < thr;
        } else {
          hit = it.pts.some(([px, py]) => Math.hypot(px - nx, py - ny) < thr);
        }
        if (hit) { list.splice(i, 1); removed = true; }
      }
      if (removed) {
        redrawPage(pageNum);
        markDirty(hasAnyAnnots());
      }
      return removed;
    }

    function pushAnnot(pageNum, item) {
      if (!pageAnnots.has(pageNum)) pageAnnots.set(pageNum, []);
      pageAnnots.get(pageNum).push(item);
      history.push({ pageNum, item });
      redoStack.length = 0;
      redrawPage(pageNum);
      markDirty(true);
    }

    function wireCanvasEvents(pageNum, wrap, drawCvs) {
      let activeItem = null;
      let erasing = false;

      const normPt = (e) => {
        const r = drawCvs.getBoundingClientRect();
        return [Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)), Math.max(0, Math.min(1, (e.clientY - r.top) / r.height))];
      };

      drawCvs.addEventListener('pointerdown', (e) => {
        if (tool === 'pan' || e.button !== 0) return;
        e.preventDefault();
        const [nx, ny] = normPt(e);
        if (tool === 'text') {
          const inp = document.createElement('input');
          inp.type = 'text';
          inp.className = 'kv-pdf-text-in';
          inp.placeholder = 'Type note, Enter to place';
          inp.style.left = (nx * 100) + '%';
          inp.style.top = (ny * 100) + '%';
          const textCol = colour === '#facc15' ? '#111827' : colour;
          inp.style.color = textCol;
          inp.style.fontFamily = textFont;
          inp.style.fontSize = Math.max(11, Math.round(textSize * (drawCvs.clientWidth / 612))) + 'px';
          inp.style.fontWeight = textBold ? '700' : '400';
          inp.style.fontStyle = textItalic ? 'italic' : 'normal';
          wrap.appendChild(inp);
          inp.focus();
          let placed = false;
          const commit = (ok) => {
            if (placed) return;
            placed = true;
            const val = inp.value.trim();
            inp.remove();
            if (ok && val) {
              pushAnnot(pageNum, {
                type: 'text', color: textCol, size: textSize, font: textFont, bold: textBold, italic: textItalic,
                x: nx, y: ny, text: val,
              });
            }
          };
          inp.onblur = () => commit(true);
          inp.onkeydown = (ke) => { if (ke.key === 'Enter') { ke.preventDefault(); commit(true); } else if (ke.key === 'Escape') { ke.preventDefault(); commit(false); } };
          return;
        }
        drawCvs.setPointerCapture(e.pointerId);
        if (tool === 'erase') { erasing = true; eraseNear(pageNum, nx, ny); return; }
        if (tool === 'rect' || tool === 'arrow') {
          activeItem = { type: tool, color: colour, width, x1: nx, y1: ny, x2: nx, y2: ny };
        } else {
          activeItem = { type: tool, color: colour, width, pts: [[nx, ny]] };
        }
        pushAnnot(pageNum, activeItem);
      });

      drawCvs.addEventListener('pointermove', (e) => {
        if (erasing) { const [nx, ny] = normPt(e); eraseNear(pageNum, nx, ny); return; }
        if (!activeItem) return;
        const [nx, ny] = normPt(e);
        if (activeItem.type === 'rect' || activeItem.type === 'arrow') {
          activeItem.x2 = nx; activeItem.y2 = ny;
          redrawPage(pageNum);
        } else {
          const last = activeItem.pts[activeItem.pts.length - 1];
          if (Math.hypot(nx - last[0], ny - last[1]) > 0.002) {
            activeItem.pts.push([nx, ny]);
            redrawPage(pageNum);
          }
        }
      });
      const end = () => { activeItem = null; erasing = false; };
      drawCvs.addEventListener('pointerup', end);
      drawCvs.addEventListener('pointercancel', end);
    }

    let pdfDoc = null;
    async function renderPdfPages(bytes) {
      scroller.innerHTML = '';
      pageEls.clear();
      if (pdfDoc) { try { await pdfDoc.destroy(); } catch {} }
      const loadingTask = window.pdfjsLib.getDocument({ data: bytes.slice(0) });
      pdfDoc = await loadingTask.promise;
      const numPages = pdfDoc.numPages;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const fitScale = scroller.clientWidth > 0 ? Math.min(scale, (scroller.clientWidth - 28) / 612) : scale;
      const useScale = scroller.clientWidth > 0 && scroller.clientWidth < 660 ? Math.max(0.5, fitScale) : scale;
      zl.textContent = Math.round(useScale * 100) + '%';

      for (let pn = 1; pn <= numPages; pn++) {
        const pg = await pdfDoc.getPage(pn);
        const vp = pg.getViewport({ scale: useScale });
        const wrap = h('div', 'kv-pdf-page');
        wrap.style.width = Math.round(vp.width) + 'px';
        wrap.style.height = Math.round(vp.height) + 'px';

        const pdfCvs = document.createElement('canvas');
        pdfCvs.width = Math.round(vp.width * dpr);
        pdfCvs.height = Math.round(vp.height * dpr);
        pdfCvs.style.width = Math.round(vp.width) + 'px';
        pdfCvs.style.height = Math.round(vp.height) + 'px';

        const drawCvs = document.createElement('canvas');
        drawCvs.className = 'kv-pdf-draw';
        drawCvs.width = pdfCvs.width;
        drawCvs.height = pdfCvs.height;
        drawCvs.style.width = pdfCvs.style.width;
        drawCvs.style.height = pdfCvs.style.height;
        drawCvs.style.touchAction = tool === 'pan' ? 'auto' : 'none';

        const badge = h('span', 'kv-pdf-num', `${pn} / ${numPages}`);
        wrap.append(pdfCvs, drawCvs, badge);
        scroller.appendChild(wrap);
        pageEls.set(pn, { wrap, pdfCvs, drawCvs, vpW: vp.width, vpH: vp.height, dpr });
        wireCanvasEvents(pn, wrap, drawCvs);

        const ctx = pdfCvs.getContext('2d');
        ctx.scale(dpr, dpr);
        await pg.render({ canvasContext: ctx, viewport: vp }).promise;
        redrawPage(pn);
      }
      cx.info(`PDF · ${numPages} page${numPages === 1 ? '' : 's'} · ${H.sizeTxt(bytes.length)}`);
    }

    async function setZoom(s) {
      scale = s;
      await renderPdfPages(origBytes);
    }

    async function savePdf() {
      if (!hasAnyAnnots()) return;
      await H.script(H.LIB.pdfLib);
      const doc = await window.PDFLib.PDFDocument.load(origBytes, { ignoreEncryption: true });
      const pages = doc.getPages();
      for (const [pn, items] of pageAnnots.entries()) {
        if (!items.length || pn < 1 || pn > pages.length) continue;
        const page = pages[pn - 1];
        const { width: pw, height: ph } = page.getSize();
        const rot = ((page.getRotation().angle % 360) + 360) % 360;
        const visW = (rot === 90 || rot === 270) ? ph : pw;
        const visH = (rot === 90 || rot === 270) ? pw : ph;

        const off = document.createElement('canvas');
        off.width = Math.max(1, Math.round(visW * 2));
        off.height = Math.max(1, Math.round(visH * 2));
        drawItemsOnCtx(off.getContext('2d'), items, off.width, off.height);
        const pngBlob = await new Promise((r) => off.toBlob(r, 'image/png'));
        const pngBytes = new Uint8Array(await pngBlob.arrayBuffer());
        const png = await doc.embedPng(pngBytes);

        if (rot === 90) page.drawImage(png, { x: pw, y: 0, width: ph, height: pw, rotate: window.PDFLib.degrees(90) });
        else if (rot === 180) page.drawImage(png, { x: pw, y: ph, width: pw, height: ph, rotate: window.PDFLib.degrees(180) });
        else if (rot === 270) page.drawImage(png, { x: 0, y: ph, width: ph, height: pw, rotate: window.PDFLib.degrees(270) });
        else page.drawImage(png, { x: 0, y: 0, width: pw, height: ph });
      }
      const saved = await doc.save();
      await saveBinary(cx.src.path, saved);
      origBytes = saved;
      pageAnnots.clear();
      history.length = 0;
      redoStack.length = 0;
      await renderPdfPages(origBytes);
      markDirty(false);
    }

    cx.later(() => { if (pdfDoc) pdfDoc.destroy().catch(() => {}); });
    cx.setSaver?.(savePdf);
    await renderPdfPages(origBytes);
  }

  /* ================================================================ 4. POWERPOINT (.pptx) SLIDE TEXT EDITOR */
  const A_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main';
  const PPTX_SIZES = [['', 'Size'], [10, '10 pt'], [12, '12 pt'], [14, '14 pt'], [16, '16 pt'], [18, '18 pt'], [20, '20 pt'], [24, '24 pt'], [28, '28 pt'], [32, '32 pt'], [36, '36 pt'], [44, '44 pt'], [54, '54 pt']];

  function attachPptx(cx, H, opts) {
    if (!cx.src.path || !opts.slideBindings?.length) return;
    let origBytes = opts.u8;
    const bindings = opts.slideBindings;
    const dirtyBindings = new Set();
    let editing = false;
    let dirty = false;
    let activeBinding = bindings[0];
    let savedRange = null;

    const tools = h('div', 'kv-bar kv-editbar');
    cx.root.insertBefore(tools, cx.root.firstChild);

    function updateInfo() {
      cx.info(`PowerPoint · ${num(opts.slideCount)} slide${opts.slideCount === 1 ? '' : 's'}${editing ? ' · Editing' : ''}${dirty ? ' · Unsaved' : ''}`);
    }
    function markDirty(v) {
      dirty = !!v;
      cx.setDirty?.(dirty);
      saveBtn.disabled = !dirty;
      saveBtn.classList.toggle('primary', dirty);
      updateInfo();
    }

    function captureSel() {
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount) return;
      const r = sel.getRangeAt(0);
      const hit = bindings.find((b) => b.domEl.contains(r.commonAncestorContainer));
      if (hit) { activeBinding = hit; savedRange = r.cloneRange(); }
    }
    function applyToSlideSelection(stylePatch, datasetPatch) {
      if (!activeBinding) return;
      const styler = activeBinding._styler;
      if (savedRange && activeBinding.domEl.contains(savedRange.commonAncestorContainer)) {
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(savedRange);
        styler.capture();
      }
      if (styler.applyInline(stylePatch, datasetPatch)) {
        dirtyBindings.add(activeBinding);
        markDirty(true);
        captureSel();
      }
    }

    const editBtn = H.btn('Edit', 'Edit slide text and styles', () => setEditing(!editing));
    const saveBtn = H.btn('Save', 'Save PowerPoint presentation (Ctrl+S)', () => {
      savePptx().then(() => toast('Saved ' + base(cx.src.path), 1200)).catch((e) => toast('Save failed: ' + e.message));
    });
    saveBtn.disabled = true;

    const fmtGroup = h('span', 'kv-grp');
    fmtGroup.hidden = true;

    const fontSel = makeSelect(FONTS, 'Slide font', (val) => {
      applyToSlideSelection({ fontFamily: val ? `"${val}", var(--sans)` : '' });
    });
    const sizeSel = makeSelect(PPTX_SIZES, 'Slide font size', (val) => {
      if (!val || !activeBinding) return;
      const pt = +val;
      const cqw = `${((pt * 12700) / activeBinding.slideW) * 100}cqw`;
      applyToSlideSelection({ fontSize: cqw }, { pt: String(pt) });
    });
    const pCmd = (label, title, cmd) => {
      const x = H.btn(label, title, () => {
        if (!activeBinding) return;
        activeBinding.domEl.focus();
        if (savedRange && activeBinding.domEl.contains(savedRange.commonAncestorContainer)) {
          const s = window.getSelection(); s.removeAllRanges(); s.addRange(savedRange);
        }
        document.execCommand(cmd, false, null);
        dirtyBindings.add(activeBinding);
        markDirty(true);
        captureSel();
      });
      x.onmousedown = (e) => e.preventDefault();
      return x;
    };
    const colCtl = makeColorControl('<b>A</b>', 'Slide text colour', '#111827', (hex) => {
      applyToSlideSelection({ color: hex });
    });
    const alBtns = ['left', 'center', 'right'].map((a) => {
      const x = H.btn(ALIGN_ICONS[a], 'Align ' + a, () => {
        if (!activeBinding) return;
        const blocks = activeBinding._styler.activeBlocks('div.kv-p, p, div');
        const targets = blocks.length ? blocks : [...activeBinding.domEl.querySelectorAll('.kv-p')];
        targets.forEach((p) => { p.style.textAlign = a; });
        dirtyBindings.add(activeBinding);
        markDirty(true);
      });
      x.onmousedown = (e) => e.preventDefault();
      return x;
    });

    fmtGroup.append(fontSel, sizeSel, sep(), pCmd('<b>B</b>', 'Bold', 'bold'), pCmd('<i>I</i>', 'Italic', 'italic'), pCmd('<u>U</u>', 'Underline', 'underline'), sep(), colCtl, sep(), ...alBtns);
    tools.append(editBtn, fmtGroup, H.spacer(), saveBtn);

    for (const b of bindings) {
      b._styler = createSelectionStyler(b.domEl);
      b.domEl.addEventListener('input', () => {
        if (!editing) return;
        activeBinding = b;
        dirtyBindings.add(b);
        markDirty(true);
      });
      b.domEl.addEventListener('focus', () => { activeBinding = b; });
      b.domEl.addEventListener('keyup', captureSel);
      b.domEl.addEventListener('mouseup', captureSel);
    }

    const onSel = () => { if (editing) captureSel(); };
    document.addEventListener('selectionchange', onSel);
    cx.later(() => document.removeEventListener('selectionchange', onSel));

    function setEditing(on) {
      editing = !!on;
      editBtn.classList.toggle('on', editing);
      editBtn.textContent = editing ? 'Done' : 'Edit';
      fmtGroup.hidden = !editing;
      for (const b of bindings) {
        b.domEl.contentEditable = editing ? 'true' : 'false';
        b.domEl.classList.toggle('kv-tx-edit', editing);
      }
      updateInfo();
    }

    function rebuildTxBody(binding) {
      const { slideDoc, txNode, domEl, slideW } = binding;
      const mkA = (tag, attrs) => {
        const elN = slideDoc.createElementNS(A_NS, 'a:' + tag);
        for (const [k, v] of Object.entries(attrs || {})) elN.setAttribute(k, String(v));
        return elN;
      };
      // Keep <a:bodyPr> and <a:lstStyle>, replace <a:p> elements
      const oldPs = xmlKids(txNode, 'p');
      const oldPPrs = oldPs.map((p) => xmlKid(p, 'pPr')?.cloneNode(true) || null);
      oldPs.forEach((p) => txNode.removeChild(p));

      const paras = [...domEl.children].filter((c) => c.nodeType === 1);
      const paraEls = paras.length ? paras : [domEl];

      const algnMap = { left: 'l', center: 'ctr', right: 'r', justify: 'just' };

      paraEls.forEach((pEl, pi) => {
        const aP = mkA('p');
        const pPr = oldPPrs[pi] || (oldPPrs[0] ? oldPPrs[0].cloneNode(true) : mkA('pPr'));
        const ta = pEl.style?.textAlign;
        if (ta && algnMap[ta]) pPr.setAttribute('algn', algnMap[ta]);
        if (pPr.attributes.length || pPr.children.length) aP.appendChild(pPr);

        function walkRuns(node, fmt) {
          if (node.nodeType === 3) {
            const txt = node.nodeValue.replace(/\u200B/g, '').replace(/^\u00A0$/, '');
            if (!txt) return;
            const aR = mkA('r');
            const rPrAttrs = { lang: 'en-GB' };
            if (fmt.sizePt) rPrAttrs.sz = String(Math.round(fmt.sizePt * 100));
            if (fmt.b) rPrAttrs.b = '1';
            if (fmt.i) rPrAttrs.i = '1';
            if (fmt.u) rPrAttrs.u = 'sng';
            const rPr = mkA('rPr', rPrAttrs);
            if (fmt.color) {
              const sf = mkA('solidFill');
              sf.appendChild(mkA('srgbClr', { val: fmt.color }));
              rPr.appendChild(sf);
            }
            if (fmt.font) rPr.appendChild(mkA('latin', { typeface: fmt.font }));
            aR.appendChild(rPr);
            const aT = mkA('t');
            aT.textContent = txt;
            aR.appendChild(aT);
            aP.appendChild(aR);
            return;
          }
          if (node.nodeType !== 1) return;
          if (node.classList.contains('kv-bu')) return;
          const tag = node.tagName.toLowerCase();
          if (tag === 'br') { aP.appendChild(mkA('br')); return; }
          const next = { ...fmt };
          if (tag === 'b' || tag === 'strong') next.b = true;
          if (tag === 'i' || tag === 'em') next.i = true;
          if (tag === 'u') next.u = true;
          if (node.dataset?.pt) next.sizePt = parseFloat(node.dataset.pt);
          const st = node.style;
          if (st) {
            if (st.fontWeight === '700' || st.fontWeight === 'bold') next.b = true;
            if (st.fontStyle === 'italic') next.i = true;
            if (st.textDecoration?.includes('underline')) next.u = true;
            const ff = cleanFontName(st.fontFamily);
            if (ff) next.font = ff;
            const col = cssColorToHex(st.color);
            if (col) next.color = col;
            if (!next.sizePt && st.fontSize && st.fontSize.endsWith('cqw')) {
              const cqwVal = parseFloat(st.fontSize);
              if (isFinite(cqwVal)) next.sizePt = +(((cqwVal / 100) * slideW) / 12700).toFixed(1);
            }
          }
          for (const ch of node.childNodes) walkRuns(ch, next);
        }
        walkRuns(pEl, {});
        txNode.appendChild(aP);
      });
    }

    async function savePptx() {
      if (!dirty || !dirtyBindings.size) return;
      const modifiedSlides = new Map();
      for (const b of dirtyBindings) {
        rebuildTxBody(b);
        modifiedSlides.set(b.slidePath, b.slideDoc);
      }
      const z = await H.zipOpen(H.memSrc(cx.src.name, origBytes));
      const entries = new Map();
      for (const en of z.entries) if (!en.dir) entries.set(en.name, await z.read(en, 64 * MB));
      const ser = new XMLSerializer();
      const enc = new TextEncoder();
      for (const [sp, sDoc] of modifiedSlides.entries()) {
        entries.set(sp, enc.encode(ser.serializeToString(sDoc)));
      }
      const outZip = await makeZip([...entries.entries()]);
      await saveBinary(cx.src.path, outZip);
      origBytes = outZip;
      dirtyBindings.clear();
      markDirty(false);
    }

    cx.setSaver?.(savePptx);
  }

  /* ================================================================ 5. MARKDOWN (.md) VISUAL WYSIWYG EDITOR */
  function domToMarkdown(root) {
    function inlineMd(node) {
      if (node.nodeType === 3) return node.nodeValue.replace(/\u200B/g, '');
      if (node.nodeType !== 1) return '';
      const tag = node.tagName.toLowerCase();
      if (tag === 'br') return '  \n';
      if (tag === 'img') return `![${node.getAttribute('alt') || ''}](${node.getAttribute('src') || ''})`;
      if (tag === 'span' && node.classList.contains('kv-check')) return node.classList.contains('on') ? '[x] ' : '[ ] ';
      let inner = '';
      for (const ch of node.childNodes) inner += inlineMd(ch);
      if (!inner && tag !== 'a') return '';
      if (tag === 'strong' || tag === 'b') return `**${inner}**`;
      if (tag === 'em' || tag === 'i') return `*${inner}*`;
      if (tag === 'del' || tag === 's' || tag === 'strike') return `~~${inner}~~`;
      if (tag === 'code') return `\`${inner.replace(/`/g, '\\`')}\``;
      if (tag === 'a') return `[${inner || node.getAttribute('href') || ''}](${node.getAttribute('href') || ''})`;
      return inner;
    }
    function blockMd(container, indent = '') {
      const blocks = [];
      for (const node of container.childNodes) {
        if (node.nodeType === 3) {
          const t = node.nodeValue.replace(/\u200B/g, '').trim();
          if (t) blocks.push(indent + t);
          continue;
        }
        if (node.nodeType !== 1) continue;
        const tag = node.tagName.toLowerCase();
        if (/^h[1-6]$/.test(tag)) {
          blocks.push(`${'#'.repeat(+tag[1])} ${inlineMd(node).trim()}`);
        } else if (tag === 'p' || tag === 'div') {
          const t = inlineMd(node).trim();
          if (t) blocks.push(indent + t);
        } else if (tag === 'hr') {
          blocks.push('---');
        } else if (tag === 'blockquote') {
          const inner = blockMd(node).split('\n').map((l) => '> ' + l).join('\n');
          if (inner) blocks.push(inner);
        } else if (tag === 'pre') {
          const code = node.querySelector('code');
          const cls = code?.className || '';
          const lang = (/language-([\w-]+)/.exec(cls) || [])[1] || '';
          const bodyTxt = (code || node).textContent.replace(/\n$/, '');
          blocks.push('```' + lang + '\n' + bodyTxt + '\n```');
        } else if (tag === 'ul' || tag === 'ol') {
          let idx = 1;
          const lines = [];
          for (const li of node.children) {
            if (li.tagName.toLowerCase() !== 'li') continue;
            const prefix = tag === 'ol' ? `${idx++}. ` : '- ';
            let line = '';
            const nested = [];
            for (const ch of li.childNodes) {
              if (ch.nodeType === 1 && (ch.tagName === 'UL' || ch.tagName === 'OL')) {
                nested.push(blockMd(ch, indent + '  '));
              } else {
                line += inlineMd(ch);
              }
            }
            lines.push(indent + prefix + line.trim());
            if (nested.length) lines.push(...nested);
          }
          if (lines.length) blocks.push(lines.join('\n'));
        } else if (tag === 'table') {
          const trs = [...node.querySelectorAll('tr')];
          if (trs.length) {
            const rows = trs.map((tr) => [...tr.children].map((c) => inlineMd(c).trim().replace(/\|/g, '\\|')));
            const colN = Math.max(...rows.map((r) => r.length));
            const pad = (r) => Array.from({ length: colN }, (_, i) => r[i] || '');
            const hdr = pad(rows[0]);
            const lines = [
              '| ' + hdr.join(' | ') + ' |',
              '| ' + hdr.map(() => '---').join(' | ') + ' |',
              ...rows.slice(1).map((r) => '| ' + pad(r).join(' | ') + ' |'),
            ];
            blocks.push(lines.join('\n'));
          }
        } else {
          const t = inlineMd(node).trim();
          if (t) blocks.push(indent + t);
        }
      }
      return blocks.join('\n\n');
    }
    return blockMd(root).replace(/\n{3,}/g, '\n\n').trim() + '\n';
  }

  async function markdownEditor(cx, H) {
    const { text, partial } = await cx.src.text();
    const canEdit = !!cx.src.path && !partial;
    const tools = H.bar(cx);
    tools.classList.add('kv-editbar');

    const b = H.body(cx, 'kv-scroll');
    const wrap = h('article', 'prose kv-doc');
    b.appendChild(wrap);

    let frontBlock = '';
    let src = text;
    const fm = /^---\r?\n([\s\S]*?)\r?\n(---|\.\.\.)\r?\n/.exec(text);
    if (fm) {
      frontBlock = fm[0];
      src = text.slice(fm[0].length);
      wrap.appendChild(h('pre', 'kv-front', esc(fm[1])));
    }
    const contentEl = h('div', 'kv-md-body');
    contentEl.innerHTML = H.sanitize(H.mdHtml(src));
    H.wireDoc(contentEl, cx);
    wrap.appendChild(contentEl);

    let editing = false;
    let dirty = false;
    const styler = createSelectionStyler(contentEl);

    function updateInfo() {
      const wc = contentEl.innerText.split(/\s+/).filter(Boolean).length;
      cx.info(`${num(wc)} words${partial ? ' · first 4 MB' : ''}${editing ? ' · Editing' : ''}${dirty ? ' · Unsaved' : ''}`);
    }
    function markDirty(v) {
      dirty = !!v;
      cx.setDirty?.(dirty);
      saveBtn.disabled = !dirty;
      saveBtn.classList.toggle('primary', dirty);
      updateInfo();
    }

    async function saveMd() {
      if (!canEdit || !dirty) return;
      const out = frontBlock + domToMarkdown(contentEl);
      const res = await api('/api/files/write', { path: cx.src.path, content: out, force: true });
      if (cx.tab) { cx.tab.text = out; cx.tab.mtime = res.mtime; }
      markDirty(false);
    }

    const editBtn = H.btn('Edit', 'Edit this Markdown document visually', () => setEditing(!editing));
    const saveBtn = H.btn('Save', 'Save Markdown file (Ctrl+S)', () => {
      saveMd().then(() => toast('Saved ' + base(cx.src.path), 1200)).catch((e) => toast('Save failed: ' + e.message));
    });
    saveBtn.disabled = true;

    const fmtGroup = h('span', 'kv-grp');
    fmtGroup.hidden = true;
    const mBtn = (label, title, cmd, arg) => {
      const x = H.btn(label, title, () => {
        contentEl.focus();
        styler.restore();
        document.execCommand(cmd, false, arg || null);
        styler.capture();
        markDirty(true);
      });
      x.onmousedown = (e) => e.preventDefault();
      return x;
    };
    const blkSel = makeSelect([['P', 'Paragraph'], ['H1', 'Heading 1'], ['H2', 'Heading 2'], ['H3', 'Heading 3']], 'Block style', (v) => {
      contentEl.focus();
      styler.restore();
      document.execCommand('formatBlock', false, v);
      markDirty(true);
    });
    const codeBtn = H.btn('`Code`', 'Inline code', () => {
      contentEl.focus();
      styler.restore();
      const sel = window.getSelection();
      const txt = sel?.toString() || 'code';
      document.execCommand('insertHTML', false, `<code>${esc(txt)}</code>`);
      markDirty(true);
    });
    codeBtn.onmousedown = (e) => e.preventDefault();
    const quoteBtn = mBtn('Quote', 'Blockquote', 'formatBlock', 'BLOCKQUOTE');
    const linkBtn = H.btn('+ Link', 'Insert link', () => {
      styler.restore();
      const url = window.prompt('Link URL');
      if (!url) return;
      contentEl.focus();
      styler.restore();
      document.execCommand('createLink', false, url);
      markDirty(true);
    });
    linkBtn.onmousedown = (e) => e.preventDefault();

    fmtGroup.append(
      blkSel, sep(),
      mBtn('<b>B</b>', 'Bold', 'bold'),
      mBtn('<i>I</i>', 'Italic', 'italic'),
      mBtn('<s>S</s>', 'Strikethrough', 'strikeThrough'),
      codeBtn, sep(),
      mBtn('• List', 'Bullet list', 'insertUnorderedList'),
      mBtn('1. List', 'Numbered list', 'insertOrderedList'),
      quoteBtn, linkBtn, sep(),
      mBtn('Undo', 'Undo', 'undo'),
      mBtn('Redo', 'Redo', 'redo')
    );

    if (canEdit) {
      tools.append(editBtn, fmtGroup, H.spacer(), saveBtn);
    } else {
      tools.hidden = true;
    }

    function setEditing(on) {
      editing = !!on;
      contentEl.contentEditable = editing ? 'true' : 'false';
      contentEl.spellcheck = editing;
      wrap.classList.toggle('editing', editing);
      editBtn.classList.toggle('on', editing);
      editBtn.textContent = editing ? 'Done' : 'Edit';
      fmtGroup.hidden = !editing;
      if (editing) contentEl.focus();
      updateInfo();
    }

    contentEl.addEventListener('input', () => { if (editing) markDirty(true); });
    const onSel = () => { if (editing) styler.capture(); };
    document.addEventListener('selectionchange', onSel);
    cx.later(() => document.removeEventListener('selectionchange', onSel));
    contentEl.addEventListener('keydown', (e) => {
      if (!editing) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        saveMd().then(() => toast('Saved ' + base(cx.src.path), 1200)).catch((err) => toast('Save failed: ' + err.message));
      }
    });

    cx.setSaver?.(saveMd);
    updateInfo();
  }

  window.KCV_EDIT = {
    docx: docxEditor,
    sheet: sheetEditor,
    csv: csvEditor,
    pdf: pdfEditor,
    attachPptx,
    markdown: markdownEditor,
  };
})();
