"""Cost facts: every expected number below is computed by hand from the small synthetic datasets."""
import copy
import json
import unittest
from datetime import datetime, timezone

from tokenatlas import insights, why
from tokenatlas.insights import cost_facts
from tokenatlas.report import build_report
from test_fresh_report import Base, payload
from test_pricing import REF
from test_top_prompts import jl, claude_row, user

M = 1_000_000


def entry(model, inp, cr, out, provider='openai', long=None, modifiers=None, write=None, **kw):
    e = dict(provider=provider, model=model, aliases=[], currency='USD', input=inp, cache_write_5m=None, cache_write_1h=None, cache_write=write,
             cache_read=cr, output=out, free=False, modifiers=modifiers or {}, long_context=long and dict(above_input_tokens=long[0], cache_write=None, **long[1]), **REF)
    return dict(e, **kw)


def lc(above, inp, cr, out):
    return (above, dict(input=inp, cache_read=cr, output=out))


TABLE = {'schema': 1, 'retrieved_on': '2026-09-01', 'unit': 'per_million_tokens', 'provider_aliases': {'openai-codex': 'openai'},
         'local_providers': ['m5'], 'models': [
    entry('gpt-a', 2, .5, 10, long=lc(272000, 4, 1, 15)),
    entry('gpt-b', 1, .25, 8, long=lc(100000, 2, .5, 12)),
    entry('gpt-c', 3, .3, 12),
    entry('gpt-e', 5, 1, 20),
    entry('gpt-f', 6, 1, 30),
    entry('gpt-null', None, None, None),                       # no prices at all: never an alternative
    entry('gpt-eur', 1, 1, 1, currency='EUR'),                  # not USD: never an alternative
    entry('gpt-free', None, None, None, free=True),             # free: never an alternative
    entry('claude-x', 1, 1, 1, provider='anthropic'),           # another provider: never an alternative of an openai model
    entry('gpt-cr', 2, .5, 10, write=3),
    entry('gpt-l', 2, 1, 8, long=lc(1000, 4, 1, 16)),
    entry('gpt-t', 2, 1, 10, modifiers={'service_tier=fast': dict(input=4, cache_read=2, cache_write=None, output=20),
                                         'service_tier=flex': dict(input=1, cache_read=.5, cache_write=None, output=5)}),
] + [entry(f'm{n}', 1, 1, 10) for n in range(1, 8)]}


def ob(i, ts='2026-09-03T10:00:00+00:00', model='gpt-a', provider='openai', harness='codex', fresh=0, read=0, write=0, out=0,
       session='s', turn=None, kind='main', tariff=None, project='/w/secretapp'):
    return {'id': i, 'harness': harness, 'session': session, 'agent': 'main', 'thread_kind': kind, 'parent_session': session if kind == 'subagent' else None,
            'turn_id': turn, 'turn_confidence': 'derived', 'ts': ts, 'model': model, 'provider': provider, 'machine': 'm1',
            'project_id': project, 'project_label': 'secretapp', 'effort': None, 'origin': 'cli', 'raw_usage': {}, 'tariff': tariff,
            'tokens': dict(fresh_input=fresh, cache_write=write, cache_read=read, output=out, reasoning=0),
            'complete': True, 'id_synthetic': False, 'warnings': [], 'sources': []}


def by_id(result):
    return {f['id']: f for f in result['facts']}


# Dataset D: gpt-a o1 (150k fresh, 10k out) and o2 (400k fresh, 20k out), gpt-c c1 (100k fresh), gpt-b b1 (10k fresh), one unpriced.
D = [ob('o1', model='gpt-a', fresh=150000, out=10000), ob('o2', model='gpt-a', fresh=400000, out=20000),
     ob('c1', model='gpt-c', fresh=100000), ob('b1', model='gpt-b', fresh=10000), ob('u1', model='mystery', fresh=5000)]


class Shape(unittest.TestCase):
    def test_nothing_without_data(self):
        self.assertEqual(cost_facts([], TABLE)['facts'], [])

    def test_fact_fields_and_provenance(self):
        facts = cost_facts(D, TABLE)['facts']
        for f in facts:
            self.assertEqual(sorted(f), ['assumption_keys', 'assumptions', 'computation', 'computation_key', 'id', 'params', 'price_assumptions', 'provenance', 'title_key', 'values'])
            self.assertTrue(f['computation'] and f['assumptions'] and all(isinstance(a, str) for a in f['assumptions']))
            self.assertNotIn('{', f['computation'] + ''.join(f['assumptions']), 'every placeholder is filled')
        self.assertEqual({f['id']: f['provenance'] for f in facts}['context_size'], 'measured')
        self.assertEqual({f['id']: f['provenance'] for f in facts}['model_share'], 'computed')

    def test_only_unpriced_has_only_price_free_facts(self):
        res = cost_facts([ob('u', model='mystery', fresh=10, read=0)], TABLE)
        self.assertEqual([f['id'] for f in res['facts']], ['context_size', 'credits', 'energy'])  # energy and credits need no USD price (the credit fact only counts the unrated model)
        self.assertEqual((res['requests'], res['priced_requests'], res['unpriced_requests']), (1, 0, 1))

    def test_no_wording_of_advice(self):
        text = json.dumps(cost_facts(D, TABLE)) + json.dumps({k: v for k, v in insights._texts().items() if k.startswith('ins_')})
        for word in ('should', 'recommend', 'consider', 'we suggest', 'better', 'waste'):
            self.assertNotIn(word, text.lower())

    def test_window_start_inclusive_end_exclusive(self):
        rows = [ob('a', '2026-09-01T00:00:00+00:00', model='m1', out=M // 10), ob('b', '2026-09-02T00:00:00+00:00', model='m2', out=M // 10),
                ob('c', '2026-09-03T00:00:00+00:00', model='m3', out=M // 10)]
        res = cost_facts(rows, TABLE, start=datetime(2026, 9, 2, tzinfo=timezone.utc), end=datetime(2026, 9, 3, tzinfo=timezone.utc))
        self.assertEqual(res['requests'], 1)
        self.assertEqual([m['name'] for m in by_id(res)['model_share']['values']['models']], ['m2'])
        self.assertEqual(res['window'], {'start': '2026-09-02T00:00:00+00:00', 'end': '2026-09-03T00:00:00+00:00'})


class F1ModelShare(unittest.TestCase):
    def test_shares_and_unpriced_scope(self):
        v = by_id(cost_facts(D, TABLE))['model_share']['values']
        # gpt-a 0.4 + 1.9 = 2.3; gpt-c 0.3; gpt-b 0.01; total 2.61
        self.assertEqual([m['name'] for m in v['models']], ['gpt-a', 'gpt-c', 'gpt-b'])
        self.assertAlmostEqual(v['models'][0]['cost'], 2.3, 9)
        self.assertAlmostEqual(v['models'][0]['share'], 2.3 / 2.61, 9)
        self.assertEqual(v['models'][0]['priced_requests'], 2)
        self.assertIsNone(v['other'])
        self.assertAlmostEqual(v['priced_cost'], 2.61, 9)
        self.assertEqual((v['requests'], v['priced_requests'], v['unpriced_requests'], v['unpriced_share']), (5, 4, 1, 0.2))

    def test_top_five_plus_other(self):
        rows = [ob(f'r{n}', model=f'm{n}', out=n * 100000) for n in range(1, 8)]  # m<n> costs exactly $n
        v = by_id(cost_facts(rows, TABLE))['model_share']['values']
        self.assertEqual([(m['name'], m['cost']) for m in v['models']], [('m7', 7.0), ('m6', 6.0), ('m5', 5.0), ('m4', 4.0), ('m3', 3.0)])
        self.assertEqual((v['other']['models'], v['other']['cost'], v['other']['share'], v['other']['priced_requests']), (2, 3.0, 3 / 28, 2))
        self.assertEqual(v['priced_cost'], 28.0)

    def test_non_usd_and_local_are_unpriced(self):
        rows = [ob('a', model='m1', out=M), ob('e', model='gpt-eur', fresh=M), ob('l', model='x', provider='m5', fresh=M)]
        v = by_id(cost_facts(rows, TABLE))['model_share']['values']
        self.assertEqual((v['priced_requests'], v['unpriced_requests'], v['priced_cost']), (1, 2, 10.0))


F2_TABLE = dict(TABLE, models=[e for e in TABLE['models'] if e['model'].startswith('gpt-') and e['model'] not in ('gpt-cr', 'gpt-l', 'gpt-t')] + [e for e in TABLE['models'] if e['model'] == 'claude-x'])


class F2PriceLadder(unittest.TestCase):
    def test_ladder_includes_the_actual_model_sorted_by_cost_descending(self):
        f = by_id(cost_facts(D, F2_TABLE))['price_comparison']
        models = {m['name']: m for m in f['values']['models']}
        self.assertEqual(sorted(models), ['gpt-a', 'gpt-c'])  # gpt-b has 0.38% of priced cost: below 10%
        a = models['gpt-a']
        self.assertAlmostEqual(a['cost'], 2.3, 9)
        # o1 150k/10k, o2 400k/20k. gpt-f 4.2 (.9+.3 + 2.4+.6); gpt-e 3.35; gpt-a 2.3 (actual); gpt-c 2.01; gpt-b 1.46 (long above 100k, both long).
        # gpt-null/-eur/-free and claude-x (another provider) cannot be in the ladder.
        self.assertEqual([(x['name'], x['actual']) for x in a['ladder']], [('gpt-f', False), ('gpt-e', False), ('gpt-a', True), ('gpt-c', False), ('gpt-b', False)])
        for x, total in zip(a['ladder'], (4.2, 3.35, 2.3, 2.01, 1.46)):
            self.assertAlmostEqual(x['cost'], total, 9)
        self.assertEqual(a['ladder_models'], 5)
        # c1 is 100000 input: exactly gpt-b's threshold, which is not exceeded. f .6, e .5, c .3 (actual), a .2, b .1
        c = models['gpt-c']
        self.assertEqual([(x['name'], round(x['cost'], 9), x['actual']) for x in c['ladder']],
                         [('gpt-f', .6, False), ('gpt-e', .5, False), ('gpt-c', .3, True), ('gpt-a', .2, False), ('gpt-b', .1, False)])

    def test_neutral_wording_and_assumption_text(self):
        f = by_id(cost_facts(D, F2_TABLE))['price_comparison']
        self.assertIn('same token counts; output quality and token counts of another model are not measured', ' '.join(f['assumptions']))
        self.assertEqual(insights._texts()['ins_price_comparison'], "The same tokens at other models' list prices (same provider)")
        text = (f['computation'] + ' '.join(f['assumptions']) + insights._texts()['ins_price_comparison']).lower()
        for word in ('cheap', 'saving', 'save', 'switch', 'instead'):
            self.assertNotIn(word, text)

    def test_alias_of_the_current_model_is_the_actual_entry(self):
        table = copy.deepcopy(F2_TABLE)
        table['models'][1]['aliases'] = ['gpt-b-alias']
        m = by_id(cost_facts([ob('x', model='gpt-b-alias', fresh=M)], table))['price_comparison']['values']['models'][0]
        self.assertEqual([x['name'] for x in m['ladder'] if x['actual']], ['gpt-b'])
        self.assertEqual([x['name'] for x in m['ladder']].count('gpt-b'), 1)

    def test_cap_of_eight_keeps_the_actual_model_and_its_neighbours(self):
        table = dict(TABLE, models=[e for e in TABLE['models'] if e['model'] in ('m1', 'm2', 'm3', 'm4', 'm5', 'm6', 'm7', 'gpt-e', 'gpt-f')])
        m = by_id(cost_facts([ob('x', model='m4', out=M)], table))['price_comparison']['values']['models'][0]
        # costs: gpt-f 30, gpt-e 20, m1..m7 10 each (name order); 9 models, the 8 around m4 (position 6 of 9) are shown
        self.assertEqual(m['ladder_models'], 9)
        self.assertEqual([x['name'] for x in m['ladder']], ['gpt-e', 'm1', 'm2', 'm3', 'm4', 'm5', 'm6', 'm7'])
        self.assertEqual([x['name'] for x in m['ladder'] if x['actual']], ['m4'])

    def test_only_the_actual_model_gives_no_ladder(self):
        only = dict(F2_TABLE, models=[e for e in F2_TABLE['models'] if e['model'] in ('gpt-a', 'gpt-null', 'gpt-eur', 'gpt-free', 'claude-x')])
        self.assertNotIn('price_comparison', by_id(cost_facts(D[:2], only)))
        # a model that cannot price one of the requests (no output price) is left out, the others stay
        table = copy.deepcopy(F2_TABLE)
        table['models'] = [e for e in table['models'] if e['model'] in ('gpt-a', 'gpt-c')] + [entry('gpt-noout', 1, 1, None)]
        m = by_id(cost_facts(D[:2], table))['price_comparison']['values']['models'][0]
        self.assertEqual([x['name'] for x in m['ladder']], ['gpt-a', 'gpt-c'])


def random_rows(rnd, n):
    models = [('anthropic', 'claude-x'), ('anthropic', 'claude-long'), ('anthropic', 'claude-long2'), ('openai', 'gpt-a'), ('openai', 'gpt-b'), ('openai', 'gpt-c'),
              ('openai-codex', 'gpt-a'), ('anthropic', 'mystery')]
    tariffs = [None, {'speed': 'standard'}, {'speed': 'fast'}, {'inference_geo': 'us'}, {'speed': 'fast', 'inference_geo': 'us'}, {'service_tier': 'fast'},
               {'service_tier': 'standard'}, {'service_tier': 'weird'}]
    rows = []
    for i in range(n):
        provider, model = rnd.choice(models)
        size = rnd.choice((0, 50, 1000, 99_999, 100_001, 150_000, 199_999, 200_001, 272_001, 400_000, 600_000))
        fresh, read = rnd.randint(0, size), rnd.randint(0, size)
        write = rnd.choice((0, 0, rnd.randint(0, size)))
        raw = {}
        if provider == 'anthropic' and write and rnd.random() < .7:
            five = rnd.randint(0, write)
            raw = {'cache_creation': {'ephemeral_5m_input_tokens': five, 'ephemeral_1h_input_tokens': write - five + (rnd.random() < .1)}}
        o = ob(f'r{i}', model=model, provider=provider, harness='claude' if provider == 'anthropic' else 'codex', fresh=fresh, read=read, write=write,
               out=rnd.choice((0, 7, 5000, 123_456)), tariff=rnd.choice(tariffs))
        o['raw_usage'] = raw
        if rnd.random() < .03:
            o['tokens']['cache_read'] = None
        rows.append(o)
    return rows


RANDOM_TABLE = {'schema': 1, 'retrieved_on': '2026-09-01', 'unit': 'per_million_tokens', 'provider_aliases': {'openai-codex': 'openai'}, 'local_providers': [], 'models': [
    entry('claude-x', 4, .2, 20, provider='anthropic', cache_write_5m=5, cache_write_1h=8, modifiers={
        'speed=fast': dict(input=20, cache_write_5m=25, cache_write_1h=40, cache_read=1, output=100), 'inference_geo=us': {'multiplier': 1.1}}),
    entry('claude-long', 4, .2, 20, provider='anthropic', cache_write_5m=5, cache_write_1h=8, long=lc(200000, 8, .4, 30),
          modifiers={'speed=fast': dict(input=20, cache_write_5m=25, cache_write_1h=40, cache_read=1, output=100), 'inference_geo=us': {'multiplier': 1.2}}),
    entry('claude-long2', 3, .3, 15, provider='anthropic', cache_write_5m=4, cache_write_1h=6, long=lc(500000, 6, .6, 22), modifiers={'inference_geo=us': {'multiplier': 1.1}}),
    entry('gpt-a', 2, .5, 10, long=lc(272000, 4, 1, 15), modifiers={'service_tier=fast': dict(input=4, cache_read=1, cache_write=None, output=20)}),
    entry('gpt-b', 1, .25, 8, long=lc(100000, 2, .5, 12), modifiers={'service_tier=fast': dict(input=2, cache_read=.5, cache_write=None, output=16)}),
    entry('gpt-c', 3, .3, 12, write=3.5),
]}


class GroupedPricingIsExact(unittest.TestCase):
    def test_grouped_ladder_equals_per_observation_pricing(self):
        import random
        from tokenatlas.pricing import price_observation
        rnd = random.Random(20260930)
        rows = random_rows(rnd, 4000)
        checked = unpriced = skipped = 0
        for provider, model in (('anthropic', 'claude-x'), ('anthropic', 'claude-long'), ('anthropic', 'claude-long2'), ('openai', 'gpt-a'), ('openai', 'gpt-b'), ('openai', 'gpt-c')):
            group = [r for r in rows if (r['provider'], r['model']) in ((provider, model), ('openai-codex', model)) and insights._usd(price_observation(r, RANDOM_TABLE)) is not None]
            self.assertGreater(len(group), 20)
            # the whole group (most alternatives cannot price some odd row) and many small random subsets (where they can)
            for subset in [group] + [rnd.sample(group, 12) for _ in range(150)]:
                got = insights._reprice(subset, provider, model, RANDOM_TABLE)
                for alt in RANDOM_TABLE['models']:
                    if alt['provider'] != provider or alt['model'] == model:
                        continue
                    parts = [insights._usd(price_observation(dict(r, provider=alt['provider'], model=alt['model']), RANDOM_TABLE)) for r in subset]
                    if None in parts:
                        self.assertNotIn(alt['model'], got, (provider, model, alt['model']))
                        skipped += 1
                    else:
                        self.assertAlmostEqual(got[alt['model']] / sum(parts), 1.0, delta=1e-9, msg=(provider, model, alt['model']))
                        checked += 1
        unpriced = sum(insights._usd(price_observation(r, RANDOM_TABLE)) is None for r in rows)
        self.assertGreater(checked, 300)
        self.assertGreater(unpriced, 100)  # unknown models, unknown speeds/tiers and unknown token classes are in the set
        self.assertGreater(skipped, 0)  # some alternative cannot price some request (for example fast mode it has no price for)


class F3CostParts(unittest.TestCase):
    def test_parts_and_shares(self):
        # input 1M x 2 = 2, cache write 1M x 3 = 3, cache read 2M x .5 = 1, output 400k x 10 = 4; total 10
        v = by_id(cost_facts([ob('p', model='gpt-cr', fresh=M, write=M, read=2 * M, out=400000)], TABLE))['cost_parts']['values']
        self.assertEqual([(p['part'], p['cost'], p['share']) for p in v['parts']],
                         [('input', 2.0, .2), ('cache_write', 3.0, .3), ('cache_read', 1.0, .1), ('output', 4.0, .4)])
        self.assertEqual(v['priced_cost'], 10.0)


class F4ContextSize(unittest.TestCase):
    def test_median_and_p90_per_harness_over_known_requests(self):
        rows = [ob(f'c{i}', harness='claude', provider='anthropic', model='mystery', fresh=n - 60, read=40, write=20) for i, n in enumerate((100, 200, 300, 400))]
        rows += [ob(f'x{i}', harness='codex', model='mystery', fresh=n) for i, n in enumerate((10, 20, 30))]
        rows += [ob('unk', harness='codex', model='mystery', fresh=999)]
        rows[-1]['tokens']['cache_read'] = None
        v = by_id(cost_facts(rows, TABLE))['context_size']['values']
        # claude 100,200,300,400: median (200+300)/2, nearest-rank p90 = ceil(.9 * 4) = 4th = 400; codex 10,20,30: 20 and ceil(2.7) = 3rd = 30
        self.assertEqual(v['harnesses'], [{'harness': 'claude', 'requests': 4, 'median': 250.0, 'p90': 400},
                                          {'harness': 'codex', 'requests': 3, 'median': 20.0, 'p90': 30}])
        self.assertEqual(v['excluded_requests'], 1)

    def test_an_imported_harness_name_is_pooled_as_other_in_values_and_text(self):  # #108, same allowlist as limit_hits
        rows = [ob('a', harness='private-harness-xyz', model='mystery', fresh=10), ob('b', harness='another-secret', model='mystery', fresh=30),
                ob('c', harness='opencode', model='mystery', fresh=20)]
        result = cost_facts(rows, TABLE)
        v = by_id(result)['context_size']['values']['harnesses']
        self.assertEqual([(h['harness'], h['requests'], h['median']) for h in v], [('opencode', 1, 20.0), ('other', 2, 20.0)])
        shown = json.dumps(result) + insights.render_text(result)
        for name in ('private-harness-xyz', 'another-secret'):
            self.assertNotIn(name, shown)

    def test_p90_nearest_rank_over_ten(self):
        rows = [ob(f'r{i}', model='mystery', fresh=i) for i in range(1, 11)]
        v = by_id(cost_facts(rows, TABLE))['context_size']['values']['harnesses'][0]
        self.assertEqual((v['median'], v['p90']), (5.5, 9))  # ceil(.9 * 10) = 9th


class F5LongContext(unittest.TestCase):
    def test_premium_exact(self):
        rows = [ob('big', model='gpt-l', fresh=M, out=500000), ob('edge', model='gpt-l', fresh=1000), ob('small', model='gpt-l', fresh=10)]
        v = by_id(cost_facts(rows, TABLE))['long_context_premium']['values']
        # big: 1M > 1000: long 1M x 4 + .5M x 16 = 12; standard 1M x 2 + .5M x 8 = 6. edge: 1000 does not exceed 1000.
        self.assertEqual((v['requests'], v['actual'], v['standard'], v['premium'], v['priced_requests']), (1, 12.0, 6.0, 6.0, 3))

    def test_absent_without_long_requests(self):
        self.assertNotIn('long_context_premium', by_id(cost_facts([ob('s', model='gpt-l', fresh=10)], TABLE)))

    def test_standard_pricing_of_non_long_is_unchanged_by_the_switch(self):
        from tokenatlas.pricing import price_observation
        o = ob('big', model='gpt-l', fresh=M, out=500000)
        self.assertEqual(price_observation(o, TABLE, long_context=False)['cost'], 6.0)
        self.assertEqual(price_observation(o, TABLE)['cost'], 12.0)
        tier = {}
        price_observation(o, TABLE, tier=tier)
        self.assertEqual(tier, {'long': True, 'has_long': True, 'modifier': None})


class F6BigTurns(unittest.TestCase):
    def rows(self):
        def r(i, minute, turn, tokens, kind='main'):
            return ob(i, f'2026-09-03T10:{minute:02d}:00+00:00', model='m1', harness='claude', provider='openai', session='s', turn=turn, out=tokens, kind=kind)
        # output price 10: tA 2 x 2.5M = 25 + 25 = 50.0; tB 4,999,999 = 49.99999; tC 3 x 4M = 120.0; one unattributed request of $10
        return [r('a1', 0, 'tA', 2500000), r('a2', 1, 'tA', 2500000), r('b1', 5, 'tB', 4999999), r('c1', 10, 'tC', 4 * M),
                r('c2', 11, 'tC', 4 * M), r('c3', 12, 'tC', 4 * M),
                ob('n0', '2026-09-03T09:00:00+00:00', model='m1', harness='claude', provider='openai', session='s', out=M)]

    def test_threshold_is_inclusive_and_shares_use_attributed_cost(self):
        v = by_id(cost_facts(self.rows(), TABLE))['big_turns']['values']
        self.assertEqual((v['count'], v['threshold'], v['turns']), (2, 50.0, 3))
        self.assertAlmostEqual(v['cost'], 170.0, 9)
        self.assertAlmostEqual(v['attributed_cost'], 219.99999, 9)
        self.assertAlmostEqual(v['share'], 170.0 / 219.99999, 9)
        self.assertEqual(v['median_requests'], 2.5)  # tA 2 requests, tC 3
        self.assertEqual(v['unattributed_requests'], 1)

    def test_custom_threshold_and_absent_when_none(self):
        v = by_id(cost_facts(self.rows(), TABLE, big_turn=120.0))['big_turns']['values']
        self.assertEqual((v['count'], v['median_requests']), (1, 3))
        self.assertNotIn('big_turns', by_id(cost_facts(self.rows(), TABLE, big_turn=120.00001)))

    def test_window_counts_only_requests_inside(self):
        res = cost_facts(self.rows(), TABLE, start=datetime(2026, 9, 3, 10, 10, tzinfo=timezone.utc))
        v = by_id(res)['big_turns']['values']  # only tC (120.0) remains: tB ends before, tA too
        self.assertEqual((v['count'], v['turns'], v['cost'], v['share']), (1, 1, 120.0, 1.0))


class TurnsMatchTopPrompts(unittest.TestCase):
    def test_big_turn_numbers_equal_top_prompts(self):
        import random
        from tokenatlas.prompts import top_prompts
        rnd = random.Random(7)
        rows = []
        for i in range(600):
            kind = rnd.choice(('main', 'main', 'subagent'))
            sess = f's{rnd.randint(0, 7)}'
            rows.append(ob(f'r{i}', f'2026-09-{1 + i % 28:02d}T{i % 24:02d}:{i % 60:02d}:00+00:00', model=rnd.choice(('m1', 'm2', 'mystery')), harness='claude', provider='openai',
                           session=sess, turn=f't{rnd.randint(0, 5)}' if kind == 'main' and rnd.random() < .9 else None, kind=kind, out=rnd.randint(0, 3 * M)))
        for window in ((None, None), (datetime(2026, 9, 10, tzinfo=timezone.utc), datetime(2026, 9, 20, tzinfo=timezone.utc))):
            keep = None if window == (None, None) else {(r['provider'], r['harness'], r.get('machine'), r['id']) for r in rows if window[0] <= datetime.fromisoformat(r['ts']) < window[1]}
            ref = top_prompts(rows, TABLE, 10 ** 9, 'cost', keep)
            costed = [p for p in ref['prompts'] if p['cost'] is not None]
            for threshold in (1.0, 20.0, 50.0):
                big = [p for p in costed if p['cost'] >= threshold]
                f = by_id(cost_facts(rows, TABLE, *window, big_turn=threshold)).get('big_turns')
                if not big:
                    self.assertIsNone(f)
                    continue
                v = f['values']
                self.assertEqual((v['count'], v['turns'], v['unattributed_requests']), (len(big), len(costed), ref['unattributed_observations']))
                self.assertAlmostEqual(v['cost'], sum(p['cost'] for p in big), 6)
                self.assertAlmostEqual(v['attributed_cost'], sum(p['cost'] for p in costed), 6)
                self.assertEqual(v['median_requests'], statistics_median([p['requests'] for p in big]))


def statistics_median(x):
    import statistics
    return float(statistics.median(x))


class F7Subagents(unittest.TestCase):
    def test_share_of_priced_cost(self):
        rows = [ob('m', model='m1', out=600000, turn='t'), ob('s', '2026-09-03T10:01:00+00:00', model='m1', out=200000, kind='subagent'),
                ob('su', '2026-09-03T10:02:00+00:00', model='mystery', out=10 * M, kind='subagent')]
        v = by_id(cost_facts(rows, TABLE))['subagent_share']['values']
        self.assertEqual((v['subagent_cost'], v['total_cost'], v['share'], v['subagent_requests'], v['priced_requests']), (2.0, 8.0, .25, 1, 2))

    def test_absent_without_subagents(self):
        self.assertNotIn('subagent_share', by_id(cost_facts([ob('m', model='m1', out=M)], TABLE)))


class F8PremiumTiers(unittest.TestCase):
    def test_extra_over_standard(self):
        fast = {'service_tier': 'fast'}
        rows = [ob('f', model='gpt-t', fresh=M, out=100000, tariff=fast), ob('flex', model='gpt-t', fresh=M, tariff={'service_tier': 'flex'}),
                ob('std', model='gpt-t', fresh=M, tariff={'service_tier': 'standard'})]
        v = by_id(cost_facts(rows, TABLE))['premium_tiers']['values']
        # fast: 1M x 4 + .1M x 20 = 6 vs standard 1M x 2 + .1M x 10 = 3; the flex (discount) and standard requests are not counted
        self.assertEqual((v['requests'], v['actual'], v['standard'], v['extra'], v['tiers']), (1, 6.0, 3.0, 3.0, {'service_tier=fast': 1}))

    def test_absent_without_premium_requests(self):
        rows = [ob('flex', model='gpt-t', fresh=M, tariff={'service_tier': 'flex'}), ob('n', model='gpt-t', fresh=M)]
        self.assertNotIn('premium_tiers', by_id(cost_facts(rows, TABLE)))


class Reliability(unittest.TestCase):
    """The report's own rule (template aggregate): id_synthetic = ambiguous identity, counted in no total; complete=False = lower bound, still counted."""
    def rows(self):
        bad = ob('amb', model='m1', out=10 * M, turn='tx')
        bad['id_synthetic'] = True
        low = ob('low', '2026-09-03T10:01:00+00:00', model='m1', out=200000)
        low['complete'] = False
        return [ob('ok', model='m1', out=600000), low, bad, ob('unp', '2026-09-03T10:02:00+00:00', model='mystery', fresh=7)]

    def test_ambiguous_rows_are_excluded_from_every_fact(self):
        res = cost_facts(self.rows(), TABLE)
        v = by_id(res)['model_share']['values']
        self.assertEqual((v['priced_cost'], v['requests'], v['priced_requests'], v['unpriced_requests']), (8.0, 3, 2, 1))
        self.assertEqual((res['requests'], res['ambiguous_requests'], res['incomplete_requests']), (3, 1, 1))
        self.assertEqual(by_id(res)['context_size']['values']['harnesses'][0]['requests'], 3)  # 'amb' (10M output, no input) is not among them
        for f in res['facts']:
            self.assertEqual((f['values']['ambiguous_requests'], f['values']['incomplete_requests']), (1, 1), f['id'])
            self.assertIn('Left out: 1 requests with an uncertain identity', ' '.join(f['assumptions']))
            self.assertIn('Included as incomplete: 1 requests', ' '.join(f['assumptions']))

    def test_ambiguous_row_does_not_make_a_big_turn_or_a_subagent_share(self):
        bad = ob('amb', model='m1', out=10 * M, turn='tx', harness='claude', provider='openai', kind='subagent')
        bad['id_synthetic'] = True
        res = by_id(cost_facts([bad, ob('ok', model='m1', out=M, harness='claude', provider='openai', turn='t1')], TABLE))
        self.assertNotIn('big_turns', res)
        self.assertNotIn('subagent_share', res)
        self.assertEqual(res['model_share']['values']['priced_cost'], 10.0)

    def test_incomplete_rows_are_kept_and_make_the_facts_lower_bounds(self):
        f = by_id(cost_facts(self.rows(), TABLE))
        v = f['model_share']['values']
        self.assertEqual((v['lower_bound'], v['lower_bound_requests']), (True, 1))
        self.assertIn('1 of the requests behind this fact have incomplete token counters: amounts and figures marked ≥ are lower bounds', ' '.join(f['model_share']['assumptions']))
        self.assertIn('≥$8.00', insights.render_text(cost_facts(self.rows(), TABLE)))
        self.assertTrue(f['cost_parts']['values']['lower_bound'])
        self.assertTrue(f['context_size']['values']['lower_bound'])

    def test_difference_facts_use_complete_requests_only(self):
        def big(i, complete=True):
            o = ob(i, model='gpt-l', fresh=M, out=500000)
            o['complete'] = complete
            return o
        def fast(i, complete=True):
            o = ob(i, model='gpt-t', fresh=M, tariff={'service_tier': 'fast'})
            o['complete'] = complete
            return o
        exact = by_id(cost_facts([big('b1'), fast('f1')], TABLE))
        mixed_res = cost_facts([big('b1'), big('b2', False), fast('f1'), fast('f2', False)], TABLE)
        mixed = by_id(mixed_res)
        for fid, key in (('long_context_premium', 'premium'), ('premium_tiers', 'extra')):
            v = mixed[fid]['values']
            self.assertEqual(v['requests'], 1)
            self.assertAlmostEqual(v[key], exact[fid]['values'][key])  # the incomplete twin changes nothing: exact for the requests counted
            self.assertEqual((v['lower_bound'], v['incomplete_left_out']), (False, 1))
            self.assertIn('Requests with incomplete token counters left out of this fact: 1.', ' '.join(mixed[fid]['assumptions']))
            self.assertNotIn('ins_a_lower', mixed[fid]['assumption_keys'])
            self.assertNotIn('ins_a_complete_only', exact[fid]['assumption_keys'])
        text = insights.render_text(mixed_res)
        self.assertRegex(text, r'premium: \$')
        self.assertRegex(text, r'extra cost: \$')
        self.assertNotRegex(text, r'(premium|extra cost): ≥')
        self.assertTrue(mixed['model_share']['values']['lower_bound'])  # the other facts still keep incomplete requests as lower bounds
        only = by_id(cost_facts([big('b2', False), fast('f2', False)], TABLE))
        self.assertNotIn('long_context_premium', only)
        self.assertNotIn('premium_tiers', only)

    def test_an_incomplete_request_below_the_threshold_is_left_out_of_the_long_context_count(self):
        small = ob('s', model='gpt-l', fresh=10)
        small['complete'] = False  # its true input may cross the threshold
        v = by_id(cost_facts([ob('b', model='gpt-l', fresh=M, out=500000), small], TABLE))['long_context_premium']['values']
        self.assertEqual((v['requests'], v['incomplete_left_out']), (1, 1))

    def test_complete_data_is_not_marked(self):
        f = by_id(cost_facts([ob('ok', model='m1', out=M)], TABLE))
        for fact in f.values():
            self.assertFalse(fact['values']['lower_bound'])
            self.assertNotIn('lower bounds', ' '.join(fact['assumptions']))
        self.assertNotIn('≥', insights.render_text(cost_facts([ob('ok', model='m1', out=M)], TABLE)))

    def test_incomplete_but_unpriced_row_only_bounds_the_token_fact(self):
        low = ob('low', model='mystery', fresh=100)
        low['complete'] = False
        f = by_id(cost_facts([ob('ok', model='m1', out=M), low], TABLE))
        self.assertFalse(f['model_share']['values']['lower_bound'])
        self.assertTrue(f['context_size']['values']['lower_bound'])
        self.assertEqual(f['model_share']['values']['incomplete_requests'], 1)


class PricingAssumptions(unittest.TestCase):
    TIER = 'Service tier not recorded, priced as standard (requests: {n}).'

    def test_assumed_rows_propagate_with_counts_to_every_cost_fact(self):
        std = {'service_tier': 'standard'}
        rows = [ob('a1', model='gpt-l', fresh=M, out=500000), ob('a2', model='gpt-l', fresh=10), ob('a3', model='gpt-l', fresh=10, tariff=std)]  # first two assumed
        f = by_id(cost_facts(rows, dict(TABLE, models=[e for e in TABLE['models'] if e['model'] in ('gpt-l', 'gpt-e')])))
        for fid, n in (('model_share', 2), ('cost_parts', 2), ('price_comparison', 2), ('long_context_premium', 1)):
            self.assertEqual([(a['key'], a['requests']) for a in f[fid]['price_assumptions']], [('ins_pa_tier', n)], fid)
            self.assertIn(self.TIER.format(n=n), f[fid]['assumptions'], fid)
        self.assertNotIn('service tier not recorded', ' '.join(f['context_size']['assumptions']).lower())

    def test_subagent_turn_and_tier_facts(self):
        rows = [ob('m', model='m1', out=600000, harness='claude', provider='openai', turn='t1', tariff={'speed': 'standard'}),
                ob('s', '2026-09-03T10:01:00+00:00', model='m1', out=M, harness='claude', provider='openai', kind='subagent'),
                ob('x', '2026-09-03T10:02:00+00:00', model='m2', out=5 * M, harness='claude', provider='openai', turn='t2')]
        f = by_id(cost_facts(rows, TABLE, big_turn=5.0))
        claude = 'Speed not recorded, priced as standard (requests: {n}).'
        self.assertIn(claude.format(n=2), f['subagent_share']['assumptions'])  # priced requests: s and x are assumed; m has a recorded speed ... and s is
        self.assertEqual([(a['key'], a['requests']) for a in f['subagent_share']['price_assumptions']], [('ins_pa_speed', 2)])
        self.assertEqual([(a['key'], a['requests']) for a in f['big_turns']['price_assumptions']], [('ins_pa_speed', 2)])  # t1 (m) and t2 (x) both attributed; s rolls up into t1
        fast = [ob('f', model='gpt-t', fresh=M, tariff={'service_tier': 'fast'})]
        self.assertEqual(by_id(cost_facts(fast, TABLE))['premium_tiers']['price_assumptions'], [])

    def test_unknown_assumption_text_is_shown_as_it_is(self):
        fact = insights._finish(insights._fact('x', {}, 'ins_subagent_share_c', (), used=[(ob('a'), {'assumptions': ['odd thing']}, 1.0, {})]),
                                dict(ambiguous=0, incomplete=0, retrieved=None))
        self.assertIn('odd thing (requests: 1)', fact['assumptions'])


    def test_counts_in_texts_are_thousands_separated(self):
        fact = insights._finish(insights._fact('x', {}, 'ins_big_turns_c', ('ins_a_list',), used=[(ob('a'), {'assumptions': ['odd thing']}, 1.0, {})], big_turn=50.0),
                                dict(ambiguous=12345, incomplete=4726, retrieved='2026-09-01'))
        text = ' '.join(fact['assumptions'])
        self.assertIn('12,345', text)
        self.assertIn('4,726', text)
        self.assertNotIn('4726', text)
        self.assertIn('$50', fact['computation'])
        self.assertNotIn('50.0', fact['computation'])
        self.assertEqual([insights._num(x) for x in (323100, 1080.0, 1080.5, 0, '2026-09-01')], ['323,100', '1,080', '1,080.5', '0', '2026-09-01'])

    def test_fact_texts_have_the_same_placeholders_in_both_languages(self):
        import re
        texts = json.loads(insights.I18N.read_text(encoding='utf-8'))
        for key in (k for k in texts['en'] if k.startswith('ins_')):
            self.assertEqual(set(re.findall(r'\{(\w+)\}', texts['sv'][key])), set(re.findall(r'\{(\w+)\}', texts['en'][key])), key)

class PriceTableWording(unittest.TestCase):
    def test_selected_table_with_its_date(self):
        f = by_id(cost_facts(D, TABLE))['model_share']
        text = ' '.join(f['assumptions'])
        self.assertIn('from the selected price table (retrieved on 2026-09-01)', text)
        self.assertNotIn('packaged', text + json.dumps(insights._texts()))
        self.assertEqual(cost_facts(D, TABLE)['price_table'], {'retrieved_on': '2026-09-01'})

    def test_no_date_when_the_table_has_none(self):
        table = {k: v for k, v in TABLE.items() if k != 'retrieved_on'}
        f = by_id(cost_facts(D, table))['model_share']
        self.assertIn('from the selected price table, not what was paid', ' '.join(f['assumptions']))
        self.assertNotIn('retrieved', ' '.join(f['assumptions']))
        self.assertTrue(all('{' not in a for a in f['assumptions']))


class WindowEnd(unittest.TestCase):
    def test_end_is_exclusive_and_later_rows_are_out(self):
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        rows = [ob('before', '2026-09-19T23:59:59+00:00', model='m1', out=M), ob('at', '2026-09-20T00:00:00+00:00', model='m2', out=M),
                ob('after', '2026-09-21T00:00:00+00:00', model='m3', out=M), ob('old', '2026-08-10T00:00:00+00:00', model='m4', out=M)]
        rep = build_report(rows, {}, redact=False, table=TABLE, now=now)
        thirty, everything = rep['insights']['windows']
        self.assertEqual(thirty['window'], {'start': '2026-08-21T00:00:00+00:00', 'end': '2026-09-20T00:00:00+00:00'})
        self.assertEqual([m['name'] for m in by_id(thirty)['model_share']['values']['models']], ['m1'])
        self.assertEqual(sorted(m['name'] for m in by_id(everything)['model_share']['values']['models']), ['m1', 'm2', 'm3', 'm4'])


class PricedRequestsLabel(unittest.TestCase):
    def test_json_key_and_cli_text_say_priced(self):
        res = cost_facts(D, TABLE)
        m = by_id(res)['model_share']['values']['models'][0]
        self.assertIn('priced_requests', m)
        self.assertNotIn('requests', m)
        text = insights.render_text(res)
        self.assertIn('gpt-a: $2.30 (88.1%), 2 priced requests', text)

    def test_big_turn_denominator_wording_and_value(self):
        for lang, text in (('en', 'the cost of all turns with at least one priced request in the window'), ('sv', 'alla turer med minst ett prissatt anrop i fönstret')):
            self.assertIn(text, json.loads(insights.I18N.read_text(encoding='utf-8'))[lang]['ins_big_turns_c'])
        rows = F6BigTurns().rows() + [ob('z', '2026-09-03T10:20:00+00:00', model='mystery', harness='claude', provider='openai', session='s', turn='tZ', out=M)]
        v = by_id(cost_facts(rows, TABLE))['big_turns']['values']
        self.assertEqual(v['turns'], 3)  # tZ has no priced request: not in the denominator


class Report(unittest.TestCase):
    NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)

    def rows(self):
        return [ob('old', '2026-07-01T10:00:00+00:00', model='m1', out=M), ob('new', '2026-09-10T10:00:00+00:00', model='m2', out=M)]

    def test_two_windows_computed_server_side(self):
        rep = build_report(self.rows(), {}, redact=False, table=TABLE, now=self.NOW)
        ins = rep['insights']
        self.assertEqual([w['id'] for w in ins['windows']], ['30d', 'all'])
        self.assertEqual(ins['windows'][0]['window']['start'], '2026-08-21T00:00:00+00:00')
        v = {w['id']: by_id(w)['model_share']['values'] for w in ins['windows']}
        self.assertEqual([m['name'] for m in v['30d']['models']], ['m2'])
        self.assertEqual(sorted(m['name'] for m in v['all']['models']), ['m1', 'm2'])
        self.assertNotIn('computation', ins['windows'][0]['facts'][0])  # texts live in the page's i18n, not in the payload

    def test_shared_report_redacts_names_and_has_no_private_strings(self):
        table = copy.deepcopy(TABLE)
        table['models'] += [entry('internal-secret-model', 1, 1, 10, provider='openrouter'), entry('internal-alt', 1, 1, 1, provider='openrouter'), entry('gpt-5.5', 1, 1, 10)]
        rows = [ob('a', model='internal-secret-model', provider='openrouter', out=M, session='private-session-id', turn='private-turn', project='/w/secretapp'),
                ob('b', '2026-09-10T11:00:00+00:00', model='gpt-5.5', fresh=M, session='private-session-id', turn='private-turn')]
        rep = build_report(rows, {}, redact=True, table=table, now=self.NOW)
        text = json.dumps(rep['insights'])
        for private in ('internal-secret-model', 'internal-alt', 'secretapp', 'private-session-id', 'private-turn', '/w/'):
            self.assertNotIn(private, text)
        names = [m['name'] for m in by_id(rep['insights']['windows'][1])['model_share']['values']['models']]
        self.assertEqual(sorted(names), ['gpt-5.5', rep['columns']['dict']['model'][0]])  # only a packaged public identifier stays visible
        self.assertRegex(rep['columns']['dict']['model'][0], r'^model \d{3}$')
        local = build_report(rows, {}, redact=False, table=table, now=self.NOW)
        self.assertIn('internal-secret-model', json.dumps(local['insights']))
        self.assertEqual(payload(__import__('tokenatlas.report', fromlist=['x']).render_report(rep))['insights'], rep['insights'])

    def test_report_state_covers_the_insights_day(self):
        from tokenatlas.report import report_state
        a = report_state(1, 'm', {}, {}, 't', day='2026-09-20')
        self.assertEqual(a, report_state(1, 'm', {}, {}, 't', day='2026-09-20'))
        self.assertNotEqual(a[1], report_state(1, 'm', {}, {}, 't', day='2026-09-21')[1])
        self.assertEqual(a[0], report_state(1, 'm', {}, {}, 't', day='2026-09-21')[0])


class Cli(Base):
    def setUp(self):
        super().setUp()
        d = why.CLAUDE_PROJECTS / 'proj'
        jl(d / 'sess.jsonl', [user('2026-09-03T09:59:00Z', 'u1'), claude_row('2026-09-03T10:00:00Z', 'r1', 10),
                              user('2026-09-03T11:00:00Z', 'u2'), claude_row('2026-09-03T11:00:05Z', 'r2', 1000000)])
        jl(d / 'sess/subagents/agent-a.jsonl', [claude_row('2026-09-03T10:05:00Z', 'r3', 1000000, agent='a')])
        from test_pricing import TABLE as PT
        self.prices = self.state.parent / 'prices.json'
        self.prices.write_text(json.dumps(PT))
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)

    def insights(self, *args):
        return self.run_cli('insights', '--prices', str(self.prices), *args)

    def test_json(self):
        code, out, err = self.insights('--json')
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        f = by_id(res)
        # claude-x: 1M x 4 input each; r1 +10 x 20e-6 = .0002; r2 and r3 +1M x 20 = 20 -> 4.0002 + 24 + 24 = 52.0002
        self.assertAlmostEqual(f['model_share']['values']['priced_cost'], 52.0002, 6)
        self.assertEqual(f['model_share']['values']['models'][0]['name'], 'claude-x')
        self.assertAlmostEqual(f['subagent_share']['values']['share'], 24 / 52.0002, 6)
        self.assertEqual((res['requests'], res['unpriced_requests']), (3, 0))
        self.assertEqual(res['window'], {'start': None, 'end': None})
        for private in ('secret', '/work/app', 'sess.jsonl', 'r-r1', 'u1'):
            self.assertNotIn(private, out)

    def test_text(self):
        code, out, err = self.insights()
        self.assertEqual(code, 0, err)
        self.assertIn('Cost facts', out)
        self.assertIn('claude-x', out)
        self.assertIn('$52.00', out)
        self.assertIn('computation: ', out)
        self.assertIn('assumptions:', out)
        self.assertIn('[computed]', out)
        self.assertIn('[measured]', out)
        for private in ('secret', '/work/app', 'sess.jsonl', 'r-r1'):
            self.assertNotIn(private, out)

    def test_days_end_at_the_captured_now(self):
        jl(why.CLAUDE_PROJECTS / 'proj' / 'future.jsonl', [claude_row('2099-01-01T00:00:00Z', 'rf', 5, session='fut')])
        self.assertEqual(self.run_cli('refresh', '--harness', 'claude')[0], 0)
        self.assertEqual(json.loads(self.insights('--json')[1])['requests'], 4)
        res = json.loads(self.insights('--json', '--days', '36500')[1])
        self.assertEqual(res['requests'], 3)  # the 2099 row is after now
        self.assertTrue(res['window']['end'])

    def test_window_options(self):
        res = json.loads(self.insights('--json', '--start', '2026-09-03T10:30:00+00:00')[1])
        self.assertEqual(res['requests'], 1)
        self.assertEqual(res['window']['start'], '2026-09-03T10:30:00+00:00')
        self.assertEqual(json.loads(self.insights('--json', '--days', '36500')[1])['requests'], 3)
        code, out, _ = self.insights('--days', '1')
        self.assertEqual(code, 0)
        self.assertIn('no cost facts', out.lower())
        self.assertEqual(self.insights('--days', '1', '--start', '2026-09-03T10:30:00+00:00')[0], 2)
        self.assertEqual(self.insights('--days', '0')[0], 2)
        self.assertEqual(self.insights('--start', '2026-09-03T10:30:00')[0], 2)



class InterruptedTurnsFact(unittest.TestCase):
    def rows(self, flagged=True):
        a = ob('a1', '2026-09-03T10:00:00+00:00', model='gpt-a', fresh=100000, session='s', turn='t1')
        a2 = ob('a2', '2026-09-03T10:01:00+00:00', model='gpt-a', fresh=100000, session='s', turn='t1')
        if flagged:
            a2['flags'] = ['interrupted']
        b = ob('b1', '2026-09-03T10:10:00+00:00', model='gpt-a', fresh=200000, session='s', turn='t2')
        return [a, a2, b]

    def test_count_cost_and_share_are_computed_from_list_prices(self):
        f = by_id(cost_facts(self.rows(), TABLE))['interrupted_turns']
        v = f['values']
        self.assertEqual((v['count'], v['unpriced_turns'], v['partly_priced_turns']), (1, 0, 0))
        self.assertAlmostEqual(v['cost'], 0.4)  # 200k fresh at $2/M
        self.assertAlmostEqual(v['priced_cost'], 0.8)
        self.assertAlmostEqual(v['share'], 0.5)
        self.assertEqual(f['provenance'], 'computed')
        text = insights.render_text(cost_facts(self.rows(), TABLE))
        self.assertIn('turns interrupted by the user: 1', text)
        self.assertNotIn('wasted', text.lower())

    def test_omitted_when_nothing_was_interrupted(self):
        self.assertNotIn('interrupted_turns', by_id(cost_facts(self.rows(False), TABLE)))

    def test_unpriced_turn_is_counted_and_adds_no_cost(self):
        rows = self.rows() + [dict(ob('m1', '2026-09-03T11:00:00+00:00', model='mystery', fresh=5000, session='s', turn='t3'), flags=['interrupted'])]
        v = by_id(cost_facts(rows, TABLE))['interrupted_turns']['values']
        self.assertEqual((v['count'], v['unpriced_turns']), (2, 1))
        self.assertAlmostEqual(v['cost'], 0.4)

    def test_window_restricts_the_turns(self):
        res = cost_facts(self.rows(), TABLE, datetime(2026, 9, 3, 10, 5, tzinfo=timezone.utc), None)
        self.assertNotIn('interrupted_turns', by_id(res))  # the flagged request is before the window

    def test_texts_exist_in_both_languages(self):
        texts = json.loads(insights.I18N.read_text(encoding='utf-8'))
        for lang in ('sv', 'en'):
            for key in ('ins_interrupted_turns', 'ins_interrupted_turns_c', 'ins_a_interrupted', 'ins_l_intcount', 'ins_l_intcost',
                        'ins_v_intcost', 'ins_l_intunpriced', 'ins_l_intpartly', 'pr_interrupted'):
                self.assertTrue(texts[lang].get(key), (lang, key))
            self.assertNotIn('wasted', texts[lang]['ins_a_interrupted'].lower())


class InterruptedDisclosures(unittest.TestCase):
    def test_incomplete_request_outside_the_interrupted_turns_makes_the_share_a_lower_bound(self):
        a = dict(ob('a1', '2026-09-03T10:00:00+00:00', model='gpt-a', fresh=100000, session='s', turn='t1'), flags=['interrupted'])
        b = dict(ob('b1', '2026-09-03T10:10:00+00:00', model='gpt-a', fresh=100000, session='s', turn='t2'), complete=False)
        v = by_id(cost_facts([a, b], TABLE))['interrupted_turns']['values']
        self.assertTrue(v['lower_bound'])
        self.assertEqual(v['lower_bound_requests'], 1)


if __name__ == '__main__':
    unittest.main()
