/* Offline browser smoke for the subscription plan filter and request metadata. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function ready(page, errors) {
  const deadline = Date.now() + 30000;
  while (!(await page.evaluate(() => window.reportReady === true))) {
    if (Date.now() > deadline) throw new Error(`report did not become ready: ${errors.join('; ')}`);
    await page.waitForTimeout(50);
  }
}

function withData(source, mutate) {
  const match = source.match(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+/=]+)(<\/script>)/);
  if (!match) throw new Error('report data payload not found');
  const data = JSON.parse(zlib.gunzipSync(Buffer.from(match[2], 'base64')).toString('utf8'));
  mutate(data);
  const encoded = zlib.gzipSync(Buffer.from(JSON.stringify(data))).toString('base64');
  return source.replace(match[0], match[1] + encoded + match[3]);
}

async function main() {
  if (!process.argv[2]) throw new Error('usage: node test_plans_browser.cjs REPORT.html');
  const source = fs.readFileSync(process.argv[2], 'utf8');
  const planHitScope = {project_id:'\x01p001', model:'gpt-5.5', effort:'high', provider:'openai', session:'codex:pro', agent:'private-agent', plan:'business'};
  const filteredSource = withData(source, data => {
    data.limit_hits = [
      {at:'2026-10-10T12:00:00+00:00', local_date:'2026-10-10', harness:'codex', window_minutes:300, reached:'five_hour', prompt:null, scope:planHitScope},
      {at:'2026-10-10T12:01:00+00:00', local_date:'2026-10-10', harness:'codex', window_minutes:10080, reached:'weekly', prompt:1, scope:planHitScope},
    ];
  });
  const browser = await playwright[process.env.BROWSER || 'chromium'].launch({headless: true});
  const screenshots = process.env.SCREENSHOT_DIR;
  if (screenshots) fs.mkdirSync(screenshots, {recursive: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, locale: 'en-US'});
  await context.route('**/*', route => route.request().url().startsWith('file:') ? route.continue() : route.abort());
  const page = await context.newPage();
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (/^https?:/.test(request.url())) requests.push(request.url()); });
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tokenatlas-plans-'));
  const file = path.join(dir, 'report.html'); fs.writeFileSync(file, filteredSource);
  await page.goto(`file://${file}`);
  await ready(page, errors);
  await context.setOffline(true);
  assert.deepEqual(errors, []);
  assert.deepEqual(requests, []);
  await page.locator('details.advanced summary').click();
  assert.deepEqual(await page.locator('#plan option').evaluateAll(options => options.map(option => option.textContent)), ['All', 'Business', 'Pro', 'Unknown']);
  assert.equal(await page.evaluate(() => UsageReport.getSelected().length), 5);
  const allCost = await page.evaluate(() => UsageReport.aggregate(UsageReport.getSelected()).known_tokens);
  assert.equal(allCost, 16500);
  assert.ok(await page.evaluate(() => UsageReport.getSelected().reduce((sum, row) => sum + (row.cost || 0), 0) > 0));
  await page.selectOption('#plan', 'pro');
  assert.equal(await page.evaluate(() => UsageReport.getSelected().length), 2);
  assert.match(await page.locator('#selection').innerText(), /2 of 5/);
  assert.equal(await page.evaluate(() => UsageReport.aggregate(UsageReport.getSelected()).known_tokens), 7700);
  assert.equal(await page.evaluate(() => Number(UsageReport.getSelected().reduce((sum, row) => sum + (row.cost || 0), 0).toFixed(3))), 0.056);
  assert.equal(await page.evaluate(() => UsageReport.glanceFacts(UsageReport.getSelected()).hits.n), 1, 'plan filter excludes out-of-plan scoped hits but keeps selected-prompt hits');
  assert.equal(await page.evaluate(() => UsageReport.efficiencyBundle.filters.plan), true, 'efficiency export records an active plan filter');
  await page.selectOption('#plan', 'business');
  await page.locator('.session summary').first().click();
  await page.locator('.turn summary').first().click();
  const planCell = page.locator('td[title="business"]').first();
  assert.match(await planCell.innerText(), /Business/);
  assert.equal(await planCell.getAttribute('title'), 'business');
  assert.equal(await page.locator('#plan-facts .plan-facts-table tr').count(), 2);
  if (screenshots) {
    await page.screenshot({path: path.join(screenshots, 'plans-desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({path: path.join(screenshots, 'plans-mobile.png'), fullPage: false});
  }
  await page.setViewportSize({width: 390, height: 844});
  await page.selectOption('#plan', 'unknown');
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'unpriced plan facts overflow on mobile');
  await page.selectOption('#plan', 'business');
  assert.ok(await page.locator('#plan-facts').isVisible());
  await page.click('[data-lang="sv"]');
  assert.equal((await page.locator('label:has(#plan) span').textContent()).toLocaleLowerCase('sv'), 'abonnemang');
  assert.equal(await page.evaluate(() => UsageReport.getSelected().length), 2);
  assert.equal(await page.locator('#plan-facts .plan-note').innerText(), 'Turer som byter abonnemang kan räknas i flera rader.');
  const common={harness:'codex',account:null,plan_type:'pro',observed_at:new Date().toISOString(),resets_at:new Date(Date.now()+3600000).toISOString()};
  const counters=[{...common,minutes:300,inferred_account:1,used_percent:20},{...common,minutes:300,inferred_account:2,used_percent:80},{...common,minutes:10080,inferred_account:1,used_percent:40}];
  const accountHtml=filteredSource.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+/=]+)(<\/script>)/,(_,open,encoded,close)=>{
    const data=JSON.parse(zlib.gunzipSync(Buffer.from(encoded,'base64')).toString('utf8'));data.quota_status=counters;
    return open+zlib.gzipSync(Buffer.from(JSON.stringify(data))).toString('base64')+close;
  });
  const accountFile=path.join(dir,'accounts.html');fs.writeFileSync(accountFile,accountHtml);
  const accountContext=await browser.newContext({viewport:{width:390,height:844}});
  await accountContext.route('**/*',route=>route.request().url().startsWith('file:')?route.continue():route.abort());
  const accountPage=await accountContext.newPage();await accountPage.goto(`file://${accountFile}`);await ready(accountPage,[]);await accountContext.setOffline(true);await accountPage.click('[data-lang="sv"]');
  assert.equal(await accountPage.locator('.quota-scope').count(),3,'counter numbers must not pair five-hour and weekly accounts');
  assert.equal(await accountPage.locator('.quota-scope .quota-window').count(),3);
  assert.match(await accountPage.locator('#subscriptions-body').innerText(),/kvotgrupp 2 \(härledd inom detta tidsfönster\)/);
  if(screenshots)await accountPage.locator('#subscriptions').screenshot({path:path.join(screenshots,'plans-inferred-mobile-sv.png')});
  await accountContext.close();
  if (screenshots) {
    await page.locator('.filters').screenshot({path:path.join(screenshots, 'plans-filter-sv.png')});
    await page.locator('#plan-facts').screenshot({path:path.join(screenshots, 'plans-facts-sv.png')});
    await page.locator('#sessions').screenshot({path:path.join(screenshots, 'plans-requests-sv.png')});
  }
  await context.close();
  await browser.close();
}

main().catch(error => { console.error(error.stack || error); process.exit(1); });
