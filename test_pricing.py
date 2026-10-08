import copy
import json
import tempfile
import unittest
from pathlib import Path

from tokenatlas.pricing import (load_prices, price_observation, price_vector, reference_rate_card,
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
        self.assertAlmostEqual(r['cost'], 5.0)  # input 1M*2 + write 1M*3
        r = self.price(obs(harness='pi', provider='openai', model='gpt-x', fresh=1, write=5, raw={'input_tokens': 6}))
        self.assertEqual((r['status'], r['parts']['cache_write'], r['cost']), ('partial', None, None))

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



if __name__ == '__main__':
    unittest.main()
