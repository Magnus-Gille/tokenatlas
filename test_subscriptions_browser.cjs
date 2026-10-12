/* Focused browser regression for the saved subscription panel.
 * Usage: PLAYWRIGHT_MODULE=/path/to/playwright node test_subscriptions_browser.cjs report.html
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const rows = [
  {harness:'codex',account:null,plan_type:'pro',minutes:300,used_percent:31,resets_at:'2026-10-12T13:00:00Z',observed_at:'2026-10-12T10:00:00Z'},
  {harness:'codex',account:null,plan_type:'pro',minutes:300,used_percent:42,resets_at:'2026-10-12T14:00:00Z',observed_at:'2026-10-12T11:45:00Z'},
  {harness:'codex',account:null,plan_type:'pro',minutes:10080,used_percent:78,resets_at:'2026-10-13T12:00:00Z',observed_at:'2026-10-12T10:00:00Z'},
  {harness:'claude',account:'work',plan_type:'max',minutes:300,used_percent:null,resets_at:null,observed_at:'2026-10-12T11:50:00Z'},
  {harness:'claude',account:'work',plan_type:'max',minutes:10080,used_percent:100,resets_at:'2026-10-12T11:00:00Z',observed_at:'2026-10-12T11:50:00Z'},
  {harness:'claude',account:'work',plan_type:'legacy',minutes:300,used_percent:12,resets_at:'2026-10-12T15:00:00Z',observed_at:'2026-10-12T10:00:00Z'},
];
function edit(html, value) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+/=]+)(<\/script>)/, (_, open, b64, close) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    data.quota_status = value;
    return open + zlib.gzipSync(Buffer.from(JSON.stringify(data))).toString('base64') + close;
  });
}
async function ready(page, errors=[]) {
  const deadline = Date.now() + 30000;
  for (;;) {
    if (await page.evaluate(() => window.reportReady === true)) return;
    if (Date.now() > deadline) throw new Error(`report did not become ready: ${errors.join('; ')}`);
    await page.waitForTimeout(50);
  }
}
async function run() {
  if (!process.argv[2]) throw new Error('usage: node test_subscriptions_browser.cjs REPORT.html');
  const source = fs.readFileSync(process.argv[2], 'utf8');
  let browser;
  const contexts = [], tempDirs = [];
  const fixedNow = Date.parse('2026-10-12T12:00:00Z');
  const init = () => ({now}) => {
    const RealDate = Date;
    let currentNow = now;
    class FixedDate extends RealDate { constructor(...args) { super(...(args.length ? args : [currentNow])); } static now() { return currentNow; } }
    window.Date = FixedDate;
    window.__setQuotaNow = value => { currentNow = value; };
  };
  async function pageFor(payload, viewport={width:1440,height:1000}) {
    const context = await browser.newContext({viewport,locale:'sv-SE',timezoneId:'Europe/Stockholm',offline:true});
    contexts.push(context);
    await context.addInitScript(init(), {now: fixedNow});
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tokenatlas-subscriptions-')); tempDirs.push(dir);
    const file = path.join(dir, 'report.html');
    fs.writeFileSync(file, payload);
    const page = await context.newPage();
    const errors = [], requests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (/^https?:/.test(request.url())) requests.push(request.url()); });
    await page.goto(`file://${file}`);
    await ready(page, errors);
    return {context,page,errors,requests,file};
  }
  try {
  browser = await playwright[process.env.BROWSER || 'chromium'].launch({headless:true});
  const screenshotDir = process.env.SCREENSHOT_DIR;
  if (screenshotDir) fs.mkdirSync(screenshotDir, {recursive:true});
  const full = await pageFor(edit(source, rows));
  assert.deepEqual(full.errors, []);
  assert.deepEqual(full.requests, []);
  assert.equal(await full.page.locator('#subscriptions').isVisible(), true);
  assert.equal(await full.page.locator('[data-scope]').count(), 3);
  assert.match(await full.page.locator('[data-scope]').first().innerText(), /42/);
  assert.match(await full.page.locator('[data-scope]').first().innerText(), /16:00 CEST/);
  assert.doesNotMatch(await full.page.locator('[data-scope]').first().innerText(), /31/);
  assert.match(await full.page.locator('[data-scope]').nth(1).innerText(), /inväntar ny avläsning/i);
  assert.match(await full.page.locator('[data-scope]').nth(1).innerText(), /Återställning ej tillgänglig/i);
  assert.match(await full.page.locator('[data-scope]').nth(1).innerText(), /gräns: work/i);
  assert.match(await full.page.locator('[data-scope]').nth(2).innerText(), /äldre än 30 minuter/i);
  assert.equal(await full.page.locator('[data-scope]').nth(1).locator('[data-quota-window="10080"] .quota-values').innerText(), 'Inväntar ny avläsning');
  if (screenshotDir) {
    await full.page.locator('#subscriptions [data-view="subscriptions"]').click();
    await full.page.screenshot({path:path.join(screenshotDir, 'subscriptions-desktop-sv.png'),fullPage:true});
    await full.page.locator('#subscriptions [data-view="overview"]').click();
  }
  const before = await full.page.locator('#subscriptions-body').innerText();
  await full.page.locator('[data-view="subscriptions"]').first().focus();
  await full.page.keyboard.press('Enter');
  assert.equal(await full.page.locator('body').evaluate(node => node.classList.contains('quota-focus')), true);
  for (const selector of ['.filters', '#analytics', '#sessions', '#coverage'])
    assert.equal(await full.page.locator(selector).isVisible(), false, `${selector} hidden in subscription view`);
  await full.page.locator('#subscriptions [data-view="overview"]').focus();
  await full.page.keyboard.press('Enter');
  assert.equal(await full.page.locator('body').evaluate(node => node.classList.contains('quota-focus')), false);
  for (const selector of ['.filters', '#analytics', '#sessions', '#coverage'])
    assert.equal(await full.page.locator(selector).isVisible(), true, `${selector} restored in overview`);
  await full.page.locator('a[href="#sessions"]').click();
  assert.match(full.page.url(), /#sessions$/);
  await full.page.click('[data-view="overview"]');
  assert.equal(await full.page.locator('body').evaluate(node => node.classList.contains('quota-focus')), false);
  await full.page.locator('details.advanced > summary').click();
  await full.page.fill('#search', 'something-that-matches-nothing');
  await full.page.click('[data-view="subscriptions"]');
  assert.equal(await full.page.locator('#subscriptions-body').innerText(), before);
  await full.page.click('[data-lang="en"]');
  assert.match(await full.page.locator('#subscriptions-title').innerText(), /Subscriptions now/);
  assert.equal(await full.page.locator('body').evaluate(node => node.classList.contains('quota-focus')), true);
  if (screenshotDir) await full.page.screenshot({path:path.join(screenshotDir, 'subscriptions-desktop-en.png'),fullPage:true});

  const deep = await pageFor(edit(source, rows));
  await deep.page.goto(`file://${deep.file}#subscriptions`);
  await ready(deep.page);
  assert.equal(await deep.page.locator('body').evaluate(node => node.classList.contains('quota-focus')), true);
  assert.equal(await deep.page.locator('[data-lang="en"]').isVisible(), true);
  await deep.context.close();

  const mobile = await pageFor(edit(source, rows), {width:390,height:900});
  await mobile.page.locator('#subscriptions [data-view="subscriptions"]').click();
  assert.ok(await mobile.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'populated mobile panel overflows');
  if (screenshotDir) await mobile.page.screenshot({path:path.join(screenshotDir, 'subscriptions-mobile-390.png'),fullPage:true});

  const evolving = await pageFor(edit(source, [{harness:'claude',account:'work',plan_type:'fresh',minutes:300,used_percent:12,resets_at:null,observed_at:'2026-10-12T11:59:00Z'}]));
  assert.doesNotMatch(await evolving.page.locator('[data-scope]').innerText(), /äldre än 30 minuter/i);
  await evolving.page.evaluate(ms => window.__setQuotaNow(ms), fixedNow + 31 * 60 * 1000);
  await evolving.page.waitForTimeout(1200);
  assert.match(await evolving.page.locator('[data-scope]').innerText(), /äldre än 30 minuter/i);

  const expiring = await pageFor(edit(source, [{harness:'codex',account:null,plan_type:'soon',minutes:300,used_percent:42,resets_at:'2026-10-12T12:00:02Z',observed_at:'2026-10-12T11:59:00Z'}]));
  assert.match(await expiring.page.locator('.quota-values').first().innerText(), /Kvar 58 %/);
  await expiring.page.evaluate(ms => window.__setQuotaNow(ms), fixedNow + 3 * 1000);
  await expiring.page.waitForTimeout(1200);
  assert.equal(await expiring.page.locator('.quota-values').first().innerText(), 'Inväntar ny avläsning');

  const empty = await pageFor(edit(source, []), {width:390,height:900});
  assert.match(await empty.page.locator('#subscriptions-body').innerText(), /Inga abonnemangsavläsningar/);
  assert.ok(await empty.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile panel overflows');
  assert.deepEqual(empty.errors, []);
  } finally {
    for (const context of contexts) await context.close().catch(() => {});
    for (const dir of tempDirs) fs.rmSync(dir, {recursive:true,force:true});
    if (browser) await browser.close().catch(() => {});
  }
  console.log(JSON.stringify({pass:true,checks:'latest rows, scopes, unavailable, stale, expiry, no reset, language, focus hash, filters, empty mobile, offline'}));
}
run().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
