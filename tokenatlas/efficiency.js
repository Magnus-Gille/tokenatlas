/* Numeric contract shared with efficiency.py; exact parity fixtures run in CI. */
(function (root) {
  'use strict';
  const KEYS = ['fresh_input', 'cache_read', 'cache_write', 'output'];
  const INPUT = KEYS.slice(0, 3);
  const SEMANTICS = 'hypothetical_input_reduction_not_savings';
  const FILTERS = ['harness','provider','model','effort','project_id','session','agent','thread_kind','search','zoom'];
  function instant(value) {
    if (typeof value !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(value) || !Number.isFinite(Date.parse(value))) throw new TypeError('Offset ISO timestamp required');
    return Date.parse(value);
  }
  const iso = value => new Date(value).toISOString();
  function integer(value, name, minimum, maximum = Number.MAX_SAFE_INTEGER) {
    if (!Number.isInteger(value) || value < minimum || value > maximum) throw new TypeError('Invalid '+name);
    return value;
  }
  function clean(row) {
    if (!row || typeof row !== 'object') throw new TypeError('Invalid row');
    const tokens = Object.fromEntries(KEYS.map(k => {
      const v = row.tokens && row.tokens[k];
      return [k, Number.isInteger(v) && v >= 0 ? v : null];
    }));
    const prompt = row.prompt == null ? null : integer(row.prompt, 'prompt', 0);
    const project = row.efficiency_project_id === undefined ? row.project_id : row.efficiency_project_id;
    const match = typeof project === 'string' && /^(?:\x01p|Project )(\d{3,})$/.exec(project);
    return {ts:instant(row.ts), tokens, prompt, project:match ? 'Project '+match[1] : null,
      complete:row.complete === true, synthetic:row.id_synthetic === true,
      kind:['main','subagent','automation'].includes(row.thread_kind) ? row.thread_kind : 'other',
      derived:row.turn_confidence === 'derived', auto:row.efficiency_auto_review === true, rolled:row.efficiency_rolled_up === true};
  }
  const tokenValue = r => KEYS.reduce((n,k) => n+(r.tokens[k] || 0),0);
  const inputValue = r => INPUT.reduce((n,k) => n+(r.tokens[k] || 0),0);
  const completeRow = r => r.complete && KEYS.every(k => r.tokens[k] !== null);
  function summary(rows, synthetic = 0) {
    const tokens = Object.fromEntries(KEYS.map(k => [k,0]));
    const missing = Object.fromEntries(KEYS.map(k => [k,0]));
    for (const r of rows) for (const k of KEYS) {tokens[k] += r.tokens[k] || 0; if (r.tokens[k] === null) missing[k]++;}
    return {requests:rows.length,tokens,missing,known_tokens:KEYS.reduce((n,k)=>n+tokens[k],0),known_input:INPUT.reduce((n,k)=>n+tokens[k],0),complete:rows.length>0 && !synthetic && rows.every(completeRow)};
  }
  function coverage(rows, synthetic) {
    return {requests:rows.length,synthetic_excluded:synthetic,incomplete_requests:rows.filter(r=>!completeRow(r)).length,
      unlinked_requests:rows.filter(r=>r.prompt===null).length,rolled_up_requests:rows.filter(r=>r.rolled).length,derived_requests:rows.filter(r=>r.derived).length};
  }
  function groups(rows) {
    const map = new Map();
    for (const r of rows) if (r.prompt!==null) {if (!map.has(r.prompt)) map.set(r.prompt,[]); map.get(r.prompt).push(r);}
    return [...map].map(([turn,rs])=>({turn,...summary(rs)}));
  }
  const ranked = (rows, key='known_tokens') => groups(rows).sort((a,b)=>b[key]-a[key] || a.turn-b.turn);
  function fact(id,formula,numerator,denominator,complete,values) {
    return {id,formula,unit:'tokens',numerator,denominator,share:denominator===0?null:numerator/denominator,complete:!!complete,values,provenance:'computed'};
  }
  function deltas(current, previous, field, limit) {
    const a=new Map(),b=new Map();
    for (const [rows,map] of [[current,a],[previous,b]]) for (const r of rows) if (r[field]!==null) map.set(r[field],(map.get(r[field])||0)+tokenValue(r));
    const result=[];
    for (const key of new Set([...a.keys(),...b.keys()])) {
      const after=a.get(key)||0,before=b.get(key)||0;
      if (after!==before) result.push({[field==='prompt'?'turn':'project']:key,current:after,previous:before,delta:after-before});
    }
    return result.sort((x,y)=>Math.abs(y.delta)-Math.abs(x.delta)||(field==='prompt'?x.turn-y.turn:Number(x.project.slice(8))-Number(y.project.slice(8)))).slice(0,limit);
  }
  function facts(rows, options) {
    if (!Array.isArray(rows) || !options) throw new TypeError('Rows and options required');
    const start=instant(options.start),end=instant(options.end),snapshot=instant(options.snapshot);
    if (end<=start) throw new TypeError('End must be after start');
    const timezone=options.timezone===undefined?'UTC':options.timezone;
    if (typeof timezone!=='string') throw new TypeError('Invalid timezone');
    new Intl.DateTimeFormat('en-US',{timeZone:timezone});
    const topN=integer(options.top_n===undefined?50:options.top_n,'top_n',1,10000);
    const threshold=integer(options.context_threshold===undefined?200000:options.context_threshold,'context_threshold',1);
    const limit=integer(options.contributor_limit===undefined?5:options.contributor_limit,'contributor_limit',1,100);
    const filters={};
    if (options.filters != null && (typeof options.filters!=='object' || Array.isArray(options.filters))) throw new TypeError('Invalid filters');
    for (const key of FILTERS) if (options.filters && typeof options.filters[key]==='boolean') filters[key]=options.filters[key];
    const previousStart=start-(end-start),current=[],previous=[];
    let synthetic=0,previousSynthetic=0;
    for (const raw of rows) {
      const r=clean(raw);
      if (r.ts>snapshot) continue;
      if (start<=r.ts && r.ts<end) {if(r.synthetic) synthetic++; else current.push(r);}
      else if(previousStart<=r.ts && r.ts<start) {if(r.synthetic) previousSynthetic++; else previous.push(r);}
    }
    const overall=summary(current,synthetic),before=summary(previous,previousSynthetic);
    const auto=current.filter(r=>r.auto),work=current.filter(r=>!r.auto),linked=work.filter(r=>r.prompt!==null);
    const top=ranked(work),selected=top.slice(0,topN);
    const concentration=fact('token_concentration','top_n_linked_work_tokens / all_linked_work_tokens',selected.reduce((n,g)=>n+g.known_tokens,0),linked.reduce((n,r)=>n+tokenValue(r),0),linked.length>0 && !synthetic && linked.every(completeRow),
      {top_n:topN,turns:top.length,top_turns:selected.slice(0,limit),unlinked:summary(current.filter(r=>r.prompt===null)),auto_review:summary(auto)});
    const inputRows=current.filter(r=>INPUT.every(k=>r.tokens[k]!==null)),large=inputRows.filter(r=>inputValue(r)>=threshold);
    const sizes=inputRows.map(inputValue).sort((a,b)=>a-b),largeSummary=summary(large);
    const context=fact('context_volume','eligible_known_input / all_known_input',largeSummary.known_input,overall.known_input,current.length>0 && !synthetic && inputRows.length===current.length && current.every(r=>r.complete),
      {threshold,eligible_requests:large.length,excluded_unknown_input:current.length-inputRows.length,median:sizes.length?(sizes[Math.floor((sizes.length-1)/2)]+sizes[Math.floor(sizes.length/2)])/2:null,p90:sizes.length?sizes[Math.ceil(.9*sizes.length)-1]:null,large:largeSummary,top_turns:ranked(large,'known_input').slice(0,limit)});
    const sub=work.filter(r=>r.kind==='subagent'),subSummary=summary(sub);
    const delegation=fact('delegation_volume','subagent_work_tokens / all_known_tokens',subSummary.known_tokens,overall.known_tokens,overall.complete,
      {main:summary(work.filter(r=>r.kind==='main')),subagent:subSummary,auto_review:summary(auto),other:summary(work.filter(r=>!['main','subagent'].includes(r.kind))),top_turns:ranked(sub).slice(0,limit)});
    const change=fact('token_change','current_known_tokens - previous_known_tokens',overall.known_tokens-before.known_tokens,before.known_tokens,overall.complete && before.complete && snapshot>=end,
      {previous:before,projects:deltas(current,previous,'project',limit),turns:deltas(current,previous,'prompt',limit),comparison:'equal_elapsed_windows'});
    return {schema_version:1,window:{start:iso(start),end:iso(end),snapshot:iso(snapshot),timezone,partial:snapshot<end},
      previous_window:{start:iso(previousStart),end:iso(start),partial:snapshot<start},settings:{top_n:topN,context_threshold:threshold,contributor_limit:limit},filters,
      coverage:coverage(current,synthetic),previous_coverage:coverage(previous,previousSynthetic),totals:overall,facts:[concentration,context,delegation,change],
      scenarios:{populations:{large_context_input:largeSummary,subagent_input:subSummary},overlap:true,semantics:SEMANTICS}};
  }
  function scenario(bundle,population,percent) {
    if(!['large_context_input','subagent_input'].includes(population)) throw new TypeError('Invalid scenario population');
    if(!Number.isFinite(percent) || percent<0 || percent>100) throw new TypeError('Percentage must be between 0 and 100');
    const s=bundle.scenarios.populations[population],input=s.known_input;
    return {population,percent,input_tokens:input,hypothetical_reduction:input*percent/100,formula:'known_input * percent / 100',complete:s.complete,overlap:true,semantics:SEMANTICS};
  }
  const api={facts,scenario};root.TokenEfficiency=api;
  if(typeof module!=='undefined' && module.exports) module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:this);
