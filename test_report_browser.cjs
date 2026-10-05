/* Optional browser regression: PLAYWRIGHT_MODULE=/path/to/@playwright/test node test_report_browser.cjs report.html */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
const zlib = require('node:zlib');
const L = {
  sv: {lang:'sv', locale:'sv-SE', h1:'Tokenanvändning', prompts:'Dyraste turerna', inputs:'Inmatningar', labels:['Titel','Första inmatningen','Plats','Slutrapport','Aktivitet','PR:er','Commits'], activity:'1 842 shell · 1 redigering · 21 webb', total:'Totalt', totalRe:/^Totalt( \(minst\))?$/, atLeast:/ \(minst\)$/,
    note:/^[\d\s\u00a0\u202f]+ anrop · [\d\s\u00a0\u202f]+ (?:sessioner|session)$/, cache:/^(—|\d+,\d % av all input)$/, selection:/ anrop · /, reasoning:/^varav reasoning |^inkl\. reasoning$/,
    unknown:'Okänt', short:['43,8 mdr','136,5 milj.',(12345).toLocaleString('sv-SE')], money:['$9,00','≥$3,00','$0,60','n/a'], credit:'≈ 15,0 krediter', creditFact:'≈ 15,0 krediter', toggleLabel:'Språk'},
  en: {lang:'en', locale:'en-US', h1:'Token usage', prompts:'Costliest turns', inputs:'Inputs', labels:['Title','Initiating input','Place','Final message','Activity','PRs','Commits'], activity:'1,842 shell · 1 edit · 21 web', total:'Total', totalRe:/^Total( \(at least\))?$/, atLeast:/ \(at least\)$/,
    note:/^[\d,]+ requests? · [\d,]+ sessions?$/, cache:/^(—|\d+\.\d% of all input)$/, selection:/ requests · /, reasoning:/^of which reasoning |^incl\. reasoning$/,
    unknown:'Unknown', short:['43.8B','136.5M','12.3K'], money:['$9.00','≥$3.00','$0.60','n/a'], credit:'≈ 15.0 credits', creditFact:'≈ 15.0 credits', toggleLabel:'Language'},
};
// Cost facts card (fixed fixture clock 2026-09-20): window texts, provenance badges and the figures of both windows.
const INS = {
  sv: {title:'Kostnadsfakta', w:['Senaste 30 dagarna','Hela historiken'], prov:['Beräknad','Uppmätt','Uppskattning'], total:'$12,60', totalAll:'≥$21,72', spd:'Hastighet inte loggad, prissatt som standard (anrop: 2).', tier:'Servicenivå inte loggad, prissatt som standard (anrop: 1).', table:'ur den valda pristabellen (hämtad ', lower:'1 av anropen bakom det här faktat har ofullständiga tokenräknare', left:'Utelämnade: 1 anrop med osäker identitet',cplt:'Anrop med ofullständiga tokenräknare som utelämnas ur det här faktat: 1.', unpriced:'2 av 5 (40,0 %)', how:'Så räknas det', assume:'Antaganden', note:'följer inte filtren'},
  en: {title:'Cost facts', w:['Last 30 days','All history'], prov:['Computed','Measured','Estimate'], total:'$12.60', totalAll:'≥$21.72', spd:'Speed not recorded, priced as standard (requests: 2).', tier:'Service tier not recorded, priced as standard (requests: 1).', table:'from the selected price table (retrieved on ', lower:'1 of the requests behind this fact have incomplete token counters', left:'Left out: 1 requests with an uncertain identity',cplt:'Requests with incomplete token counters left out of this fact: 1.', unpriced:'2 of 5 (40.0%)', how:'How it is computed', assume:'Assumptions', note:'do not follow the filters'},
};
const norm = x => x.replace(/[\s\u00a0\u202f]+/g, ' ');
const facts = page => page.evaluate(() => [...document.querySelectorAll('#ins-body .ins-fact')].map(a => ({id:a.dataset.fact, prov:a.querySelector('.ins-prov').textContent, rows:[...a.querySelectorAll('.ins-row')].map(r => [...r.children].map(c => c.textContent)),
  how:a.querySelector('.ins-how').textContent, assumptions:[...a.querySelectorAll('li')].map(l => l.textContent), markup:a.querySelectorAll('b,i,u,img,script').length})));
// Re-encode a report with an explicit payload language (the page only reads it after decoding).
function withLang(html, lang) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    data.lang = lang;
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
// Re-encode a report with one limit hit (a card turn, a turn without a card, lower bound), for the Limit hits section and the card badge.
function withQuota(html) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    data.quota_shares = {0: {harness: 'codex', minutes: 10080, label: 'observed', percent: 3, shared_with: 0}};
    data.quota_windows = [{harness: 'codex', account: null, minutes: 10080, resets_at: '2026-09-08T00:00:00+00:00', start: '2026-09-01T00:00:00+00:00', peak_percent: 70, peak_at: '2026-09-07T12:41:00+00:00', hit: false, snapshots: 5, cost: 1.5, unpriced_requests: 0}];
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
// Re-encode a report with a weekly Codex quota share on every turn (the costliest card gets one whichever it is).
function withTopShare(html, label = 'observed', percent = 26, harness = 'codex') {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    data.quota_shares = Object.fromEntries([0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map(i => [i, {harness, minutes: 10080, label, percent, shared_with: 0}]));
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
// A weekly Codex share that is a range (#131): lower-upper, with a point when the range is narrow.
function withTopRange(html, share) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    data.quota_shares = Object.fromEntries([0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map(i => [i, {harness: 'codex', minutes: 10080, percent: null, shared_with: 4, ...share}]));
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
// Re-encode a report's payload with an arbitrary edit (columns are index-aligned arrays).
function edited(html, fn) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    fn(data);
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
const stockholm = iso => new Intl.DateTimeFormat('en-CA', {timeZone: 'Europe/Stockholm', year: 'numeric', month: '2-digit', day: '2-digit'}).format(Date.parse(iso));
function withLimitHit(html) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    const label = {at: '2026-09-03T10:15:00+00:00', harness: 'claude', text: null};
    data.limit_hits = [{harness: 'claude', at: '2026-09-03T10:15:00+00:00', local_date: '2026-09-03', reached: 'five_hour', window_minutes: 300, resets_at: '2026-09-03T14:00:00+00:00', retries: 2, prompt: 0, label,
      window: {start: '2026-09-03T09:00:00+00:00', end: '2026-09-03T10:15:00+00:00', requests: 3, unpriced_requests: 0, cost: 12.5, lower_bound: true,
               top: [{prompt: 0, label, requests: 2, cost: 9, share: 0.72}, {prompt: null, label, requests: 1, cost: 3.5, share: 0.28}]}}];
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
// Re-encode a report with `demo: true` (what scripts/demo.py does to the demo report).
function withDemo(html) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    data.demo = true;
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
async function ready(page, errors, what = 'report') {
  // The page forbids eval (CSP), which waitForFunction's polling needs in WebKit; poll via evaluate instead.
  for (const deadline = Date.now() + 60000; !(await page.evaluate(() => window.reportReady === true));) {
    if (Date.now() > deadline) throw new Error(what + ' did not become ready: ' + errors.join('; '));
    await page.waitForTimeout(50);
  }
}
(async()=>{
  const browserName=process.env.BROWSER || 'chromium';
  const screenshotDir=process.env.SCREENSHOT_DIR || os.tmpdir();
  const smoke=fs.readFileSync(process.argv[2],'utf8'),fixture=process.argv[3]?fs.readFileSync(process.argv[3],'utf8'):null,sharedFixture=process.argv[4]?fs.readFileSync(process.argv[4],'utf8'):null;if(process.env.REQUIRE_FIXTURES==='1'&&!(fixture&&sharedFixture))throw new Error('REQUIRE_FIXTURES: the prompts and shared fixtures must both be given');
  const browser=await playwright[browserName].launch({headless:true});
  const summary={};
  const newPage=async(opts,html,init)=>{
    const context=await browser.newContext({viewport:{width:1440,height:1080},offline:true,acceptDownloads:true,...opts});
    if(init)await context.addInitScript(init);
    const page=await context.newPage(),errors=[],requests=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url())});
    await page.setContent(html,{waitUntil:'load'});await ready(page,errors);
    return {context,page,errors,requests};
  };
  // The full report regression, once per language (the browser locale picks it: lang "auto").
  async function suite(T) {
    const {context,page,errors,requests}=await newPage({locale:T.locale},smoke);
    assert.equal(await page.evaluate(()=>document.documentElement.lang),T.lang);
    const check=await page.evaluate(()=>{
      const d={records:window.UsageReport.all};
      const total=d.records.filter(r=>!r.id_synthetic).reduce((n,r)=>n+['fresh_input','cache_read','cache_write','output'].reduce((s,k)=>s+(r.tokens[k]??0),0),0);
      const a=window.UsageReport.aggregate(d.records);
       const sample=(dimension,name,tokens,extra={})=>({session:'fixed-session',harness:'fixed-harness',model:'fixed-model',project_id:'fixed-project',id_synthetic:false,tokens,[dimension]:name,...extra});
       const comparisons={};
       for(const dimension of ['session','harness','model','project_id']){
         const rows=[
           sample(dimension,dimension+'-high',{fresh_input:40,cache_read:50,cache_write:10,output:1}),
           sample(dimension,dimension+'-high',{fresh_input:40,cache_read:50,cache_write:10,output:1}),
           sample(dimension,dimension+'-low',{fresh_input:70,cache_read:20,cache_write:10,output:1}),
           sample(dimension,dimension+'-missing',{fresh_input:10,cache_read:null,cache_write:0,output:1}),
           sample(dimension,dimension+'-synthetic',{fresh_input:10,cache_read:90,cache_write:0,output:1},{id_synthetic:true}),
           sample(dimension,null,{fresh_input:10,cache_read:90,cache_write:0,output:1}),
         ];
         comparisons[dimension]=window.UsageReport.cacheComparison(rows,dimension);
       }
      return {expected:total,actual:a.known_tokens,records:d.records.length,buckets:window.UsageReport.groupBuckets(d.records,'hour').reduce((n,b)=>n+b.known_tokens,0),csv:window.UsageReport.csvCell('=1+1'),comparisons};
    });
    assert.equal(check.actual,check.expected);assert.equal(check.buckets,check.expected);assert.ok(check.csv.startsWith('"\''));
     for(const [dimension,result] of Object.entries(check.comparisons)){
       assert.equal(result.total_groups,5);assert.equal(result.compared_groups,2);assert.equal(result.unknown_groups,3);assert.equal(result.compared_input_tokens,300);
       assert.deepEqual(result.best,{name:dimension+'-high',cache_ratio:.5,observations:2,input_tokens:200});
       assert.deepEqual(result.worst,{name:dimension+'-low',cache_ratio:.2,observations:1,input_tokens:100});
     }
    assert.equal(await page.locator('#cache-comparisons [data-cache-dimension]').count(),4);
    assert.equal(await page.locator('h1').innerText(),T.h1);
    assert.equal(await page.locator('#prompts h2').innerText(),T.prompts);
    {
      const I=INS[T.lang];assert.equal(await page.locator('#cost-facts h2').innerText(),I.title);
      assert.ok(norm(await page.locator('#cost-facts > .panel-top p').first().innerText()).includes(I.note));
      assert.deepEqual(await page.locator('#cost-facts [data-win]').allInnerTexts(),I.w);
      const own=await facts(page);  // the smoke data may have no listed prices: facts are then omitted by design; the fixture below asserts them exactly
      for(const f of own){assert.ok(I.prov.includes(f.prov),'provenance badge '+f.prov);assert.equal(f.markup,0);assert.ok(f.how.startsWith(I.how+': '));assert.ok(f.assumptions.length>=1)}
      const cs=own.find(f=>f.id==='context_size'),ms=own.find(f=>f.id==='model_share');if(cs)assert.equal(cs.prov,I.prov[1]);if(ms)assert.equal(ms.prov,I.prov[0]);
      // the card is computed server-side: the filters above do not touch it
      const before=await page.locator('#ins-body').innerText();await page.selectOption('#harness',{index:1});assert.equal(await page.locator('#ins-body').innerText(),before);await page.selectOption('#harness',{index:0});
      assert.equal(await page.locator('#cost-facts [data-win="30d"]').getAttribute('aria-pressed'),'true');
    }
    assert.equal(await page.evaluate(()=>document.title.startsWith('TokenAtlas')),true);
     await page.screenshot({path:path.join(screenshotDir,'energy-report-desktop-'+T.lang+'.png'),fullPage:true});
     const harnessOptions=await page.locator('#harness option').count(),harness=await page.locator('#harness option').nth(1).getAttribute('value'),cacheBefore=await page.locator('#cache-comparisons').innerText();
     if(harness){await page.selectOption('#harness',harness);assert.equal(await page.evaluate(()=>new Set(UsageReport.getSelected().map(r=>r.harness)).size),1);if(harnessOptions>2)assert.notEqual(await page.locator('#cache-comparisons').innerText(),cacheBefore)}
    await page.locator('.advanced summary').click();await page.fill('#search','this-match-does-not-exist-19042026');
    assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),0);
    await page.locator('#chart-empty').isVisible().then(v=>assert.ok(v));
     await page.screenshot({path:path.join(screenshotDir,'energy-report-empty-'+T.lang+'.png'),fullPage:true});
    await page.click('#reset');
    for(const gran of ['hour','minute','day']){await page.click('[data-gran="'+gran+'"]');assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),check.records)}
    if(check.records){await page.locator('#chart .bar').first().click();assert.ok(await page.evaluate(()=>UsageReport.getSelected().length)>0);await page.click('#reset');await page.locator('.session summary').first().click();await page.locator('.turn summary').first().click();await page.locator('.turn table').first().waitFor();assert.ok(await page.locator('.turn table').count()>0)}
    const download=page.waitForEvent('download');await page.click('#export-json');const saved=await download;const file=await saved.path();const exportData=JSON.parse(fs.readFileSync(file,'utf8'));assert.equal(exportData.totals.known_tokens,check.expected);
    await page.setViewportSize({width:390,height:844});
     await page.screenshot({path:path.join(screenshotDir,'energy-report-mobile-'+T.lang+'.png'),fullPage:false});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile overflow');
    const texts=await page.evaluate(()=>['total-label','total','total-note','cache','cache-note','output','reasoning','selection','quality'].map(id=>[id,document.getElementById(id).textContent]));
    const Tx=Object.fromEntries(texts);assert.match(Tx['total-label'],T.totalRe);assert.match(Tx['total-note'],T.note);assert.match(Tx['cache-note'],T.cache);assert.match(Tx.output,/./);assert.match(Tx.selection,T.selection);assert.ok(T.reasoning.test(Tx.reasoning),Tx.reasoning);
    const cards=await page.evaluate(()=>[...document.querySelectorAll('.kpis .kpi')].map(k=>({label:k.querySelector('.label').textContent,title:k.querySelector('.value').title,text:k.querySelector('.value').textContent})));
    assert.deepEqual(cards.map(c=>c.label.replace(T.atLeast,'')),[T.total,'Input','Cache write','Cache read','Output']);
    const exact=t=>{const m=/^([\d\s,.  ]+) tokens/.exec(t);assert.ok(m,'title '+t);return Number(m[1].replace(/\D/g,''))};
    const parts=cards.slice(1).map(c=>c.text===T.unknown?0:exact(c.title));
    assert.equal(parts.reduce((x,y)=>x+y,0),exact(cards[0].title),'four categories sum to total');
    assert.equal(exact(cards[0].title),await page.evaluate(()=>UsageReport.aggregate(UsageReport.getSelected()).known_tokens));
    assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('.legend > span')].map(s=>s.textContent)),['Input','Cache write','Cache read','Output']);
    assert.equal(await page.evaluate(()=>[UsageReport.short(43812345678),UsageReport.short(136500000),UsageReport.short(12345)].join('|')),T.short.join('|'));
    // The toggle marks the active language; the export buttons and filter labels are localized too.
    assert.equal(await page.locator('.langtoggle').getAttribute('aria-label'),T.toggleLabel);
    assert.equal(await page.locator('[data-lang="'+T.lang+'"]').getAttribute('aria-pressed'),'true');
    // "Dyraste prompterna" / "Costliest prompts": the card renders the top prompts of the current selection (at most 10), previews only in private fixtures.
    const promptRows=await page.evaluate(()=>({card:!!document.getElementById('top-prompts'),rows:document.querySelectorAll('#top-prompts tr.prompt-row').length,expected:Math.min(10,UsageReport.topPrompts(UsageReport.getSelected()).length)}));
    assert.ok(promptRows.card);assert.equal(promptRows.rows,promptRows.expected);
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
    summary[T.lang]={records:check.records,known_tokens:check.actual,network_requests:requests.length,console_errors:errors.length};
    await context.close();
    if(fixture){
      const {context:c2,page:p2,errors:errors2}=await newPage({locale:T.locale},fixture);
      const card=await p2.evaluate(()=>({rows:[...document.querySelectorAll('#top-prompts tr.prompt-row')].map(r=>[...r.children].map(c=>c.firstChild.textContent)),credits:[...document.querySelectorAll('#top-prompts tr.prompt-row')].map(r=>{const x=r.children[9].querySelector('.cr');return x&&x.textContent}),texts:[...document.querySelectorAll('#top-prompts tr.prompt-text')].map(r=>r.textContent),markup:document.querySelectorAll('#top-prompts tr.prompt-text b').length}));
      assert.equal(card.rows.length,4);assert.equal(card.rows[0][0],'1');assert.deepEqual(card.credits,[null,null,T.credit,null],'credits only for the Codex turn whose requests all have a rate');
      assert.deepEqual(card.rows.map(r=>r[9].replace(/ /g,' ')),T.money);assert.equal(card.rows[1][6],'1');assert.equal(card.rows[1][5],'2');assert.deepEqual(card.rows.map(r=>r[7]),['3','14','–','–'],'inputs column: stored counts, – when unknown');
      assert.equal(await p2.locator('#top-prompts th').nth(9).innerText(),T.lang==='sv'?'Kostnad':'Cost');assert.equal(await p2.locator('#top-prompts th').nth(7).innerText(),T.inputs);
      {// the page rounds credits half away from zero on the shortest decimal form, like credits.fmt (1.25 -> 1.3, 100.5 -> 101, 0.125 -> 0.13, 1.15 -> 1.2, 0.615 -> 0.62, 9.95 -> 10.0)
        const got=await p2.evaluate(()=>[1.25,100.5,0.125,1.15,0.615,9.95].map(x=>UsageReport.cr(x))),dp=T.lang==='sv'?',':'.';
        assert.deepEqual(got,['1'+dp+'3','101','0'+dp+'13','1'+dp+'2','0'+dp+'62','10'+dp+'0'],'credit rounding: '+got);
      }
      {// credits in the costliest-turns card: desktop table, then the 390 px layout (the table scrolls inside its wrapper, the page must not)
        const cr=p2.locator('#top-prompts tr.prompt-row .cr');assert.equal(await cr.count(),1);assert.equal((await cr.first().innerText()).trim(),T.credit);assert.equal(await cr.first().isVisible(),true);
        await p2.locator('#prompts').screenshot({path:path.join(screenshotDir,'credits-turns-desktop-'+T.lang+'.png')});
        const vp=p2.viewportSize();await p2.setViewportSize({width:390,height:844});
        await cr.first().scrollIntoViewIfNeeded();assert.equal(await cr.first().isVisible(),true);assert.equal((await cr.first().innerText()).trim(),T.credit,'credits at 390 px');
        const fits=await p2.evaluate(()=>{const c=document.querySelector('#top-prompts tr.prompt-row .cr').getBoundingClientRect(),w=document.querySelector('#top-prompts .table-wrap').getBoundingClientRect();return {inside:c.left>=w.left-1&&c.right<=w.right+1,page:document.documentElement.scrollWidth<=innerWidth+1}});
        assert.ok(fits.page,'390 px: the page must not overflow');
        await p2.locator('#prompts').screenshot({path:path.join(screenshotDir,'credits-turns-390-'+T.lang+'.png')});
        await p2.setViewportSize(vp);
      }
      assert.deepEqual(card.texts,['Refactor the importer','Fix the <b>failing</b> build']);assert.equal(card.markup,0,'preview must be text, not markup');
      await p2.screenshot({path:path.join(screenshotDir,'energy-report-prompts-'+T.lang+'.png'),fullPage:true});
      // Private context: a collapsed details block per stored turn; opening shows title, place, final message and activity, all as text.
      const det=p2.locator('#top-prompts tr.prompt-ctx details.tc');assert.equal(await det.count(),3);
      const costly=det.nth(1);assert.equal(await costly.locator('.tc-row').first().isVisible(),false,'collapsed until opened');
      const tableWidth=()=>p2.evaluate(()=>{const tb=document.querySelector('#top-prompts table')||document.querySelector('#top-prompts');return {table:tb.scrollWidth,box:tb.parentElement.clientWidth}});const before=await tableWidth();await costly.locator('summary').click();assert.equal(await costly.locator('.tc-row').first().isVisible(),true);const after=await tableWidth();assert.ok(after.table<=Math.max(before.table,after.box)+1,'opening context must not widen the table: '+JSON.stringify({before,after}));
      const body=(await costly.innerText()).replace(/\s/g,' ');
      for(const x of [...T.labels,'Fix the <i>build</i> pipeline','Fix the <b>failing</b> build','also <u>lint</u>','and tests','Done: <b>all green</b>','https://example.test/o/app.git · feat/x · /w/app','#16','Fix <b>build</b> order','Add lint',T.activity])assert.ok(body.includes(x),x+' in '+body);
      assert.equal(await p2.locator('#top-prompts details.tc :is(b,i,u)').count(),0,'context must be text, not markup');
      const sparse=det.nth(0);await sparse.locator('summary').click();const sparseText=await sparse.innerText();
      assert.ok(sparseText.includes('Refactor the importer'));for(const x of [T.labels[0],T.labels[2],T.labels[3],T.labels[4]])assert.ok(!sparseText.includes(x),'unknown parts are omitted: '+x);
      await p2.locator('#prompts').screenshot({path:path.join(screenshotDir,'energy-report-context-'+T.lang+'.png')});

      // Back to the conversation (#84): private reports only; a link for Codex, the quoted command, copy buttons, the turn time.
      {
        const RS={sv:{open:'Öppna i Codex',copy:'Kopiera',prompt:'Kopiera prompten',copied:'Kopierat',selected:'Markerad – tryck ⌘C/Ctrl+C',toast:x=>'Här hade du öppnat konversationen i '+x+'.',hint:'Öppnar hela konversationen'},en:{open:'Open in Codex',copy:'Copy',prompt:'Copy prompt',copied:'Copied',selected:'Selected – press ⌘C/Ctrl+C',toast:x=>'Here you would open the conversation in '+x+'.',hint:'Opens the whole conversation'}}[T.lang];
        const stub=()=>{Object.defineProperty(navigator,'clipboard',{value:{writeText:v=>{window.__copied=v;return Promise.resolve()}},configurable:true})};
        const reject=()=>{Object.defineProperty(navigator,'clipboard',{value:{writeText:()=>Promise.reject(new Error('denied'))},configurable:true})};
        const CODEX='codex://threads/019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b',CODEX_CMD="cd '/w/my app/it'\"'\"'s' && codex resume 019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b";
        const grab=pg=>pg.evaluate(()=>[...document.querySelectorAll('#top-prompts .rs')].map(b=>({links:[...b.querySelectorAll('a')].map(a=>[a.getAttribute('href'),a.textContent]),code:[...b.querySelectorAll('code')].map(c=>c.textContent),buttons:[...b.querySelectorAll('button')].map(x=>x.textContent),hint:b.querySelector('.rs-hint')?.textContent??null})));
        const r1=await newPage({locale:T.locale},fixture,stub);
        const rs=await grab(r1.page);assert.equal(rs.length,3);
        assert.deepEqual(rs[0],{links:[],code:[],buttons:[RS.prompt],hint:null},'no directory: no Claude command, prompt copy only, no open hint');
        assert.deepEqual([rs[1].links,rs[1].code,rs[1].buttons],[[],['cd /w/app && claude --resume s1'],[RS.prompt,RS.copy]]);
        assert.deepEqual([rs[2].links,rs[2].code,rs[2].buttons],[[[CODEX,RS.open]],[CODEX_CMD],[RS.copy]]);
        for(const b of rs.slice(1)){assert.ok(b.hint.startsWith(RS.hint)&&/2026-09-03 1[0-2]:\d\d/.test(b.hint),'hint with the turn time: '+b.hint)}
        // Copy: the command goes to the clipboard and the note confirms; the prompt button copies the prompt text.
        const blk=r1.page.locator('#top-prompts .rs');
        await blk.nth(1).getByRole('button',{name:RS.copy,exact:true}).click();
        assert.equal(await r1.page.evaluate(()=>window.__copied),'cd /w/app && claude --resume s1');assert.equal((await blk.nth(1).locator('.rs-note').innerText()).trim(),RS.copied);
        await blk.nth(1).getByRole('button',{name:RS.prompt,exact:true}).click();assert.equal(await r1.page.evaluate(()=>window.__copied),'Fix the <b>failing</b> build');
        assert.deepEqual(r1.errors,[]);await r1.context.close();
        // A rejected or missing clipboard falls back to selecting the text; no dialog.
        for(const init of [reject,()=>{Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true})}]){
          const r2=await newPage({locale:T.locale},fixture,init);const dialogs=[];r2.page.on('dialog',d=>{dialogs.push(d.type());d.dismiss()});
          const b2=r2.page.locator('#top-prompts .rs').nth(1);await b2.getByRole('button',{name:RS.copy,exact:true}).click();
          for(let i=0;i<50&&(await b2.locator('.rs-note').innerText()).trim()==='';i++)await r2.page.waitForTimeout(50);
          assert.equal((await b2.locator('.rs-note').innerText()).trim(),RS.selected);assert.equal(await r2.page.evaluate(()=>String(getSelection())),'cd /w/app && claude --resume s1');
          assert.deepEqual(dialogs,[]);assert.deepEqual(r2.errors,[]);await r2.context.close();
        }
        // Limit windows at 390 px: each window is a small card inside the Limits card, with no sideways scrolling; reset, peak, hit and list price are visible.
        {const qn=await newPage({locale:T.locale,viewport:{width:390,height:900}},withQuota(fixture));await qn.page.locator('#quota-windows').scrollIntoViewIfNeeded();
          const m=await qn.page.evaluate(()=>{const box=document.getElementById('quota-windows'),w=box.querySelector('.table-wrap'),W=window.innerWidth,B=box.getBoundingClientRect();
            const cells=[...box.querySelectorAll('#qw-table td')].filter(c=>c.offsetParent!==null);return {wrap:w.scrollWidth<=w.clientWidth+1,box:box.scrollWidth<=box.clientWidth+1,doc:document.documentElement.scrollWidth<=W+1,
              inside:cells.every(c=>{const r=c.getBoundingClientRect();return r.left>=B.left-1&&r.right<=B.right+1&&r.width>0}),head:[...box.querySelectorAll('#qw-table th')].every(h=>h.offsetParent===null),
              labels:cells.map(c=>c.dataset.label+': '+c.innerText.trim())}});
          assert.ok(m.wrap&&m.box&&m.doc&&m.inside&&m.head,'#qw 390 px: no horizontal overflow, cells inside the card: '+JSON.stringify(m));
          const joined=m.labels.join(' | ');assert.ok(/\d{4}-\d\d-\d\d \d\d:\d\d UTC/.test(joined)&&joined.split(':').length>6&&m.labels.some(l=>l.startsWith(T.lang==='sv'?'Återställs':'Resets'))&&m.labels.some(l=>l.startsWith(T.lang==='sv'?'Högsta (när)':'Peak (when)'))&&m.labels.some(l=>l.startsWith(T.lang==='sv'?'Gräns nådd':'Limit hit')),'#qw labels visible: '+joined);
          fs.mkdirSync(path.join(screenshotDir,'qw-shots'),{recursive:true});await qn.page.locator('#quota-windows').screenshot({path:path.join(screenshotDir,'qw-shots','qw-390-'+T.lang+'.png')});
          assert.deepEqual(qn.errors,[]);await qn.context.close()}
        // Screenshots: the private context with its resume row, desktop and 390 px.
        const dir=path.join(screenshotDir,'resume-shots');fs.mkdirSync(dir,{recursive:true});
        for(const [name,vp] of [['desktop',{width:1440,height:1080}],['390',{width:390,height:900}]]){
          const r3=await newPage({locale:T.locale,viewport:vp},fixture);await r3.page.locator('#top-prompts tr.prompt-ctx details.tc summary').nth(1).click();
          await r3.page.locator('#prompts').screenshot({path:path.join(dir,'resume-context-'+name+'-'+T.lang+'.png')});
          if(name==='390')assert.equal(await r3.page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),true,'no horizontal overflow at 390 px');
          assert.deepEqual(r3.errors,[]);await r3.context.close();
        }
        // Demo: clicking explains instead of opening or copying; same page, no popup, no clipboard write.
        const d1=await newPage({locale:T.locale},withDemo(fixture),stub);const popups=[];d1.context.on('page',x=>popups.push(x.url()));
        const url0=d1.page.url();
        await d1.page.locator('#top-prompts .rs a').click();
        await d1.page.locator('#toast.show').waitFor({timeout:5000});
        assert.equal((await d1.page.locator('#toast').innerText()).trim(),RS.toast('Codex'));
        assert.equal(d1.page.url(),url0);assert.deepEqual(popups,[]);
        await d1.page.screenshot({path:path.join(dir,'demo-toast-'+T.lang+'.png')});
        await d1.page.locator('#top-prompts .rs').nth(1).getByRole('button',{name:RS.copy,exact:true}).click();
        assert.equal((await d1.page.locator('#toast').innerText()).trim(),RS.toast('Claude Code'));assert.equal(await d1.page.evaluate(()=>window.__copied===undefined),true,'demo copies nothing');
        assert.equal((await d1.page.locator('#top-prompts .rs code').first().innerText()),'cd /w/app && claude --resume s1','demo still shows the commands');
        {let hidden=false;for(let i=0;i<160&&!hidden;i++){hidden=await d1.page.evaluate(()=>!document.getElementById('toast').classList.contains('show'));if(!hidden)await d1.page.waitForTimeout(50)}assert.ok(hidden,'toast hides after about 4 s')}
        assert.deepEqual(d1.errors,[]);await d1.context.close();
        {// every turn row is an anchor target, and a populated report with a limit hit reaches its ready state without page errors
          const lh=await newPage({locale:T.locale},withLimitHit(fixture));
          assert.equal(await lh.page.evaluate(()=>[...document.querySelectorAll('#top-prompts tr.prompt-row')].every(r=>/^turn-\d+$/.test(r.id))),true);
          assert.equal(await lh.page.locator('#limit-hits').isVisible(),true);assert.equal(await lh.page.locator('#limit-list .lh-hit').count(),1);
          assert.equal(await lh.page.locator('#top-prompts .lh-badge').count(),1);assert.ok((await lh.page.locator('#limit-hits').innerText()).includes('≥$12'+(T.lang==='sv'?',':'.')+'50'));
          assert.equal(await lh.page.locator('#limit-list a[href^="#turn-"]').count(),2,'the two links to a shown card (hit turn and top turn 1)');
          assert.ok((await lh.page.locator('#limit-hits').innerText()).includes(T.lang==='sv'?'2 nekade försök':'2 rejected attempts'));
          assert.deepEqual(lh.errors,[]);await lh.context.close();
          const qw=await newPage({locale:T.locale},withQuota(fixture));
          assert.equal(await qw.page.locator('#limit-hits').isVisible(),true);assert.equal(await qw.page.locator('#qw-table tr').count(),2);assert.equal(await qw.page.locator('#top-prompts .qs').count(),1);
          {const qt=await qw.page.locator('#top-prompts .qs').innerText();assert.ok(/3 ?%/.test(qt)&&qt.includes(T.lang==='sv'?'veckogränsen för Codex':'the weekly Codex limit'),'#115: the card names the agent: '+qt)}assert.deepEqual(qw.errors,[]);await qw.context.close();
          {// #125: a share belongs to the whole turn; when the selection holds only part of it the card and the summary say so
            const sv=T.lang==='sv',whole=sv?'(hela turen)':'(whole turn)',card=pg=>pg.locator('#turn-0 .qs').first().innerText().then(norm),glance=pg=>pg.locator('#glance-text').innerText().then(norm);
            const wp=await newPage({locale:T.locale},withTopShare(fixture)),info=await wp.page.evaluate(()=>{const rows=UsageReport.getSelected().filter(r=>r.prompt===0),child=rows.find(r=>r.thread_kind==='subagent'),parent=rows.find(r=>r.thread_kind!=='subagent');return {n:rows.length,total:UsageReport.data.prompt_requests[0],model:child.model,parentModel:parent.model,agentLabel:child.agent,minute:parent.minute,date:parent.date}});
            assert.ok(info.n>1&&info.n===info.total&&info.model!==info.parentModel,'the fixture turn 0 has a subagent with another model');
            {const c=await card(wp.page);assert.ok(c.includes(sv?'~26 % av veckogränsen för Codex':'~26% of the weekly Codex limit')&&!c.includes(whole),'whole turn selected, no label: '+c);assert.ok(!(await glance(wp.page)).includes(whole),'the summary has no label either')}
            await wp.page.selectOption('#model',info.model);{const c=await card(wp.page);assert.ok(c.includes(sv?'~26 % av veckogränsen för Codex '+whole:'~26% of the weekly Codex limit '+whole),'model filter cuts the turn: '+c)}
            await wp.page.click('#reset');await wp.page.locator('.advanced summary').click();await wp.page.selectOption('#agent',{index:await wp.page.evaluate(a=>[...document.querySelectorAll('#agent option')].findIndex(o=>o.value&&o.textContent.includes(a)),info.agentLabel)});
            {const c=await card(wp.page);assert.ok(c.includes(whole),'subagent filter cuts the turn: '+c)}
            await wp.page.click('#reset');await wp.page.selectOption('#model',info.parentModel);{const c=await card(wp.page);assert.ok(c.includes(whole),'the parent model alone is a part too: '+c)}
            await wp.page.click('#reset');await wp.page.fill('#from','2026-01-01');await wp.page.fill('#to','2026-12-31');await wp.page.click('[data-gran="minute"]');
            const bars=await wp.page.locator('#chart .bar').count();let zoomed=null;
            for(let i=0;i<bars&&!zoomed;i++){await wp.page.locator('#chart .bar').nth(i).click();if(await wp.page.locator('#turn-0').count()&&(await wp.page.evaluate(()=>UsageReport.getSelected().filter(r=>r.prompt===0).length))<info.total)zoomed=true;else if(await wp.page.locator('#zoomout').isVisible())await wp.page.click('#zoomout')}
            assert.ok(zoomed,'a minute zoom that holds only part of turn 0');
            {const c=await card(wp.page),g=await glance(wp.page);assert.ok(c.includes(whole),'minute zoom: '+c);assert.ok(g.includes(sv?'~26 % av veckogränsen för Codex '+whole:'~26% of the weekly Codex limit '+whole),'the summary labels it too: '+g)}
            if(process.env.SHOT)await wp.page.locator('#top-prompts').screenshot({path:process.env.SHOT+'-'+T.lang+'.png'});
            assert.deepEqual(wp.errors,[]);await wp.context.close();
            {// a fully selected turn with a synthetic-id request is not partial: the summary counts the same requests as the card
              const mp=await newPage({locale:T.locale},edited(withTopShare(fixture),d=>{const i=d.columns.prompt.findIndex((x,k)=>x===0&&d.columns.price[k]==null);d.columns.id_synthetic[i]=1;for(const f of Object.keys(d.columns.tokens))d.columns.tokens[f]=d.columns.tokens[f].map((x,k)=>d.columns.prompt[k]===1?0:x)}));
              const c=await card(mp.page),g=await glance(mp.page);assert.ok(c.includes(sv?'~26 %':'~26%')&&!c.includes(whole),'mixed identities, whole selection, card: '+c);assert.ok(g.includes(sv?'~26 % av veckogränsen':'~26% of the weekly')&&!g.includes(whole),'summary: '+g);assert.deepEqual(mp.errors,[]);await mp.context.close()}
            const ls=sv?'Träffar som togs med när rapporten byggdes; påverkas inte av filtren ovan.':'Hits included when this report was built; not affected by the filters above.',ws=sv?'De senaste fönstren för varje gräns; påverkas inte av filtren ovan.':'Recent windows of each limit; not affected by the filters above.',lq=await newPage({locale:T.locale},withQuota(fixture));
            assert.deepEqual(await lq.page.evaluate(()=>[...document.querySelectorAll('#limit-hits [data-t="lh_scope"],#limit-hits [data-t="qw_scope"]')].map(e=>e.textContent)),[ls,ws],'#125: both limit sections disclose that they ignore the filters, without claiming to be all history');
            assert.equal(await lq.page.locator('#quota-windows [data-t="qw_scope"]').count(),1);assert.deepEqual(lq.errors,[]);await lq.context.close();
          }
          {// At a glance (#78): the numbers come from the same rows as the KPIs and follow the filters; limit hits and the quota share join in when the payload has them
            const g=await newPage({locale:T.locale},fixture),sv=T.lang==='sv',text=()=>g.page.locator('#glance-text').innerText(),
              truth=()=>g.page.evaluate(([loc])=>{const rows=UsageReport.getSelected().filter(r=>!r.id_synthetic),priced=rows.filter(r=>r.cost!=null),f=x=>x.toLocaleString(loc,{minimumFractionDigits:2,maximumFractionDigits:2});
                return {n:UsageReport.getSelected().length,days:new Set(rows.map(r=>r.date)).size,note:document.getElementById('total-note').textContent,cost:f(priced.reduce((s,r)=>s+r.cost,0)),lower:rows.some(r=>r.cost==null||!r.complete)}},[T.locale]);
            const first=await truth(),t1=norm(await text());
            assert.ok(first.n>1&&first.days>1);assert.ok(t1.includes(norm((first.lower?'≥':'')+'$'+first.cost)),'cost matches the rows: '+t1);assert.ok(norm(first.note).startsWith(t1.match(/\(([\d\s,.]+) (?:requests?|anrop)\)/)[1]+' '),'request count matches the KPI note');
            assert.ok(t1.includes(sv?'Dagen med högst registrerad, prissatt kostnad var 2026-':'The day with the highest recorded, priced cost was 2026-'),'the fixture has unpriced requests: '+t1);assert.ok(!/(?:gränsträff|limit hit)/.test(t1),'no hits without a payload');
            assert.equal(await g.page.locator('#glance .eyebrow').textContent(),sv?'I korthet':'At a glance');
            {// #145: the summary is a real list with the key numbers in <strong>; the plain text stays the joined items
              assert.equal(await g.page.locator('ul#glance-text').count(),1);const n=await g.page.locator('#glance-text > li').count();assert.ok(n>=2,'one fact per item: '+n);
              const bold=await g.page.locator('#glance-text li strong').allInnerTexts();assert.ok(bold.length>=n-1&&bold.every(x=>/\d/.test(x)),'key numbers are bold: '+bold);assert.ok(norm(bold.join(' ')).includes('$'),'the cost is bold');
              assert.equal(await g.page.evaluate(()=>UsageReport.glanceText(UsageReport.glanceFacts(UsageReport.getSelected()))===UsageReport.glanceItems(UsageReport.glanceFacts(UsageReport.getSelected())).map(i=>i.text).join(' ')),true);if(sv){const ab=await g.page.evaluate(()=>[...document.querySelectorAll('#glance-text abbr.term')].map(x=>x.textContent));if((await g.page.locator('#glance-text').innerText()).includes('Den dyraste turen'))assert.ok(ab.includes('turen'),'"turen" carries the glossary tooltip: '+ab)}{// one-sided range (no reading after the last request): the explanation names the missing reading, not rounding or overlap
              const open=await g.page.evaluate(()=>{const f=UsageReport.glanceFacts(UsageReport.getSelected());f.quota={percent:4,minutes:10080,harness:'codex',range:{label:'range',lower:4,upper:null,shared_with:3}};return UsageReport.glanceText(f)});assert.ok(open.includes(sv?'minst 4 %':'at least 4%')&&open.includes(sv?'bara den nedre gränsen är känd':'only the lower bound is known')&&!open.includes(sv?'andra turer':'other turns'),open)}assert.equal(await g.page.locator('#glance').getAttribute('aria-live'),'polite');
              assert.ok(await g.page.locator('#glance-text abbr.term[title]').count()>=1,'terms carry the glossary sentence as a tooltip');
              // the glossary: collapsed, one sentence per term, the list price names the bundled table
              assert.equal(await g.page.locator('#glossary').evaluate(d=>d.open),false);assert.equal(norm(await g.page.locator('#glossary summary').innerText()),sv?'Ordlista':'Glossary');assert.equal(await g.page.locator('#glossary dt').count(),6);
              const dds=(await g.page.locator('#glossary dd').allTextContents()).map(norm);assert.ok(dds.every(x=>x.length>20&&x.endsWith('.')),dds.join('|'));assert.ok(dds.some(x=>x.includes(sv?'pristabell':'price table')),'list price source');assert.ok(dds.some(x=>x.includes(sv?'avrundad':'rounded')),'limit share explanation');
              // token unit on the cards
              for(const id of ['total','fresh','cache-write','cache','output']){const u=norm(await g.page.locator('#'+id+' .unit').innerText().catch(()=>''));if(u)assert.equal(u,'tokens',id)}assert.equal(norm(await g.page.locator('#total .unit').innerText()),'tokens');assert.ok(norm(await g.page.locator('#output .unit').innerText())==='tokens');
              // private vs shared note above the prompts
              const note=norm(await g.page.locator('#p4-priv').innerText());assert.ok(sv?note.includes('Texten visas här för att rapporten är privat')&&note.includes('(--shared)'):note.includes('shown here because this report is private')&&note.includes('(--shared)'),note);assert.ok(!norm(await g.page.locator('[data-t="p4_p"]').innerText()).includes(sv?'aldrig':'never'));
              if(sharedFixture){const sp=await newPage({locale:T.locale},sharedFixture),sn=norm(await sp.page.locator('#p4-priv').innerText());assert.equal(sn,sv?'Text och kontext ingår aldrig i delade rapporter, bara antalet inmatningar.':'Text and context are never included in shared reports, only the number of inputs.');assert.deepEqual(sp.errors,[]);await sp.context.close()}}
            {// #148: back-to-top button (page.waitForFunction is blocked by the page's CSP, so poll with evaluate)
              const until=async(pg,fn)=>{for(let i=0;i<60;i++){if(await pg.evaluate(fn))return;await pg.waitForTimeout(100)}throw new Error('timeout: '+fn)};
              const q=await newPage({locale:T.locale},fixture),btn=q.page.locator('#to-top'),label=sv?'Till toppen':'Back to top';
              assert.equal(await btn.evaluate(b=>b.tagName),'BUTTON');assert.equal(await btn.getAttribute('aria-label'),label);assert.ok(await btn.isHidden(),'hidden at the top');
              await q.page.evaluate(()=>window.scrollTo(0,document.documentElement.scrollHeight));await until(q.page,()=>!document.getElementById('to-top').classList.contains('hidden'));assert.ok(await btn.isVisible());
              const box=await btn.boundingBox(),vp=q.page.viewportSize();assert.ok(vp.width-(box.x+box.width)>=15&&vp.height-(box.y+box.height)>=15,'16 px from the edges');
              await q.page.emulateMedia({reducedMotion:'reduce'});await q.page.keyboard.press('Shift');await btn.focus();assert.ok(await btn.evaluate(b=>b.matches(':focus-visible')&&getComputedStyle(b).outlineStyle!=='none'&&parseFloat(getComputedStyle(b).outlineWidth)>0),'visible keyboard focus ring');await q.page.keyboard.press('Enter');await until(q.page,()=>window.scrollY===0);await until(q.page,()=>document.getElementById('to-top').classList.contains('hidden'));assert.ok(await btn.isHidden(),'hidden again at the top');
              await q.page.emulateMedia({reducedMotion:'no-preference',media:'print'});await q.page.evaluate(()=>window.scrollTo(0,1e6));assert.equal(await btn.evaluate(b=>getComputedStyle(b).display),'none','hidden in print');assert.deepEqual(q.errors,[]);await q.context.close();
              const ph=await newPage({locale:T.locale,viewport:{width:390,height:800}},fixture);await ph.page.evaluate(()=>window.scrollTo(0,document.documentElement.scrollHeight));await until(ph.page,()=>!document.getElementById('to-top').classList.contains('hidden'));const pb=await ph.page.locator('#to-top').boundingBox();assert.ok(390-(pb.x+pb.width)>=15&&800-(pb.y+pb.height)>=15&&pb.width<=60,'phone: small, 16 px from the edges');await ph.page.locator('#to-top').click();await until(ph.page,()=>window.scrollY===0);assert.deepEqual(ph.errors,[]);await ph.context.close()}
            {// #147: many ok roots collapse to one line per harness; a failing root is still listed; singular forms
              const mk=(imps)=>edited(fixture,d=>{const h=d.coverage.ranges[0].harness;d.coverage.imports=imps.map(i=>({harness:h,status:'ok',files_seen:1,files_parsed:1,malformed_lines:0,partial_lines:0,read_errors:0,unparsed_usage_lines:0,...i}))}),
                run=async imps=>{const q=await newPage({locale:T.locale},mk(imps)),box=q.page.locator('#sources .source').first(),lines=(await box.locator(':scope > p').allTextContents()).map(norm),all=await box.locator('details.import-all p').allTextContents();const notes=(await q.page.locator('#sources > p').allTextContents()).map(norm),open=await box.locator('details.import-all').count()?await box.locator('details.import-all').evaluate(d=>d.open):null;assert.deepEqual(q.errors,[]);await q.context.close();return {lines,all,open,notes}};
              const many=await run(Array.from({length:204},()=>({}))),visible=many.lines.filter(x=>/^(Import|\d+ (källmappar|source folders))/.test(x));
              assert.equal(visible.length,1,'one summary line: '+many.lines.slice(0,6).join(' | '));assert.ok(sv?visible[0].startsWith('204 källmappar · 204 filer · alla ok · 0 källdiagnoser'):visible[0].startsWith('204 source folders · 204 files · all ok · 0 source diagnostics'),visible[0]);assert.equal(many.all.length,204);assert.equal(many.open,false);
              assert.equal(many.notes.filter(x=>x.includes(sv?'Källdiagnoser är':'Source diagnostics are')).length,1,'diagnostics explained once');
              const mixed=await run([{},{},{status:'failed',files_seen:2,read_errors:1},{malformed_lines:3}]),sumLine=mixed.lines.find(x=>/^4 /.test(x));assert.ok(sumLine&&sumLine.includes(sv?'1 inte ok':'1 not ok')&&sumLine.includes(sv?'4 källdiagnoser':'4 source diagnostics'),sumLine);
              const listed=mixed.lines.filter(x=>x.startsWith('Import:'));assert.equal(listed.length,2+0,'the failing and the diagnostic root are listed: '+listed);assert.ok(mixed.lines.some(x=>x.startsWith('Import: ')&&x.includes('failed')||x.includes(sv?'2 filer':'2 files')));
              const one=await run([{files_seen:1}]);assert.ok(one.lines.some(x=>x===(sv?'Import: ok · 1 fil · 0 källdiagnoser':'Import: ok · 1 file · 0 source diagnostics')),one.lines.join('|'));assert.equal(one.all.length,0);
              const two=await run([{files_seen:1,malformed_lines:1},{files_seen:1}]);assert.ok(two.lines.some(x=>x.startsWith(sv?'2 källmappar · 2 filer · alla ok · 1 källdiagnos':'2 source folders · 2 files · all ok · 1 source diagnostic')&&!/diagnostics$|källdiagnoser$/.test(x)),two.lines.join('|'))}
            await g.page.selectOption('#harness',{index:1});const second=await truth(),t2=norm(await text());assert.notEqual(t2,t1);assert.ok(second.n<first.n);assert.ok(t2.includes(norm((second.lower?'≥':'')+'$'+second.cost)));assert.ok(norm(second.note).startsWith(t2.match(/\(([\d\s,.]+) (?:requests?|anrop)\)/)[1]+' '));
            await g.page.selectOption('#harness',{index:0});assert.equal(norm(await text()),t1);
            const day=await g.page.evaluate(()=>UsageReport.getSelected()[0].date);await g.page.fill('#from',day);await g.page.fill('#to',day);const one=await text();assert.ok(one.includes(day+':')&&!/dagen var|costliest day/.test(one),'a single day has no costliest-day sentence: '+one);await g.page.click('#reset');
            await g.page.locator('.advanced summary').click();await g.page.fill('#search','this-match-does-not-exist-19042026');assert.equal((await text()).trim(),sv?'Inga anrop matchar det aktuella urvalet.':'No requests match the current selection.');
            assert.deepEqual(g.errors,[]);assert.deepEqual(g.requests,[]);await g.context.close();
            {// #78 review: ambiguous (synthetic-id) rows are not money; a priced one flips the summary exactly as the KPI rule says
              const priced=d=>d.columns.price.findIndex((x,i)=>x!=null&&!d.columns.id_synthetic[i]),sy=await newPage({locale:T.locale},edited(fixture,d=>{d.columns.id_synthetic[priced(d)]=1}));
              const tr=await sy.page.evaluate(([loc])=>{const f=x=>x.toLocaleString(loc,{minimumFractionDigits:2,maximumFractionDigits:2}),all=UsageReport.getSelected(),keep=all.filter(r=>!r.id_synthetic);return {kpi:f(keep.reduce((s,r)=>s+(r.cost||0),0)),withAmbiguous:f(all.reduce((s,r)=>s+(r.cost||0),0)),n:all.length-keep.length}},[T.locale]);
              const st=norm(await sy.page.locator('#glance-text').innerText());assert.ok(tr.n>=1);assert.notEqual(tr.kpi,tr.withAmbiguous);assert.ok(st.includes('$'+tr.kpi)&&!st.includes('$'+tr.withAmbiguous),st);assert.deepEqual(sy.errors,[]);await sy.context.close();
            }
            {// hits follow the hit's own time in the report's timezone (Europe/Stockholm), the date range and the zoom
              const at=iso=>edited(withLimitHit(fixture),d=>{d.limit_hits[0].at=iso;d.limit_hits[0].local_date=stockholm(iso)}),hit=sv?'1 gränsträff':'1 limit hit',run=async(iso,from,to)=>{const q=await newPage({locale:T.locale},at(iso));if(from)await q.page.fill('#from',from);if(to)await q.page.fill('#to',to);const x=await q.page.locator('#glance-text').innerText();assert.deepEqual(q.errors,[]);await q.context.close();return x.includes(hit)};
              assert.equal(await run('2026-09-02T22:15:00+00:00','2026-09-03','2026-09-03'),true,'UTC 22:15 on the 2nd is the 3rd in Stockholm');assert.equal(await run('2026-09-03T22:15:00+00:00','2026-09-03','2026-09-03'),false,'UTC 22:15 on the 3rd is the 4th in Stockholm');
              assert.equal(await run('2026-09-03T21:59:00+00:00','2026-09-03','2026-09-03'),true,'23:59 local is still the 3rd');
              assert.equal(await run('2026-09-03T10:15:00+00:00','2026-09-04','2026-09-04'),false,'a linked hit outside the range is not counted');assert.equal(await run('2026-09-03T10:15:00+00:00','2026-09-03','2026-09-03'),true);
              const z=await newPage({locale:T.locale},withLimitHit(fixture));await z.page.click('[data-gran="hour"]');const seen=new Set,bars=await z.page.locator('#chart .bar').count();for(let i=0;i<bars;i++){await z.page.locator('#chart .bar').nth(i).click();seen.add((await z.page.locator('#glance-text').innerText()).includes(hit));await z.page.click('#reset');await z.page.click('[data-gran="hour"]')}
              assert.deepEqual([...seen].sort(),[false,true],'only the zoomed hour that holds the hit counts it');assert.deepEqual(z.errors,[]);await z.context.close();
            }
            {// a hit inside the date range or zoom whose turn's usage is on another day still counts, with or without the harness filter
              const base=await newPage({locale:T.locale},withLimitHit(fixture)),dates=await base.page.evaluate(()=>{const rows=UsageReport.getSelected();return {own:rows.find(r=>r.prompt===0).date,all:[...new Set(rows.map(r=>r.date))]}});await base.context.close();
              const other=dates.all.find(x=>x!==dates.own),hit=sv?'1 gränsträff':'1 limit hit',html=edited(withLimitHit(fixture),d=>{d.limit_hits[0].at=other+'T10:15:00+00:00';d.limit_hits[0].local_date=other});assert.ok(other);
              for(const harness of [false,true]){const q=await newPage({locale:T.locale},html);await q.page.fill('#from',other);await q.page.fill('#to',other);if(harness)await q.page.selectOption('#harness',{label:'claude'});
                assert.ok((await q.page.locator('#glance-text').innerText()).includes(hit),'range, harness='+harness);assert.deepEqual(q.errors,[]);await q.context.close();
                const z=await newPage({locale:T.locale},html);if(harness)await z.page.selectOption('#harness',{label:'claude'});await z.page.click('[data-gran="hour"]');const seen=new Set,bars=await z.page.locator('#chart .bar').count();
                for(let i=0;i<bars;i++){await z.page.locator('#chart .bar').nth(i).click();seen.add((await z.page.locator('#glance-text').innerText()).includes(hit));await z.page.locator('#zoomout').isVisible()&&await z.page.click('#zoomout');await z.page.click('[data-gran="hour"]')}
                assert.ok(seen.has(true),'zoom, harness='+harness);assert.deepEqual(z.errors,[]);await z.context.close()}
            }
            {// #78 round 3: no timezone database in the page, the period covers counted hits, estimate and ambiguous wording
              const tz=await newPage({locale:T.locale},edited(withLimitHit(fixture),d=>{d.timezone='Factory'})),tzt=await tz.page.locator('#glance-text').innerText();assert.ok(tzt.includes(sv?'1 gränsträff':'1 limit hit'),tzt);assert.deepEqual(tz.errors,[]);await tz.context.close();
              const late=edited(withLimitHit(fixture),d=>{d.limit_hits[0].at='2026-09-10T10:15:00+00:00';d.limit_hits[0].local_date='2026-09-10';d.limit_hits[0].prompt=null}),lp=await newPage({locale:T.locale},late),lt=norm(await lp.page.locator('#glance-text').innerText());
              assert.ok(lt.includes('– 2026-09-10:')&&lt.includes(sv?'1 gränsträff':'1 limit hit'),'a rejection after the last usage date is inside the printed period: '+lt);
              await lp.page.selectOption('#harness',{label:'claude'});assert.ok(norm(await lp.page.locator('#glance-text').innerText()).includes('– 2026-09-10:'),'with a harness filter too');
              await lp.page.fill('#to','2026-09-03');const cut=norm(await lp.page.locator('#glance-text').innerText());assert.ok(!cut.includes('2026-09-10')&&!/gränsträff|limit hit/.test(cut),cut);assert.deepEqual(lp.errors,[]);await lp.context.close();
              for(const [label,percent,want,not] of [['observed',26,sv?'~26 %':'~26%',sv?'(uppskattning)':'(estimate)'],['estimate',26,sv?'≈ 26 %':'≈ 26%',null],['estimate',0.2,sv?'< 1 %':'< 1%',null],['observed',0.2,sv?'< 1 %':'< 1%',sv?'(uppskattning)':'(estimate)']]){
                const e=await newPage({locale:T.locale},withTopShare(fixture,label,percent)),et=norm(await e.page.locator('#glance-text').innerText());assert.ok(et.includes(want),et);const mark=sv?'(uppskattning)':'(estimate)';assert.equal(et.includes(mark),label==='estimate',label+' '+percent+': '+et);if(not)assert.ok(!et.includes(not));assert.deepEqual(e.errors,[]);await e.context.close()}
              {// #131: a shared turn shows a range, on the card, the phone card and the summary; a narrow range also shows its point
                const win=sv?'veckogränsen för Codex':'the weekly Codex limit',shw=sv?'(4 andra turer samtidigt)':'(4 other turns at the same time)',why=sv?'4 andra turer pågick samtidigt':'4 other turns ran at the same time';
                for(const [share,want,point] of [[{label:'range',lower:2,upper:28},sv?'minst 2 %, högst 28 %':'at least 2%, at most 28%',false],[{label:'range',lower:0.2,upper:28},sv?'högst 28 %':'at most 28%',false],[{label:'estimate',percent:9,lower:7,upper:11},sv?'≈ 9 % (minst 7 %, högst 11 %)':'≈ 9% (at least 7%, at most 11%)',true],[{label:'estimate',percent:0.3,lower:0,upper:2},sv?'högst 2 %':'at most 2%',false],[{label:'range',lower:2,upper:2.5},sv?'minst 2 %, högst 3 %':'at least 2%, at most 3%',false],[{label:'range',lower:4,upper:null},sv?'minst 4 %':'at least 4%',false],[{label:'range',lower:3.6,upper:null},sv?'minst 3 %':'at least 3%',false],[{label:'range',lower:3.6,upper:9.2},sv?'minst 3 %, högst 10 %':'at least 3%, at most 10%',false]]){
                  const rp=await newPage({locale:T.locale},withTopRange(fixture,share)),ct=norm(await rp.page.locator('#turn-0 .qs').first().innerText()),gt2=norm(await rp.page.locator('#glance-text').innerText());
                  assert.ok(ct.includes(want)&&ct.includes(win)&&ct.includes(shw),'desktop card: '+ct);if(share.percent===0.3)assert.ok(!ct.includes('≈'),'a point that rounds to 0 is left out: '+ct);assert.ok(gt2.includes(want)&&gt2.includes(share.upper===null?(sv?'bara den nedre gränsen är känd':'only the lower bound is known'):why)&&!gt2.includes(sv?'delad med':'shared with'),'summary: '+gt2);if(sv)assert.ok((await rp.page.evaluate(()=>[...document.querySelectorAll('#glance-text abbr.term')].map(x=>x.textContent))).includes('turen'),'"turen" carries the glossary tooltip');if(share.upper===null)assert.equal(await rp.page.locator('#turn-0 .qs').first().getAttribute('title'),sv?'Mätningen efter turens sista anrop saknas, så bara den nedre gränsen är känd. Mätaren räknar även användning som loggarna inte ser.':"The reading after the turn's last request is missing, so only the lower bound is known. The meter also counts usage the logs do not see.",'one-sided tooltip');assert.ok(!/\d[–-]\d/.test(ct),'no bare dash range on the card: '+ct);assert.ok(!ct.includes(sv?'uppskattning':'(estimate)'),'no estimate label: '+ct);assert.deepEqual(rp.errors,[]);await rp.context.close();
                  const ph=await newPage({locale:T.locale,viewport:{width:390,height:900}},withTopRange(fixture,share)),pt=norm(await ph.page.locator('#turn-0 .qs').first().innerText());
                  assert.ok(pt.includes(want)&&pt.includes(shw),'phone card: '+pt);assert.ok(await ph.page.locator('#turn-0 .qs').first().isVisible(),'the phone card shows the range');assert.ok(await ph.page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'no horizontal scroll at 390 px');assert.deepEqual(ph.errors,[]);await ph.context.close()}
              }
              {// #131: the insights fact lists a range per turn
                const fact=turns=>({id:'quota_share',title_key:'ins_quota_share',values:{window_minutes:10080,considered:turns.length,turns,observed_turns:0,estimated_turns:turns.length,lower_bound:false,lower_bound_requests:0,ambiguous_requests:0,incomplete_requests:0},params:{retrieved:'',ambiguous:0,incomplete:0,lower:0},provenance:'estimate',computation_key:'ins_quota_share_c',assumption_keys:['ins_a_quota_account','ins_a_quota_whole'],price_assumptions:[]});
                const ins=await newPage({locale:T.locale},edited(fixture,d=>{for(const w of d.insights.windows)w.facts.push(fact([{cost:9,percent:null,lower:2,upper:28,label:'range',harness:'codex'},{cost:5,percent:9,lower:7,upper:11,label:'estimate',harness:'codex'},{cost:3,percent:0.3,lower:0,upper:2,label:'estimate',harness:'codex'},{cost:2,percent:null,lower:4,upper:null,label:'range',harness:'codex'}]))}));
                const row=norm(await ins.page.locator('[data-fact="quota_share"] .ins-row').first().innerText());
                for(const want of sv?['2–28 %','≈ 9 % (7–11 %)','< 1 %–2 %','≥ 4 %']:['2–28%','≈ 9% (7–11%)','< 1%–2%','≥ 4%'])assert.ok(row.includes(want),'insights row: '+row);
                assert.ok(!row.includes('≈ 0'),row);assert.deepEqual(ins.errors,[]);await ins.context.close()}
              {// a turn shared with exactly one other turn says "1 turn", on the card and in the summary
                const one=sv?'(1 annan tur samtidigt)':'(1 other turn at the same time)',oneG=sv?'1 annan tur pågick samtidigt':'1 other turn ran at the same time',bad=sv?'1 turer':'1 turns';
                const o1=await newPage({locale:T.locale},withTopRange(fixture,{label:'range',lower:2,upper:28,shared_with:1})),c1=norm(await o1.page.locator('#turn-0 .qs').first().innerText()),g1=norm(await o1.page.locator('#glance-text').innerText());
                assert.ok(c1.includes(one)&&g1.includes(oneG)&&!c1.includes(bad)&&!g1.includes(bad),'singular: '+c1+' | '+g1);assert.deepEqual(o1.errors,[]);await o1.context.close()}
              const am=await newPage({locale:T.locale},edited(fixture,d=>{d.columns.id_synthetic=d.columns.id_synthetic.map(()=>1)})),at2=norm(await am.page.locator('#glance-text').innerText());
              assert.ok(at2.includes(sv?'osäker identitet':'ambiguous identity')&&!/listpris för dessa|no list price/.test(at2),at2);assert.deepEqual(am.errors,[]);await am.context.close();
            }
            {// #78 round 4: one ambiguous request among complete, priced ones makes the amount a lower bound like the KPI, with a disclosure; rejection-only selections keep their hit sentence
              const ms=await newPage({locale:T.locale},edited(fixture,d=>{const i=d.columns.price.findIndex((x,k)=>x!=null&&d.columns.complete[k]&&!d.columns.id_synthetic[k]);for(let k=0;k<d.columns.n;k++)if(d.columns.id_synthetic[k]||!d.columns.complete[k]||d.columns.price[k]==null)d.columns.price[k]=d.columns.price[i];d.columns.complete=d.columns.complete.map(()=>1);d.columns.id_synthetic=d.columns.id_synthetic.map((x,k)=>k===i?1:0)}));
              const mt=norm(await ms.page.locator('#glance-text').innerText()),kpi=norm(await ms.page.locator('#total-label').innerText()),clean=await newPage({locale:T.locale},edited(fixture,d=>{const i=d.columns.price.findIndex((x,k)=>x!=null&&d.columns.complete[k]&&!d.columns.id_synthetic[k]);for(let k=0;k<d.columns.n;k++)if(d.columns.id_synthetic[k]||!d.columns.complete[k]||d.columns.price[k]==null)d.columns.price[k]=d.columns.price[i];d.columns.complete=d.columns.complete.map(()=>1);d.columns.id_synthetic=d.columns.id_synthetic.map(()=>0)}));
              const cleanText=norm(await clean.page.locator('#glance-text').innerText()),cleanKpi=norm(await clean.page.locator('#total-label').innerText());
              assert.ok(!cleanText.includes('≥')&&cleanKpi===T.total,'control: complete and priced shows no lower bound: '+cleanText+' / '+cleanKpi);
              assert.notEqual(kpi,T.total,'the KPI says at least with one ambiguous request');assert.ok(/(?:till listpris|at list price)/.test(mt)&&mt.includes('≥$')&&mt.includes(sv?'osäker identitet':'ambiguous identity'),mt);assert.deepEqual([ms.errors,clean.errors],[[],[]]);await ms.context.close();await clean.context.close();
              const lonely=edited(withLimitHit(fixture),d=>{d.limit_hits[0].at='2026-09-10T10:15:00+00:00';d.limit_hits[0].local_date='2026-09-10';d.limit_hits[0].prompt=null}),hit=sv?'1 gränsträff':'1 limit hit',empty=sv?'Inga anrop matchar det aktuella urvalet.':'No requests match the current selection.';
              const ro=await newPage({locale:T.locale},lonely);await ro.page.fill('#from','2026-09-10');await ro.page.fill('#to','2026-09-10');const rt=norm(await ro.page.locator('#glance-text').innerText());assert.ok(rt.startsWith(empty)&&rt.includes(hit),'a rejection-only date: '+rt);assert.deepEqual(ro.errors,[]);await ro.context.close();
              const none=edited(lonely,d=>{const c=d.columns;for(const k of Object.keys(c)){if(Array.isArray(c[k]))c[k]=[];else if(c[k]&&typeof c[k]==='object')for(const j of Object.keys(c[k]))c[k][j]=[]}c.n=0;c.id_prefix=null;delete d.prompt_texts;delete d.prompt_context;delete d.prompt_resume;delete d.quota_shares;delete d.quota_windows}),rr=await newPage({locale:T.locale},none),rrt=norm(await rr.page.locator('#glance-text').innerText());
              assert.ok(rrt.startsWith(empty)&&rrt.includes(hit),'a rejection-only report: '+rrt);assert.deepEqual(rr.errors,[]);await rr.context.close();
            }
            {// #78 round 5: shares of an incomplete total say "recorded", the costliest day among unpriced days says "highest priced cost"
              const fix=(d,keep)=>{const i=d.columns.price.findIndex((x,k)=>x!=null&&d.columns.complete[k]&&!d.columns.id_synthetic[k]);for(let k=0;k<d.columns.n;k++)if(d.columns.id_synthetic[k]||!d.columns.complete[k]||d.columns.price[k]==null)d.columns.price[k]=d.columns.price[i];d.columns.complete=d.columns.complete.map(()=>1);d.columns.id_synthetic=d.columns.id_synthetic.map(()=>0);keep&&keep(d)};
              const rec=sv?'av den registrerade kostnaden':'of the recorded cost',both=sv?'av den registrerade, prissatta kostnaden':'of the recorded, priced cost';
              const ic=await newPage({locale:T.locale},edited(fixture,d=>fix(d,d=>{const first=d.columns.prompt.find(x=>x!=null);d.columns.prompt.forEach((p,k)=>{if(p===first)d.columns.interrupted[k]=1});d.columns.complete[d.columns.prompt.findIndex(x=>x===first)]=0}))),it=norm(await ic.page.locator('#glance-text').innerText());
              assert.ok(it.includes(rec)&&!it.includes(sv?'prissatta kostnaden':'priced cost'),'incomplete but priced: '+it);assert.deepEqual(ic.errors,[]);await ic.context.close();
              const gt=await newPage({locale:T.locale},fixture),facts={requests:50,turns:20,unattributed:0,from:'2026-09-01',to:'2026-09-02',days:2,cost:5,lower:true,unpriced:false,incomplete:true,top:{k:10,share:30},interrupted:{n:2,unknown:false,share:10}};
              const g1=norm(await gt.page.evaluate(f=>UsageReport.glanceText(f),facts)),g2=norm(await gt.page.evaluate(f=>UsageReport.glanceText(f),{...facts,unpriced:true}));assert.equal(g1.split(rec).length,3,g1);assert.equal(g2.split(both).length,3,g2);await gt.context.close();
              const twoDays=async(unpriced)=>{const q=await newPage({locale:T.locale},edited(fixture,d=>fix(d,d=>{if(unpriced){const days=[];let ms=0;d.columns.ts.forEach((x,k)=>{ms+=x;days[k]=new Date(ms).toISOString().slice(0,10)});const last=days[days.length-1];days.forEach((x,k)=>{if(x===last)d.columns.price[k]=null})}}))),x=norm(await q.page.locator('#glance-text').innerText());assert.deepEqual(q.errors,[]);await q.context.close();return x};
              assert.ok((await twoDays(true)).includes(sv?'Dagen med högst prissatt kostnad var':'The day with the highest priced cost was'));assert.ok((await twoDays(false)).includes(sv?'Dyraste dagen var':'The costliest day was'));
            }
            {// #78 round 6: a hit matches the project, session and provider filters by its own scope (cross-day, and a rejection-only turn); the day wording follows incomplete days; at most six sentences
              const base=await newPage({locale:T.locale},withLimitHit(fixture)),own=await base.page.evaluate(()=>{const rows=UsageReport.getSelected(),r=rows.find(x=>x.prompt===0);return {project_id:r.project_id,session:r.session,provider:r.provider,model:r.model,agent:r.agent,date:r.date,other:rows.map(x=>x.date).find(x=>x!==r.date)}});await base.context.close();
              const hit=sv?'1 gränsträff':'1 limit hit',scoped=(prompt,scope)=>edited(withLimitHit(fixture),d=>{d.limit_hits[0].at=own.other+'T10:15:00+00:00';d.limit_hits[0].local_date=own.other;d.limit_hits[0].prompt=prompt;d.limit_hits[0].scope={project_id:own.project_id,session:own.session,provider:own.provider,model:own.model,effort:null,agent:own.agent}});
              for(const prompt of [0,null])for(const [key,value] of [['project_id',own.project_id],['session',own.session],['provider',own.provider]]){
                const q=await newPage({locale:T.locale},scoped(prompt,0));await q.page.locator('.advanced summary').click();await q.page.fill('#from',own.other);await q.page.fill('#to',own.other);await q.page.selectOption('#'+key,value);
                assert.ok((await q.page.locator('#glance-text').innerText()).includes(hit),'scope filter '+key+' prompt='+prompt);
                const other=await q.page.locator('#'+key+' option').evaluateAll((os,v)=>os.map(o=>o.value).filter(x=>x&&x!==v),value);if(other.length){await q.page.selectOption('#'+key,other[0]);assert.ok(!(await q.page.locator('#glance-text').innerText()).includes(hit),'a hit of another '+key+' is not counted')}
                assert.deepEqual(q.errors,[]);await q.context.close()}
              const dayPage=async(mut)=>{const q=await newPage({locale:T.locale},edited(fixture,d=>{const i=d.columns.price.findIndex((x,k)=>x!=null&&d.columns.complete[k]&&!d.columns.id_synthetic[k]);for(let k=0;k<d.columns.n;k++)if(d.columns.id_synthetic[k]||!d.columns.complete[k]||d.columns.price[k]==null)d.columns.price[k]=d.columns.price[i];d.columns.complete=d.columns.complete.map(()=>1);d.columns.id_synthetic=d.columns.id_synthetic.map(()=>0);mut(d)})),x=norm(await q.page.locator('#glance-text').innerText());assert.deepEqual(q.errors,[]);await q.context.close();return x};
              assert.ok((await dayPage(d=>{d.columns.complete[d.columns.n-1]=0})).includes(sv?'Dagen med högst registrerad kostnad var':'The day with the highest recorded cost was'),'an incomplete day');
              assert.ok((await dayPage(d=>{d.columns.complete[d.columns.n-1]=0;d.columns.price[0]=null})).includes(sv?'högst registrerad, prissatt kostnad':'highest recorded, priced cost'),'incomplete and unpriced');
              assert.ok((await dayPage(()=>{})).includes(sv?'Dyraste dagen var':'The costliest day was'),'control');
              const full=await newPage({locale:T.locale},fixture),all=norm(await full.page.evaluate(f=>UsageReport.glanceText(f),{requests:50,turns:20,unattributed:3,from:'2026-09-01',to:'2026-09-02',days:2,cost:5,lower:true,unpriced:true,incomplete:true,ambiguous:2,top:{k:10,share:30},day:{day:'2026-09-02',cost:3,lower:true},hits:{n:2,five_hour:1,weekly:1,other:0},interrupted:{n:2,unknown:false,share:10},quota:{percent:26,estimate:true,minutes:10080,harness:'codex'}}));
              assert.ok((all.match(/\.(?=\s|$)/g)||[]).length<=6&&all.includes(sv?'osäker identitet':'ambiguous identity'),all);await full.context.close();
            }
            {// #78 round 7: a hit whose turn is selected counts although the filter matches only a subagent's metadata; the zoomed interval is the period; cost that cannot be determined
              const base=await newPage({locale:T.locale},withLimitHit(fixture)),sub=await base.page.evaluate(()=>{const rows=UsageReport.getSelected().filter(r=>r.prompt===0),parent=rows.find(r=>r.thread_kind!=='subagent'),child=rows.find(r=>r.thread_kind==='subagent'&&r.model!==parent.model);return child&&{model:child.model,parentModel:parent.model,agent:child.agent,date:parent.date}});await base.context.close();
              assert.ok(sub,'the fixture has a subagent with other metadata in the hit turn');const hit=sv?'1 gränsträff':'1 limit hit';
              const sh=await newPage({locale:T.locale},edited(withLimitHit(fixture),d=>{d.limit_hits[0].local_date=sub.date;d.limit_hits[0].scope={project_id:null,session:null,provider:null,model:sub.parentModel,effort:null,agent:null}}));
              await sh.page.selectOption('#model',sub.model);assert.ok((await sh.page.locator('#glance-text').innerText()).includes(hit),'the subagent model filter keeps the parent turn\'s hit');assert.deepEqual(sh.errors,[]);await sh.context.close();
              const zp=await newPage({locale:T.locale},fixture);await zp.page.fill('#from','2026-01-01');await zp.page.fill('#to','2026-12-31');await zp.page.click('[data-gran="hour"]');await zp.page.locator('#chart .bar').first().click();
              const zt=norm(await zp.page.locator('#glance-text').innerText());assert.ok(/^2026-\d\d-\d\d \d\d:\d\d–\d\d:\d\d:/.test(zt)&&!zt.includes('2026-01-01'),'the zoomed interval is the period: '+zt);assert.deepEqual(zp.errors,[]);await zp.context.close();
              const nc=await newPage({locale:T.locale},edited(fixture,d=>{d.columns.price=d.columns.price.map(()=>null)})),nt=norm(await nc.page.locator('#glance-text').innerText());
              assert.ok(nt.includes(sv?'kostnaden till listpris kan inte bestämmas':'the list-price cost cannot be determined')&&!/inget listpris|no list price/.test(nt),nt);assert.deepEqual(nc.errors,[]);await nc.context.close();
            }
            {// unpriced requests: shares are of the priced cost, an entirely unpriced interrupted set says the cost is unknown, and unattributed requests are named
              const turnRows=d=>{const first=d.columns.prompt.find(x=>x!=null);return d.columns.prompt.map((p,i)=>p===first?i:-1).filter(i=>i>=0)};
              const un=await newPage({locale:T.locale},edited(fixture,d=>{for(const i of turnRows(d)){d.columns.interrupted[i]=1;d.columns.price[i]=null}})),ut=norm(await un.page.locator('#glance-text').innerText());
              assert.ok(ut.includes(sv?'(kostnaden okänd)':'(cost unknown)')&&!/0[.,]0 ?%/.test(ut),ut);assert.deepEqual(un.errors,[]);await un.context.close();
              const pp=await newPage({locale:T.locale},fixture),pt=norm(await pp.page.evaluate(()=>UsageReport.glanceText({requests:50,turns:20,unattributed:0,from:'2026-09-01',to:'2026-09-02',days:2,cost:5,lower:false,unpriced:true,top:{k:10,share:30},interrupted:{n:2,unknown:false,share:10}})));
              assert.ok(pt.includes(sv?'av den prissatta kostnaden':'of the priced cost')&&pt.split(sv?'prissatta':'priced').length===3,pt);assert.deepEqual(pp.errors,[]);await pp.context.close();
              const nq=await newPage({locale:T.locale},edited(withTopShare(fixture),d=>{d.columns.price=d.columns.price.map(()=>null)})),nqt=norm(await nq.page.locator('#glance-text').innerText());
              assert.ok(!/costliest turn used|dyraste turen använde/.test(nqt),'no quota sentence without a cost ranking: '+nqt);assert.deepEqual(nq.errors,[]);await nq.context.close();
              assert.ok(pt.includes(sv?'av den prissatta kostnaden':'of the priced cost'),pt);assert.deepEqual(pp.errors,[]);await pp.context.close();
              const na=await newPage({locale:T.locale},edited(fixture,d=>{d.columns.prompt[d.columns.prompt.findIndex(x=>x!=null)]=null})),nt=norm(await na.page.locator('#glance-text').innerText());
              assert.ok(nt.includes(sv?'utan tur':'not attributed to a turn')&&nt.includes(sv?'identifierade':'identified'),nt);assert.ok(/(?:identifierade|identified)/.test(t1),'the baseline fixture already has unattributed requests: '+t1);assert.deepEqual(na.errors,[]);await na.context.close();
            }
            const h=await newPage({locale:T.locale},withLimitHit(fixture));assert.ok((await h.page.locator('#glance-text').innerText()).includes(sv?'1 gränsträff i perioden: 1 mot 5-timmarsgränsen.':'1 limit hit in the period: 1 five-hour.'));
            const q=await newPage({locale:T.locale},withTopShare(fixture)),qt=norm(await q.page.locator('#glance-text').innerText());assert.ok(qt.includes(sv?'~26 % av veckogränsen för Codex':'~26% of the weekly Codex limit'),qt);assert.deepEqual(q.errors,[]);await q.context.close();
            {  // #116: automatic shares name their evidence; one above a whole window is unknown, never a number
              const auto=(extra)=>edited(fixture,d=>{d.quota_shares=Object.fromEntries([0,1,2,3,4,5,6,7,8,9].map(i=>[i,{harness:'claude',minutes:10080,label:'auto-calibrated',percent:4,shared_with:null,date:'2026-10-03',lower_bound:false,source:'limit_hit',hits:0,readings:0,unfit:false,...extra}]))});
              const lim=sv?'veckogränsen för Claude':'the weekly Claude limit',from=sv?'uppskattad från':'estimated from';
              const cases=[[{hits:5,source:'limit_hit'},sv?`≈ 4 % av ${lim} (${from} 5 gränsträffar)`:`≈ 4% of ${lim} (${from} 5 limit hits)`],
                [{hits:1,source:'limit_hit'},sv?`≈ 4 % av ${lim} (${from} 1 gränsträff)`:`≈ 4% of ${lim} (${from} 1 limit hit)`],
                [{readings:12,source:'statusline'},sv?`≈ 4 % av ${lim} (${from} 12 statusradsavläsningar)`:`≈ 4% of ${lim} (${from} 12 statusline readings)`],
                [{hits:1,readings:12,source:'limit_hit+statusline'},sv?`≈ 4 % av ${lim} (${from} 1 gränsträff och 12 statusradsavläsningar)`:`≈ 4% of ${lim} (${from} 1 limit hit and 12 statusline readings)`],
                [{percent:null,unfit:true,hits:3,source:'limit_hit'},sv?'andel okänd: den automatiska uppskattningen passar inte den här turen':'share unknown: the automatic estimate does not fit this turn']];
              for(const [extra,want] of cases){const a=await newPage({locale:T.locale},auto(extra)),got=norm(await a.page.locator('#top-prompts .qs').first().innerText());assert.equal(got,want);assert.deepEqual(a.errors,[]);await a.context.close()}
            }
            const qc=await newPage({locale:T.locale},withTopShare(fixture,'estimate',8,'claude')),qct=norm(await qc.page.locator('#glance-text').innerText()),cardt=norm(await qc.page.locator('#top-prompts .qs').first().innerText()),claudeLimit=sv?'veckogränsen för Claude':'the weekly Claude limit';assert.ok(qct.includes(claudeLimit)&&cardt.includes(claudeLimit)&&!/Claude Code/.test(qct+cardt),'#115: summary and card name the same Claude limit: '+qct+' | '+cardt);assert.deepEqual(qc.errors,[]);await qc.context.close();
          }
          const none=await newPage({locale:T.locale},fixture);assert.equal(await none.page.locator('#limit-hits').isVisible(),false);assert.deepEqual(none.errors,[]);await none.context.close();
        }
        // The toast is pure DOM: it works inside a sandboxed iframe (no top navigation, no popups).
        const outer=await browser.newContext({viewport:{width:1440,height:1080},offline:true,locale:T.locale});const op=await outer.newPage();
        await op.setContent('<iframe id="f" style="width:1400px;height:1000px" sandbox="allow-scripts allow-downloads allow-modals" srcdoc="'+withDemo(fixture).replace(/&/g,'&amp;').replace(/"/g,'&quot;')+'"></iframe>');
        const fh=await op.waitForSelector('#f');const fr=await fh.contentFrame();
        for(let i=0;i<1200&&!(await fr.evaluate(()=>window.reportReady===true).catch(()=>false));i++)await op.waitForTimeout(50);
        await fr.locator('#top-prompts .rs a').click();await fr.locator('#toast.show').waitFor({timeout:5000});
        assert.equal((await fr.locator('#toast').innerText()).trim(),RS.toast('Codex'));await outer.close();
      }
      {
        // Section order (#73): the costliest turns come right after the totals, then cost facts and energy; turns are numbered 01
        {const pos=await p2.evaluate(()=>['prompts','cost-facts','energy','sessions','coverage'].map(id=>document.getElementById(id).getBoundingClientRect().top+window.scrollY));
         assert.ok(pos.every((v,i)=>i===0||pos[i-1]<v),'section order prompts < cost facts < energy < sessions < coverage: '+pos);
         const e4=norm(await p2.locator('#prompts [data-t="e4"]').innerText());assert.ok(e4.startsWith('01'),'turns eyebrow: '+e4);}
        // Energy card (#61): an order-of-magnitude estimate that follows the filters, with its range, the unweighted count and the proxy note
        const e=await p2.evaluate(()=>({title:document.querySelector('#energy h2').textContent,value:document.getElementById('energy-value').textContent,range:document.getElementById('energy-range').textContent,unw:document.getElementById('energy-unweighted').textContent,proxy:document.getElementById('energy-proxy').textContent,calc:UsageReport.energyOf(UsageReport.getSelected())}));
        assert.equal(norm(e.title),T.lang==='sv'?'Energi (uppskattning)':'Energy (estimate)');
        assert.ok(e.value.includes('~')&&e.value!=='—','energy value: '+e.value);
        assert.ok(e.range.includes('÷3')&&e.range.includes('×3'),'energy range: '+e.range);
        assert.ok(e.calc.mid>0&&e.calc.unweighted>0&&norm(e.unw).length>0,'unweighted requests are counted and shown: '+e.unw);
        assert.ok(e.proxy.includes('README'),'proxy note: '+e.proxy);
        assert.equal(await p2.locator('#cost-facts article[data-fact="energy"]').count(),0,'energy is not shown as a cost fact');
        // parity: the page's computation over all rows equals the Python energy fact for all history (same rows, constants and factors)
        const par=await p2.evaluate(()=>{const e=UsageReport.energyOf(UsageReport.all),f=UsageReport.data.insights.windows.find(w=>w.id==='all').facts.find(x=>x.id==='energy').values;return {page:e.mid,py:f.mid_mwh,rows:[e.rows,f.requests],unw:[e.unweighted,f.unweighted_requests],inc:[e.incomplete,f.lower_bound_requests],parts:f.parts.map(x=>[x.part,e.parts[x.part],x.mwh])}});
        assert.ok(Math.abs(par.page-par.py)<=1e-9*Math.max(1,par.py),'page '+par.page+' vs python '+par.py);assert.deepEqual(par.rows[0],par.rows[1]);assert.deepEqual(par.unw[0],par.unw[1]);
        assert.deepEqual(par.inc[0],par.inc[1]);assert.ok(par.inc[0]>0,'the fixture has an incomplete row');for(const [k,pg,py] of par.parts)assert.ok(Math.abs(pg-py)<=1e-9*Math.max(1,py),k+': page '+pg+' vs python '+py);
      }
      {
        const I=INS[T.lang],money=async()=>{const f=(await facts(p2)).find(x=>x.id==='model_share');return {ids:(await facts(p2)).map(x=>x.id),total:norm(f.rows.find(r=>r[0]===(T.lang==='sv'?'Prissatt kostnad':'Priced cost'))[1]),unpriced:norm(f.rows.at(-1)[1])}};
        const first=(await facts(p2)).find(x=>x.id==='model_share').assumptions.join(' | ');
        assert.ok(first.includes(I.spd)&&first.includes(I.tier)&&first.includes(I.table),'pricing assumptions with counts and the table date: '+first);assert.ok(!first.includes(I.lower),'the 30-day window has no incomplete request');
        {const cf=(await facts(p2)).find(x=>x.id==='credits');assert.ok(cf,'credits fact');
         assert.ok(cf.rows.some(r=>norm(r[1]).includes(T.creditFact)),'credit equivalent: '+JSON.stringify(cf.rows));
         const at=cf.assumptions.join(' | ');for(const x of (T.lang==='sv'?['motsvarar','inte vad som dragits','standardhastighet','äldre kreditprislista','dollar']:['corresponds to','not what was drawn','standard speed','legacy rate card','not dollars']))assert.ok(at.includes(x),'credits assumption '+x+': '+at);
         await p2.locator('#cost-facts article[data-fact="credits"]').screenshot({path:path.join(screenshotDir,'credits-fact-'+T.lang+'.png')})}
        const a=await money();assert.deepEqual(a.ids,['model_share','price_comparison','cost_parts','context_size','credits']);assert.equal(a.total,I.total);assert.equal(a.unpriced,I.unpriced);
        {const per=norm(await p2.locator('#ins-period').innerText());assert.ok(per.includes('2026-08-21 – 2026-09-20'));assert.ok(!per.includes(I.left),'no exclusion note without excluded requests: '+per)}
        await p2.click('#cost-facts [data-win="all"]');
        const b=await money();assert.deepEqual(b.ids,['model_share','price_comparison','cost_parts','context_size','long_context_premium','subagent_share','credits']);assert.equal(b.total,I.totalAll,'the window toggle switches the values');
        {const ms=(await facts(p2)).find(x=>x.id==='model_share'),txt=ms.assumptions.join(' | ');assert.ok(txt.includes(I.lower)&&txt.includes(I.left),'lower bound and left-out disclosures: '+txt);assert.ok(ms.rows.some(r=>r[1].startsWith('≥')),'amounts marked as lower bounds');
         const ctx=(await facts(p2)).find(x=>x.id==='context_size');assert.ok(ctx.rows[0][1].includes('≥'));
         const lc=(await facts(p2)).find(x=>x.id==='long_context_premium');assert.ok(!lc.assumptions.join(' | ').includes(I.cplt),'the incomplete request is of a model without a long-context tier: nothing to leave out: '+lc.assumptions.join(' | '));assert.ok(!lc.assumptions.join(' | ').includes(I.lower));assert.ok(lc.rows.every(r=>!r[1].includes('≥')),'exact, not a lower bound: '+JSON.stringify(lc.rows))}
        assert.equal(await p2.locator('#cost-facts [data-win="all"]').getAttribute('aria-pressed'),'true');assert.equal(await p2.locator('#cost-facts [data-win="30d"]').getAttribute('aria-pressed'),'false');
        {const per=norm(await p2.locator('#ins-period').innerText());assert.ok(per.includes(T.lang==='sv'?'första anropet – 2026-09-20':'the first request – 2026-09-20'));assert.ok(per.includes(I.left),'the window header discloses left-out requests even without facts: '+per)}
        const all=await facts(p2);assert.deepEqual(all.map(f=>f.prov),all.map(f=>f.id==='context_size'?I.prov[1]:I.prov[0]));assert.ok(all.every(f=>f.markup===0));
        // price ladder: the same tokens at every same-provider model, cost descending, the model used marked, no 'cheapest' framing
        const cmp=all.find(f=>f.id==='price_comparison'),marker=T.lang==='sv'?'(använd modell)':'(model used)',costs=cmp.rows.map(r=>parseFloat(norm(r[1]).replace('$','').replace(/\s/g,'').replace(',','.')));
        assert.ok(cmp.rows.length>=2);assert.equal(cmp.rows.filter(r=>r[0].endsWith(marker)).length,1);assert.deepEqual(costs,[...costs].sort((x,y)=>y-x),'cost descending');
        assert.ok(!/cheap|saving|billig|besparing/i.test(await p2.locator('[data-fact="price_comparison"]').innerText()));
        assert.equal(await p2.locator('[data-fact="price_comparison"] h3 span').first().innerText(),T.lang==='sv'?'Samma tokens till listpris för andra modeller från samma leverantör':"The same tokens at other models' list prices (same provider)");
        await p2.locator('#cost-facts').screenshot({path:path.join(screenshotDir,'energy-report-costfacts-'+T.lang+'.png')});
        await p2.click('[data-lang="'+(T.lang==='sv'?'en':'sv')+'"]');assert.equal(await p2.locator('#cost-facts h2').innerText(),INS[T.lang==='sv'?'en':'sv'].title);assert.equal(await p2.locator('#cost-facts [data-win="all"]').getAttribute('aria-pressed'),'true','the window survives a language switch');
      }
      {
        // Mobile (#80): the costliest turns become cards at 390 px (cost and prompt text on screen, no sideways scroll); the desktop table is unchanged.
        await p2.setViewportSize({width:390,height:844});
        const m=await p2.evaluate(()=>{const box=document.getElementById('top-prompts'),w=box.querySelector('.table-wrap'),W=window.innerWidth,over=e=>e.scrollWidth<=e.clientWidth+1,fit=e=>e.getBoundingClientRect().right<=W&&over(e);
          return {box:over(box),wrap:over(w),doc:document.documentElement.scrollWidth<=W+1,rows:box.querySelectorAll('tr.prompt-row').length,costs:[...box.querySelectorAll('tr.prompt-row td:last-child')].map(fit),texts:[...box.querySelectorAll('tr.prompt-text td')].map(fit),labels:[...box.querySelectorAll('tr.prompt-row')].every(r=>[...r.children].every(c=>c.dataset.label))}});
        assert.ok(m.rows>0&&m.costs.length===m.rows&&m.texts.length>0,'prompt rows with texts at 390 px');
        assert.ok(m.box&&m.wrap&&m.doc,'no horizontal overflow at 390 px: '+JSON.stringify(m));assert.ok(m.costs.every(Boolean),'every cost cell is on screen at 390 px');assert.ok(m.texts.every(Boolean),'every prompt text fits at 390 px');assert.ok(m.labels,'cells carry their localized column label');
        assert.equal(await p2.evaluate(()=>getComputedStyle(document.querySelector('#top-prompts tr.prompt-row')).borderTopWidth),'0px','no rule above the first card');
        await p2.locator('#prompts').screenshot({path:path.join(screenshotDir,'energy-report-prompts-390-'+T.lang+'.png')});
        // A narrow printed page keeps the table: the cards are a screen layout only.
        await p2.emulateMedia({media:'print'});
        assert.ok((await p2.evaluate(()=>[...document.querySelectorAll('#top-prompts th')].map(h=>getComputedStyle(h).display))).every(x=>x!=='none'),'table header kept in print at 390 px');
        await p2.emulateMedia({media:'screen'});
        await p2.setViewportSize({width:1366,height:900});
        const d=await p2.evaluate(()=>[...document.querySelectorAll('#top-prompts th')].map(h=>getComputedStyle(h).display));
        assert.equal(d.length,10);assert.ok(d.every(x=>x!=='none'),'table header visible on desktop: '+d);
        await p2.setViewportSize({width:1440,height:1080});
      }
      assert.deepEqual(errors2,[]);await c2.close();
    }
    if(sharedFixture){
      // Shared: no side-file data at all (the Inputs column is all unknown); no details block, no context text of any kind.
      const {context:c3,page:p3,errors:errors3}=await newPage({locale:T.locale},sharedFixture);
      const shared=await p3.evaluate(()=>({rows:[...document.querySelectorAll('#top-prompts tr.prompt-row')].map(r=>[...r.children].map(c=>c.textContent)),details:document.querySelectorAll('#top-prompts details, #top-prompts tr.prompt-ctx, #top-prompts tr.prompt-text').length,page:document.body.innerText,data:Object.keys(window.UsageReport.data)}));
      assert.deepEqual(shared.rows.map(r=>r[7]),['–','–','–','–']);assert.equal(await p3.locator('#top-prompts .rs, #top-prompts a, #top-prompts code, #top-prompts button').count(),0,'shared: no resume links, commands or buttons');assert.ok(!shared.data.includes('prompt_resume')&&!shared.data.includes('demo'));assert.equal(shared.details,0);assert.equal(await p3.locator('#top-prompts th').nth(7).innerText(),T.inputs);
      for(const x of ['feat/x','Fix the','pipeline','all green','example.test','#16','Add lint','claude --resume','codex resume','codex://','019a1b2c'])assert.ok(!shared.page.includes(x),'shared page must not show '+x);
      assert.ok(!shared.data.includes('prompt_inputs')&&!shared.data.includes('prompt_context')&&!shared.data.includes('prompt_texts'));
      {
        const card=await p3.locator('#cost-facts').innerText();for(const x of ['mystery','/w/','secret','s1'])assert.ok(!card.includes(x),'shared cost facts must not show '+x);
        await p3.click('#cost-facts [data-win="all"]');assert.ok((await facts(p3)).length>=5);
      }
      assert.deepEqual(errors3,[]);await c3.close();
    }
  }
  try {
    await suite(L.sv);
    await suite(L.en);
    // Toggle: click EN then SV live, without a reload; <html lang>, labels and number formats follow.
    {
      const {context,page,errors}=await newPage({locale:'sv-SE'},smoke);
      assert.equal(await page.locator('h1').innerText(),L.sv.h1);
      await page.selectOption('#harness',{index:1});
      const before=await page.evaluate(()=>UsageReport.getSelected().length);
      await page.click('[data-lang="en"]');
      assert.equal(await page.locator('h1').innerText(),L.en.h1);
      assert.equal(await page.evaluate(()=>document.documentElement.lang),'en');
      assert.equal(await page.locator('#prompts h2').innerText(),L.en.prompts);
      assert.equal(await page.locator('[data-lang="en"]').getAttribute('aria-pressed'),'true');
      assert.equal(await page.locator('[data-lang="sv"]').getAttribute('aria-pressed'),'false');
      assert.equal(await page.evaluate(()=>UsageReport.short(136500000)),'136.5M');
      assert.equal(await page.locator('#harness option').first().innerText(),'All');
      assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),before,'the filter selection survives a language switch');
      await page.click('[data-lang="sv"]');
      assert.equal(await page.locator('h1').innerText(),L.sv.h1);assert.equal(await page.evaluate(()=>document.documentElement.lang),'sv');
      assert.equal(await page.locator('#harness option').first().innerText(),'Alla');
      assert.deepEqual(errors,[]);await context.close();
    }
    // Explicit payload language beats the browser locale; the toggle still works on top of it.
    {
      const {context,page,errors}=await newPage({locale:'sv-SE'},withLang(smoke,'en'));
      assert.equal(await page.locator('h1').innerText(),L.en.h1);
      await page.click('[data-lang="sv"]');assert.equal(await page.locator('h1').innerText(),L.sv.h1);
      assert.deepEqual(errors,[]);await context.close();
      const other=await newPage({locale:'en-US'},withLang(smoke,'sv'));
      assert.equal(await other.page.locator('h1').innerText(),L.sv.h1);assert.deepEqual(other.errors,[]);await other.context.close();
    }
    // The choice is remembered (file:// origin; storage may legitimately be unavailable, e.g. in WebKit).
    {
      const file=path.join(fs.mkdtempSync(path.join(os.tmpdir(),'tokenatlas-lang-')),'report.html');fs.writeFileSync(file,smoke);
      const context=await browser.newContext({locale:'sv-SE'});const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
      await page.goto('file://'+file);await ready(page,errors);
      await page.click('[data-lang="en"]');
      const stored=await page.evaluate(()=>{try{return localStorage.getItem('tokenatlas-lang')}catch(e){return 'unavailable'}});
      if(stored==='en'){await page.goto('file://'+file);await ready(page,errors);assert.equal(await page.locator('h1').innerText(),L.en.h1,'remembered choice')}
      summary.remembered=stored;assert.deepEqual(errors,[]);await context.close();
    }
    // Storage that throws (blocked cookies, privacy modes) must not break the report or the toggle.
    for(const init of [
      ()=>{Storage.prototype.getItem=function(){throw new Error('blocked')};Storage.prototype.setItem=function(){throw new Error('blocked')}},
      ()=>{Object.defineProperty(window,'localStorage',{configurable:true,get(){throw new DOMException('denied','SecurityError')}})},
    ]){
      const {context,page,errors}=await newPage({locale:'sv-SE'},smoke,init);
      assert.equal(await page.locator('h1').innerText(),L.sv.h1);
      await page.click('[data-lang="en"]');assert.equal(await page.locator('h1').innerText(),L.en.h1);
      assert.equal(await page.evaluate(()=>document.documentElement.lang),'en');
      assert.deepEqual(errors,[]);await context.close();
    }
    console.log(JSON.stringify({pass:true,browser:browserName,prompts_fixture:!!fixture,shared_fixture:!!sharedFixture,...summary,checks:'both languages (summary cards, legend, totals, K/M/B vs mdr/milj., money, cache comparisons, bucket conservation, filters, empty state, zoom, drilldown, export, mobile overflow, prompts card), live toggle, explicit payload language, remembered choice, throwing storage'}));
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
