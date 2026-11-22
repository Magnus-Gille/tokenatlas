/* Browser regression (TOKEN-178): calendar-month comparison and monthly aggregate CSV.
   PLAYWRIGHT_MODULE=/path/to/playwright node test_months_browser.cjs report.html [more.html] */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
(async () => {
  const browser = await playwright[process.env.BROWSER || 'chromium'].launch({headless: true});
  const files = process.argv.slice(2).filter(Boolean);
  assert.ok(files.length, 'usage: report.html [more reports]');
  for (const file of files) {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, offline: true, acceptDownloads: true, locale: 'en-US'});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setContent(fs.readFileSync(file, 'utf8'), {waitUntil: 'load'});
    await page.waitForFunction(() => window.reportReady === true, null, {timeout: 60000});
    const months = await page.$$eval('#month option', o => o.map(x => x.value));
    assert.ok(months.length > 0, 'months are listed from the data');
    assert.deepEqual(months, [...months].sort().reverse(), 'newest month first');
    // Boundaries: year change, and an empty previous month is unknown, not zero.
    assert.equal(await page.evaluate(() => UsageReport.prevMonth('2026-01')), '2025-12');
    assert.equal(await page.evaluate(() => UsageReport.prevMonth('2026-03')), '2026-02');
    const rows = await page.evaluate(m => UsageReport.monthRows(m).length, months[0]);
    const total = await page.evaluate(() => UsageReport.all.length);
    assert.ok(rows > 0 && rows <= total);
    const oldest = months[months.length - 1];
    await page.selectOption('#month', oldest);
    const text = await page.locator('#month-table').innerText();
    assert.match(text, /no observations \(unknown, not zero\)/, 'the month before the oldest is a gap: ' + text);
    assert.ok(!/\b0 requests\b/i.test(text));
    // The month limits rows to that month and other filters stay active.
    const all = await page.evaluate(m => UsageReport.monthRows(m).every(r => r.date.slice(0, 7) === m), oldest);
    assert.ok(all);
    const harness = await page.$$eval('#harness option', o => o.map(x => x.value).filter(Boolean));
    if (harness.length) {
      await page.selectOption('#harness', harness[0]);
      const n = await page.evaluate(m => UsageReport.monthRows(m).length, oldest);
      assert.ok(n <= await page.evaluate(m => { const f = UsageReport.all.filter(r => r.date.slice(0, 7) === m); return f.length; }, oldest));
      await page.selectOption('#harness', '');
    }
    // Lower bounds: a month with an incomplete or unpriced request shows a floor, never an exact cost.
    const flags = await page.evaluate(() => UsageReport.monthRows(document.getElementById('month').value).some(r => r.cost == null || !r.complete));
    const costCell = (await page.locator('#month-table tr', {hasText: 'List price'}).innerText());
    if (flags) assert.ok(costCell.includes('≥') || /unknown|Unknown/.test(costCell), 'cost floor is flagged: ' + costCell);
    // The current month is judged in the report time zone and marked partial only when it is that month.
    const partialOk = await page.evaluate(() => { const g = new Intl.DateTimeFormat('sv-SE', {timeZone: UsageReport.data.timezone || 'UTC', year: 'numeric', month: '2-digit'}).format(new Date(UsageReport.data.generated_at)); return [...document.querySelectorAll('#month option')].map(o => o.value).includes(g); });
    if (partialOk) {
      const g = await page.evaluate(() => new Intl.DateTimeFormat('sv-SE', {timeZone: UsageReport.data.timezone || 'UTC', year: 'numeric', month: '2-digit'}).format(new Date(UsageReport.data.generated_at)));
      await page.selectOption('#month', g);
      assert.match(await page.locator('#month-table').innerText(), /partial/);
      const other = months.find(m => m !== g);
      if (other) { await page.selectOption('#month', other); assert.ok(!/partial/.test(await page.locator('#month-table').innerText())); }
    }
    // CSV: coverage fields, filters, period and time zone; formula-looking text stays escaped.
    await page.selectOption('#month', months[0]);
    const [download] = await Promise.all([page.waitForEvent('download'), page.click('#export-months')]);
    const csv = fs.readFileSync(await download.path(), 'utf8').replace(/^﻿/, '');
    const lines = csv.split('\r\n');
    assert.equal(lines.length, 3, 'header, previous month, selected month');
    const header = lines[0].split(',').map(x => x.replace(/"/g, ''));
    for (const k of ['period', 'timezone', 'filters', 'requests', 'turns', 'known_tokens', 'tokens_complete', 'priced_requests', 'unpriced_requests', 'cost_is_lower_bound', 'first_date', 'last_date', 'partial_month']) assert.ok(header.includes(k), k);
    assert.ok(lines[2].startsWith('"' + months[0] + '"'));
    assert.equal(await page.evaluate(() => UsageReport.csvCell('=1+1')), '"\'=1+1"');
    // Phone width: no horizontal scroll caused by the month table.
    await page.setViewportSize({width: 390, height: 800});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'no horizontal scroll at phone width');
    assert.deepEqual(errors, []);
    await context.close();
  }
  await browser.close();
  console.log('months browser test ok');
})().catch(e => { console.error(e); process.exit(1); });
