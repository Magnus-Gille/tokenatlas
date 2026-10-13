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
  {harness:'codex',account:'old-limit',plan_type:'legacy',minutes:300,used_percent:12,resets_at:'2026-10-12T15:00:00Z',observed_at:'2026-10-12T09:00:00Z'},
  {harness:'codex',account:'old-limit',plan_type:'legacy',minutes:10080,used_percent:90,resets_at:'2026-10-13T15:00:00Z',observed_at:'2026-10-12T09:00:00Z'},
  {harness:'codex',account:'no-date',plan_type:'undated',minutes:300,used_percent:88,resets_at:'2026-10-12T16:00:00Z',observed_at:null},
  {harness:'codex',account:'bad-date',plan_type:'invalid',minutes:300,used_percent:77,resets_at:'2026-10-12T17:00:00Z',observed_at:'not-a-timestamp'},
  {harness:'codex',account:'tie-a',plan_type:'tie-a',minutes:300,used_percent:66,resets_at:'2026-10-12T18:00:00Z',observed_at:'2026-10-12T11:45:00Z'},
  {harness:'codex',account:'tie-b',plan_type:'tie-b',minutes:300,used_percent:67,resets_at:'2026-10-12T19:00:00Z',observed_at:'2026-10-12T11:45:00Z'},
  {harness:'claude',account:'work',plan_type:'max',minutes:300,used_percent:null,resets_at:null,observed_at:'2026-10-12T11:50:00Z'},
  {harness:'claude',account:'work',plan_type:'max',minutes:10080,used_percent:100,resets_at:'2026-10-12T11:00:00Z',observed_at:'2026-10-12T11:50:00Z'},
  {harness:'claude',account:'personal',plan_type:'pro',minutes:300,used_percent:55,resets_at:'2026-10-12T20:00:00Z',observed_at:'2026-10-12T11:30:00Z'},
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
async function scopeCard(page, match) {
  const index = await page.locator('[data-scope]').evaluateAll((nodes, wanted) => nodes.findIndex(node => {
    let value;
    try { value = JSON.parse(node.dataset.scope || 'null'); } catch (_) { return false; }
    return Array.isArray(value) && value[0] === wanted.harness && value[1] === wanted.account && value[2] === wanted.plan;
  }), match);
  assert.notEqual(index, -1, `scope is present: ${JSON.stringify(match)}`);
  return page.locator('[data-scope]').nth(index);
}
async function scopeKeys(page, visibleOnly=false) {
  return page.locator('[data-scope]').evaluateAll((nodes, visible) => nodes.filter(node => !visible || !node.closest('details:not([open])')).map(node => {
    const value = JSON.parse(node.dataset.scope || 'null');
    return Array.isArray(value) ? value.slice(0, 3) : value;
  }), visibleOnly);
}
async function sortedScopeKeys(page, visibleOnly=false) {
  return (await scopeKeys(page, visibleOnly)).map(value => JSON.stringify(value)).sort();
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
    const context = await browser.newContext({viewport,locale:'sv-SE',timezoneId:'Europe/Stockholm'});
    contexts.push(context);
    // WebKit's simulated offline mode also rejects file: navigation. Block all
    // network URLs while loading the local artifact, then simulate offline use.
    await context.route('**/*', route => route.request().url().startsWith('file:') ? route.continue() : route.abort());
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
    await context.setOffline(true);
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
  const visibleKeys = await scopeKeys(full.page, true);
  assert.deepEqual(visibleKeys.filter(([h]) => h === 'codex').sort(), [
    ['codex', null, 'pro'], ['codex', 'tie-a', 'tie-a'], ['codex', 'tie-b', 'tie-b'],
  ].sort(), 'latest Codex scope and exact timestamp ties stay primary');
  assert.deepEqual(visibleKeys.filter(([h]) => h === 'claude').sort(), [
    ['claude', 'personal', 'pro'], ['claude', 'work', 'max'],
  ].sort(), 'Claude scopes remain primary');
  assert.ok(visibleKeys.findIndex(([h]) => h === 'codex') < visibleKeys.findIndex(([h]) => h === 'claude'), 'latest Codex scopes precede Claude scopes');
  const helperFacts = await full.page.evaluate(inputRows => {
    const scopes = UsageReport.subscriptionScopes(inputRows);
    const found = (harness, account, plan) => scopes.find(scope => scope.harness === harness && scope.account === account && scope.plan_type === plan);
    const primary = UsageReport.partitionSubscriptionScopes(scopes);
    return {
      latest: (() => { const scope = found('codex', null, 'pro'); return {...scope, rowKeys:[...scope.rows.keys()]}; })(),
      primary: primary.primary.map(scope => [scope.harness, scope.account, scope.plan_type]),
      other: primary.other.map(scope => [scope.harness, scope.account, scope.plan_type]),
    };
  }, rows);
  assert.equal(helperFacts.latest.latestObservedAt, '2026-10-12T11:45:00Z', 'scope helper uses the maximum valid observation');
  assert.deepEqual(helperFacts.latest.rowKeys, [300, 10080], 'scope helper preserves both quota durations in its rows Map');
  assert.deepEqual(helperFacts.primary.filter(([h]) => h === 'codex').sort(), [
    ['codex', null, 'pro'], ['codex', 'tie-a', 'tie-a'], ['codex', 'tie-b', 'tie-b'],
  ].sort());
  assert.ok(helperFacts.other.some(([h, account, plan]) => h === 'codex' && account === 'old-limit' && plan === 'legacy'));
  assert.ok(helperFacts.other.some(([h, account, plan]) => h === 'codex' && account === 'bad-date' && plan === 'invalid'), 'invalid timestamps cannot outrank dated helper scopes');

  const unknownLatest = await full.page.evaluate(() => {
    const scopes=UsageReport.subscriptionScopes([
      {harness:'codex',account:null,plan_type:'pro',minutes:300,observed_at:'2026-10-12T11:45:00Z'},
      {harness:'codex',account:null,plan_type:null,minutes:300,observed_at:'2026-10-12T11:59:00Z'},
      {harness:'codex',account:'same-instant',plan_type:'team',minutes:10080,observed_at:'2026-10-12T13:59:00+02:00'},
    ]);
    const parts=UsageReport.partitionSubscriptionScopes(scopes);
    return {primary:parts.primary.map(x=>x.plan_type),other:parts.other.map(x=>x.plan_type)};
  });
  assert.deepEqual(unknownLatest.primary.sort(), [null,'team'].sort(), 'latest selection uses instants and never prefers a known plan over a newer unknown one');
  assert.deepEqual(unknownLatest.other, ['pro']);

  const numericOrder = await full.page.evaluate(() => {
    const rows=[10,2].map(n=>({harness:'codex',account:null,plan_type:'pro',minutes:300,inferred_account:n,observed_at:'2026-10-12T11:45:00Z'}));
    return UsageReport.partitionSubscriptionScopes(UsageReport.subscriptionScopes(rows)).primary.map(x=>x.inferred_account);
  });
  assert.deepEqual(numericOrder,[2,10], 'inferred group identifiers sort numerically at equal timestamps');

  const latest = await scopeCard(full.page, {harness:'codex',account:null,plan:'pro'});
  assert.match(await latest.innerText(), /42/);
  assert.match(await latest.innerText(), /78/);
  assert.match(await latest.innerText(), /16:00 CEST/);
  assert.doesNotMatch(await latest.innerText(), /31/);
  assert.equal(await latest.locator('[data-quota-window="10080"] .quota-values').innerText(), 'Kvar 22 % · Använt 78 %');

  const claudeWork = await scopeCard(full.page, {harness:'claude',account:'work',plan:'max'});
  assert.match(await claudeWork.innerText(), /inväntar ny avläsning/i);
  assert.match(await claudeWork.innerText(), /Återställning ej tillgänglig/i);
  assert.match(await claudeWork.innerText(), /gräns: work/i);
  assert.equal(await claudeWork.locator('[data-quota-window="10080"] .quota-values').innerText(), 'Inväntar ny avläsning');

  const history = full.page.locator('details.quota-history');
  assert.equal(await history.count(), 1, 'other Codex readings use a native disclosure');
  assert.equal(await history.evaluate(node => node.open), false, 'other Codex readings start collapsed');
  const historySummary = await history.locator('summary').innerText();
  assert.match(historySummary, /2/);
  const historyText = await history.textContent();
  assert.match(historyText, /Codex/i);
  assert.match(historyText, /2026|okt|oct/i, 'history exposes dates');
  assert.match(await full.page.locator('.quota-identity-note').innerText(), /samma konto.*inte säkert identifiera konton/i, 'visible note explains account uncertainty');
  assert.equal(await (await scopeCard(full.page, {harness:'codex',account:'old-limit',plan:'legacy'})).isVisible(), false);
  assert.equal(await (await scopeCard(full.page, {harness:'codex',account:'no-date',plan:'undated'})).isVisible(), false);
  assert.equal(await full.page.locator('[data-scope*="bad-date"]').count(), 0, 'malformed timestamp rows remain rejected by quotaRows');

  await history.locator('summary').focus();
  await full.page.keyboard.press('Enter');
  assert.equal(await history.evaluate(node => node.open), true, 'keyboard expands other Codex readings');
  const old = await scopeCard(full.page, {harness:'codex',account:'old-limit',plan:'legacy'});
  assert.equal(await old.isVisible(), true);
  assert.match(await old.innerText(), /12/);
  assert.match(await old.innerText(), /äldre än 30 minuter/i);
  const undated = await scopeCard(full.page, {harness:'codex',account:'no-date',plan:'undated'});
  assert.equal(await undated.locator('[data-observed-at]').count(), 0, 'undated secondary readings have no dated reading indication');
  await history.locator('summary').focus();
  await full.page.keyboard.press('Enter');
  assert.equal(await history.evaluate(node => node.open), false, 'keyboard collapses other Codex readings');

  const reversed = await pageFor(edit(source, rows.slice().reverse()));
  assert.deepEqual(await sortedScopeKeys(reversed.page, true), await sortedScopeKeys(full.page, true), 'input order does not change primary scopes');
  assert.deepEqual(await sortedScopeKeys(reversed.page), await sortedScopeKeys(full.page), 'input order does not change grouped scope set');
  await reversed.context.close();
  if (screenshotDir) {
    await full.page.locator('#subscriptions [data-view="subscriptions"]').click();
    await full.page.screenshot({path:path.join(screenshotDir, 'subscriptions-desktop-sv.png'),fullPage:true});
    await full.page.locator('#subscriptions [data-view="overview"]').click();
  }
  // WebKit's innerText can add a trailing layout newline when siblings hide.
  const before = (await full.page.locator('#subscriptions-body').innerText()).trimEnd();
  await full.page.locator('[data-view="subscriptions"]').first().focus();
  await full.page.keyboard.press('Enter');
  assert.equal(await full.page.locator('body').evaluate(node => node.classList.contains('quota-focus')), true);
  for (const selector of ['.filters', '#analytics', '#sessions', '#coverage'])
    assert.equal(await full.page.locator(selector).isVisible(), false, `${selector} hidden in subscription view`);
  for (const target of ['sessions', 'coverage']) {
    await full.page.locator(`a[href="#${target}"]`).click();
    await full.page.waitForTimeout(50);
    const top = await full.page.locator(`#${target}`).evaluate(node => node.getBoundingClientRect().top);
    assert.ok(top >= 0 && top < 1000, `${target} is reached from focused subscriptions (top=${top})`);
    await full.page.locator('#subscriptions [data-view="subscriptions"]').click();
  }
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
  assert.equal((await full.page.locator('#subscriptions-body').innerText()).trimEnd(), before);
  await full.page.click('[data-lang="en"]');
  assert.match(await full.page.locator('#subscriptions-title').innerText(), /Subscriptions now/);
  assert.match(await history.locator('summary').innerText(), /2/);
  assert.match(await history.textContent(), /Codex/i);
  assert.match(await full.page.locator('.quota-identity-note').innerText(), /same account.*cannot reliably identify accounts/i, 'English visible note explains account uncertainty');
  assert.match(await history.textContent(), /2026|Oct/i, 'English history keeps dates');
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

  const inferredRows = [
    {harness:'codex',account:null,plan_type:'pro',minutes:300,used_percent:20,resets_at:'2026-10-12T13:00:00Z',observed_at:'2026-10-12T11:20:00Z',inferred_account:1},
    {harness:'codex',account:null,plan_type:'pro',minutes:10080,used_percent:40,resets_at:'2026-10-13T13:00:00Z',observed_at:'2026-10-12T11:20:00Z',inferred_account:1},
    {harness:'codex',account:null,plan_type:'pro',minutes:300,used_percent:80,resets_at:'2026-10-12T13:00:00Z',observed_at:'2026-10-12T11:20:00Z',inferred_account:2},
    {harness:'codex',account:null,plan_type:'pro',minutes:10080,used_percent:90,resets_at:'2026-10-13T13:00:00Z',observed_at:'2026-10-12T11:20:00Z',inferred_account:2},
  ];
  const inferred = await pageFor(edit(source, inferredRows));
  assert.equal(await inferred.page.locator('.quota-scope').count(), 4, 'inferred counters and durations remain four distinct scopes');
  assert.equal(await inferred.page.locator('.quota-scope .quota-window').count(), 4, 'inferred scopes keep their duration grouping');
  assert.match(await inferred.page.locator('#subscriptions-body').innerText(), /kvotgrupp 1 \(härledd inom detta tidsfönster\)/);
  assert.match(await inferred.page.locator('#subscriptions-body').innerText(), /kvotgrupp 2 \(härledd inom detta tidsfönster\)/);

  // A report without a dated Codex scope must not invent a latest account.
  const undatedOnly = await pageFor(edit(source, [{...rows[5], observed_at:null}]));
  assert.equal(await undatedOnly.page.locator('.quota-primary-scopes').count(), 0);
  assert.match(await undatedOnly.page.locator('.quota-no-dated').innerText(), /Ingen daterad Codex-avläsning/);
  assert.equal(await undatedOnly.page.locator('.quota-history').evaluate(node=>node.open), false);
  await undatedOnly.page.locator('.quota-history summary').click();
  assert.equal(await undatedOnly.page.locator('.quota-history .quota-scope').isVisible(), true);

  const evolving = await pageFor(edit(source, [{harness:'claude',account:'work',plan_type:'fresh',minutes:300,used_percent:12,resets_at:null,observed_at:'2026-10-12T11:59:00Z'}]));
  assert.doesNotMatch(await evolving.page.locator('[data-scope]').innerText(), /äldre än 30 minuter/i);
  await evolving.page.evaluate(ms => window.__setQuotaNow(ms), fixedNow + 31 * 60 * 1000);
  await evolving.page.waitForTimeout(1200);
  assert.match(await evolving.page.locator('[data-scope]').innerText(), /äldre än 30 minuter/i);

  const overshoot = await pageFor(edit(source, [{...rows[0], used_percent:104}]));
  assert.equal(await overshoot.page.locator('.quota-values').first().innerText(), 'Kvar 0 % · Använt 104 %');

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
