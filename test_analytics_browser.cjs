/* Optional browser regression: PLAYWRIGHT_MODULE=/path/to/@playwright/test node test_analytics_browser.cjs report.html */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
const zlib = require('node:zlib');

function edited(html, fn) {
  const match = html.match(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/);
  assert.ok(match, 'report contains the compressed report-data payload');
  const data = JSON.parse(zlib.gunzipSync(Buffer.from(match[2], 'base64')).toString('utf8'));
  fn(data);
  return html.replace(match[0], match[1] + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + match[3]);
}

// Seven deliberately uneven observations exercise 7/30-day gaps and the
// Stockholm date boundary. Values are synthetic and contain no source text.
function withAnalyticsFixture(html) {
  return edited(html, data => {
    const c = data.columns;
    const rows = [
      {days: 0, harness: 'codex', model: 'gpt-5', effort: 'high', thread_kind: 'main', origin: 'cli', session: 'session-a', turn_id:'shared-turn-id', prompt: 0, tokens: [10, 20, 5, 15], complete: 1},
      {days: 0, harness: 'codex', model: 'gpt-5', effort: 'high', thread_kind: 'main', origin: 'cli', session: 'session-a', prompt: 0, tokens: [5, 0, 0, 5], complete: 1},
      {days: 2, harness: 'claude', model: 'sonnet', effort: 'medium', thread_kind: 'subagent', origin: 'web', session: 'session-a', turn_id:'shared-turn-id', prompt: 1, tokens: [40, 10, 0, 20], complete: 1},
      {days: 6, harness: 'codex', model: 'gpt-4.1', effort: 'low', thread_kind: 'main', origin: 'ide', session: 'session-c', prompt: 2, tokens: [0, 0, 10, 30], complete: 1},
      {days: 8, harness: 'claude', model: 'sonnet', effort: 'medium', thread_kind: 'main', origin: 'cli', session: 'session-d', prompt: 3, tokens: [60, 20, 0, 40], complete: 1},
      {days: 29, harness: 'codex', model: 'gpt-5', effort: 'high', thread_kind: 'main', origin: 'cli', session: 'session-e', prompt: 4, tokens: [100, 20, 0, 80], complete: 1},
      {days: 31, harness: 'claude', model: 'sonnet', effort: 'medium', thread_kind: 'main', origin: 'web', session: 'session-outside', prompt: 5, tokens: [0, 0, 0, 0], complete: 1},
      {days: 1, harness: 'codex', model: 'gpt-5', effort: 'high', thread_kind: 'main', origin: 'cli', session: 'session-unknown', prompt: 6, tokens: [null, 0, 0, 10], complete: 0},
      {days: 1, harness: 'codex', model: 'gpt-5', effort: 'high', thread_kind: 'main', origin: 'cli', session: 'session-synthetic', prompt: 7, tokens: [1000, 0, 0, 1000], complete: 1, synthetic: 1},
    ];
    data.generated_at = '2026-10-10T12:00:00+00:00';
    const generated = Date.parse(data.generated_at);
    assert.ok(Number.isFinite(generated), 'fixture report has generated_at');
    const dictKeys = ['harness', 'provider', 'model', 'effort', 'thread_kind', 'origin', 'session', 'parent_session', 'turn_id'];
    const dictionaries = Object.fromEntries(dictKeys.map(k => [k, new Map((c.dict[k] || []).map((v, i) => [v, i]))]));
    const indexFor = (key, value) => {
      if (!dictionaries[key]) return 0;
      if (!dictionaries[key].has(value)) {
        dictionaries[key].set(value, c.dict[key].length);
        c.dict[key].push(value);
      }
      return dictionaries[key].get(value);
    };
    for (const key of dictKeys) c.idx[key] = [];
    c.idx.off = [];
    c.ts = [];
    c.dict.off = c.dict.off || [];
    for (const key of Object.keys(c.tokens)) c.tokens[key] = [];
    for (const key of ['id', 'prompt', 'price', 'credit', 'cw1h', 'complete', 'interrupted', 'id_synthetic']) c[key] = [];
    const stamp = r => r.time === 'boundary' ? Date.UTC(2026, 8, 9, 23, 30) : generated - r.days * 86400000;
    rows[6].time = 'boundary'; // 23:30 UTC is the next calendar day in Stockholm (UTC+02).
    const stamps = rows.map(stamp).sort((a, b) => a - b);
    const sortedRows = rows.slice().sort((a, b) => stamp(a) - stamp(b));
    let previous = 0;
    for (let i = 0; i < sortedRows.length; i++) {
      const r = sortedRows[i], ms = stamps[i];
      const names = {harness: r.harness, provider: r.harness === 'claude' ? 'anthropic' : 'openai', model: r.model, effort: r.effort, thread_kind: r.thread_kind, origin: r.origin, session: r.session, parent_session: null, turn_id: r.turn_id || `turn-${r.prompt}`};
      for (const key of dictKeys) c.idx[key].push(indexFor(key, names[key]));
      const off = c.dict.off.indexOf(120);
      if (off < 0) { c.dict.off.push(120); c.idx.off.push(c.dict.off.length - 1); }
      else c.idx.off.push(off);
      c.ts.push(ms - previous); previous = ms;
      c.id.push(i + 1); c.prompt.push(r.prompt); c.price.push(null); c.credit.push(null); c.cw1h.push(null);
      c.complete.push(r.complete); c.interrupted.push(0); c.id_synthetic.push(r.synthetic || 0);
      const values = {fresh_input:r.tokens[0], cache_read:r.tokens[1], cache_write:r.tokens[2], output:r.tokens[3], reasoning: i === 0 ? 999 : 0};
      for (const key of Object.keys(c.tokens)) c.tokens[key].push(Object.hasOwn(values, key) ? values[key] : 0);
    }
    c.n = rows.length;
    const byPrompt = Object.fromEntries(rows.map(r => [r.prompt, r]));
    data.usage = {...data.usage, source_keys: sortedRows.map(r => r.harness === 'claude' ? 'claude_cli' : 'codex'), local_rows: [], plans: [], groups: [{
      project: 'analytics-fixture', repository: null, branch: null,
      rows: sortedRows.map((_, i) => i),
      turns: sortedRows.map((r, i) => ({title:`Fixture turn ${r.prompt}`, prompt:null, rows:[i]})),
    }]};
    data.prompt_context = Object.fromEntries(Object.keys(byPrompt).map(k => [k, {activity:{shell:1, edits:2, web:3}}]));
    for (const context of Object.values(data.prompt_context)) context.activity.subagents = 4;
    data.prompt_texts = {};
    data.prompt_resume = {};
    data.analytics_metadata = {
      request_meta:{dict:{speed:['fast'],service_tier:['priority']},idx:{speed:sortedRows.map(r=>r.prompt===4?0:null),service_tier:sortedRows.map(r=>r.prompt===4?0:null)}},
      observed_turn_start_dates:Object.fromEntries(sortedRows.map(r=>[String(r.prompt),new Date(stamp(r)+120*60000).toISOString().slice(0,10)])),
      activity_coverage:{retained_context_turns:8,turns_with_activity:8,dimensions:['shell','edits','web','subagents'],full_tool_events:false,skills:false},
    };
  });
}

async function ready(page, errors) {
  for (const deadline = Date.now() + 60000; !(await page.evaluate(() => window.reportReady === true));) {
    if (errors.length) throw new Error(errors.join('; '));
    if (Date.now() > deadline) throw new Error('report did not become ready: ' + errors.join('; '));
    await page.waitForTimeout(50);
  }
}

(async () => {
  const browserName = process.env.BROWSER || 'chromium';
  const html = withAnalyticsFixture(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await playwright[browserName].launch({headless:true});
  try {
    for (const locale of ['en-US', 'sv-SE']) {
      const context = await browser.newContext({locale, viewport:{width:1440,height:1000}, offline:true});
      const page = await context.newPage(), errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.setContent(html, {waitUntil:'load'}); await ready(page, errors);
      assert.equal(await page.locator('#analytics').count(), 1, 'analytics section is present');
      assert.equal(await page.locator('#analytics-group option').count(), 6, 'all group dimensions are selectable');
      assert.equal(await page.locator('html').getAttribute('lang'), locale.startsWith('sv') ? 'sv' : 'en');
      const facts = await page.evaluate(() => {
        const rows = UsageReport.all;
        const start = rows.map(r => r.date).sort()[0];
        const end = rows.map(r => r.date).sort().at(-1);
        const selected = UsageReport.analyticsRows(rows, start, end);
        const expected = selected.filter(r => !r.id_synthetic).reduce((n,r) => n + ['fresh_input','cache_read','cache_write','output'].reduce((s,k) => s + (r.tokens[k] ?? 0), 0), 0);
        const series = ['model','thread_kind','origin','harness','effort'].map(d => [d, UsageReport.analyticsSeries(selected,d)]);
        const models = Object.create(null);
        for (const day of UsageReport.analyticsSeries(selected,'model')) for (const group of day.groups) models[group.name] = (models[group.name] || 0) + group.total;
        const efficiency = UsageReport.analyticsEfficiency(selected);
        const weekly = UsageReport.analyticsWeekly(selected);
      const timezone = rows.find(r => new Date(Date.parse(r.ts)).toISOString().slice(0,10) !== r.date);
      return {rows:selected.length, expected, series, models, efficiency, weekly, timezoneDate:timezone?.date, timezoneUtc:new Date(Date.parse(timezone?.ts)).toISOString().slice(0,10)};
      });
      assert.equal(facts.rows, 9, 'fixture rows retain unique observation identity');
      assert.equal(facts.expected, 500, 'known token sum excludes reasoning and synthetic counters while retaining partial known tokens');
      assert.equal(facts.timezoneDate, '2026-09-10', 'analytics date uses Europe/Stockholm at the UTC day boundary');
      assert.equal(facts.timezoneUtc, '2026-09-09', 'boundary fixture crosses the UTC calendar date');
      assert.deepEqual(facts.models, {'gpt-5':270, sonnet:190, 'gpt-4.1':40}, 'model grouping conserves exact non-synthetic token totals');
      for (const [dimension, series] of facts.series) {
        assert.ok(series.length > 0, `${dimension} grouping has observations`);
        assert.equal(series.reduce((n, day) => n + day.total, 0), facts.expected, `${dimension} grouping conserves known token total`);
      }
      assert.deepEqual(facts.efficiency, {tokens:500, requests:8, linkedTurns:7, linkedTokens:500, tokensPerRequest:62.5, tokensPerTurn:500/7, cacheReuse:null, complete:false, ambiguous:1, incomplete:1}, 'efficiency uses exact non-synthetic request and linked-turn denominators; incomplete cache ratio is unknown');
      assert.ok(facts.weekly.length > 0, 'weekly grouping is available');

      await page.locator('#analytics').scrollIntoViewIfNeeded();
      await page.locator('[data-analytics-days="7"]').click();
      assert.ok(await page.locator('#analytics-period').innerText(), '7-day period is labeled');
      const seven = await page.evaluate(() => UsageReport.analyticsRows(UsageReport.all, document.querySelector('#analytics-period').dataset.start, document.querySelector('#analytics-period').dataset.end));
      assert.equal(seven.length, 6, '7-day range includes day 0 through day 6 and excludes older observations');
      const range7 = await page.locator('#analytics-period').evaluate(e => [e.dataset.start,e.dataset.end]);
      assert.equal((Date.parse(range7[1])-Date.parse(range7[0]))/86400000,6,'7-day dates have an inclusive six-day difference');
      const beforePrevious = range7.map(x => Date.parse(x));
      await page.locator('#analytics-prev').click();
      const rangePrevious = await page.locator('#analytics-period').evaluate(e => [e.dataset.start,e.dataset.end]);
      assert.deepEqual(rangePrevious.map(x => Date.parse(x)),beforePrevious.map(x => x-7*86400000),'previous shifts both range boundaries by exactly seven days');
      await page.locator('#analytics-next').click();
      assert.deepEqual(await page.locator('#analytics-period').evaluate(e => [e.dataset.start,e.dataset.end]),range7,'next restores the exact 7-day boundaries');
      await page.locator('[data-analytics-days="30"]').click();
      const thirty = await page.evaluate(() => UsageReport.analyticsRows(UsageReport.all, document.querySelector('#analytics-period').dataset.start, document.querySelector('#analytics-period').dataset.end));
      assert.equal(thirty.length, 8, '30-day range includes day 29 and excludes day 31');
      assert.equal((await page.locator('#analytics-day-list details.analytics-day').count()),30,'daily details retain every calendar day including gaps');
      const gaps = await page.locator('#analytics-chart .analytics-meta').last().innerText();
      assert.equal(gaps, locale.startsWith('sv') ? 'Alla kalenderdagar i det valda fönstret visas. Inga registrerade anrop bevisar inte att aktivitet saknas.' : 'All calendar days in the selected window are shown. No recorded usage does not prove no activity.');
      const range30 = await page.locator('#analytics-period').evaluate(e => [e.dataset.start,e.dataset.end]);
      const beforePrevious30 = range30.map(x => Date.parse(x));
      await page.locator('#analytics-prev').click();
      assert.deepEqual((await page.locator('#analytics-period').evaluate(e => [e.dataset.start,e.dataset.end])).map(x => Date.parse(x)),beforePrevious30.map(x => x-30*86400000),'previous shifts both 30-day boundaries by exactly thirty days');
      await page.locator('#analytics-next').click();
      assert.deepEqual(await page.locator('#analytics-period').evaluate(e => [e.dataset.start,e.dataset.end]),range30,'next restores the exact 30-day boundaries');

      for (const dimension of ['model','thread_kind','origin','harness','effort']) {
        await page.locator('#analytics-group').selectOption(dimension);
        assert.equal(await page.locator('#analytics-chart svg').count(), 1, `${dimension} chart renders`);
        assert.ok((await page.locator('#analytics-legend').innerText()).length, `${dimension} legend renders`);
        if (dimension === 'model') {
          const entries = await page.locator('#analytics-legend > span').allInnerTexts();
          assert.ok(entries.some(x=>x.includes('gpt-5')) && entries.some(x=>x.includes('sonnet')) && entries.some(x=>x.includes('gpt-4.1')), 'model chart shows all three groups');
          const share = name => Number((entries.find(x=>x.includes(name)).match(/([\d,\.]+)\s*%/)||[])[1]?.replace(',','.'));
          assert.equal(share('gpt-5'),54,'gpt-5 legend share equals 270 of 500 tokens');
          assert.equal(share('sonnet'),38,'sonnet legend share equals 190 of 500 tokens');
          assert.equal(share('gpt-4.1'),8,'gpt-4.1 legend share equals 40 of 500 tokens');
        }
      }
      assert.ok(await page.locator('#analytics-sessions details.analytics-row').count() > 0, 'top sessions render drilldown rows');
      const summaries = await page.locator('#analytics-sessions details.analytics-row summary').allInnerTexts();
      assert.equal(summaries.filter(x=>x.includes('session-a')).length,2,'same session string from two harnesses remains two top-session groups');
      await page.locator('#analytics-sessions details.analytics-row').first().locator('summary').click();
      assert.ok((await page.locator('#analytics-sessions details.analytics-row').first().innerText()).includes('fast / priority'), 'session drilldown shows request metadata');
      const week = page.locator('#analytics-weeks details').first();
      await week.locator('summary').click();
      assert.ok(await week.locator(':scope > :not(summary)').count() > 0, 'weekly breakdown expands natively');
      assert.ok((await page.locator('#analytics-activity-coverage').innerText()).includes('8'), 'retained-context coverage reports the exact number of saved and activity-counted turns');
      assert.equal(await page.locator('#analytics-activity svg').count(),1,'retained-context activity is charted');
      const activityLegend = await page.locator('#analytics-activity .analytics-legend').innerText();
      for (const total of ['7','14','21','28']) assert.ok(activityLegend.includes(total), `retained activity count ${total} is represented`);

      await page.locator('#model').selectOption({label:'sonnet'});
      const zeroBaseline = await page.locator('#analytics-kpis .analytics-kpi small').first().innerText();
      assert.equal(zeroBaseline, locale.startsWith('sv') ? 'Nytt från noll' : 'New from zero', 'complete current tokens with a zero-token prior window avoid percentage division');
      await page.locator('#reset').click();
      await page.locator('#from').fill('2026-10-08'); await page.locator('#to').fill('2026-10-10');
      await page.locator('#model').selectOption({label:'gpt-5'});
      assert.equal((await page.locator('#analytics-kpis .analytics-kpi strong').first().innerText()).replace(/[^\d]/g,''),'70','global date and model filters update the analytics total');
      await page.locator('#reset').click();
      const zoomBars = await page.locator('#chart .bar').count();
      assert.ok(zoomBars > 0, 'the report chart has selectable bars');
      await page.locator('#chart .bar').first().click();
      await page.locator('#chart .bar').first().click();
      assert.equal(await page.locator('#zoomout').isVisible(),true,'hour chart zoom becomes active');
      assert.ok(await page.evaluate(()=>UsageReport.getSelected().length) < 9,'zoom selects a strict subset of requests');
      assert.ok((await page.locator('#analytics-comparison-period').innerText()).length > 0);
      await page.locator('#reset').click();
      assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),9,'filter reset restores all requests');

      const before = await page.evaluate(() => ({width:document.documentElement.scrollWidth, viewport:innerWidth}));
      await page.setViewportSize({width:390,height:844});
      const mobile = await page.evaluate(() => ({width:document.documentElement.scrollWidth, viewport:innerWidth, section:document.querySelector('#analytics').scrollWidth, client:document.querySelector('#analytics').clientWidth}));
      assert.ok(mobile.width <= mobile.viewport + 1, 'mobile analytics has no page-level horizontal overflow: '+JSON.stringify(mobile));
      assert.ok(mobile.section <= mobile.client + 1, 'mobile analytics section has no internal horizontal overflow');
      assert.ok(before.width <= before.viewport + 1, 'desktop analytics has no page-level horizontal overflow');
      // A real model named like the remainder label must retain its own colour.
      const remainderName = locale.startsWith('sv') ? 'Övrigt' : 'Other';
      await page.evaluate(name => UsageReport.all.forEach((row, index) => {
        row.model = row.tokens.fresh_input === 100 ? name : 'model-' + index;
      }), remainderName);
      await page.locator('#analytics-group').selectOption('model');
      const swatches = await page.locator('#analytics-legend i').evaluateAll(nodes => nodes.map(node => node.style.background));
      assert.equal(swatches.length, 7, 'six named models and a remainder group');
      assert.notEqual(swatches[0], swatches[6], 'a named model never collides with the remainder colour');
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log(JSON.stringify({pass:true,browser:browserName,fixture:'synthetic-analytics',checks:'7/30 day range, previous/next, grouping conservation, unique rows, top-session drilldown, weekly expand, retained-context activity, English/Swedish, mobile overflow'}));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
