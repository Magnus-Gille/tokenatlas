/* Synthetic engine/browser contract checks. Run: node test_efficiency_browser.cjs */
'use strict';
const assert = require('node:assert/strict');
const {facts, scenario} = require('./tokenatlas/efficiency.js');

const tokens = (fresh, read, write, output, reasoning=999) => ({fresh_input:fresh, cache_read:read, cache_write:write, output, reasoning});
const rows = [
  {ts:'2026-01-01T00:00:00Z',tokens:tokens(10,2,3,5),prompt:0,project_id:'\x01p001',complete:true,thread_kind:'main'},
  {ts:'2026-01-02T00:00:00Z',tokens:tokens(20,0,0,10),prompt:10,project_id:'\x01p002',complete:true,thread_kind:'main'},
  {ts:'2026-01-02T01:00:00Z',tokens:tokens(20,0,0,10),prompt:2,project_id:'\x01p003',complete:true,thread_kind:'main'},
  {ts:'2026-01-03T00:00:00Z',tokens:tokens(null,1,0,2),prompt:null,project_id:null,complete:false,thread_kind:'unknown'},
  {ts:'2026-01-03T01:00:00Z',tokens:tokens(900,0,0,900),prompt:99,project_id:'\x01p999',complete:true,thread_kind:'main',id_synthetic:true},
  {ts:'2026-01-04T00:00:00Z',tokens:tokens(1000,0,0,1000),prompt:4,project_id:'\x01p004',complete:true,thread_kind:'main'},
  {ts:'2025-12-31T00:00:00Z',tokens:tokens(5,0,0,1),prompt:5,project_id:'\x01p001',complete:true,thread_kind:'main'},
  {ts:'2026-01-02T02:00:00Z',tokens:tokens(7,2,0,3),prompt:7,project_id:'\x01p002',complete:true,thread_kind:'subagent',efficiency_auto_review:true},
  {ts:'2026-01-02T03:00:00Z',tokens:tokens(8,2,0,3),prompt:8,project_id:'\x01p002',complete:true,thread_kind:'subagent'},
];
const bundle = facts(rows, {start:'2026-01-01T00:00:00+00:00',end:'2026-01-04T00:00:00+00:00',snapshot:'2026-01-03T12:00:00+00:00',timezone:'UTC',top_n:2,context_threshold:10,contributor_limit:3,filters:{model:true,search:true}});
assert.equal(bundle.schema_version,1);
assert.deepEqual(bundle.window,{start:'2026-01-01T00:00:00.000Z',end:'2026-01-04T00:00:00.000Z',snapshot:'2026-01-03T12:00:00.000Z',timezone:'UTC',partial:true});
assert.equal(bundle.coverage.requests,6);
assert.equal(bundle.coverage.synthetic_excluded,1);
assert.equal(bundle.coverage.incomplete_requests,1);
assert.equal(bundle.coverage.unlinked_requests,1);
assert.equal(bundle.totals.known_tokens,108);
assert.equal(bundle.totals.tokens.reasoning,undefined);
assert.equal(bundle.facts[0].values.top_turns[0].turn,2,'numeric ordinal breaks equal-size ranking ties');
assert.equal(bundle.facts[1].values.large.known_input,65);
assert.equal(bundle.facts[2].values.auto_review.requests,1);
assert.equal(bundle.facts[2].values.subagent.requests,1);
assert.equal(bundle.facts[3].values.previous.known_tokens,6);
assert.equal(bundle.facts[3].numerator,102);
assert.deepEqual(bundle.filters,{model:true,search:true});
assert.equal(JSON.stringify(bundle).includes('session-a'),false);
const projection = scenario(bundle,'subagent_input',50);
assert.equal(projection.input_tokens,10);
assert.equal(projection.hypothetical_reduction,5);
assert.equal(projection.semantics,'hypothetical_input_reduction_not_savings');
assert.throws(()=>scenario(bundle,'output',10),TypeError);
assert.throws(()=>scenario(bundle,'subagent_input',101),TypeError);
assert.throws(()=>facts(rows,{start:'2026-01-02T00:00:00Z',end:'2026-01-01T00:00:00Z',snapshot:'2026-01-03T00:00:00Z'}),TypeError);
console.log('efficiency synthetic checks passed');
if (process.argv[2]) {
  (async () => {
    const fs=require('node:fs'),path=require('node:path');
    const playwright=require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
    const browser=await playwright[process.env.BROWSER || 'chromium'].launch({headless:true});
    try {
      for(const [lang,width] of [['sv',1440],['en',390]]){
        const context=await browser.newContext({viewport:{width,height:1000},offline:true,acceptDownloads:true});
        const page=await context.newPage(),errors=[];let network=0;
        page.on('pageerror',e=>errors.push(e.message));
        await context.route('**/*',route=>{if(route.request().url().startsWith('file:'))return route.continue();network++;return route.abort();});
        await page.setContent(fs.readFileSync(path.resolve(process.argv[2]),'utf8'),{waitUntil:'load'});
        await page.waitForFunction(()=>window.reportReady || window.reportError);
        assert.equal(await page.evaluate(()=>window.reportReady),true);
        assert.equal(await page.evaluate(()=>UsageReport.all.every((r,i)=>
          r.efficiency_auto_review===!!UsageReport.data.analytics_metadata.efficiency.auto_review[i] &&
          r.efficiency_rolled_up===!!UsageReport.data.analytics_metadata.efficiency.rolled_up[i] &&
          r.efficiency_project_id===UsageReport.data.columns.dict.project_id[UsageReport.data.columns.idx.project_id[i]])),true,
          'expanded rows must preserve privacy-safe role metadata and canonical project aliases');
        await page.locator('[data-lang='+lang+']').click();
        assert.equal(await page.locator('#eff-cards article').count(),4);
        assert.equal(await page.locator('#eff-percent').inputValue(),'');
        assert.equal(await page.evaluate(()=>UsageReport.efficiencyScenario),null);
        const b=await page.evaluate(()=>UsageReport.efficiencyBundle);
        assert.equal(b.schema_version,1);
        assert.ok(b.previous_coverage);
        assert.equal((await page.locator('#token-efficiency').innerText()).includes('NaN'),false);
        await page.locator('#eff-cards > article > details').first().locator('summary').first().click();
        await page.locator('#eff-population').selectOption('subagent_input');
        await page.locator('#eff-calculate').click();
        assert.equal(await page.evaluate(()=>UsageReport.efficiencyScenario),null,'blank percentage must not imply zero');
        await page.locator('#eff-percent').fill('25');
        await page.locator('#eff-calculate').click();
        const scenario=await page.evaluate(()=>UsageReport.efficiencyScenario);
        assert.equal(scenario.percent,25);
        assert.equal(scenario.hypothetical_reduction,b.scenarios.populations.subagent_input.known_input/4);
        const download=page.waitForEvent('download');await page.locator('#eff-export').click();
        const file=await (await download).path(),exported=JSON.parse(fs.readFileSync(file,'utf8'));
        assert.deepEqual(exported.bundle,b);
        const secrets=await page.evaluate(()=>UsageReport.all.flatMap(r=>[r.session,r.project_label]).filter(v=>typeof v==='string'&&v.length>12&&!/^Project |^Projekt /.test(v)));
        const text=JSON.stringify(exported);for(const secret of secrets)assert.ok(!text.includes(secret),'aggregate export must omit raw identities and private labels');
        await page.locator('#eff-threshold').fill('500000');await page.locator('#eff-refresh').click();
        assert.equal(await page.evaluate(()=>UsageReport.efficiencyBundle.settings.context_threshold),500000);
        assert.equal(await page.evaluate(()=>UsageReport.efficiencyScenario),null,'changing evidence invalidates scenario');
        if(await page.locator('#model option').count()>1){await page.locator('#model').selectOption({index:1});assert.equal(await page.evaluate(()=>UsageReport.efficiencyBundle.filters.model),true);}
        await page.locator('#reset').click();await page.locator('[data-gran=hour]').click();
        if(await page.locator('#chart .bar').count()){
          await page.locator('#chart .bar').first().click();
          const zoom=await page.evaluate(()=>({bundle:UsageReport.efficiencyBundle,requests:UsageReport.getSelected().filter(r=>!r.id_synthetic).length}));
          assert.equal(zoom.bundle.filters.zoom,true);
          assert.equal(zoom.bundle.coverage.requests,zoom.requests,'efficiency must follow the exact chart zoom');
          assert.ok(Date.parse(zoom.bundle.window.end)-Date.parse(zoom.bundle.window.start)<=3600000);
          await page.locator('#zoomout').click();
        }
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,'mobile/desktop overflow');
        assert.deepEqual(errors,[]);assert.equal(network,0);
        if(process.env.SCREENSHOT_DIR)await page.locator('#token-efficiency').screenshot({path:path.join(process.env.SCREENSHOT_DIR,'efficiency-'+lang+'-'+(process.env.BROWSER||'chromium')+'.png')});
        await context.close();
      }
    } finally {await browser.close();}
    console.log('efficiency report UI passed: Swedish/English, desktop/mobile, explicit scenarios, export privacy, filter invalidation, offline');
  })().catch(error=>{console.error(error);process.exitCode=1;});
}
