/* Optional browser regression (TOKEN-177): coverage beside the cost and energy figures.
   PLAYWRIGHT_MODULE=/path/to/playwright-or-@playwright/test node test_coverage_line_browser.cjs report.html [shared-report.html] */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const zlib = require('node:zlib');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
const norm = x => x.replace(/[\s  ]+/g, ' ').trim();
const T = {
  en: {locale: 'en-US', line: /^Coverage: \d+ of \d+ requests? ha(?:s|ve) a price · \d+ of \d+ ha(?:s|ve) complete token counters\./, none: 'No observed requests in this selection. That means unknown, not zero usage.', energy: /^Coverage: \d+ of \d+ requests? ha(?:s|ve) a model weighting · \d+ of \d+ ha(?:s|ve) complete token counters\./, groups: 'Incomplete or unpriced:', link: 'Data coverage and limitations'},
  sv: {locale: 'sv-SE', line: /^Täckning: \d+ av \d+ anrop har pris · \d+ av \d+ har kompletta tokenräknare\./, none: 'Inga observerade anrop i urvalet. Det betyder okänt, inte noll användning.', energy: /^Täckning: \d+ av \d+ anrop har modellviktning · \d+ av \d+ har kompletta tokenräknare\./, groups: 'Ofullständigt eller utan pris:', link: 'Datatäckning och begränsningar'},
};
function edited(html, change) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    change(data);
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
(async () => {
  const browser = await playwright[process.env.BROWSER || 'chromium'].launch({headless: true});
  const open = async (locale, html) => {
    const context = await browser.newContext({viewport: {width: 1440, height: 1080}, offline: true, locale});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setContent(html, {waitUntil: 'load'});
    for (const deadline = Date.now() + 60000; !(await page.evaluate(() => window.reportReady === true));) {
      if (Date.now() > deadline) throw new Error('report did not become ready: ' + errors.join('; '));
      await page.waitForTimeout(50);
    }
    return {context, page, errors};
  };
  const lines = async page => ({answer: norm(await page.locator('#answer-coverage').innerText()), energy: norm(await page.locator('#energy-coverage').innerText())});
  const files = process.argv.slice(2).filter(Boolean);
  assert.ok(files.length, 'usage: report.html [more reports]');
  for (const file of files) {
    const html = fs.readFileSync(file, 'utf8');
    for (const [lang, L] of Object.entries(T)) {
      // Baseline: counts and a link to the detailed section, beside both the cost and the energy figures.
      const base = await open(L.locale, html);
      const got = await lines(base.page);
      assert.match(got.answer, L.line, lang + ' answer: ' + got.answer);
      assert.match(got.energy, L.energy, lang + ' energy: ' + got.energy);
      assert.equal(await base.page.locator('#answer-coverage a').getAttribute('href'), '#coverage');
      assert.equal(await base.page.locator('#energy-coverage a').getAttribute('href'), '#coverage');
      assert.equal(await base.page.locator('#answer-coverage a').innerText(), L.link);
      // An unpriced request is named by harness and model, and counted against "priced".
      const n = await base.page.evaluate(() => UsageReport.getSelected().filter(r => !r.id_synthetic).length);
      assert.ok(n > 0);
      await base.context.close();
      const unpriced = await open(L.locale, edited(html, d => { d.columns.price[0] = null; }));
      const un = await lines(unpriced.page);
      const [, pricedN, totalN] = un.answer.match(/(\d+) (?:of|av) (\d+)/).map(Number);
      assert.ok(pricedN < totalN, 'one request lost its price: ' + un.answer);
      assert.ok(un.answer.includes(L.groups) && !/\bnull\b/.test(un.answer), 'groups listed: ' + un.answer);
      const counted = await unpriced.page.evaluate(() => { const r = UsageReport.getSelected().filter(x => !x.id_synthetic); return [r.filter(x => x.cost != null).length, r.length]; });
      assert.deepEqual([pricedN, totalN], counted, 'numbers match the selection');
      assert.deepEqual(unpriced.errors, []);
      await unpriced.context.close();
      // An empty selection is unobserved, never a measured zero.
      const empty = await open(L.locale, html);
      await empty.page.fill('#from', '2001-01-01');
      await empty.page.fill('#to', '2001-01-02');
      const em = await lines(empty.page);
      assert.equal(em.answer, L.none);
      assert.equal(em.energy, '', 'the energy section has its own empty text');
      assert.equal(await empty.page.locator('#answer-coverage a').count(), 0);
      assert.deepEqual(empty.errors, []);
      await empty.context.close();
    }
    // A shared (redacted) report shows only counts, harness and model names: no paths, no session ids.
    const shared = await open('en-US', html);
    const text = (await lines(shared.page)).answer;
    assert.ok(!/\/(?:Users|home|work|tmp)\b|[A-Za-z]:\\|session-/i.test(text), 'no paths or ids: ' + text);
    if (/shared/.test(file)) assert.ok(!text.includes('mystery-model') && /model \d+/.test(text), 'shared reports pseudonymise model names: ' + text);
    await shared.context.close();
  }
  await browser.close();
  console.log('coverage line browser test ok');
})().catch(e => { console.error(e); process.exit(1); });
