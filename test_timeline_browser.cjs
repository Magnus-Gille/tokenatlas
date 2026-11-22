/* Browser regression (TOKEN-179): event timeline of quota stops and data-quality groups.
   PLAYWRIGHT_MODULE=/path/to/playwright node test_timeline_browser.cjs report.html [more.html] */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const zlib = require('node:zlib');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
function withHit(html) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    const harness = data.coverage.ranges[0].harness;
    data.limit_hits = [{harness, at: '2026-09-03T10:00:00+00:00', local_date: '2026-09-03', reached: 'five_hour', window_minutes: 300, resets_at: null, retries: 0, prompt: null, label: null, window: null}];
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
(async () => {
  const browser = await playwright[process.env.BROWSER || 'chromium'].launch({headless: true});
  const files = process.argv.slice(2).filter(Boolean);
  assert.ok(files.length, 'usage: report.html [more reports]');
  for (const file of files) {
    for (const locale of ['en-US', 'sv-SE']) {
      const context = await browser.newContext({viewport: {width: 1440, height: 1000}, offline: true, locale});
      const page = await context.newPage(), errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.setContent(withHit(fs.readFileSync(file, 'utf8')), {waitUntil: 'load'});
      await page.waitForFunction(() => window.reportReady === true, null, {timeout: 60000});
      const items = () => page.$$eval('#tl-list li', l => l.map(x => ({kind: x.dataset.kind || '', text: x.innerText})));
      const base = await items();
      const dated = base.filter(x => x.kind);
      const expected = await page.evaluate(() => {
        const unpriced = UsageReport.getSelected().filter(r => !r.id_synthetic && (r.cost == null || !r.complete));
        return {groups: new Set(unpriced.map(r => r.date + '|' + r.harness + '|' + r.model)).size, hits: (UsageReport.data.limit_hits || []).length};
      });
      assert.equal(dated.filter(x => x.kind === 'price').length + dated.filter(x => x.kind === 'limit').length, dated.length);
      assert.ok(dated.filter(x => x.kind === 'price').length <= expected.groups, 'one bounded event per group, not per request');
      assert.ok(dated.filter(x => x.kind === 'limit').length <= expected.hits);
      // A limit hit is a dated event, unlinked when its turn is not in the report, and the date filter applies to it.
      assert.equal(dated.filter(x => x.kind === 'limit').length, 1, 'the injected limit hit');
      assert.match(dated.find(x => x.kind === 'limit').text, /2026-09-03/);
      assert.match(dated.find(x => x.kind === 'limit').text, /unlinked|olänkad/);
      // Deterministic: newest day first.
      const days = dated.map(x => x.text.match(/\d{4}-\d{2}-\d{2}/)[0]);
      assert.deepEqual(days, [...days].sort().reverse(), 'newest first');
      // A blank day never becomes an event.
      assert.ok(!dated.some(x => /gap|lucka/i.test(x.kind)));
      // Non-date filters apply to quota stops too: an unscoped hit is dropped when a model filter is active.
      const models = await page.$$eval('#model option', o => o.map(x => x.value).filter(Boolean));
      if (models.length) {
        await page.selectOption('#model', models[0]);
        assert.equal((await items()).filter(x => x.kind === 'limit').length, 0, 'unscoped hit excluded by a model filter');
        await page.selectOption('#model', '');
      }
      // An empty selection says so and does not claim nothing happened.
      await page.fill('#from', '2001-01-01');
      await page.fill('#to', '2001-01-02');
      const empty = await items();
      assert.equal(empty.length, 1);
      assert.match(empty[0].text, /does not mean|inte att/);
      assert.equal(await page.locator('#tl-more').isHidden(), true);
      // Shared reports show no raw paths or session ids in the timeline.
      const text = await page.locator('#timeline').innerText();
      if (/shared/.test(file)) assert.ok(!/\/(?:Users|home|work|tmp)\b|[A-Za-z]:\\|session-/i.test(text), 'redacted: ' + text);
      assert.deepEqual(errors, []);
      await context.close();
    }
  }
  await browser.close();
  console.log('timeline browser test ok');
})().catch(e => { console.error(e); process.exit(1); });
