'use strict';
/* Flock Visuals v1: zero-dependency, CSP-safe SVG/DOM renderer for inline charts, KPI stat tiles, rich sortable tables,
   method steps, callouts, and upgraded plain Markdown tables. Works in browsers (window.KCV) and Node (module.exports). */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.KCVis = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const KINDS = Object.freeze(['chart', 'compare', 'numbers', 'table', 'steps']);
  const CHART_TYPES = Object.freeze(['bar', 'hbar', 'line', 'area', 'scatter', 'donut', 'progress']);
  const TONES = Object.freeze(['neutral', 'ok', 'warn', 'bad', 'accent', 'predicted']);
  const STEP_STATES = Object.freeze(['pending', 'active', 'done', 'error']);
  const SERIES_VARS = ['--vis-s1', '--vis-s2', '--vis-s3', '--vis-s4', '--vis-s5', '--vis-s6'];
  const MAX_SPEC_CHARS = 20000;
  const MAX_SERIES = 12;
  const MAX_POINTS = 300;
  const MAX_ROWS = 100;
  const MAX_COLS = 20;
  const CACHE_CAP = 600;

  const SPEC_CACHE = new Map();
  let lazyObserver = null;

  function cachePut(key, raw) {
    if (SPEC_CACHE.has(key)) SPEC_CACHE.delete(key);
    SPEC_CACHE.set(key, raw);
    if (SPEC_CACHE.size > CACHE_CAP) {
      const oldest = SPEC_CACHE.keys().next().value;
      SPEC_CACHE.delete(oldest);
    }
  }

  function visualKind(lang) {
    const w = String(lang == null ? '' : lang).trim().split(/\s+/)[0].toLowerCase();
    return KINDS.indexOf(w) === -1 ? null : w;
  }

  function hashKey(str) {
    let h = 0x811c9dc5;
    const s = String(str == null ? '' : str);
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h.toString(36);
  }

  /* ---- Number & text formatting ---- */
  const SUP = { '-': '⁻', '+': '', 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹' };
  function decorateNumber(text) {
    const m = /^(-?)(\d+)(?:\.(\d+))?(?:e([-+]?\d+))?$/i.exec(text);
    if (!m) return text;
    const [, sign, intPart, frac, exp] = m;
    let out = (sign ? '−' : '') + (exp ? intPart : intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',')) + (frac ? '.' + frac : '');
    if (exp) out += ' × 10' + [...String(Number(exp))].map((c) => SUP[c] || c).join('');
    return out;
  }
  function sigDigits(v) { return String(Math.abs(v)).replace(/e.*$/i, '').replace('.', '').replace(/^0+/, '').length; }
  function formatNumber(value) {
    if (typeof value !== 'number') return String(value == null ? '—' : value);
    if (!Number.isFinite(value)) return '—';
    const abs = Math.abs(value);
    const sci = abs !== 0 && (abs < 1e-4 || abs >= 1e15);
    if (!Number.isInteger(value) && sigDigits(value) >= 12) {
      return decorateNumber(sci ? value.toExponential(4).replace('e+', 'e').replace(/\.?0+e/, 'e') : String(Number(value.toPrecision(5))));
    }
    return decorateNumber(sci ? value.toExponential().replace('e+', 'e') : String(value));
  }
  function formatCompact(v) {
    if (!Number.isFinite(v)) return '';
    const a = Math.abs(v);
    if (a >= 1e9) return (v < 0 ? '−' : '') + Number((a / 1e9).toFixed(1)) + 'B';
    if (a >= 1e6) return (v < 0 ? '−' : '') + Number((a / 1e6).toFixed(1)) + 'M';
    if (a >= 1e4) return (v < 0 ? '−' : '') + Number((a / 1e3).toFixed(1)) + 'k';
    return formatNumber(Number(v.toPrecision(4)));
  }
  function toNumber(v) {
    if (typeof v === 'number') return Number.isFinite(v) ? v : NaN;
    if (typeof v !== 'string') return NaN;
    let t = v.trim().replace(/[−–]/g, '-');
    while (/\d[, \u2009\u202f]\d{3}(?!\d)/.test(t)) t = t.replace(/(\d)[, \u2009\u202f](\d{3})(?!\d)/, '$1$2');
    return /^[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?$/i.test(t) ? Number(t) : NaN;
  }
  const NUM_CELL_RE = /^[-+−]?[£$€]?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?(?:e[-+]?\d+)?\s?%?$/i;
  function isNumericText(s) {
    const t = String(s == null ? '' : s).trim();
    return t !== '' && /\d/.test(t) && NUM_CELL_RE.test(t);
  }
  function numSortVal(v) {
    if (typeof v === 'number') return Number.isFinite(v) ? v : null;
    const t = String(v == null ? '' : v).trim().replace(/^[£$€]/, '').replace(/%$/, '');
    const n = toNumber(t);
    return Number.isFinite(n) ? n : null;
  }

  /* ---- Safe JSON parse & strict validation ---- */
  class SpecError extends Error {}
  function fail(msg) { throw new SpecError(msg); }

  function safeReviver(key, val) {
    if (key === '__proto__' || key === 'constructor' || key === 'prototype') return undefined;
    return val;
  }
  function parseSpec(raw) {
    const src = String(raw == null ? '' : raw).trim();
    if (!src) return { error: 'its specification is empty' };
    if (src.length > MAX_SPEC_CHARS) return { error: `its specification exceeds ${MAX_SPEC_CHARS} characters` };
    for (const cand of [src, src.replace(/,(\s*[}\]])/g, '$1')]) {
      try { return { value: JSON.parse(cand, safeReviver) }; } catch { /* try next */ }
    }
    return { error: 'its specification is not valid JSON' };
  }

  function asObj(v, what) {
    if (!v || typeof v !== 'object' || Array.isArray(v)) fail(`${what} must be an object`);
    return v;
  }
  function asArr(v, what, min, max) {
    if (!Array.isArray(v)) fail(`${what} must be a list`);
    if (v.length < min) fail(min === 1 ? `${what} is empty` : `${what} needs at least ${min} entries`);
    if (v.length > max) fail(`${what} has more than ${max} entries`);
    return v;
  }
  function asWords(v, max, what, required) {
    if (v == null || v === '') { if (required) fail(`${what} is missing`); return ''; }
    if (typeof v !== 'string' && typeof v !== 'number') fail(`${what} must be text`);
    let t = String(v).replace(/\s+/g, ' ').trim();
    if (!t && required) fail(`${what} is missing`);
    if (t.length > max) t = t.slice(0, max - 1).trimEnd() + '…';
    return t;
  }
  function checkTone(v) {
    const t = String(v || '').trim().toLowerCase();
    return TONES.includes(t) ? t : 'neutral';
  }
  function optNumOrNull(v, what) {
    if (v === null || v === undefined || v === '') return null;
    const n = toNumber(v);
    if (!Number.isFinite(n)) fail(`${what} needs a finite numeric value`);
    return n;
  }

  function validateChart(raw, aliasKind) {
    const spec = asObj(raw, 'the chart');
    const rawType = spec.type != null ? String(spec.type).trim().toLowerCase() : (aliasKind === 'compare' ? 'hbar' : 'bar');
    if (!CHART_TYPES.includes(rawType)) fail(`unknown chart type "${rawType}"`);
    const title = asWords(spec.title, 100, 'the title', false);
    const subtitle = asWords(spec.subtitle, 120, 'the subtitle', false);
    const unit = asWords(spec.unit, 20, 'the unit', false);
    const caption = asWords(spec.caption, 300, 'the caption', false);
    const xLabel = asWords(spec.xLabel ?? spec.x_label, 48, 'the x-axis label', false);
    const yLabel = asWords(spec.yLabel ?? spec.y_label, 48, 'the y-axis label', false);
    const stacked = Boolean(spec.stacked);
    const predictedAll = Boolean(spec.predicted);

    let ref = null;
    if (spec.ref != null) {
      if (typeof spec.ref === 'number' || typeof spec.ref === 'string') {
        const rv = toNumber(spec.ref);
        if (!Number.isFinite(rv)) fail('the reference value must be numeric');
        ref = { value: rv, label: '' };
      } else {
        const ro = asObj(spec.ref, 'the reference line');
        const rv = toNumber(ro.value);
        if (!Number.isFinite(rv)) fail('the reference line needs a numeric value');
        ref = { value: rv, label: asWords(ro.label, 36, 'the reference label', false) };
      }
    }

    if (rawType === 'scatter') {
      const rawSeries = asArr(spec.series, 'the series list', 1, MAX_SERIES);
      let totalPts = 0;
      const series = rawSeries.map((s, si) => {
        const so = asObj(s, `series ${si + 1}`);
        const name = asWords(so.name, 48, `series ${si + 1} name`, false) || `Series ${si + 1}`;
        let pts = [];
        if (Array.isArray(so.points)) {
          const arr = asArr(so.points, `series ${si + 1} points`, 1, MAX_POINTS);
          pts = arr.map((p, pi) => {
            const po = asObj(p, `point ${pi + 1}`);
            const x = toNumber(po.x), y = toNumber(po.y);
            if (!Number.isFinite(x) || !Number.isFinite(y)) fail(`point ${pi + 1} needs finite numeric x and y`);
            return { x, y, label: asWords(po.label, 48, 'point label', false) };
          });
        } else if (Array.isArray(so.values) && Array.isArray(spec.x)) {
          const arr = asArr(so.values, `series ${si + 1} values`, 1, MAX_POINTS);
          for (let pi = 0; pi < arr.length; pi++) {
            const xv = toNumber(spec.x[pi]), yv = optNumOrNull(arr[pi], `point ${pi + 1}`);
            if (yv === null) continue;
            if (!Number.isFinite(xv)) fail(`x[${pi}] must be numeric for scatter`);
            pts.push({ x: xv, y: yv, label: '' });
          }
          if (!pts.length) fail(`series ${si + 1} has no finite points`);
        } else {
          fail(`series ${si + 1} needs a points list`);
        }
        totalPts += pts.length;
        return { name, points: pts, predicted: Boolean(so.predicted) || predictedAll };
      });
      if (totalPts > MAX_POINTS * MAX_SERIES) fail('too many scatter points');
      return { type: 'scatter', title, subtitle, unit, xLabel, yLabel, caption, ref, series, x: [], items: null, stacked: false, autoFlip: false };
    }

    // Shorthand items form (items / values)
    const rawItems = Array.isArray(spec.items) ? spec.items : (aliasKind === 'compare' && Array.isArray(spec.values) ? spec.values : null);
    if (rawItems) {
      const list = asArr(rawItems, 'the items list', 1, MAX_POINTS);
      const items = list.map((it, idx) => {
        const o = asObj(it, `item ${idx + 1}`);
        const label = asWords(o.label ?? o.name, 60, `item ${idx + 1} label`, true);
        const value = optNumOrNull(o.value, `item "${label}"`);
        const low = o.low == null ? null : optNumOrNull(o.low, `item "${label}" low`);
        const high = o.high == null ? null : optNumOrNull(o.high, `item "${label}" high`);
        if ((low !== null) !== (high !== null)) fail(`interval for "${label}" needs both low and high`);
        if (low !== null && value !== null && !(low <= value && value <= high)) fail(`interval for "${label}" must contain its value`);
        const target = o.target == null ? null : optNumOrNull(o.target, `item "${label}" target`);
        if (rawType === 'progress' && (value === null || target === null || target <= 0)) {
          fail(`progress item "${label}" needs a numeric value and positive target`);
        }
        if (rawType === 'donut' && (value === null || value < 0)) {
          fail(`donut slice "${label}" needs a non-negative value`);
        }
        return {
          label,
          value,
          low,
          high,
          target,
          unit: asWords(o.unit, 20, 'item unit', false) || unit,
          tone: checkTone(o.tone),
          predicted: Boolean(o.predicted) || predictedAll,
        };
      });
      if (items.every((it) => it.value === null)) fail('all values are missing');
      const x = items.map((it) => it.label);
      const series = [{
        name: title || 'Value',
        values: items.map((it) => it.value),
        low: items.some((it) => it.low !== null) ? items.map((it) => it.low) : null,
        high: items.some((it) => it.high !== null) ? items.map((it) => it.high) : null,
        predicted: predictedAll,
      }];
      const maxLbl = x.reduce((m, s) => Math.max(m, s.length), 0);
      const autoFlip = rawType === 'bar' && (x.length > 7 || maxLbl > 12);
      return { type: rawType, title, subtitle, unit, xLabel, yLabel, caption, ref, x, series, items, stacked, autoFlip };
    }

    // Standard x + series form
    const rawX = asArr(spec.x, 'the x-axis labels', 1, MAX_POINTS);
    const x = rawX.map((v, i) => asWords(v, 60, `x[${i}]`, true));
    const rawSeries = asArr(spec.series, 'the series list', 1, MAX_SERIES);
    const series = rawSeries.map((s, si) => {
      const so = asObj(s, `series ${si + 1}`);
      const name = asWords(so.name, 48, `series ${si + 1} name`, false) || `Series ${si + 1}`;
      const vals = asArr(so.values, `series "${name}" values`, 1, MAX_POINTS);
      const values = x.map((_, i) => optNumOrNull(i < vals.length ? vals[i] : null, `${name}[${i}]`));
      if (values.every((v) => v === null)) fail(`series "${name}" has no numeric values`);
      if (rawType === 'donut' && values.some((v) => v !== null && v < 0)) fail('donut values cannot be negative');
      let low = null, high = null;
      if (so.low != null || so.high != null) {
        if (!Array.isArray(so.low) || !Array.isArray(so.high)) fail(`series "${name}" intervals need both low and high lists`);
        low = x.map((_, i) => optNumOrNull(i < so.low.length ? so.low[i] : null, `${name}.low[${i}]`));
        high = x.map((_, i) => optNumOrNull(i < so.high.length ? so.high[i] : null, `${name}.high[${i}]`));
        for (let i = 0; i < x.length; i++) {
          if ((low[i] !== null) !== (high[i] !== null)) fail(`series "${name}" interval at ${x[i]} needs both low and high`);
          if (low[i] !== null && values[i] !== null && !(low[i] <= values[i] && values[i] <= high[i])) {
            fail(`series "${name}" interval at ${x[i]} must contain its value`);
          }
        }
      }
      return { name, values, low, high, predicted: Boolean(so.predicted) || predictedAll };
    });
    const maxLbl = x.reduce((m, s) => Math.max(m, s.length), 0);
    const autoFlip = rawType === 'bar' && (x.length > 7 || maxLbl > 12);
    const items = (rawType === 'donut' || rawType === 'hbar' || rawType === 'progress')
      ? x.map((lbl, i) => ({
          label: lbl,
          value: series[0].values[i],
          low: series[0].low ? series[0].low[i] : null,
          high: series[0].high ? series[0].high[i] : null,
          target: ref ? ref.value : null,
          unit,
          tone: 'neutral',
          predicted: series[0].predicted,
        }))
      : null;
    return { type: rawType, title, subtitle, unit, xLabel, yLabel, caption, ref, x, series, items, stacked, autoFlip };
  }

  function validateNumbers(raw) {
    const isArr = Array.isArray(raw);
    const title = isArr ? '' : asWords(asObj(raw, 'numbers').title, 100, 'the title', false);
    const caption = isArr ? '' : asWords(raw.caption, 300, 'the caption', false);
    const list = asArr(isArr ? raw : raw.items, 'the numbers list', 1, 6);
    const items = list.map((it, i) => {
      const o = asObj(it, `metric ${i + 1}`);
      const label = asWords(o.label, 48, `metric ${i + 1} label`, true);
      let value = o.value;
      if (typeof value === 'number') {
        if (!Number.isFinite(value)) fail(`"${label}" has an invalid number`);
      } else if (typeof value === 'string') {
        const trimmed = value.trim();
        if (!trimmed) fail(`"${label}" is missing a value`);
        const n = toNumber(trimmed);
        value = Number.isFinite(n) && !/[a-z/%×^]/i.test(trimmed) ? n : asWords(trimmed, 36, `"${label}"`, true);
      } else {
        fail(`"${label}" is missing a value`);
      }
      return {
        label,
        value,
        unit: asWords(o.unit, 20, 'unit', false),
        delta: asWords(o.delta, 36, 'delta', false),
        note: asWords(o.note ?? o.source, 80, 'note', false),
        tone: checkTone(o.tone || (o.predicted ? 'predicted' : (o.evidence === 'predicted' ? 'predicted' : 'neutral'))),
      };
    });
    return { title, caption, items };
  }

  function validateTable(raw) {
    const spec = asObj(raw, 'the table');
    const title = asWords(spec.title, 100, 'the title', false);
    const caption = asWords(spec.caption, 300, 'the caption', false);
    const rawCols = asArr(spec.columns, 'the columns list', 1, MAX_COLS);
    const columns = rawCols.map((c, i) => {
      if (typeof c === 'string') return { key: c, label: asWords(c, 48, `column ${i + 1}`, true), unit: '', num: null, bar: false };
      const co = asObj(c, `column ${i + 1}`);
      const label = asWords(co.label ?? co.key, 48, `column ${i + 1}`, true);
      const key = asWords(co.key ?? label, 48, `column ${i + 1} key`, true);
      return {
        key,
        label,
        unit: asWords(co.unit, 20, 'column unit', false),
        num: typeof co.num === 'boolean' ? co.num : (typeof co.numeric === 'boolean' ? co.numeric : null),
        bar: Boolean(co.bar),
      };
    });
    const rawRows = asArr(spec.rows, 'the rows list', 1, MAX_ROWS);
    const rows = rawRows.map((r, ri) => {
      if (Array.isArray(r)) {
        return columns.map((_, ci) => {
          const v = ci < r.length ? r[ci] : '';
          return v == null ? '' : (typeof v === 'number' && Number.isFinite(v) ? v : asWords(String(v), 240, `row ${ri + 1}`, false));
        });
      }
      const ro = asObj(r, `row ${ri + 1}`);
      return columns.map((col) => {
        const v = ro[col.key] !== undefined ? ro[col.key] : ro[col.label];
        return v == null ? '' : (typeof v === 'number' && Number.isFinite(v) ? v : asWords(String(v), 240, `row ${ri + 1}`, false));
      });
    });
    // Auto-detect numeric columns where not explicitly set
    for (let c = 0; c < columns.length; c++) {
      if (columns[c].num === null) {
        let nonEmpty = 0, numeric = 0;
        for (const r of rows) {
          const v = r[c];
          if (v === '' || v === '—' || v === '-') continue;
          nonEmpty++;
          if (typeof v === 'number' || isNumericText(v)) numeric++;
        }
        columns[c].num = nonEmpty > 0 && numeric === nonEmpty;
      }
      if (columns[c].bar) columns[c].num = true;
    }
    let footer = null;
    if (Array.isArray(spec.footer)) {
      footer = columns.map((_, ci) => {
        const v = ci < spec.footer.length ? spec.footer[ci] : '';
        return v == null ? '' : (typeof v === 'number' && Number.isFinite(v) ? v : asWords(String(v), 120, 'footer', false));
      });
    }
    return { title, caption, columns, rows, footer };
  }

  function validateSteps(raw) {
    const spec = asObj(raw, 'steps');
    const title = asWords(spec.title, 100, 'the title', false);
    const caption = asWords(spec.caption, 300, 'the caption', false);
    const rawSteps = asArr(spec.steps, 'the steps list', 2, 8);
    const steps = rawSteps.map((s, i) => {
      const o = typeof s === 'string' ? { label: s } : asObj(s, `step ${i + 1}`);
      const st = String(o.state || 'pending').trim().toLowerCase();
      return {
        label: asWords(o.label, 56, `step ${i + 1} label`, true),
        detail: asWords(o.detail, 120, `step ${i + 1} detail`, false),
        state: STEP_STATES.includes(st) ? st : 'pending',
      };
    });
    return { title, caption, steps };
  }

  function validate(kind, rawOrObj) {
    const k = visualKind(kind);
    if (!k) fail('unsupported visual type');
    let val = rawOrObj;
    if (typeof rawOrObj === 'string') {
      const p = parseSpec(rawOrObj);
      if (p.error) fail(p.error);
      val = p.value;
    }
    if (k === 'chart' || k === 'compare') return { kind: 'chart', spec: validateChart(val, k) };
    if (k === 'numbers') return { kind: 'numbers', spec: validateNumbers(val) };
    if (k === 'table') return { kind: 'table', spec: validateTable(val) };
    if (k === 'steps') return { kind: 'steps', spec: validateSteps(val) };
    fail('unsupported visual type');
  }

  /* ---- TSV Export ---- */
  function cleanCell(v) { return String(v == null ? '' : v).replace(/[\t\r\n]+/g, ' '); }
  function toTSV(kind, spec) {
    if (kind === 'table') {
      const head = spec.columns.map((c) => cleanCell(c.unit ? `${c.label} (${c.unit})` : c.label)).join('\t');
      const body = spec.rows.map((r) => r.map(cleanCell).join('\t'));
      if (spec.footer) body.push(spec.footer.map(cleanCell).join('\t'));
      return [head, ...body].join('\n') + '\n';
    }
    if (kind === 'numbers') {
      return ['Label\tValue\tUnit\tDelta\tNote', ...spec.items.map((it) => [it.label, it.value, it.unit, it.delta, it.note].map(cleanCell).join('\t'))].join('\n') + '\n';
    }
    if (kind === 'steps') {
      return ['Step\tLabel\tDetail\tState', ...spec.steps.map((s, i) => [i + 1, s.label, s.detail, s.state].map(cleanCell).join('\t'))].join('\n') + '\n';
    }
    if (kind === 'chart') {
      if (spec.type === 'scatter') {
        const lines = ['Series\tLabel\tX\tY'];
        for (const s of spec.series) for (const p of s.points) lines.push([s.name, p.label, p.x, p.y].map(cleanCell).join('\t'));
        return lines.join('\n') + '\n';
      }
      if (spec.items) {
        const hasRange = spec.items.some((it) => it.low !== null);
        const hasTarget = spec.items.some((it) => it.target !== null);
        const h = ['Label', spec.unit ? `Value (${spec.unit})` : 'Value', ...(hasRange ? ['Low', 'High'] : []), ...(hasTarget ? ['Target'] : [])];
        const lines = [h.join('\t')];
        for (const it of spec.items) {
          lines.push([it.label, it.value ?? '', ...(hasRange ? [it.low ?? '', it.high ?? ''] : []), ...(hasTarget ? [it.target ?? ''] : [])].map(cleanCell).join('\t'));
        }
        return lines.join('\n') + '\n';
      }
      const head = ['Category', ...spec.series.map((s) => cleanCell(spec.unit ? `${s.name} (${spec.unit})` : s.name))];
      const lines = [head.join('\t')];
      spec.x.forEach((lbl, i) => {
        lines.push([lbl, ...spec.series.map((s) => (s.values[i] == null ? '' : s.values[i]))].map(cleanCell).join('\t'));
      });
      return lines.join('\n') + '\n';
    }
    return '';
  }

  /* ---- DOM helpers (strict textContent / setAttribute, zero innerHTML) ---- */
  function dom(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function svgEl(tag, attrs, text) {
    const n = document.createElementNS(SVG_NS, tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) n.setAttribute(k, String(v));
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function seriesColor(i) { return `var(${SERIES_VARS[i % SERIES_VARS.length]})`; }
  function toneColor(tone, fallbackIdx) {
    return { ok: 'var(--live)', warn: 'var(--warn)', bad: 'var(--red)', accent: 'var(--accent)', predicted: 'var(--vis-pred)' }[tone] || seriesColor(fallbackIdx || 0);
  }
  function copyText(text, btn) {
    const done = () => {
      if (!btn) return;
      const prev = btn.textContent;
      btn.textContent = 'Copied';
      btn.classList.add('ok');
      setTimeout(() => { btn.textContent = prev; btn.classList.remove('ok'); }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => {});
    }
  }
  function inlineSvgForExport(svgNode) {
    const clone = svgNode.cloneNode(true);
    clone.setAttribute('xmlns', SVG_NS);
    const cs = getComputedStyle(document.documentElement);
    const bg = cs.getPropertyValue('--surface').trim() || '#1c1c1a';
    const vb = (clone.getAttribute('viewBox') || '0 0 600 260').split(/\s+/).map(Number);
    const rect = svgEl('rect', { x: vb[0], y: vb[1], width: vb[2], height: vb[3], fill: bg, rx: 8 });
    clone.insertBefore(rect, clone.firstChild);
    // Resolve var(--...) tokens so standalone SVG renders identically outside the page
    const resolveVars = (s) => String(s || '').replace(/var\((--[a-z0-9-]+)\)/gi, (_, name) => cs.getPropertyValue(name).trim() || '#888');
    const origNodes = [svgNode, ...svgNode.querySelectorAll('*')];
    const cloneNodes = [clone, ...clone.querySelectorAll('*')];
    for (let i = 0; i < origNodes.length && i < cloneNodes.length; i++) {
      const o = origNodes[i], c = cloneNodes[i + 1];
      if (!c) continue;
      const st = getComputedStyle(o);
      if (o.tagName === 'text') {
        c.setAttribute('fill', st.fill);
        c.setAttribute('font-family', st.fontFamily);
        c.setAttribute('font-size', st.fontSize);
        c.setAttribute('font-weight', st.fontWeight);
      } else {
        for (const attr of ['fill', 'stroke']) {
          const av = c.getAttribute(attr);
          if (av && av.includes('var(')) c.setAttribute(attr, resolveVars(av));
          else if (!av && st[attr] && st[attr] !== 'none') c.setAttribute(attr, st[attr]);
        }
        if (st.strokeDasharray && st.strokeDasharray !== 'none') c.setAttribute('stroke-dasharray', st.strokeDasharray);
        if (st.strokeWidth && c.getAttribute('stroke') && c.getAttribute('stroke') !== 'none') c.setAttribute('stroke-width', st.strokeWidth);
        if (st.opacity && st.opacity !== '1') c.setAttribute('opacity', st.opacity);
      }
    }
    return new XMLSerializer().serializeToString(clone);
  }

  /* ---- Axis nice ticks ---- */
  function niceRange(minVal, maxVal, forceZero, tickTarget) {
    let lo = minVal, hi = maxVal;
    if (forceZero) { lo = Math.min(0, lo); hi = Math.max(0, hi); }
    if (lo === hi) { hi = lo === 0 ? 10 : lo + Math.abs(lo) * 0.25; if (forceZero && lo > 0) lo = 0; }
    const span = hi - lo;
    const rawStep = span / Math.max(2, tickTarget || 4);
    const pow = Math.pow(10, Math.floor(Math.log10(rawStep)));
    let step = 10 * pow;
    for (const m of [1, 2, 2.5, 5, 10]) if (rawStep <= m * pow) { step = m * pow; break; }
    const nLo = Math.floor(lo / step) * step;
    const nHi = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let v = nLo; v <= nHi + step * 1e-6; v += step) {
      const clean = Math.abs(v) < step * 1e-9 ? 0 : Number(v.toPrecision(10));
      ticks.push(clean);
      if (ticks.length > 10) break;
    }
    return { lo: nLo, hi: nHi, ticks };
  }

  /* ---- Chart renderers ---- */
  function setReadout(bar, parts) {
    bar.textContent = '';
    if (!parts || !parts.length) {
      bar.dataset.empty = '1';
      bar.textContent = 'Tap or hover a mark to inspect exact values';
      return;
    }
    bar.dataset.empty = '0';
    for (const p of parts) {
      if (p.dot) {
        const d = dom('span', 'kc-vis-dot');
        d.style.background = p.dot;
        bar.appendChild(d);
      }
      if (p.strong) bar.appendChild(dom('strong', '', p.strong));
      if (p.text) bar.appendChild(dom('span', '', p.text));
    }
  }

  function formatReadoutParts(label, seriesName, val, unit, low, high, refVal, refLabel, color) {
    const u = unit ? ' ' + unit : '';
    const parts = [];
    if (color) parts.push({ dot: color });
    parts.push({ strong: label });
    if (seriesName) parts.push({ text: seriesName + ':' });
    if (val === null) {
      parts.push({ text: 'No data (gap)' });
      return parts;
    }
    parts.push({ strong: formatNumber(val) + u });
    if (low !== null && high !== null) parts.push({ text: `(${formatNumber(low)} to ${formatNumber(high)})` });
    if (refVal !== null && Number.isFinite(refVal)) {
      const d = val - refVal;
      const sign = d > 0 ? '+' : '';
      parts.push({ text: `${sign}${formatNumber(Number(d.toPrecision(4)))}${u} vs ${refLabel || 'target'}` });
    }
    return parts;
  }

  function renderVerticalBarSVG(spec, hiddenSet, readout) {
    const W = 600, H = 248;
    const padL = 52, padR = 16, padT = 22, padB = 34;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const visSeries = spec.series.map((s, idx) => ({ s, idx })).filter(({ idx }) => !hiddenSet.has(idx));
    const vals = [];
    if (spec.stacked) {
      for (let i = 0; i < spec.x.length; i++) {
        let pos = 0, neg = 0;
        for (const { s } of visSeries) {
          const v = s.values[i];
          if (v === null) continue;
          if (v >= 0) pos += v; else neg += v;
        }
        vals.push(pos, neg);
      }
    } else {
      for (const { s } of visSeries) {
        for (let i = 0; i < s.values.length; i++) {
          if (s.values[i] !== null) vals.push(s.values[i]);
          if (s.low && s.low[i] !== null) vals.push(s.low[i]);
          if (s.high && s.high[i] !== null) vals.push(s.high[i]);
        }
      }
    }
    if (spec.ref) vals.push(spec.ref.value);
    const range = niceRange(Math.min(0, ...vals), Math.max(0, ...vals), true, 4);
    const Y = (v) => padT + plotH - ((v - range.lo) / (range.hi - range.lo)) * plotH;
    const zeroY = Y(0);

    const svg = svgEl('svg', { class: 'kc-vis-svg', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': spec.title || 'Bar chart' });
    for (const t of range.ticks) {
      const y = Y(t);
      svg.appendChild(svgEl('line', { class: t === 0 ? 'kv-zero' : 'kv-grid', x1: padL, y1: y, x2: W - padR, y2: y }));
      svg.appendChild(svgEl('text', { class: 'kv-tick', x: padL - 7, y: y + 3.5, 'text-anchor': 'end' }, formatCompact(t)));
    }
    svg.appendChild(svgEl('line', { class: 'kv-axis', x1: padL, y1: padT, x2: padL, y2: padT + plotH }));

    const n = spec.x.length;
    const slotW = plotW / n;
    const groupW = Math.min(slotW * 0.72, spec.stacked ? 44 : 56);
    const barCount = spec.stacked ? 1 : Math.max(1, visSeries.length);
    const barW = Math.max(6, Math.min(36, (groupW - (barCount - 1) * 3) / barCount));
    const totalGroupW = barW * barCount + (barCount - 1) * 3;
    const hits = [];

    const maxChars = Math.max(4, Math.floor((slotW - 6) / 6.6));
    spec.x.forEach((lbl, i) => {
      const cx = padL + slotW * (i + 0.5);
      const shortLbl = lbl.length > maxChars ? lbl.slice(0, Math.max(3, maxChars - 1)).trimEnd() + '…' : lbl;
      const xTxt = svgEl('text', { class: 'kv-xlabel', x: cx, y: H - 10, 'text-anchor': 'middle' }, shortLbl);
      if (shortLbl !== lbl) xTxt.appendChild(svgEl('title', null, lbl));
      svg.appendChild(xTxt);

      let posAcc = 0, negAcc = 0;
      visSeries.forEach(({ s, idx }, vi) => {
        const v = s.values[i];
        const col = s.predicted ? 'var(--vis-pred)' : seriesColor(idx);
        const bx = cx - totalGroupW / 2 + (spec.stacked ? 0 : vi * (barW + 3));
        if (v === null) {
          if (!spec.stacked) {
            const gh = Math.min(plotH * 0.42, 62);
            const r = svgEl('rect', { class: 'kv-gap kv-hit', x: bx, y: zeroY - gh, width: barW, height: gh, rx: 3 });
            const t = svgEl('text', { class: 'kv-gap-lbl', x: bx + barW / 2, y: zeroY - gh / 2 + 3, 'text-anchor': 'middle' }, 'gap');
            const inspect = () => {
              hits.forEach((h) => h.classList.remove('on'));
              r.classList.add('on');
              setReadout(readout, formatReadoutParts(lbl, visSeries.length > 1 ? s.name : '', null, spec.unit, null, null, null, null, null));
            };
            r.addEventListener('click', inspect);
            r.addEventListener('mouseenter', inspect);
            hits.push(r);
            svg.append(r, t);
          }
          return;
        }
        let yTop, yBot;
        if (spec.stacked) {
          if (v >= 0) { yBot = Y(posAcc); posAcc += v; yTop = Y(posAcc); }
          else { yTop = Y(negAcc); negAcc += v; yBot = Y(negAcc); }
        } else {
          yTop = v >= 0 ? Y(v) : zeroY;
          yBot = v >= 0 ? zeroY : Y(v);
        }
        const h = Math.max(2, Math.abs(yBot - yTop));
        const attrs = { class: 'kv-hit', x: bx, y: Math.min(yTop, yBot), width: barW, height: h, rx: 3, fill: col };
        if (s.predicted) {
          attrs.fill = 'var(--vis-pred-bg)';
          attrs.stroke = 'var(--vis-pred)';
          attrs['stroke-width'] = 1.5;
          attrs['stroke-dasharray'] = '4 2.5';
        }
        const rect = svgEl('rect', attrs);
        const low = s.low ? s.low[i] : null, high = s.high ? s.high[i] : null;
        const inspect = () => {
          hits.forEach((x) => x.classList.remove('on'));
          rect.classList.add('on');
          setReadout(readout, formatReadoutParts(lbl, visSeries.length > 1 ? s.name : '', v, spec.unit, low, high, spec.ref ? spec.ref.value : null, spec.ref ? spec.ref.label : '', col));
        };
        rect.addEventListener('click', inspect);
        rect.addEventListener('mouseenter', inspect);
        hits.push(rect);
        svg.appendChild(rect);

        if (!spec.stacked && low !== null && high !== null) {
          const yHi = Y(high), yLo = Y(low), wx = bx + barW / 2;
          svg.appendChild(svgEl('line', { class: 'kv-ci', x1: wx, y1: yHi, x2: wx, y2: yLo }));
          svg.appendChild(svgEl('line', { class: 'kv-ci', x1: wx - 4, y1: yHi, x2: wx + 4, y2: yHi }));
          svg.appendChild(svgEl('line', { class: 'kv-ci', x1: wx - 4, y1: yLo, x2: wx + 4, y2: yLo }));
        }
      });
    });

    if (spec.ref) {
      const ry = Y(spec.ref.value);
      svg.appendChild(svgEl('line', { class: 'kv-ref', x1: padL, y1: ry, x2: W - padR, y2: ry }));
      const rtxt = (spec.ref.label ? spec.ref.label + ' ' : '') + formatNumber(spec.ref.value) + (spec.unit ? ' ' + spec.unit : '');
      svg.appendChild(svgEl('text', { class: 'kv-ref-lbl', x: W - padR, y: ry - 5, 'text-anchor': 'end' }, rtxt));
    }
    return svg;
  }

  function renderHBarDOM(spec, readout) {
    const items = spec.items || spec.x.map((lbl, i) => ({
      label: lbl,
      value: spec.series[0].values[i],
      low: spec.series[0].low ? spec.series[0].low[i] : null,
      high: spec.series[0].high ? spec.series[0].high[i] : null,
      target: spec.ref ? spec.ref.value : null,
      unit: spec.unit,
      tone: 'neutral',
      predicted: spec.series[0].predicted,
    }));
    const vals = [];
    for (const it of items) {
      if (it.value !== null) vals.push(it.value);
      if (it.low !== null) vals.push(it.low);
      if (it.high !== null) vals.push(it.high);
      if (it.target !== null) vals.push(it.target);
    }
    if (spec.ref) vals.push(spec.ref.value);
    const lo = Math.min(0, ...vals);
    let hi = Math.max(0, ...vals);
    if (lo === hi) hi = lo + 1;
    const X = (v) => ((v - lo) / (hi - lo)) * 1000;
    const zero = X(0);

    const ul = dom('ul', 'kc-hbar-list');
    const rows = [];
    items.forEach((it, idx) => {
      const li = dom('li', 'kc-hbar-row');
      li.appendChild(dom('span', 'kc-hbar-lbl', it.label)).title = it.label;

      const valWrap = dom('span', 'kc-hbar-val');
      const u = it.unit ? ' ' + it.unit : '';
      if (it.value === null) {
        valWrap.appendChild(dom('small', '', 'gap'));
      } else {
        valWrap.appendChild(document.createTextNode(formatNumber(it.value) + u));
        if (it.target !== null && it.target > 0) {
          const pct = Math.round((it.value / it.target) * 100);
          valWrap.appendChild(dom('small', '', `/ ${formatNumber(it.target)} (${pct}%)`));
        } else if (it.low !== null && it.high !== null) {
          valWrap.appendChild(dom('small', '', `(${formatNumber(it.low)}–${formatNumber(it.high)})`));
        }
      }
      li.appendChild(valWrap);

      const svg = svgEl('svg', { class: 'kc-hbar-track', viewBox: '0 0 1000 14', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
      svg.appendChild(svgEl('rect', { x: 0, y: 3, width: 1000, height: 8, rx: 4, fill: 'var(--vis-bar-bg)' }));
      if (lo < 0) svg.appendChild(svgEl('line', { x1: zero, y1: 0, x2: zero, y2: 14, stroke: 'var(--line-2)', 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' }));

      const col = it.predicted ? 'var(--vis-pred)' : (spec.type === 'progress' ? toneColor(it.tone, idx) : seriesColor(0));
      if (it.value !== null) {
        const a = Math.min(zero, X(it.value));
        const w = Math.max(4, Math.abs(X(it.value) - zero));
        const barAttrs = { x: a, y: 2, width: w, height: 10, rx: 4, fill: col };
        if (it.predicted) {
          barAttrs.fill = 'var(--vis-pred-bg)';
          barAttrs.stroke = 'var(--vis-pred)';
          barAttrs['stroke-width'] = 1.5;
          barAttrs['stroke-dasharray'] = '4 3';
          barAttrs['vector-effect'] = 'non-scaling-stroke';
        }
        svg.appendChild(svgEl('rect', barAttrs));
        if (it.low !== null && it.high !== null) {
          const xL = X(it.low), xH = X(it.high);
          svg.appendChild(svgEl('line', { x1: xL, y1: 7, x2: xH, y2: 7, stroke: 'var(--ink)', 'stroke-width': 1.4, 'vector-effect': 'non-scaling-stroke' }));
          svg.appendChild(svgEl('line', { x1: xL, y1: 3, x2: xL, y2: 11, stroke: 'var(--ink)', 'stroke-width': 1.4, 'vector-effect': 'non-scaling-stroke' }));
          svg.appendChild(svgEl('line', { x1: xH, y1: 3, x2: xH, y2: 11, stroke: 'var(--ink)', 'stroke-width': 1.4, 'vector-effect': 'non-scaling-stroke' }));
        }
      } else {
        svg.appendChild(svgEl('rect', { x: 4, y: 3, width: 220, height: 8, rx: 4, fill: 'none', stroke: 'var(--line-2)', 'stroke-dasharray': '4 3', 'vector-effect': 'non-scaling-stroke' }));
      }
      const markerVal = it.target !== null ? it.target : (spec.ref ? spec.ref.value : null);
      if (markerVal !== null && Number.isFinite(markerVal)) {
        const tx = X(markerVal);
        svg.appendChild(svgEl('line', { x1: tx, y1: 0, x2: tx, y2: 14, stroke: 'var(--ink)', 'stroke-width': 2, opacity: 0.75, 'vector-effect': 'non-scaling-stroke' }));
      }
      li.appendChild(svg);

      const inspect = () => {
        rows.forEach((r) => r.classList.remove('on'));
        li.classList.add('on');
        setReadout(readout, formatReadoutParts(it.label, '', it.value, it.unit, it.low, it.high, markerVal, spec.ref ? spec.ref.label : 'target', col));
      };
      li.addEventListener('click', inspect);
      li.addEventListener('mouseenter', inspect);
      rows.push(li);
      ul.appendChild(li);
    });
    return ul;
  }

  function renderProgressDOM(spec, readout) {
    // Normalize each progress item onto its own 0..max(value, target) scale so bars compare cleanly per target
    const ul = dom('ul', 'kc-hbar-list');
    const rows = [];
    spec.items.forEach((it, idx) => {
      const li = dom('li', 'kc-hbar-row');
      li.appendChild(dom('span', 'kc-hbar-lbl', it.label)).title = it.label;
      const pct = Math.round((it.value / it.target) * 100);
      const u = it.unit ? ' ' + it.unit : '';
      const valWrap = dom('span', 'kc-hbar-val');
      valWrap.appendChild(document.createTextNode(`${formatNumber(it.value)} / ${formatNumber(it.target)}${u}`));
      valWrap.appendChild(dom('small', '', `${pct}%`));
      li.appendChild(valWrap);

      const maxScale = Math.max(it.target * 1.15, it.value * 1.05);
      const X = (v) => Math.max(0, Math.min(1000, (v / maxScale) * 1000));
      const col = toneColor(it.tone, idx);
      const svg = svgEl('svg', { class: 'kc-hbar-track', viewBox: '0 0 1000 14', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
      svg.appendChild(svgEl('rect', { x: 0, y: 3, width: 1000, height: 8, rx: 4, fill: 'var(--vis-bar-bg)' }));
      svg.appendChild(svgEl('rect', { x: 0, y: 2, width: Math.max(4, X(it.value)), height: 10, rx: 4, fill: col }));
      const tx = X(it.target);
      svg.appendChild(svgEl('line', { x1: tx, y1: 0, x2: tx, y2: 14, stroke: 'var(--ink)', 'stroke-width': 2, opacity: 0.8, 'vector-effect': 'non-scaling-stroke' }));
      li.appendChild(svg);

      const inspect = () => {
        rows.forEach((r) => r.classList.remove('on'));
        li.classList.add('on');
        setReadout(readout, formatReadoutParts(it.label, '', it.value, it.unit, null, null, it.target, 'target', col));
      };
      li.addEventListener('click', inspect);
      li.addEventListener('mouseenter', inspect);
      rows.push(li);
      ul.appendChild(li);
    });
    return ul;
  }

  function renderLineAreaSVG(spec, hiddenSet, readout) {
    const W = 600, H = 248;
    const padL = 52, padR = 16, padT = 22, padB = 34;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const visSeries = spec.series.map((s, idx) => ({ s, idx })).filter(({ idx }) => !hiddenSet.has(idx));
    const n = spec.x.length;
    const isArea = spec.type === 'area';
    const stacked = isArea && spec.stacked;

    // Precompute stacked baselines & tops
    const cum = visSeries.map(() => new Array(n).fill(null));
    const base = visSeries.map(() => new Array(n).fill(0));
    const vals = [];
    if (stacked) {
      for (let i = 0; i < n; i++) {
        let acc = 0;
        visSeries.forEach(({ s }, vi) => {
          const v = s.values[i];
          if (v === null) { cum[vi][i] = null; return; }
          base[vi][i] = acc;
          acc += Math.max(0, v);
          cum[vi][i] = acc;
          vals.push(acc);
        });
      }
    } else {
      for (const { s } of visSeries) {
        for (let i = 0; i < n; i++) {
          if (s.values[i] !== null) vals.push(s.values[i]);
          if (s.low && s.low[i] !== null) vals.push(s.low[i]);
          if (s.high && s.high[i] !== null) vals.push(s.high[i]);
        }
      }
    }
    if (spec.ref) vals.push(spec.ref.value);
    const minV = vals.length ? Math.min(...vals) : 0;
    const maxV = vals.length ? Math.max(...vals) : 1;
    const range = niceRange(minV, maxV, isArea || minV >= 0 && minV < maxV * 0.25, 4);
    const X = (i) => (n <= 1 ? padL + plotW / 2 : padL + (i / (n - 1)) * plotW);
    const Y = (v) => padT + plotH - ((v - range.lo) / (range.hi - range.lo)) * plotH;
    const zeroY = Y(Math.max(range.lo, Math.min(range.hi, 0)));

    const svg = svgEl('svg', { class: 'kc-vis-svg', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': spec.title || 'Line chart' });
    for (const t of range.ticks) {
      const y = Y(t);
      svg.appendChild(svgEl('line', { class: t === 0 ? 'kv-zero' : 'kv-grid', x1: padL, y1: y, x2: W - padR, y2: y }));
      svg.appendChild(svgEl('text', { class: 'kv-tick', x: padL - 7, y: y + 3.5, 'text-anchor': 'end' }, formatCompact(t)));
    }
    svg.appendChild(svgEl('line', { class: 'kv-axis', x1: padL, y1: padT, x2: padL, y2: padT + plotH }));
    svg.appendChild(svgEl('line', { class: 'kv-axis', x1: padL, y1: padT + plotH, x2: W - padR, y2: padT + plotH }));

    const maxTicks = 8;
    const xStep = Math.max(1, Math.ceil(n / maxTicks));
    spec.x.forEach((lbl, i) => {
      if (i % xStep !== 0 && i !== n - 1) return;
      const anchor = i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle';
      const shortLbl = lbl.length > 12 ? lbl.slice(0, 11) + '…' : lbl;
      svg.appendChild(svgEl('text', { class: 'kv-xlabel', x: X(i), y: H - 10, 'text-anchor': anchor }, shortLbl));
    });

    const ptsAll = [];
    visSeries.forEach(({ s, idx }, vi) => {
      const col = s.predicted ? 'var(--vis-pred)' : seriesColor(idx);
      const yVals = stacked ? cum[vi] : s.values;

      // Confidence band (low..high) for non-stacked line/area
      if (!stacked && s.low && s.high) {
        let seg = [];
        const flushBand = () => {
          if (seg.length < 2) { seg = []; return; }
          const top = seg.map((i) => `${X(i)},${Y(s.high[i])}`);
          const bot = seg.slice().reverse().map((i) => `${X(i)},${Y(s.low[i])}`);
          svg.appendChild(svgEl('polygon', { class: 'kv-band', points: top.concat(bot).join(' '), fill: col }));
          seg = [];
        };
        for (let i = 0; i < n; i++) {
          if (s.low[i] !== null && s.high[i] !== null) seg.push(i);
          else flushBand();
        }
        flushBand();
      }

      // Area fill segments (break on null gaps)
      if (isArea) {
        let seg = [];
        const flushArea = () => {
          if (seg.length < 2) { seg = []; return; }
          const top = seg.map((i) => `${X(i)},${Y(yVals[i])}`);
          const bot = seg.slice().reverse().map((i) => `${X(i)},${stacked ? Y(base[vi][i]) : zeroY}`);
          svg.appendChild(svgEl('polygon', { class: 'kv-area', points: top.concat(bot).join(' '), fill: col }));
          seg = [];
        };
        for (let i = 0; i < n; i++) {
          if (yVals[i] !== null) seg.push(i);
          else flushArea();
        }
        flushArea();
      }

      // Line segments (break cleanly across null gaps)
      let d = '';
      let open = false;
      for (let i = 0; i < n; i++) {
        if (yVals[i] === null) { open = false; continue; }
        d += (open ? ' L ' : ' M ') + `${X(i).toFixed(1)} ${Y(yVals[i]).toFixed(1)}`;
        open = true;
      }
      if (d) svg.appendChild(svgEl('path', { class: 'kv-line' + (s.predicted ? ' kv-line--pred' : ''), d: d.trim(), stroke: col }));

      // Interactive point markers
      for (let i = 0; i < n; i++) {
        if (yVals[i] === null) continue;
        const c = svgEl('circle', { class: 'kv-pt', cx: X(i), cy: Y(yVals[i]), r: n > 40 ? 2.8 : 4, fill: col });
        const rawVal = s.values[i];
        const low = s.low ? s.low[i] : null, high = s.high ? s.high[i] : null;
        const inspect = () => {
          ptsAll.forEach((p) => p.classList.remove('on'));
          c.classList.add('on');
          setReadout(readout, formatReadoutParts(spec.x[i], spec.series.length > 1 ? s.name : '', rawVal, spec.unit, low, high, spec.ref ? spec.ref.value : null, spec.ref ? spec.ref.label : '', col));
        };
        c.addEventListener('click', inspect);
        c.addEventListener('mouseenter', inspect);
        ptsAll.push(c);
        svg.appendChild(c);
      }
    });

    if (spec.ref) {
      const ry = Y(spec.ref.value);
      svg.appendChild(svgEl('line', { class: 'kv-ref', x1: padL, y1: ry, x2: W - padR, y2: ry }));
      const rtxt = (spec.ref.label ? spec.ref.label + ' ' : '') + formatNumber(spec.ref.value) + (spec.unit ? ' ' + spec.unit : '');
      svg.appendChild(svgEl('text', { class: 'kv-ref-lbl', x: W - padR, y: ry - 5, 'text-anchor': 'end' }, rtxt));
    }
    return svg;
  }

  function renderScatterSVG(spec, hiddenSet, readout) {
    const W = 600, H = 260;
    const padL = 56, padR = 20, padT = 18, padB = 42;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const visSeries = spec.series.map((s, idx) => ({ s, idx })).filter(({ idx }) => !hiddenSet.has(idx));
    const xs = [], ys = [];
    for (const { s } of visSeries) for (const p of s.points) { xs.push(p.x); ys.push(p.y); }
    if (spec.ref) ys.push(spec.ref.value);
    const rx = niceRange(xs.length ? Math.min(...xs) : 0, xs.length ? Math.max(...xs) : 1, false, 5);
    const ry = niceRange(ys.length ? Math.min(...ys) : 0, ys.length ? Math.max(...ys) : 1, false, 4);
    const X = (v) => padL + ((v - rx.lo) / (rx.hi - rx.lo)) * plotW;
    const Y = (v) => padT + plotH - ((v - ry.lo) / (ry.hi - ry.lo)) * plotH;

    const svg = svgEl('svg', { class: 'kc-vis-svg', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': spec.title || 'Scatter plot' });
    for (const t of ry.ticks) {
      const y = Y(t);
      svg.appendChild(svgEl('line', { class: 'kv-grid', x1: padL, y1: y, x2: W - padR, y2: y }));
      svg.appendChild(svgEl('text', { class: 'kv-tick', x: padL - 7, y: y + 3.5, 'text-anchor': 'end' }, formatCompact(t)));
    }
    for (const t of rx.ticks) {
      const x = X(t);
      svg.appendChild(svgEl('line', { class: 'kv-grid', x1: x, y1: padT, x2: x, y2: padT + plotH }));
      svg.appendChild(svgEl('text', { class: 'kv-tick', x, y: padT + plotH + 15, 'text-anchor': 'middle' }, formatCompact(t)));
    }
    svg.appendChild(svgEl('line', { class: 'kv-axis', x1: padL, y1: padT, x2: padL, y2: padT + plotH }));
    svg.appendChild(svgEl('line', { class: 'kv-axis', x1: padL, y1: padT + plotH, x2: W - padR, y2: padT + plotH }));
    if (spec.xLabel) svg.appendChild(svgEl('text', { class: 'kv-xlabel', x: padL + plotW / 2, y: H - 6, 'text-anchor': 'middle' }, spec.xLabel));
    if (spec.yLabel) {
      const yl = svgEl('text', { class: 'kv-xlabel', x: 14, y: padT + plotH / 2, 'text-anchor': 'middle', transform: `rotate(-90 14 ${padT + plotH / 2})` }, spec.yLabel);
      svg.appendChild(yl);
    }

    const pts = [];
    visSeries.forEach(({ s, idx }) => {
      const col = seriesColor(idx);
      for (const p of s.points) {
        const cx = X(p.x), cy = Y(p.y);
        const c = svgEl('circle', { class: 'kv-pt', cx, cy, r: 5.5, fill: col });
        const inspect = () => {
          pts.forEach((n) => n.classList.remove('on'));
          c.classList.add('on');
          const lbl = p.label || s.name;
          setReadout(readout, [
            { dot: col },
            { strong: lbl },
            { text: `(${formatNumber(p.x)}, ${formatNumber(p.y)}${spec.unit ? ' ' + spec.unit : ''})` },
            ...(p.label && spec.series.length > 1 ? [{ text: '· ' + s.name }] : []),
          ]);
        };
        c.addEventListener('click', inspect);
        c.addEventListener('mouseenter', inspect);
        pts.push(c);
        svg.appendChild(c);
        if (p.label && s.points.length <= 8) {
          const nearRight = cx > W - padR - 95;
          const lblTxt = p.label.length > 16 ? p.label.slice(0, 15) + '…' : p.label;
          svg.appendChild(svgEl('text', {
            class: 'kv-tick',
            x: nearRight ? cx - 8 : cx + 8,
            y: cy + 3.5,
            'text-anchor': nearRight ? 'end' : 'start',
          }, lblTxt));
        }
      }
    });
    if (spec.ref) {
      const yRef = Y(spec.ref.value);
      svg.appendChild(svgEl('line', { class: 'kv-ref', x1: padL, y1: yRef, x2: W - padR, y2: yRef }));
    }
    return svg;
  }

  function renderDonutDOM(spec, readout) {
    const items = spec.items.filter((it) => it.value !== null && it.value > 0);
    const total = items.reduce((a, b) => a + b.value, 0) || 1;
    const wrap = dom('div', 'kc-donut-wrap');
    const S = 180, cx = 90, cy = 90, R = 68, strokeW = 24;
    const C = 2 * Math.PI * R;
    const svg = svgEl('svg', { class: 'kc-vis-svg', viewBox: `0 0 ${S} ${S}`, role: 'img', 'aria-label': spec.title || 'Donut chart' });
    svg.appendChild(svgEl('circle', { cx, cy, r: R, fill: 'none', stroke: 'var(--vis-bar-bg)', 'stroke-width': strokeW }));

    const list = dom('ul', 'kc-donut-list');
    const slices = [], rows = [];
    let offset = 0;

    items.forEach((it, idx) => {
      const frac = it.value / total;
      const len = frac * C;
      const col = seriesColor(idx);
      const seg = svgEl('circle', {
        class: 'kv-hit',
        cx, cy, r: R,
        fill: 'none',
        stroke: col,
        'stroke-width': strokeW,
        'stroke-dasharray': `${Math.max(1, len - 1.5).toFixed(2)} ${C.toFixed(2)}`,
        'stroke-dashoffset': (-offset).toFixed(2),
        transform: `rotate(-90 ${cx} ${cy})`,
      });
      offset += len;

      const li = dom('li', 'kc-donut-item');
      const sw = dom('span', 'kc-vis-swatch');
      sw.style.background = col;
      li.append(sw, dom('span', 'lbl', it.label), dom('span', 'val', formatNumber(it.value) + (it.unit ? ' ' + it.unit : '')), dom('span', 'pct', `${Math.round(frac * 100)}%`));

      const inspect = () => {
        slices.forEach((x) => x.classList.remove('on'));
        rows.forEach((x) => x.classList.remove('on'));
        seg.classList.add('on');
        li.classList.add('on');
        setReadout(readout, [
          { dot: col },
          { strong: it.label },
          { strong: formatNumber(it.value) + (it.unit ? ' ' + it.unit : '') },
          { text: `(${Math.round(frac * 100)}% of ${formatNumber(total)})` },
        ]);
      };
      seg.addEventListener('click', inspect);
      seg.addEventListener('mouseenter', inspect);
      li.addEventListener('click', inspect);
      li.addEventListener('mouseenter', inspect);
      slices.push(seg);
      rows.push(li);
      svg.appendChild(seg);
      list.appendChild(li);
    });

    svg.appendChild(svgEl('text', { class: 'kv-donut-total', x: cx, y: cy + 2, 'text-anchor': 'middle' }, formatCompact(total)));
    svg.appendChild(svgEl('text', { class: 'kv-donut-sub', x: cx, y: cy + 18, 'text-anchor': 'middle' }, spec.unit || 'total'));
    wrap.append(svg, list);
    return { wrap, svg };
  }

  /* ---- Expand Modal ---- */
  function openExpandModal(kind, spec) {
    const ov = dom('div', 'kc-vis-modal');
    const box = dom('div', 'kc-vis-modal-box');
    const top = dom('div', 'kc-vis-modal-top');
    top.appendChild(dom('strong', '', spec.title || kind.toUpperCase()));
    const closeBtn = dom('button', 'kc-vis-btn', 'Close');
    closeBtn.type = 'button';
    const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    closeBtn.onclick = close;
    ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
    document.addEventListener('keydown', onKey);
    top.appendChild(closeBtn);
    box.appendChild(top);

    const inner = dom('div', 'kc-vis');
    renderValidated(inner, kind, spec, { inModal: true });
    box.appendChild(inner);
    ov.appendChild(box);
    document.body.appendChild(ov);
  }

  /* ---- Full Block Builders ---- */
  function buildHeader(kind, spec, { inModal, getSvg }) {
    const head = dom('div', 'kc-vis-head');
    const titles = dom('div', 'kc-vis-titles');
    if (spec.title && !inModal) titles.appendChild(dom('div', 'kc-vis-title', spec.title));
    if (spec.subtitle) titles.appendChild(dom('div', 'kc-vis-sub', spec.subtitle));
    head.appendChild(titles);

    const acts = dom('div', 'kc-vis-acts');
    const tsvBtn = dom('button', 'kc-vis-btn', 'TSV');
    tsvBtn.type = 'button';
    tsvBtn.title = 'Copy data as TSV';
    tsvBtn.onclick = () => copyText(toTSV(kind, spec), tsvBtn);
    acts.appendChild(tsvBtn);

    if (getSvg) {
      const svgBtn = dom('button', 'kc-vis-btn', 'SVG');
      svgBtn.type = 'button';
      svgBtn.title = 'Copy chart SVG';
      svgBtn.onclick = () => {
        const node = getSvg();
        if (node) copyText(inlineSvgForExport(node), svgBtn);
      };
      acts.appendChild(svgBtn);
    }
    if (!inModal && kind === 'chart') {
      const expBtn = dom('button', 'kc-vis-btn', 'Expand');
      expBtn.type = 'button';
      expBtn.title = 'Open larger view';
      expBtn.onclick = () => openExpandModal(kind, spec);
      acts.appendChild(expBtn);
    }
    head.appendChild(acts);
    return head;
  }

  function renderChartBlock(host, spec, opts) {
    const card = dom('div', 'kc-vis-card');
    if (spec.autoFlip) host.classList.add('kc-vis--autoflip');
    const readout = dom('div', 'kc-vis-readout');
    setReadout(readout, null);
    const hiddenSet = new Set();
    const plotBox = dom('div', 'kc-vis-plot');
    let activeSvg = null;

    function paintPlot() {
      plotBox.textContent = '';
      activeSvg = null;
      if (spec.type === 'hbar') {
        plotBox.appendChild(renderHBarDOM(spec, readout));
      } else if (spec.type === 'progress') {
        plotBox.appendChild(renderProgressDOM(spec, readout));
      } else if (spec.type === 'donut') {
        const res = renderDonutDOM(spec, readout);
        activeSvg = res.svg;
        plotBox.appendChild(res.wrap);
      } else if (spec.type === 'line' || spec.type === 'area') {
        activeSvg = renderLineAreaSVG(spec, hiddenSet, readout);
        plotBox.appendChild(activeSvg);
      } else if (spec.type === 'scatter') {
        activeSvg = renderScatterSVG(spec, hiddenSet, readout);
        plotBox.appendChild(activeSvg);
      } else {
        // bar (with optional narrow auto-flip to hbar)
        const wide = dom('div', 'kc-vis-view--wide');
        activeSvg = renderVerticalBarSVG(spec, hiddenSet, readout);
        wide.appendChild(activeSvg);
        plotBox.appendChild(wide);
        if (spec.autoFlip) {
          const narrow = dom('div', 'kc-vis-view--narrow');
          narrow.appendChild(renderHBarDOM(spec, readout));
          plotBox.appendChild(narrow);
        }
      }
    }

    card.appendChild(buildHeader('chart', spec, { inModal: opts.inModal, getSvg: () => activeSvg }));

    // Interactive legend when multi-series
    if (spec.series.length > 1 && spec.type !== 'donut' && spec.type !== 'progress' && spec.type !== 'hbar') {
      const leg = dom('ul', 'kc-vis-legend');
      spec.series.forEach((s, idx) => {
        const li = dom('li');
        const btn = dom('button', 'kc-vis-leg-btn');
        btn.type = 'button';
        btn.setAttribute('aria-pressed', 'true');
        const sw = dom('span', 'kc-vis-swatch' + (s.predicted ? ' kc-vis-swatch--pred' : ''));
        const col = s.predicted ? 'var(--vis-pred)' : seriesColor(idx);
        sw.style.background = col;
        sw.style.color = col;
        btn.append(sw, document.createTextNode(s.name + (s.predicted ? ' (est.)' : '')));
        btn.onclick = () => {
          if (hiddenSet.has(idx)) hiddenSet.delete(idx);
          else if (hiddenSet.size < spec.series.length - 1) hiddenSet.add(idx);
          btn.setAttribute('aria-pressed', String(!hiddenSet.has(idx)));
          paintPlot();
        };
        li.appendChild(btn);
        leg.appendChild(li);
      });
      card.appendChild(leg);
    }

    card.append(readout, plotBox);
    paintPlot();
    if (spec.caption) card.appendChild(dom('p', 'kc-vis-cap', spec.caption));
    host.appendChild(card);
  }

  function renderNumbersBlock(host, spec) {
    const wrap = dom('div');
    if (spec.title) wrap.appendChild(buildHeader('numbers', spec, { inModal: true }));
    const grid = dom('div', 'kc-nums-grid');
    grid.setAttribute('role', 'group');
    grid.setAttribute('aria-label', spec.title || 'Key metrics');
    for (const it of spec.items) {
      const tile = dom('div', 'kc-num-tile');
      tile.dataset.tone = it.tone;
      tile.appendChild(dom('span', 'kc-num-lbl', it.label)).title = it.label;
      const val = dom('div', 'kc-num-val');
      val.appendChild(dom('span', '', typeof it.value === 'number' ? formatNumber(it.value) : it.value));
      if (it.unit) val.appendChild(dom('span', 'kc-num-unit', it.unit));
      tile.appendChild(val);
      if (it.delta || it.note || it.tone === 'predicted') {
        const meta = dom('div', 'kc-num-meta');
        if (it.tone === 'predicted') meta.appendChild(dom('span', 'kc-num-chip', 'Est.'));
        if (it.delta) meta.appendChild(dom('span', 'kc-num-delta', it.delta));
        if (it.note) meta.appendChild(dom('span', '', it.note));
        tile.appendChild(meta);
      }
      grid.appendChild(tile);
    }
    wrap.appendChild(grid);
    if (spec.caption) wrap.appendChild(dom('p', 'kc-vis-cap', spec.caption));
    host.appendChild(wrap);
  }

  function renderTableBlock(host, spec, opts) {
    const card = dom('div', 'kc-vis-card');
    card.appendChild(buildHeader('table', spec, { inModal: opts.inModal }));

    // Precompute max values for bar:true columns
    const colMax = spec.columns.map((c, ci) => {
      if (!c.bar) return 0;
      let mx = 0;
      for (const r of spec.rows) { const n = numSortVal(r[ci]); if (n !== null && Math.abs(n) > mx) mx = Math.abs(n); }
      return mx || 1;
    });

    let query = '', sortCol = -1, sortDir = 0, showAll = false;
    const PAGE = 10;

    if (spec.rows.length > 12) {
      const bar = dom('div', 'kc-tbl-bar');
      const inp = dom('input', 'kc-tbl-search');
      inp.type = 'search';
      inp.placeholder = `Filter ${spec.rows.length} rows…`;
      inp.addEventListener('input', () => { query = inp.value.trim().toLowerCase(); paintRows(); });
      bar.appendChild(inp);
      card.appendChild(bar);
    }

    const wrap = dom('div', 'kc-tbl-wrap');
    wrap.tabIndex = 0;
    const tbl = dom('table', 'kc-tbl');
    const thead = dom('thead');
    const hr = dom('tr');
    const ths = spec.columns.map((col, ci) => {
      const th = dom('th', col.num ? 'num' : '', col.unit ? `${col.label} (${col.unit})` : col.label);
      th.scope = 'col';
      th.title = 'Click to sort';
      th.onclick = () => {
        if (sortCol === ci) sortDir = sortDir === 1 ? -1 : sortDir === -1 ? 0 : 1;
        else { sortCol = ci; sortDir = 1; }
        ths.forEach((t, ti) => { t.dataset.sort = ti === sortCol && sortDir ? (sortDir === 1 ? 'asc' : 'desc') : ''; });
        paintRows();
      };
      hr.appendChild(th);
      return th;
    });
    thead.appendChild(hr);
    tbl.appendChild(thead);

    const tbody = dom('tbody');
    tbl.appendChild(tbody);

    if (spec.footer) {
      const tfoot = dom('tfoot');
      const fr = dom('tr');
      spec.columns.forEach((col, ci) => {
        const v = spec.footer[ci];
        fr.appendChild(dom('td', col.num ? 'num' : '', typeof v === 'number' ? formatNumber(v) : v));
      });
      tfoot.appendChild(fr);
      tbl.appendChild(tfoot);
    }
    wrap.appendChild(tbl);
    card.appendChild(wrap);

    const moreBtn = dom('button', 'kc-tbl-more');
    moreBtn.type = 'button';
    moreBtn.hidden = true;
    moreBtn.onclick = () => { showAll = !showAll; paintRows(); };
    card.appendChild(moreBtn);

    function paintRows() {
      let list = spec.rows.slice();
      if (query) list = list.filter((r) => r.some((cell) => String(cell).toLowerCase().includes(query)));
      if (sortCol >= 0 && sortDir !== 0) {
        const isNum = spec.columns[sortCol].num;
        const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
        list.sort((a, b) => {
          const av = a[sortCol], bv = b[sortCol];
          if (isNum) {
            const na = numSortVal(av), nb = numSortVal(bv);
            if (na === null) return nb === null ? 0 : 1;
            if (nb === null) return -1;
            return sortDir * (na - nb);
          }
          return sortDir * coll.compare(String(av), String(bv));
        });
      }
      const shown = showAll || list.length <= PAGE ? list : list.slice(0, PAGE);
      tbody.textContent = '';
      for (const r of shown) {
        const tr = dom('tr');
        spec.columns.forEach((col, ci) => {
          const v = r[ci];
          const td = dom('td', col.num ? 'num' : '');
          const txt = typeof v === 'number' ? formatNumber(v) : String(v);
          if (col.bar) {
            const n = numSortVal(v);
            const box = dom('div', 'kc-tbl-barcell');
            box.appendChild(dom('span', '', txt));
            const track = dom('span', 'kc-tbl-minibar');
            const fill = dom('i');
            fill.style.width = `${n !== null ? Math.min(100, Math.max(0, (Math.abs(n) / colMax[ci]) * 100)).toFixed(1) : 0}%`;
            track.appendChild(fill);
            box.appendChild(track);
            td.appendChild(box);
          } else {
            td.textContent = txt;
          }
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      }
      if (list.length > PAGE) {
        moreBtn.hidden = false;
        moreBtn.textContent = showAll ? 'Show fewer rows' : `Show all ${list.length} rows`;
      } else {
        moreBtn.hidden = true;
      }
    }
    paintRows();
    if (spec.caption) card.appendChild(dom('p', 'kc-vis-cap', spec.caption));
    host.appendChild(card);
  }

  function renderStepsBlock(host, spec) {
    const card = dom('div', 'kc-vis-card');
    if (spec.title) card.appendChild(buildHeader('steps', spec, { inModal: true }));
    const ol = dom('ol', 'kc-steps');
    spec.steps.forEach((s, i) => {
      const li = dom('li', 'kc-step');
      li.dataset.state = s.state;
      const dot = dom('span', 'kc-step-dot', s.state === 'done' ? '✓' : s.state === 'error' ? '!' : String(i + 1));
      const body = dom('div');
      body.appendChild(dom('div', 'kc-step-lbl', s.label));
      if (s.detail) body.appendChild(dom('div', 'kc-step-det', s.detail));
      li.append(dot, body);
      ol.appendChild(li);
    });
    card.appendChild(ol);
    if (spec.caption) card.appendChild(dom('p', 'kc-vis-cap', spec.caption));
    host.appendChild(card);
  }

  function renderNote(host, text, caption) {
    const card = dom('div', 'kc-vis-card kc-vis-card--note');
    const p = dom('div', 'kc-vis-note');
    const ico = svgEl('svg', { viewBox: '0 0 16 16', 'aria-hidden': 'true' });
    ico.append(svgEl('circle', { cx: 8, cy: 8, r: 6 }), svgEl('path', { d: 'M8 7v4M8 5.2v.01' }));
    p.append(ico, dom('span', '', text));
    card.appendChild(p);
    if (caption) card.appendChild(dom('p', 'kc-vis-cap', caption));
    host.appendChild(card);
  }

  function renderPending(host, kind) {
    const p = dom('div', 'kc-vis-pending');
    p.dataset.kind = kind;
    const labels = { chart: 'Drawing chart…', compare: 'Drawing comparison…', numbers: 'Preparing metrics…', table: 'Building table…', steps: 'Drawing steps…' };
    p.appendChild(dom('span', '', labels[kind] || 'Preparing visual…'));
    host.appendChild(p);
  }

  function renderValidated(host, kind, spec, opts) {
    host.textContent = '';
    if (kind === 'chart') renderChartBlock(host, spec, opts || {});
    else if (kind === 'numbers') renderNumbersBlock(host, spec);
    else if (kind === 'table') renderTableBlock(host, spec, opts || {});
    else if (kind === 'steps') renderStepsBlock(host, spec);
  }

  function renderBlock(host) {
    if (!host || host.dataset.visMounted === '1') return;
    const kind = host.dataset.visKind;
    const state = host.dataset.visState || 'ready';
    const key = host.dataset.visKey || '';
    if (state === 'pending') {
      host.textContent = '';
      renderPending(host, kind);
      return;
    }
    host.dataset.visMounted = '1';
    if (state === 'incomplete') {
      renderNote(host, `${kind === 'numbers' ? 'Metrics' : 'Visual'} could not be drawn: its specification was cut off.`);
      return;
    }
    const raw = SPEC_CACHE.get(key);
    if (raw === undefined) {
      renderNote(host, 'Visual could not be loaded.');
      return;
    }
    try {
      const { kind: normKind, spec } = validate(kind, raw);
      renderValidated(host, normKind, spec, { inModal: false });
    } catch (err) {
      let cap = '';
      const parsed = parseSpec(raw);
      if (parsed.value && typeof parsed.value === 'object' && typeof parsed.value.caption === 'string') {
        cap = parsed.value.caption.slice(0, 300);
      }
      const msg = err instanceof SpecError ? err.message : 'its specification could not be read';
      renderNote(host, `Visual could not be drawn: ${msg}.`, cap);
    }
  }

  /* ---- Markdown integration helpers ---- */
  function placeholderHTML(kind, raw, opts) {
    const k = visualKind(kind);
    if (!k) return null;
    const streaming = Boolean(opts && opts.streaming);
    const closed = opts && opts.closed !== undefined ? Boolean(opts.closed) : true;
    const state = !closed ? (streaming ? 'pending' : 'incomplete') : 'ready';
    const key = hashKey(`${k}\n${state}\n${raw || ''}`);
    if (state === 'ready') cachePut(key, String(raw || ''));
    return `<div class="kc-vis" data-vis-kind="${k}" data-vis-key="${key}" data-vis-state="${state}"></div>`;
  }

  /** Detect open (unclosed) visual fence at the tail of streaming text so raw JSON never flashes. */
  function preprocessStreaming(text, streaming) {
    const src = String(text == null ? '' : text).replace(/\r\n/g, '\n');
    const lines = src.split('\n');
    let openFence = null, openKind = null, openIdx = -1;
    for (let i = 0; i < lines.length; i++) {
      const t = lines[i].trim();
      if (!openFence) {
        const m = /^(```|~~~)([^`~]*)$/.exec(t);
        if (m) {
          openFence = m[1];
          openKind = visualKind(m[2]);
          openIdx = i;
        }
      } else if (t.startsWith(openFence)) {
        openFence = null;
        openKind = null;
        openIdx = -1;
      }
    }
    if (openFence && openKind && openIdx >= 0) {
      const before = lines.slice(0, openIdx).join('\n');
      const body = lines.slice(openIdx + 1).join('\n');
      const ph = placeholderHTML(openKind, body, { streaming, closed: false });
      return { text: before, tailPlaceholder: ph };
    }
    return { text: src, tailPlaceholder: '' };
  }

  /* ---- Callouts & Plain Markdown Table Enhancer ---- */
  const ALERT_MAP = {
    note: ['note', 'Note'],
    tip: ['tip', 'Tip'],
    important: ['note', 'Note'],
    caution: ['caution', 'Caution'],
    warning: ['caution', 'Caution'],
    prediction: ['predicted', 'Prediction'],
    predicted: ['predicted', 'Prediction'],
  };
  const ALERT_PATHS = {
    note: ['M8 7v4M8 5.2v.01'],
    tip: ['M8 2.5a3.5 3.5 0 0 1 2 6.4V11H6V8.9a3.5 3.5 0 0 1 2-6.4zM6.5 13h3'],
    caution: ['M8 2.2L14 13H2L8 2.2zM8 6.5v3M8 11.2v.01'],
    predicted: ['M4 4h8v8H4zM6.5 1.5V4M9.5 1.5V4M6.5 12v2.5M9.5 12v2.5'],
  };

  function parseAlertLine(text) {
    const m = /^\[!([A-Za-z]+)\][ \t]*([^\n]*)/.exec(String(text || '').trim());
    if (!m) return null;
    const hit = ALERT_MAP[m[1].toLowerCase()];
    if (!hit) return null;
    return { tone: hit[0], title: (m[2] || '').trim() || hit[1], matchLen: m[0].length };
  }

  function enhanceCallouts(rootEl) {
    if (!rootEl || !rootEl.querySelectorAll) return;
    for (const bq of [...rootEl.querySelectorAll('blockquote')]) {
      if (bq.dataset.kcEnhanced) continue;
      const firstP = bq.querySelector(':scope > p');
      if (!firstP) continue;
      const fullFirst = firstP.textContent || '';
      const firstLine = fullFirst.split('\n')[0];
      const alert = parseAlertLine(firstLine);
      if (!alert) continue;

      const box = dom('div', 'kc-callout');
      box.dataset.tone = alert.tone;
      box.setAttribute('role', 'note');
      const ico = svgEl('svg', { class: 'kc-callout-ico', viewBox: '0 0 16 16', 'aria-hidden': 'true' });
      if (alert.tone === 'note') ico.appendChild(svgEl('circle', { cx: 8, cy: 8, r: 6 }));
      for (const d of ALERT_PATHS[alert.tone] || ALERT_PATHS.note) ico.appendChild(svgEl('path', { d }));

      const body = dom('div', 'kc-callout-body');
      body.appendChild(dom('strong', 'kc-callout-title', alert.title));
      // Strip the "[!TYPE] optional title" prefix from the first text node of firstP
      const restLines = fullFirst.slice(firstLine.length).replace(/^\n+/, '');
      if (restLines.trim()) {
        // Preserve inline formatting if there are child elements after the marker, else plain text
        const firstChild = firstP.firstChild;
        if (firstChild && firstChild.nodeType === 3) {
          const t = firstChild.nodeValue || '';
          const nl = t.indexOf('\n');
          if (nl >= 0) firstChild.nodeValue = t.slice(nl + 1);
          else firstChild.nodeValue = t.replace(/^\[![A-Za-z]+\][^\n]*/, '').trimStart();
          // Also remove leading <br> if marked inserted one
          if (firstP.firstChild === firstChild && !firstChild.nodeValue.trim() && firstChild.nextSibling && firstChild.nextSibling.tagName === 'BR') {
            firstChild.nextSibling.remove();
          }
          body.appendChild(firstP);
        } else {
          body.appendChild(dom('p', '', restLines));
        }
      } else {
        firstP.remove();
      }
      while (bq.firstChild) body.appendChild(bq.firstChild);
      box.append(ico, body);
      bq.replaceWith(box);
    }
  }

  function enhancePlainTables(rootEl) {
    if (!rootEl || !rootEl.querySelectorAll) return;
    for (const tbl of [...rootEl.querySelectorAll('table')]) {
      if (tbl.closest('.kc-vis, .kc-md-tbl-box')) continue;
      const headCells = [...tbl.querySelectorAll('thead th')];
      const bodyRows = [...tbl.querySelectorAll('tbody tr')];
      if (!headCells.length || !bodyRows.length) continue;

      const numericCols = headCells.map((_, ci) => {
        let filled = 0, nums = 0;
        for (const tr of bodyRows) {
          const td = tr.children[ci];
          const txt = (td ? td.textContent : '').trim();
          if (!txt || /^[-—–]$/.test(txt)) continue;
          filled++;
          if (isNumericText(txt)) nums++;
        }
        return filled > 0 && nums === filled;
      });
      headCells.forEach((th, ci) => { if (numericCols[ci]) th.classList.add('num'); });
      bodyRows.forEach((tr) => {
        [...tr.children].forEach((td, ci) => { if (numericCols[ci]) td.classList.add('num'); });
      });

      const box = dom('div', 'kc-md-tbl-box');
      const bar = dom('div', 'kc-md-tbl-head');
      const copyBtn = dom('button', 'kc-vis-btn', 'Copy TSV');
      copyBtn.type = 'button';
      copyBtn.onclick = () => {
        const lines = [headCells.map((th) => cleanCell(th.textContent)).join('\t')];
        for (const tr of bodyRows) lines.push([...tr.children].map((td) => cleanCell(td.textContent)).join('\t'));
        copyText(lines.join('\n') + '\n', copyBtn);
      };
      bar.appendChild(copyBtn);
      const wrap = dom('div', 'kc-md-tbl-wrap');
      wrap.tabIndex = 0;
      tbl.parentNode.insertBefore(box, tbl);
      wrap.appendChild(tbl);
      box.append(bar, wrap);
    }
  }

  function getObserver() {
    if (lazyObserver || typeof IntersectionObserver !== 'function') return lazyObserver;
    lazyObserver = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        lazyObserver.unobserve(e.target);
        renderBlock(e.target);
      }
    }, { rootMargin: '220px 0px' });
    return lazyObserver;
  }

  function mountVisuals(rootEl, opts) {
    if (!rootEl || !rootEl.querySelectorAll) return;
    const eager = Boolean(opts && opts.eager);
    enhanceCallouts(rootEl);
    enhancePlainTables(rootEl);
    const obs = eager ? null : getObserver();
    for (const node of rootEl.querySelectorAll('.kc-vis')) {
      if (node.dataset.visMounted === '1') continue;
      if (eager || !obs || node.dataset.visState === 'pending') {
        renderBlock(node);
      } else if (node.isConnected) {
        obs.observe(node);
      } else {
        requestAnimationFrame(() => {
          if (node.dataset.visMounted === '1') return;
          if (node.isConnected) obs.observe(node);
          else renderBlock(node);
        });
      }
    }
  }

  /** Flicker-free live streaming update: preserves already-mounted visuals with identical data-vis-key. */
  function patchLive(proseEl, html) {
    if (!proseEl) return;
    const kept = new Map();
    for (const old of proseEl.querySelectorAll('.kc-vis[data-vis-mounted="1"]')) {
      const k = old.dataset.visKey;
      if (k && !kept.has(k)) kept.set(k, old);
    }
    const tpl = document.createElement('template');
    tpl.innerHTML = html;
    for (const ph of [...tpl.content.querySelectorAll('.kc-vis')]) {
      const k = ph.dataset.visKey;
      if (k && ph.dataset.visState === 'ready' && kept.has(k)) {
        ph.replaceWith(kept.get(k));
        kept.delete(k);
      }
    }
    proseEl.replaceChildren(tpl.content);
    mountVisuals(proseEl, { eager: true });
  }

  return {
    KINDS,
    CHART_TYPES,
    TONES,
    STEP_STATES,
    visualKind,
    hashKey,
    parseSpec,
    validate,
    validateChart,
    validateNumbers,
    validateTable,
    validateSteps,
    formatNumber,
    toNumber,
    isNumericText,
    toTSV,
    parseAlertLine,
    placeholderHTML,
    preprocessStreaming,
    renderBlock,
    mountVisuals,
    patchLive,
    enhanceCallouts,
    enhancePlainTables,
  };
});
