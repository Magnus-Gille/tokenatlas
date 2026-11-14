import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas.pricing import (diagnostics, load_prices, price_observation, price_vector, reference_rate_card,
                                summarize_costs, unit_prices)

REF = {'source_url': 'https://example.test/prices', 'retrieved_on': '2026-09-01', 'notes': ''}
CLAUDE = dict(provider='anthropic', model='claude-x', aliases=['claude-x-alias'], currency='USD', input=4.0,
              cache_write_5m=5.0, cache_write_1h=8.0, cache_write=None, cache_read=0.2, output=20.0,
              long_context=None, free=False, modifiers={
                  'speed=fast': dict(input=20.0, cache_write_5m=25.0, cache_write_1h=40.0, cache_read=1.0, output=100.0),
                  'inference_geo=us': {'multiplier': 1.1}}, **REF)
LONG = dict(CLAUDE, model='claude-long', aliases=[], modifiers={}, long_context=dict(
    above_input_tokens=200000, input=8.0, cache_write_5m=10.0, cache_write_1h=16.0, cache_read=0.4, output=30.0))
NOFAST = dict(CLAUDE, model='claude-nofast', aliases=[], modifiers={})
GPT = dict(provider='openai', model='gpt-x', aliases=['gpt-x-alias'], currency='USD', input=2.0, cache_write_5m=None,
           cache_write_1h=None, cache_write=None, cache_read=0.5, output=10.0, free=False, modifiers={}, long_context=dict(
               above_input_tokens=272000, input=4.0, cache_read=1.0, cache_write=None, output=15.0), **REF)
GPT_WRITE = dict(GPT, model='gpt-write', long_context=None, cache_write=3.0)
FREE = dict(GPT, model='free-model', free=True, input=None, cache_read=None, output=None, long_context=None)
EUR = dict(GPT, provider='mistral', model='eu-model', currency='EUR', input=1.0, cache_read=0.1, output=3.0, long_context=None)
TABLE = {'schema': 1, 'retrieved_on': '2026-09-01', 'unit': 'per_million_tokens',
         'provider_aliases': {'openai-codex': 'openai'}, 'local_providers': ['m5'],
         'models': [CLAUDE, LONG, NOFAST, GPT, GPT_WRITE, FREE, EUR]}
M = 1_000_000


def obs(harness='claude', provider='anthropic', model='claude-x', fresh=0, read=0, write=0, out=0, reasoning=0,
        raw=None, tariff=None):
    return {'harness': harness, 'provider': provider, 'model': model, 'tariff': tariff, 'raw_usage': raw or {},
            'tokens': {'fresh_input': fresh, 'cache_read': read, 'cache_write': write, 'output': out, 'reasoning': reasoning}}


def split(five, hour):
    return {'cache_creation': {'ephemeral_5m_input_tokens': five, 'ephemeral_1h_input_tokens': hour}}


def formula(vector, hour, o):
    t = {k: v or 0 for k, v in o['tokens'].items()}  # null counts are 0 client-side (only free models get here with one)
    p_in, p_w5, p_w1, p_cr, p_out = vector
    return (t['fresh_input'] * p_in + (t['cache_write'] - hour) * p_w5 + hour * p_w1
            + t['cache_read'] * p_cr + t['output'] * p_out)


class UnitPricesTests(unittest.TestCase):
    def matrix(self):
        splits = (None, split(M, 500_000), split(1_500_000, 0), split(0, 1_500_000), split(1, 1))  # last: sum != write
        for harness, provider, model in (('claude', 'anthropic', 'claude-x'), ('claude', 'anthropic', 'claude-x-alias'),
                                         ('claude', 'anthropic', 'claude-long'), ('claude', 'anthropic', 'claude-nofast'),
                                         ('codex', 'openai', 'gpt-x'), ('codex', 'openai-codex', 'gpt-write'),
                                         ('opencode', 'openai', 'gpt-x'), ('opencode', 'mistral', 'eu-model'),
                                         ('opencode', 'openai', 'free-model'), ('claude', 'anthropic', 'free-model'),
                                         ('claude', 'anthropic', 'nope'), ('claude', 'anthropic', None), ('pi', 'm5', 'local')):
            for tariff in (None, {'speed': 'standard'}, {'speed': 'fast'}, {'speed': 'weird'}, {'inference_geo': 'us'},
                           {'inference_geo': 'eu'}, {'service_tier': 'standard'}, {'service_tier': 'priority'},
                           {'speed': 'fast', 'inference_geo': 'us'}):
                for raw in splits:
                    for fresh, read, write, out in ((M, 2 * M, 1_500_000, 100_000), (300_000, 0, 0, 5), (0, 0, 0, 0),
                                                    (100, 50, 1_500_000, 7), (None, 5, 5, 5)):
                        o = obs(harness, provider, model, fresh=fresh, read=read, write=write, out=out, raw=raw, tariff=tariff)
                        yield o
                        if raw is None:  # unknown context: no cache_read/write/fresh classes, only the raw inclusive count
                            yield dict(o, tokens=dict(o['tokens'], cache_read=None), raw_usage={'input_tokens': 300_000})
                            yield dict(o, tokens=dict(o['tokens'], cache_read=None))

    def test_unit_prices_reproduce_price_observation_cost(self):
        seen = dict(priced=0, none=0)
        for o in self.matrix():
            priced = price_observation(o, TABLE)
            cost = priced['cost'] if priced['currency'] in (None, 'USD') else None  # unit prices are USD only
            vector, hour = price_vector(o, TABLE)
            self.assertEqual(unit_prices(o, TABLE), vector)
            self.assertEqual(vector is None, cost is None, o)
            if cost is None:
                seen['none'] += 1
                continue
            seen['priced'] += 1
            self.assertEqual(len(vector), 5)
            self.assertLessEqual(abs(formula(vector, hour, o) - cost), 1e-9 * max(1, cost), o)
        self.assertGreater(seen['priced'], 500)
        self.assertGreater(seen['none'], 500)

    def test_vector_examples(self):
        v, hour = price_vector(obs(fresh=M, write=1_500_000, raw=split(M, 500_000), tariff={'inference_geo': 'us'}), TABLE)
        self.assertEqual(hour, 500_000)
        for got, want in zip(v, (4.0, 5.0, 8.0, 0.2, 20.0)):
            self.assertAlmostEqual(got * 1e6, want * 1.1)
        self.assertEqual(price_vector(obs('codex', 'openai', 'gpt-write', write=M, tariff={'service_tier': None}), TABLE)[0][1:3], [3e-6, 3e-6])
        self.assertEqual(price_vector(obs('codex', 'openai', 'free-model', fresh=M), TABLE), ([0.0] * 5, 0))
        self.assertEqual(price_vector(obs(write=M), TABLE), (None, 0))  # cache write TTL unknown


class PriceObservationTests(unittest.TestCase):
    def price(self, o, table=TABLE):
        return price_observation(o, table)

    def test_claude_prices_each_class_with_5m_and_1h_writes_and_output_once(self):
        r = self.price(obs(fresh=M, read=2 * M, write=1_500_000, out=100_000, reasoning=40_000,
                           raw=split(M, 500_000), tariff={'speed': 'standard'}))
        # input 1M*4=4.0; read 2M*0.2=0.4; write 1M*5 + 0.5M*8 = 9.0; output 0.1M*20=2.0 (reasoning inside output) -> 15.4
        self.assertEqual(r['parts'].keys(), {'input', 'cache_write', 'cache_read', 'output'})
        for key, want in (('input', 4.0), ('cache_read', 0.4), ('cache_write', 9.0), ('output', 2.0), ('cost', 15.4)):
            self.assertAlmostEqual((r['parts'] | {'cost': r['cost']})[key], want)
        self.assertEqual((r['status'], r['currency'], r['assumptions'], r['reason']), ('priced', 'USD', [], None))
        self.assertEqual(r['price_ref'], {'provider': 'anthropic', 'model': 'claude-x',
                                          'source_url': REF['source_url'], 'retrieved_on': '2026-09-01'})

    def test_reasoning_is_never_added_to_output(self):
        a = self.price(obs(out=M, reasoning=0, tariff={'speed': 'standard'}))
        b = self.price(obs(out=M, reasoning=M, tariff={'speed': 'standard'}))
        self.assertAlmostEqual(a['cost'], 20.0)  # 1M*20
        self.assertEqual(a['cost'], b['cost'])

    def test_claude_missing_split_is_partial_when_writes_exist_and_free_when_zero(self):
        r = self.price(obs(fresh=M, write=M, tariff={'speed': 'standard'}))
        self.assertEqual((r['status'], r['cost'], r['parts']['cache_write']), ('partial', None, None))
        self.assertIn('cache write TTL unknown', r['reason'])
        self.assertAlmostEqual(r['parts']['input'], 4.0)  # known parts are kept
        r = self.price(obs(fresh=M, write=0, tariff={'speed': 'standard'}))
        self.assertEqual((r['status'], r['parts']['cache_write']), ('priced', 0.0))
        self.assertAlmostEqual(r['cost'], 4.0)
        # a split that does not add up to the writes is not trusted
        self.assertEqual(self.price(obs(write=M, raw=split(1, 2), tariff={'speed': 'standard'}))['status'], 'partial')

    def test_long_context_switches_whole_request_above_threshold(self):
        base = obs(model='claude-long', fresh=100_000, read=100_000, out=1_000, tariff={'speed': 'standard'})
        # total input 200000 is not above the threshold: 0.1M*4 + 0.1M*0.2 + 0.001M*20 = 0.4+0.02+0.02
        self.assertAlmostEqual(self.price(base)['cost'], 0.44)
        above = obs(model='claude-long', fresh=100_001, read=100_000, out=1_000, tariff={'speed': 'standard'})
        # 0.100001M*8 = 0.800008; 0.1M*0.4 = 0.04; 0.001M*30 = 0.03 -> 0.870008
        self.assertAlmostEqual(self.price(above)['cost'], 0.870008)

    def test_openai_long_context_uses_inclusive_input_tokens(self):
        codex = dict(harness='codex', provider='openai-codex', model='gpt-x')
        short = obs(**codex, fresh=172_000, read=100_000, out=1_000, raw={'input_tokens': 272_000})
        # fresh 0.172M*2=0.344; read 0.1M*0.5=0.05; output 0.001M*10=0.01 -> 0.404
        r = self.price(short)
        self.assertAlmostEqual(r['cost'], 0.404)
        self.assertEqual((r['status'], r['assumptions']), ('assumed', ['service tier not recorded; priced as standard']))
        self.assertEqual(r['price_ref']['provider'], 'openai')
        long = obs(**codex, fresh=172_001, read=100_000, out=1_000, raw={'input_tokens': 272_001})
        # 0.172001M*4=0.688004; 0.1M*1.0=0.1; 0.001M*15=0.015 -> 0.803004
        self.assertAlmostEqual(self.price(long)['cost'], 0.803004)

    def test_openai_model_via_opencode_or_pi_uses_normalized_input_for_context(self):
        # OpenCode/Pi rows carry no raw input_tokens; fresh+read+write is the request's input.
        for harness, provider, raw in (('opencode', 'openai', {'input': 172_001, 'cache': {'read': 100_000}}),
                                       ('pi', 'openai-codex', {'input': 172_001, 'cacheRead': 100_000})):
            with self.subTest(harness=harness):
                r = self.price(obs(harness=harness, provider=provider, model='gpt-x', fresh=172_001, read=100_000,
                                   out=1_000, raw=raw))
                self.assertEqual(r['status'], 'assumed')
                self.assertAlmostEqual(r['cost'], 0.803004)  # long rates, as in the Codex case above

    def test_unknown_context_size_with_long_context_pricing_is_partial(self):
        # Context size is unknown only when an input class is unknown and no raw inclusive count exists.
        r = self.price(obs(harness='codex', provider='openai', model='gpt-x', fresh=None, read=10, out=10, raw={}))
        self.assertEqual((r['status'], r['cost']), ('partial', None))
        self.assertIn('context size unknown', r['reason'])
        r = self.price(obs(model='claude-long', fresh=None, read=1, tariff={'speed': 'standard'}))
        self.assertIn('context size unknown', r['reason'])

    def test_fast_mode_uses_modifier_or_is_unpriced(self):
        fast = {'speed': 'fast'}
        r = self.price(obs(fresh=M, read=M, write=M, out=M, raw=split(M, 0), tariff=fast))
        # fast: input 20 + read 1 + write 1M*25 + output 100 = 146.0
        self.assertAlmostEqual(r['cost'], 146.0)
        r = self.price(obs(model='claude-nofast', fresh=M, tariff=fast))
        self.assertEqual((r['status'], r['cost'], r['reason']), ('unpriced', None, 'fast mode price missing'))

    def test_speed_absent_is_priced_as_standard_with_assumption(self):
        for tariff in (None, {'service_tier': 'standard'}):
            r = self.price(obs(fresh=M, tariff=tariff))
            self.assertEqual((r['status'], r['assumptions']), ('assumed', ['speed not recorded; priced as standard']))
            self.assertAlmostEqual(r['cost'], 4.0)

    def test_inference_geo_us_applies_multiplier_or_is_unpriced(self):
        r = self.price(obs(fresh=M, out=M, tariff={'speed': 'standard', 'inference_geo': 'us'}))
        self.assertAlmostEqual(r['cost'], 26.4)  # (4 + 20) * 1.1
        self.assertAlmostEqual(r['parts']['input'], 4.4)
        r = self.price(obs(model='claude-nofast', fresh=M, tariff={'speed': 'standard', 'inference_geo': 'us'}))
        self.assertEqual(r['status'], 'unpriced')
        r = self.price(obs(fresh=M, tariff={'speed': 'standard', 'inference_geo': 'not_available'}))
        self.assertAlmostEqual(r['cost'], 4.0)

    def test_unknown_geo_and_service_tier_are_unpriced_unless_modified(self):
        r = self.price(obs(fresh=M, tariff={'speed': 'standard', 'inference_geo': 'eu'}))
        self.assertEqual((r['status'], r['cost'], r['reason']), ('unpriced', None, 'unknown inference_geo eu'))
        for geo in ('global', 'not_available', None):
            r = self.price(obs(fresh=M, tariff={'speed': 'standard', 'inference_geo': geo}))
            self.assertEqual((r['status'], r['cost']), ('priced', 4.0))
        r = self.price(obs(fresh=M, tariff={'speed': 'standard', 'service_tier': 'batch'}))
        self.assertEqual((r['status'], r['reason']), ('unpriced', 'unknown service_tier batch'))
        tiered = dict(CLAUDE, model='claude-tier', aliases=[], modifiers={'service_tier=batch': dict(input=2.0, output=10.0)})
        table = dict(TABLE, models=TABLE['models'] + [tiered])
        r = self.price(obs(model='claude-tier', fresh=M, tariff={'speed': 'standard', 'service_tier': 'batch'}), table)
        self.assertAlmostEqual(r['cost'], 2.0)

    def test_non_claude_service_tier_is_resolved_from_tariff(self):
        flex = dict(GPT, model='gpt-flex', aliases=[], modifiers={'service_tier=flex': dict(input=1.0, cache_read=0.25, output=5.0)})
        table = dict(TABLE, models=TABLE['models'] + [flex])
        codex = dict(harness='codex', provider='openai', model='gpt-flex')
        r = self.price(obs(**codex, fresh=100_000, out=100_000, tariff={'service_tier': 'standard'}), table)
        self.assertEqual((r['status'], r['assumptions']), ('priced', []))
        self.assertAlmostEqual(r['cost'], 1.2)
        r = self.price(obs(**codex, fresh=100_000, out=100_000, tariff={'service_tier': None}), table)
        self.assertEqual((r['status'], r['assumptions']), ('priced', []))
        r = self.price(obs(**codex, fresh=100_000, out=100_000, tariff={'service_tier': 'flex'}), table)
        self.assertEqual(r['status'], 'priced')
        self.assertAlmostEqual(r['cost'], 0.6)  # 0.1M*1 + 0.1M*5
        r = self.price(obs(**codex, fresh=M, tariff={'service_tier': 'batch'}), table)
        self.assertEqual((r['status'], r['reason']), ('unpriced', 'unknown service_tier batch'))
        r = self.price(obs(**codex, fresh=300_000, raw={'input_tokens': 300_000}, tariff={'service_tier': 'flex'}), table)
        self.assertEqual(r['status'], 'unpriced')
        self.assertIn('service_tier flex', r['reason'])
        for tariff in (None, {}):
            r = self.price(obs(**codex, fresh=100_000, out=100_000, tariff=tariff), table)
            self.assertEqual((r['status'], r['assumptions']), ('assumed', ['service tier not recorded; priced as standard']))

    def test_unknown_model_local_provider_and_free_model(self):
        r = self.price(obs(model='claude-nope', fresh=5))
        self.assertEqual((r['status'], r['cost'], r['reason']), ('unpriced', None, 'no list price for anthropic/claude-nope'))
        self.assertEqual(self.price(obs(model=None, fresh=5))['status'], 'unpriced')
        self.assertEqual(self.price(obs(model='unknown', fresh=5))['status'], 'unpriced')
        r = self.price(obs(harness='opencode', provider='m5', model='whatever', fresh=5))
        self.assertEqual((r['status'], r['cost'], r['reason']), ('local', None, 'local model'))
        r = self.price(obs(harness='codex', provider='openai', model='free-model', fresh=M, out=M))
        self.assertEqual((r['status'], r['cost'], r['currency']), ('free', 0.0, 'USD'))

    def test_model_alias_and_provider_alias(self):
        self.assertAlmostEqual(self.price(obs(model='claude-x-alias', fresh=M, tariff={'speed': 'standard'}))['cost'], 4.0)
        r = self.price(obs(harness='codex', provider='openai-codex', model='gpt-x-alias', fresh=M, raw={'input_tokens': M}))
        self.assertEqual(r['price_ref']['model'], 'gpt-x')  # 1M > 272000: long input 4.0
        self.assertAlmostEqual(r['cost'], 4.0)

    def test_other_harness_cache_write_price_and_null_price(self):
        r = self.price(obs(harness='pi', provider='openai', model='gpt-write', fresh=M, write=M))
        self.assertAlmostEqual(r['cost'], 5.0)  # input 1M*2 + write 1M*3; no TTL evidence is priced as before
        r = self.price(obs(harness='pi', provider='openai', model='gpt-x', fresh=1, write=5, raw={'input_tokens': 6}))
        self.assertEqual((r['status'], r['parts']['cache_write'], r['cost']), ('partial', None, None))  # no cache_write rate at all

    def test_non_claude_cache_write_pricing_is_unchanged_from_before_this_pr(self):
        # fixed expectations equal to the pre-PR (c8fc3c4) results: write x cache_write rate, whatever the raw usage carries
        for harness in ('codex', 'pi', 'opencode'):
            for raw in (None, {'cache_write_ttl': '1h'}, {'input_tokens': 4 * M}):
                r = price_observation(obs(harness=harness, provider='openai', model='gpt-write', fresh=M, write=M, read=M, out=M, raw=raw), TABLE)
                self.assertEqual((r['status'], r['cost'], r['reason']), ('assumed', 2 + 3 + .5 + 10, None))
                self.assertEqual(r['parts'], {'input': 2.0, 'cache_write': 3.0, 'cache_read': .5, 'output': 10.0})
        self.assertEqual(price_vector(obs('pi', 'openai', 'gpt-write', write=M), TABLE)[0][1:3], [3e-6, 3e-6])
        r = price_observation(obs(harness='pi', provider='openai', model='gpt-x', fresh=1, write=5, raw={'input_tokens': 6}), TABLE)
        self.assertEqual((r['status'], r['parts']['cache_write'], r['cost']), ('partial', None, None))  # no cache_write rate

    def test_packaged_table_has_no_cache_write_ttl_key(self):
        self.assertFalse([e['model'] for e in load_prices()['models'] if 'cache_write_ttl' in e])

    def test_packaged_terra_long_boundary_and_tier_combinations(self):
        table = load_prices()
        exact = price_observation(obs(harness='codex', provider='openai', model='gpt-6.1-terra', fresh=272_000, out=M), table)
        self.assertAlmostEqual(exact['parts']['output'], 5.0)
        self.assertAlmostEqual(exact['cost'], 5.272)
        long = price_observation(obs(harness='codex', provider='openai', model='gpt-6.1-terra', fresh=272_001, out=M), table)
        self.assertAlmostEqual(long['parts']['input'], 0.544002)
        self.assertAlmostEqual(long['parts']['output'], 7.5)
        self.assertAlmostEqual(long['cost'], 8.044002)
        fast = price_observation(obs(harness='codex', provider='openai', model='gpt-6.1-terra', fresh=1_000, out=M,
                                     tariff={'service_tier': 'fast'}), table)
        self.assertAlmostEqual(fast['cost'], 10.002)
        for tier in ('fast', 'flex'):
            combined = price_observation(obs(harness='codex', provider='openai', model='gpt-6.1-terra', fresh=272_001, out=M,
                                              tariff={'service_tier': tier}), table)
            self.assertEqual((combined['status'], combined['cost']), ('unpriced', None))

    def test_none_token_field_is_partial_never_zero(self):
        r = self.price(obs(fresh=M, out=None, tariff={'speed': 'standard'}))
        self.assertEqual((r['status'], r['cost'], r['parts']['output']), ('partial', None, None))
        self.assertAlmostEqual(r['parts']['input'], 4.0)


class SummaryTests(unittest.TestCase):
    def test_coverage_status_counts_and_currency_separation(self):
        std = {'speed': 'standard'}
        rows = [obs(fresh=M, tariff=std),                                   # 4.0 USD, 1M tokens covered
                obs(fresh=M, write=M, tariff=std),                          # partial, 2M tokens uncovered
                obs(harness='pi', provider='mistral', model='eu-model', fresh=M, out=M),  # 1 + 3 = 4.0 EUR, 2M covered
                obs(model='claude-nope', fresh=500)]                        # unpriced
        by_key = {g['key']: g for g in summarize_costs(rows, TABLE)}
        claude = by_key[('anthropic', 'claude-x')]
        self.assertEqual((claude['observations'], claude['status']), (2, {'priced': 1, 'partial': 1}))
        self.assertEqual(claude['tokens'], {'fresh_input': 2 * M, 'cache_read': 0, 'cache_write': M, 'output': 0})
        self.assertAlmostEqual(claude['coverage'], 1 / 3)  # 1M of 3M known tokens
        self.assertEqual(claude['unpriced_tokens'], 2 * M)
        self.assertEqual(list(claude['cost']), ['USD'])
        self.assertAlmostEqual(claude['cost']['USD'], 4.0)
        self.assertEqual(len(claude['price_refs']), 1)
        self.assertAlmostEqual(by_key[('mistral', 'eu-model')]['cost']['EUR'], 4.0)
        self.assertEqual(by_key[('anthropic', 'claude-nope')]['coverage'], 0.0)
        # grouping everything together must keep currencies apart
        [total] = summarize_costs(rows, TABLE, key=lambda o: 'all')
        self.assertEqual(set(total['cost']), {'USD', 'EUR'})
        self.assertAlmostEqual(total['cost']['USD'], 4.0)
        self.assertAlmostEqual(total['cost']['EUR'], 4.0)

    def test_empty_group_coverage_is_unknown(self):
        [g] = summarize_costs([obs(model='claude-nope')], TABLE)
        self.assertIsNone(g['coverage'])


class LoadPricesTests(unittest.TestCase):
    def load(self, table):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'prices.json'
            path.write_text(json.dumps(table))
            return load_prices(path)

    def test_packaged_table_and_fixture_load(self):
        self.assertEqual(load_prices()['schema'], 1)
        self.assertEqual(self.load(TABLE)['models'][0]['model'], 'claude-x')

    def test_packaged_new_openai_models_keep_model_page_tiers_and_dates(self):
        entries = {e['model']: e for e in load_prices()['models']}
        for model, standard, long, fast, flex, date, url in (
                ('gpt-6.1-sol', (2, .1, 2.5, 10), (4, .2, 5, 15), (4, .2, 5, 20), (1, .05, 1.25, 5), '2026-11-14', 'gpt-6.1-sol'),
                ('gpt-6.1-terra', (1, .05, 1.25, 5), (2, .1, 2.5, 7.5), (2, .1, 2.5, 10), (.5, .025, .625, 2.5), '2026-11-14', 'gpt-6.1-terra')):
            entry = entries[model]
            self.assertEqual(tuple(entry[k] for k in ('input', 'cache_read', 'cache_write', 'output')), standard)
            self.assertEqual(entry['long_context']['above_input_tokens'], 272000)
            self.assertEqual(tuple(entry['long_context'][k] for k in ('input', 'cache_read', 'cache_write', 'output')), long)
            self.assertEqual(tuple(entry['modifiers']['service_tier=fast'][k] for k in ('input', 'cache_read', 'cache_write', 'output')), fast)
            self.assertEqual(tuple(entry['modifiers']['service_tier=flex'][k] for k in ('input', 'cache_read', 'cache_write', 'output')), flex)
            self.assertEqual
            self.assertTrue(entry['source_url'].endswith('/' + url))

    def test_packaged_anthropic_fable_and_haiku_dimensions_and_boundaries(self):
        table = load_prices()
        entries = {e['model']: e for e in table['models']}
        self.assertEqual(entries['claude-fable-5-2']['retrieved_on'], '2026-11-14')
        self.assertEqual(entries['claude-haiku-5-5']['retrieved_on'], '2026-11-14')
        self.assertEqual(entries['claude-fable-5-2']['source_url'], 'https://platform.claude.com/docs/en/about-claude/pricing')
        self.assertEqual(entries['claude-haiku-5-5']['source_url'], 'https://platform.claude.com/docs/en/about-claude/pricing')
        fable = price_observation(obs(harness='claude', provider='anthropic', model='claude-fable-5-2', fresh=M,
                                      read=2 * M, write=1_500_000, out=100_000,
                                      raw=split(M, 500_000)), table)
        self.assertEqual(fable['status'], 'assumed')
        self.assertAlmostEqual(fable['parts']['input'], 8.0)
        self.assertAlmostEqual(fable['parts']['cache_read'], 0.4)
        self.assertAlmostEqual(fable['parts']['cache_write'], 18.0)
        self.assertAlmostEqual(fable['parts']['output'], 4.0)
        self.assertAlmostEqual(fable['cost'], 30.4)
        self.assertIn('speed not recorded', fable['assumptions'][0])
        fable_fast = price_observation(obs(harness='claude', provider='anthropic', model='claude-fable-5-2', fresh=M,
                                           read=2 * M, write=1_500_000, out=100_000,
                                           raw=split(M, 500_000), tariff={'speed': 'fast'}), table)
        self.assertEqual(fable_fast['status'], 'priced')
        self.assertAlmostEqual(fable_fast['cost'], 60.8)

        haiku_exact = price_observation(obs(harness='claude', provider='anthropic', model='claude-haiku-5-5', fresh=100_000, out=M), table)
        self.assertAlmostEqual(haiku_exact['parts']['input'], 0.01)
        self.assertAlmostEqual(haiku_exact['parts']['output'], 0.5)
        self.assertAlmostEqual(haiku_exact['cost'], 0.51)
        haiku_cache_exact = price_observation(obs(harness='claude', provider='anthropic', model='claude-haiku-5-5', fresh=99_000,
                                                 write=1_000, raw=split(400, 600)), table)
        self.assertAlmostEqual(haiku_cache_exact['cost'], 0.01007)
        haiku_cache_long = price_observation(obs(harness='claude', provider='anthropic', model='claude-haiku-5-5', fresh=99_001,
                                                write=1_000, raw=split(400, 600)), table)
        self.assertAlmostEqual(haiku_cache_long['cost'], 0.0503505)
        haiku_long = price_observation(obs(harness='claude', provider='anthropic', model='claude-haiku-5-5', fresh=100_001,
                                           write=M, out=M, raw=split(400_000, 600_000)), table)
        self.assertAlmostEqual(haiku_long['parts']['input'], 0.0500005)
        self.assertAlmostEqual(haiku_long['parts']['cache_write'], 0.85)
        self.assertAlmostEqual(haiku_long['parts']['output'], 2.5)
        self.assertAlmostEqual(haiku_long['cost'], 3.4000005)
        haiku_fast = price_observation(obs(harness='claude', provider='anthropic', model='claude-haiku-5-5', fresh=1,
                                           tariff={'speed': 'fast'}), table)
        self.assertEqual((haiku_fast['status'], haiku_fast['cost']), ('unpriced', None))

    def test_diagnostics_count_only_no_list_price_and_rank_by_tokens(self):
        stamp = lambda r, ts='2026-10-14T00:00:00Z': dict(r, ts=ts)
        rows = [stamp(obs(model='unknown-a', fresh=10, out=5, harness='codex', provider='openai')),      # 15 tokens, 1 request
                stamp(obs(model='unknown-b', fresh=1, harness='codex', provider='openai')),              # 1 token, 2 requests
                stamp(obs(model='unknown-b', fresh=1, harness='codex', provider='openai')),
                stamp(obs(model='gpt-x', fresh=M, harness='codex', provider='openai')),
                stamp(obs(model='eu-model', fresh=M, harness='codex', provider='mistral')),             # priced in EUR: not a missing price
                stamp(obs(model='anything', fresh=M, harness='codex', provider='m5')),                  # local
                stamp(obs(model='codex-auto-review', fresh=M, harness='codex', provider='openai')),     # explicit
                dict(stamp(obs(model='synthetic-model', fresh=M, harness='codex', provider='openai')), id_synthetic=True),
                stamp(obs(model='future', fresh=M, harness='codex', provider='openai'), '2026-10-16T00:00:00+00:00'),
                dict(obs(model='no-ts', harness='codex', provider='openai'), ts=None)]
        table = dict(TABLE, unpriced_models=[dict(provider='openai', model='codex-auto-review', reason='no public rate',
                                                  source_url='https://example.test/p', retrieved_on='2026-09-01')])
        result = diagnostics(rows, table, now=datetime(2026, 10, 15, tzinfo=timezone.utc), threshold=.1)
        self.assertEqual((result['requests'], result['priced_requests'], result['no_list_price_requests']), (7, 1, 3))
        self.assertEqual(result['not_counted_as_missing'], {'explicitly_unpriced': 1, 'local': 1, 'other_currency': 1})
        self.assertEqual([(m['model'], m['requests'], m['tokens']) for m in result['largest_unpriced_models']],
                         [('unknown-a', 1, 15), ('unknown-b', 2, 2)])
        self.assertTrue(any('have no list price' in w for w in result['warnings']))

    def test_explicit_unpriced_models_are_machine_readable_and_validated(self):
        table = load_prices()
        r = price_observation(obs(harness='codex', provider='openai', model='codex-auto-review', fresh=M), table)
        self.assertEqual((r['status'], r['cost'], r['explicit_unpriced']), ('unpriced', None, True))
        self.assertTrue(r['reason'].startswith('explicitly unpriced: '))
        self.assertEqual(r['price_ref']['model'], 'codex-auto-review')
        # not the generic "no list price" case: doctor does not count it
        result = diagnostics([dict(obs(harness='codex', provider='openai', model='codex-auto-review', fresh=M), ts='2026-10-14T00:00:00Z')],
                             table, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
        self.assertEqual((result['no_list_price_requests'], result['not_counted_as_missing']), (0, {'explicitly_unpriced': 1}))
        bad = copy.deepcopy(TABLE)
        bad['unpriced_models'] = [dict(provider='openai', model='x')]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'p.json'
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError, 'unpriced_models'):
                load_prices(path)

    def test_diagnostics_stale_entries_are_counted_per_entry(self):
        old, fresh = dict(GPT, model='old', retrieved_on='2026-09-01'), dict(GPT, model='new', retrieved_on='2026-10-10')
        result = diagnostics([], dict(TABLE, models=[old, fresh]), now=datetime(2026, 10, 15, tzinfo=timezone.utc), max_age_days=30)
        self.assertEqual((result['stale_entries'], result['price_entries'], result['oldest_entry_age_days']), (1, 2, 44))
        self.assertEqual(len(result['warnings']), 1)
        self.assertIn('1 of 2 price entries', result['warnings'][0])

    def test_packaged_table_does_not_warn_on_its_own_date(self):
        table = load_prices()
        day = datetime.fromisoformat(table['retrieved_on']).replace(tzinfo=timezone.utc)
        result = diagnostics([], table, now=day)
        # Only the two OpenCode Zen rows that are no longer listed there keep an older date.
        stale = sorted(e['model'] for e in table['models'] if (day.date() - datetime.fromisoformat(e['retrieved_on']).date()).days > 30)
        self.assertEqual(stale, ['muse-spark-1.3-contributor-free', 'space-bunny-free'])
        self.assertEqual(result['stale_entries'], 2)
        fresh = [e for e in table['models'] if e['model'] not in stale]
        self.assertEqual(diagnostics([], dict(table, models=fresh), now=day)['warnings'], [])

    def test_packaged_anthropic_us_geo_and_sonnet_cache_read(self):
        table = load_prices()
        entries = {e['model']: e for e in table['models']}
        self.assertEqual(entries['claude-sonnet-5-5']['cache_read'], 0.1)
        for model, kw in (('claude-fable-5-2', dict(fresh=M, read=M, write=M, out=M, raw=split(M, 0))),
                          ('claude-haiku-5-5', dict(fresh=100_001, read=M, write=M, out=M, raw=split(0, M)))):
            std = price_observation(obs(harness='claude', provider='anthropic', model=model, **kw, tariff={'inference_geo': 'global'}), table)
            us = price_observation(obs(harness='claude', provider='anthropic', model=model, **kw, tariff={'inference_geo': 'us'}), table)
            self.assertIsNotNone(us['cost'], model)
            for part in std['parts']:
                self.assertAlmostEqual(us['parts'][part], std['parts'][part] * 1.1)
        long_us = price_observation(obs(harness='claude', provider='anthropic', model='claude-haiku-5-5', fresh=100_001, out=M,
                                        tariff={'inference_geo': 'us'}), table)
        self.assertAlmostEqual(long_us['cost'], (100_001 * 0.5 + M * 2.5) / 1e6 * 1.1)  # the >100K tier, times 1.1

    def test_packaged_ultrafast_tiers_and_their_combinations(self):
        table = load_prices()
        for model, std, long_ in (('gpt-6.1-sol', (12, .6, 60), (24, 1.2, 90)), ('gpt-6-astra', (60, 6, 300), (120, 12, 450))):
            def cost(**kw):
                return price_observation(obs(harness='codex', provider='openai', model=model, tariff={'service_tier': 'ultrafast'}, **kw), table)
            r = cost(fresh=1000, read=2000, out=3000)
            self.assertAlmostEqual(r['cost'], (1000 * std[0] + 2000 * std[1] + 3000 * std[2]) / 1e6, msg=model)
            r = cost(fresh=272_001, read=1000, out=M)
            self.assertAlmostEqual(r['cost'], (272_001 * long_[0] + 1000 * long_[1] + M * long_[2]) / 1e6, msg=model)
            r = cost(fresh=1000, write=500, out=10)  # no published ultrafast cache-write rate: partial, never a guess
            self.assertEqual((r['status'], r['cost'], r['parts']['cache_write']), ('partial', None, None))
            r = cost(fresh=272_001, write=500, out=10)
            self.assertEqual((r['status'], r['cost']), ('partial', None))
            # fast + long context and flex + long context stay unpriced
            for tier in ('fast', 'flex'):
                if tier in ('fast', 'flex') and model in ('gpt-6.1-sol', 'gpt-6-astra'):
                    r = price_observation(obs(harness='codex', provider='openai', model=model, fresh=272_001, out=M,
                                              tariff={'service_tier': tier}), table)
                    self.assertEqual((r['status'], r['cost']), ('unpriced', None), (model, tier))

    def test_modifier_long_context_is_validated(self):
        bad = copy.deepcopy(TABLE)
        bad['models'][3]['modifiers'] = {'service_tier=ultrafast': {'input': 1, 'long_context': {'input': -1}}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'p.json'
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError, 'long_context'):
                load_prices(path)

    def test_negative_price_and_missing_keys_are_rejected_with_the_model_name(self):
        bad = copy.deepcopy(TABLE)
        bad['models'][1]['long_context']['input'] = -1
        with self.assertRaisesRegex(ValueError, 'claude-long'):
            self.load(bad)
        bad = copy.deepcopy(TABLE)
        bad['models'][3]['output'] = -0.5
        with self.assertRaisesRegex(ValueError, 'gpt-x'):
            self.load(bad)
        bad = copy.deepcopy(TABLE)
        del bad['models'][0]['source_url']
        with self.assertRaisesRegex(ValueError, 'claude-x.*source_url'):
            self.load(bad)
        bad = copy.deepcopy(TABLE)
        del bad['local_providers']
        with self.assertRaisesRegex(ValueError, 'local_providers'):
            self.load(bad)
        with self.assertRaises(ValueError):
            self.load(dict(TABLE, schema=2))


class CloudReferenceTests(unittest.TestCase):
    def test_rate_card_is_compact_public_usd_standard_pricing_metadata(self):
        table = copy.deepcopy(TABLE)
        table['retrieved_on'] = 'not-a-date'
        table['models'][0]['retrieved_on'] = '2026-09-15'
        table['models'].append(dict(provider='m5', model='local-model', currency='USD', input=1.0,
                                    output=1.0, free=False, long_context=None))
        table['models'].append(dict(provider='openai', model='bad-long', currency='USD', input=1.0,
                                    output=1.0, free=False, long_context=dict(above_input_tokens='bad', input=2.0, output=None)))
        cards = reference_rate_card(table)
        self.assertEqual({(x['provider'], x['model']) for x in cards},
                         {('anthropic', 'claude-x'), ('anthropic', 'claude-long'), ('anthropic', 'claude-nofast'),
                          ('openai', 'gpt-x'), ('openai', 'gpt-write'), ('openai', 'bad-long')})
        self.assertTrue(all(set(x) == {'provider', 'model', 'input', 'output', 'long_context', 'retrieved_on'} for x in cards))
        self.assertTrue(all('source_url' not in x for x in cards))
        self.assertEqual(next(x for x in cards if x['model'] == 'claude-x')['retrieved_on'], '2026-09-15')
        self.assertTrue(all(x['retrieved_on'] == '2026-09-01' for x in cards if x['model'] not in ('claude-x', 'bad-long')))
        self.assertIsNone(next(x for x in cards if x['model'] == 'bad-long')['retrieved_on'])
        self.assertEqual(next(x for x in cards if x['model'] == 'gpt-x')['long_context'],
                         {'above_input_tokens': 272000, 'input': 4.0, 'output': 15.0})
        self.assertEqual(next(x for x in cards if x['model'] == 'bad-long')['long_context'],
                         {'above_input_tokens': None, 'input': 2.0, 'output': None})



class DoctorCliTests(unittest.TestCase):
    def test_doctor_reports_pricing_and_warnings(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from tokenatlas.__main__ import main
        from tokenatlas.history import History
        stale = copy.deepcopy(TABLE)
        stale['retrieved_on'] = '2020-01-01'
        for e in stale['models']:
            e['retrieved_on'] = '2020-01-01'
        with tempfile.TemporaryDirectory() as d:
            path, db = Path(d) / 'p.json', Path(d) / 'h.sqlite3'
            path.write_text(json.dumps(stale))
            with History(db):
                pass
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(['--db', str(db), 'doctor', '--prices', str(path)])
        self.assertEqual(code, 0)
        pricing = json.loads(out.getvalue())['pricing']
        self.assertEqual(pricing['stale_entries'], len(stale['models']))
        self.assertIn('price entries are older than', ' '.join(pricing['warnings']))


if __name__ == '__main__':
    unittest.main()
