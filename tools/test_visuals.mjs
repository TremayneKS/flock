import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
const KCV = require(path.join(ROOT, 'web', 'visuals.js'));
const fixtures = JSON.parse(fs.readFileSync(path.join(ROOT, 'tools', 'visual-spec-fixtures.json'), 'utf8'));

test('valid golden fixtures parse, validate, and export TSV cleanly', () => {
  assert.ok(fixtures.valid.length >= 15, 'expected >= 15 valid fixtures');
  for (const fx of fixtures.valid) {
    const raw = fx.raw ?? JSON.stringify(fx.spec);
    const out = KCV.validate(fx.kind, raw);
    assert.ok(out && out.spec, `${fx.id}: expected validated spec`);
    if (fx.expect.type) assert.equal(out.spec.type, fx.expect.type, `${fx.id}: chart type`);
    if (fx.expect.seriesCount != null) assert.equal(out.spec.series.length, fx.expect.seriesCount, `${fx.id}: seriesCount`);
    if (fx.expect.pointCount != null) {
      const actual = out.spec.type === 'scatter'
        ? out.spec.series.reduce((n, s) => n + s.points.length, 0)
        : (out.spec.items ? out.spec.items.length : out.spec.x.length);
      assert.equal(actual, fx.expect.pointCount, `${fx.id}: pointCount`);
    }
    if (fx.expect.autoFlip != null) assert.equal(out.spec.autoFlip, fx.expect.autoFlip, `${fx.id}: autoFlip`);
    if (fx.expect.stacked != null) assert.equal(out.spec.stacked, fx.expect.stacked, `${fx.id}: stacked`);
    if (fx.expect.hasGaps) {
      assert.ok(out.spec.series.some((s) => s.values.some((v) => v === null)), `${fx.id}: expected null gap`);
    }
    if (fx.expect.hasWhiskers) {
      assert.ok(out.spec.series.some((s) => s.low && s.high && s.low.some((v) => v !== null)), `${fx.id}: expected low/high`);
    }
    if (fx.expect.hasRef) assert.ok(out.spec.ref && Number.isFinite(out.spec.ref.value), `${fx.id}: expected ref`);
    if (fx.expect.hasNegative) {
      assert.ok(out.spec.series.some((s) => s.values.some((v) => v !== null && v < 0)), `${fx.id}: expected negative`);
    }
    if (fx.expect.itemCount != null) assert.equal(out.spec.items.length, fx.expect.itemCount, `${fx.id}: itemCount`);
    if (fx.expect.colCount != null) assert.equal(out.spec.columns.length, fx.expect.colCount, `${fx.id}: colCount`);
    if (fx.expect.rowCount != null) assert.equal(out.spec.rows.length, fx.expect.rowCount, `${fx.id}: rowCount`);
    if (fx.expect.hasFooter != null) assert.equal(Boolean(out.spec.footer), fx.expect.hasFooter, `${fx.id}: hasFooter`);
    if (fx.expect.stepCount != null) assert.equal(out.spec.steps.length, fx.expect.stepCount, `${fx.id}: stepCount`);

    const tsv = KCV.toTSV(out.kind, out.spec);
    assert.ok(tsv.includes('\t') && tsv.endsWith('\n'), `${fx.id}: TSV export`);
  }
});

test('callout parser handles NOTE, CAUTION, TIP, PREDICTION with optional custom titles', () => {
  for (const fx of fixtures.callouts) {
    const firstLine = fx.markdown.split('\n')[0].replace(/^>\s*/, '');
    const parsed = KCV.parseAlertLine(firstLine);
    assert.ok(parsed, `${fx.id}: parsed alert`);
    assert.equal(parsed.tone, fx.expectTone, `${fx.id}: tone`);
    assert.equal(parsed.title, fx.expectTitle, `${fx.id}: title`);
  }
});

test('invalid fixtures fail cleanly with readable messages', () => {
  for (const fx of fixtures.invalid) {
    if (fx.incomplete) {
      const pre = KCV.preprocessStreaming('```' + fx.kind + '\n' + fx.raw, false);
      assert.ok(pre.tailPlaceholder.includes('data-vis-state="incomplete"'), `${fx.id}: incomplete state`);
      const preStream = KCV.preprocessStreaming('```' + fx.kind + '\n' + fx.raw, true);
      assert.ok(preStream.tailPlaceholder.includes('data-vis-state="pending"'), `${fx.id}: pending while streaming`);
      continue;
    }
    assert.throws(
      () => KCV.validate(fx.kind, fx.raw),
      (err) => err && String(err.message).toLowerCase().includes(fx.expectError.toLowerCase()),
      `${fx.id}: expected error matching "${fx.expectError}"`
    );
  }
});

test('bounds caps reject oversized specs, >300 points, and >100 table rows', () => {
  const huge = '{"type":"bar","items":[' + '{"label":"A","value":1},'.repeat(1200) + '{"label":"B","value":2}]}';
  assert.throws(() => KCV.validate('chart', huge), /exceeds 20000/);

  const manyPts = JSON.stringify({
    type: 'line',
    x: Array.from({ length: 301 }, (_, i) => `P${i}`),
    series: [{ name: 'S', values: Array.from({ length: 301 }, (_, i) => i) }],
  });
  assert.throws(() => KCV.validate('chart', manyPts), /more than 300/);

  const manyRows = JSON.stringify({
    columns: ['A', 'B'],
    rows: Array.from({ length: 101 }, (_, i) => [`R${i}`, i]),
  });
  assert.throws(() => KCV.validate('table', manyRows), /more than 100/);
});

test('security fuzz cases never pollute Object.prototype and keep raw strings inert', () => {
  for (const fx of fixtures.security) {
    const raw = fx.raw ?? JSON.stringify(fx.spec);
    const out = KCV.validate(fx.kind, raw);
    assert.ok(out && out.spec, `${fx.id}: validated as data`);
    assert.equal(({}).kcPolluted, undefined, 'Object.prototype not polluted');
    assert.equal(({}).kcPolluted2, undefined, 'constructor.prototype not polluted');
    // Placeholder HTML must contain zero raw spec text or script tags
    const ph = KCV.placeholderHTML(fx.kind, raw, { streaming: false, closed: true });
    assert.ok(!ph.includes('<script'), `${fx.id}: placeholder contains no script`);
    assert.ok(!ph.includes('onerror'), `${fx.id}: placeholder contains no event handler`);
    assert.ok(!ph.includes('javascript:'), `${fx.id}: placeholder contains no javascript: url`);
  }
});

test('FNV-1a hashKey is deterministic and streaming preprocess splits only open visual fences', () => {
  const a = KCV.hashKey('chart\n{"type":"bar"}');
  const b = KCV.hashKey('chart\n{"type":"bar"}');
  const c = KCV.hashKey('chart\n{"type":"line"}');
  assert.equal(a, b);
  assert.notEqual(a, c);

  const closed = KCV.preprocessStreaming('Intro\n\n```chart\n{"type":"bar","items":[{"label":"A","value":1}]}\n```\n\nDone', true);
  assert.equal(closed.tailPlaceholder, '');
  assert.ok(closed.text.includes('Done'));

  const openCode = KCV.preprocessStreaming('```python\nprint(1)', true);
  assert.equal(openCode.tailPlaceholder, '', 'non-visual code fence is left untouched');
});

test('number formatting uses exact values, real minus, grouping, and scientific notation', () => {
  assert.equal(KCV.formatNumber(1840), '1,840');
  assert.equal(KCV.formatNumber(-360), '−360');
  assert.equal(KCV.formatNumber(0.000012), '1.2 × 10⁻⁵');
  assert.equal(KCV.toNumber('1,234.5'), 1234.5);
  assert.equal(KCV.toNumber('−42'), -42);
  assert.ok(Number.isNaN(KCV.toNumber('NaN')));
});
