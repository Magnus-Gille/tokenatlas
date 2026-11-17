/* Offline DOM regression with synthetic Python fixture. Requires an existing Playwright
 * installation and browser: PLAYWRIGHT_MODULE=/path/to/playwright node test_view_browser.cjs
 * Optional SCREENSHOT_DIR captures subscription/minimal at desktop and phone widths.
 * Capturing screenshots is not a substitute for human visual review. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const {execFileSync} = require('node:child_process');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
const html = execFileSync(process.env.PYTHON || 'python3', ['-c', 'from test_view import browser_fixture; print(browser_fixture())'], {cwd:__dirname, encoding:'utf8', maxBuffer:8*1024*1024});
const presets = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-c', 'import json; from tokenatlas.view import PRESETS; print(json.dumps(PRESETS))'], {cwd:__dirname, encoding:'utf8'}));
function withView(view, limitHit=null) {
  return html.replace(/(<script id="report-data"[^>]*>)([^<]+)(<\/script>)/, (_, a, b, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b,'base64')));
    data.lang = 'en';
    data.view = {...data.view, ...view};
    if(limitHit)data.limit_hits = [limitHit];
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data))).toString('base64') + c;
  });
}
(async () => {
  const browser = await playwright[process.env.BROWSER_NAME || 'chromium'].launch({headless:true});
  const opened = [];
  const cleanupErrors = [];
  async function pageFor(view, width=1440, limitHit=null) {
    const context = await browser.newContext({offline:true, viewport:{width,height:1000}, locale:'en-US'});
    opened.push(context);
    const page = await context.newPage(), errors=[], requests=[];
    page.on('pageerror', e=>errors.push(e.message));
    page.on('request', r=>{if(/^https?:/.test(r.url()))requests.push(r.url())});
    await page.setContent(withView(view, limitHit), {waitUntil:'load'});
    await page.waitForFunction(()=>window.reportReady===true);
    return {context, page, errors, requests};
  }
  async function closeView(context) {
    const index = opened.indexOf(context);
    if(index >= 0)opened.splice(index,1);
    try {await context.close()} catch (error) {
      const detail = error && error.stack ? error.stack : String(error);
      const message = `context cleanup failed: ${detail}`;
      cleanupErrors.push(message);
      console.error(message);
      process.exitCode = 1;
    }
  }
  const selected = page=>page.evaluate(()=>UsageReport.getSelected().length);
  const ids = page=>page.locator('#view-sections > *').evaluateAll(nodes=>nodes.map(n=>n.id));
  try {
    // An independently selected nested child, non-default order and defaults with
    // a missing project must work at initial render, through translation and reset.
    const {context, page, errors, requests} = await pageFor({sections:['quota-windows','facts','turns','glance'], top:20, money_first:true,
      defaults:{period:'30d',harness:'codex',plan:'team',project_id:'/synthetic/missing'}});
    assert.deepEqual(await ids(page), ['quota-windows','cost-facts','prompts','glance']);
    assert.equal(await page.locator('#limit-hits').count(),0);
    assert.equal(await page.locator('#energy').count(),0);
    assert.equal(await page.locator('#sessions').count(),0);
    assert.equal(await page.locator('#qw-table tr').count(),2);
    assert.equal(await selected(page),0);
    assert.equal(await page.inputValue('#from'),'2026-08-22');
    assert.equal(await page.inputValue('#to'),'2026-09-20');
    assert.equal(await page.inputValue('#harness'),'codex');
    await page.click('[data-lang="sv"]');
    assert.equal(await page.inputValue('#project_id'),'/synthetic/missing');
    assert.equal(await selected(page),0,'translation must not broaden an absent saved filter');
    await page.selectOption('#project_id','');
    assert.equal(await selected(page),15);
    await page.click('[data-lang="en"]');
    assert.equal(await page.inputValue('#project_id'),'');
    assert.equal(await selected(page),15,'cleared filter must stay cleared');
    await page.click('#reset');
    assert.equal(await selected(page),40);
    assert.equal(await page.locator('#top-prompts .prompt-row').count(),20,'saved --top');
    assert.equal(await page.locator('#top-prompts .prompt-row').first().locator('td').first().getAttribute('data-column'),'10','money first retains column role');
    await page.click('[data-lang="sv"]');
    assert.equal(await selected(page),40,'reset filters must not be reapplied by language changes');
    await page.click('#show-everything');
    assert.equal(await page.locator('#limit-hits > #quota-windows').count(),1,'restore original quota nesting');
    assert.equal(await page.locator('#analytics > #token-efficiency').count(),1,'restore original efficiency nesting');
    assert.equal(await page.locator('#glossary').getAttribute('open'),null);
    assert.deepEqual(errors,[]); assert.deepEqual(requests,[]);
    const promptId = await page.evaluate(()=>UsageReport.topPrompts(UsageReport.all,10)[0].id);
    const fallback = 'Top-ranked synthetic turn';
    const limitHit = {
      harness:'codex', at:'2026-09-20T10:00:00+00:00', reached:'five_hour',
      window_minutes:300, resets_at:'2026-09-20T15:00:00+00:00', retries:1,
      prompt:promptId, label:{at:'2026-09-20T10:00:00+00:00', harness:'codex', text:fallback}
    };
    await closeView(context);

    // Empty selection, updates while detached, recovery, then real interactions
    // on controls that previously disappeared. Active styling alone is not enough.
    const hidden = await pageFor({sections:[],defaults:{}});
    const p = hidden.page;
    assert.deepEqual(await ids(p),[]);
    assert.equal(await p.locator('#glance, #cost-facts, #charts, #analytics, #token-efficiency').count(),0);
    assert.ok((await p.locator('#generated').innerText()).length,'global footer is still rendered');
    await p.click('[data-lang="sv"]');
    await p.click('#show-everything');
    const beforeFacts = await p.locator('#ins-period').innerText();
    await p.click('[data-win="all"]');
    assert.equal(await p.locator('[data-win="all"]').getAttribute('aria-pressed'),'true');
    assert.notEqual(await p.locator('#ins-period').innerText(),beforeFacts,'recovered cost window changes results');
    await p.click('[data-gran="hour"]');
    assert.ok(await p.locator('[data-gran="hour"]').evaluate(n=>n.classList.contains('active')));
    assert.match(await p.locator('#chart .bar').first().getAttribute('aria-label'),/10:00/,'recovered chart buckets are hourly');
    await p.click('[data-rank="harness"]');
    assert.equal(await p.locator('#ranking button').count(),2);
    await p.locator('#ranking button').first().click();
    assert.equal(await selected(p),20,'recovered ranking actually selects a harness');
    await p.click('#reset');
    await p.click('[data-analytics-days="7"]');
    assert.equal(await p.locator('[data-analytics-days="7"]').getAttribute('aria-pressed'),'true');
    assert.equal(await p.locator('#analytics-period').getAttribute('data-start'),'2026-09-14');
    assert.equal(await p.locator('#analytics-period').getAttribute('data-end'),'2026-09-20');
    await p.click('[data-lang="en"]');
    assert.equal(await p.locator('[data-win="all"]').innerText(),'All history','recovered controls are translated');
    assert.equal(await selected(p),40);
    assert.deepEqual(hidden.errors,[]); assert.deepEqual(hidden.requests,[]);
    await closeView(hidden.context);

    // A limit hit for a ranked turn must remain readable while the turns
    // section is hidden, then become a working anchor after show-everything.
    const limitsOnly = await pageFor({sections:['limits'],defaults:{}},1440,limitHit);
    try {
      assert.equal(await limitsOnly.page.locator(`#limit-list a[href="#turn-${promptId}"]`).count(),0,
        'initial hidden turns must not receive a broken limit-hit anchor');
      assert.match(await limitsOnly.page.locator('#limit-list').innerText(),new RegExp(fallback),
        'initial hidden turns must retain a readable limit-hit fallback');
      await limitsOnly.page.locator('#harness').dispatchEvent('change');
      assert.equal(await limitsOnly.page.locator(`#limit-list a[href="#turn-${promptId}"]`).count(),0,
        'rerendered hidden turns must not receive a broken limit-hit anchor');
      assert.match(await limitsOnly.page.locator('#limit-list').innerText(),new RegExp(fallback),
        'rerendered hidden turns must retain a readable limit-hit fallback');
      await limitsOnly.page.click('#show-everything');
      const turnAnchor = limitsOnly.page.locator(`#limit-list a[href="#turn-${promptId}"]`);
      assert.equal(await turnAnchor.count(),1,'visible turns should receive a limit-hit anchor');
      assert.equal(await turnAnchor.evaluate(anchor=>{
        const target=document.querySelector(anchor.getAttribute('href'));
        return Boolean(target && target.isConnected);
      }),true,'limit-hit anchor should target a connected turn card');
    } finally {await closeView(limitsOnly.context)}

    // Preset layout and horizontal overflow checks at desktop and phone width.
    const domIds = {'limits':'limit-hits','facts':'cost-facts','turns':'prompts'};
    for(const preset of ['subscription','minimal'])for(const width of [1440,390]){
      const test = await pageFor({sections:presets[preset],defaults:{}},width);
      try {
        assert.deepEqual(await ids(test.page),presets[preset].map(id=>domIds[id]||id));
        assert.equal(await test.page.locator('#energy, #cache-section').count(),0);
        assert.equal(await selected(test.page),40);
        assert.ok(await test.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${preset} overflows at ${width}`);
        if(process.env.SCREENSHOT_DIR){fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});await test.page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,`${preset}-${width}.png`),fullPage:true})}
        assert.deepEqual(test.errors,[]); assert.deepEqual(test.requests,[]);
      } finally {await closeView(test.context)}
    }
    console.log('PASS: saved view initial render, defaults, missing values, clear, translation, nested child, empty selection, recovery interactions, presets at 1440/390');
  } finally {
    for(const context of [...opened])await closeView(context);
    try {await browser.close()} catch (error) {
      const detail = error && error.stack ? error.stack : String(error);
      const message = `browser cleanup failed: ${detail}`;
      cleanupErrors.push(message);
      console.error(message);
      process.exitCode = 1;
    }
  }
})().catch(error=>{console.error(error);process.exitCode=1});
