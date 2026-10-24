"""Energy estimate: order-of-magnitude proxy. Every expected number is computed by hand from the small synthetic observations."""
import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas import energy, insights
from tokenatlas.insights import cost_facts
from tokenatlas.report import build_report, render_report
from test_fresh_report import payload
from test_insights import ob, TABLE, by_id

ROOT = Path(__file__).resolve().parent
TEXTS = json.loads((ROOT / 'tokenatlas/report_i18n.json').read_text(encoding='utf-8'))
TEMPLATE = (ROOT / 'tokenatlas/report_template.html').read_text(encoding='utf-8')
CLAUDE = dict(provider='anthropic', harness='claude')


def tokens(fresh=0, cr=0, cw=0, out=0):
    return dict(fresh_input=fresh, cache_read=cr, cache_write=cw, output=out, reasoning=0)


class Constants(unittest.TestCase):
    def test_constants_equal_the_methodology_table(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        rows = dict(re.findall(r'^\| ([^|]+?) \| ([\d,]+) mWh/1k tokens \|', readme, re.M))
        self.assertEqual({k: int(v.replace(',', '')) for k, v in rows.items()},
                         {'Fresh input (prefill)': 390, 'Output (decode)': 1400, 'Cached input (cache read)': 15, 'Cache creation (write)': 490})
        self.assertEqual(energy.PER_1K, dict(fresh_input=390, output=1400, cache_read=15, cache_write=490))
        self.assertEqual(energy.TIERS, dict(haiku=0.3, sonnet=0.6, opus=1.0))
        self.assertEqual(energy.UNCERTAINTY, 3)

    def test_same_tier_multipliers_as_the_documented_constants(self):
        doc = (ROOT / 'docs/energy-constants.md').read_text(encoding='utf-8')
        for tier, mult in energy.TIERS.items():
            self.assertRegex(doc, rf'(?i){tier}[^\n]*\b{mult}\b', tier)


class Arithmetic(unittest.TestCase):
    def test_per_class_parts_of_one_observation(self):
        t = tokens(fresh=2000, cr=10000, cw=1000, out=500)
        self.assertEqual(energy.parts(t), dict(fresh_input=780, output=700, cache_read=150, cache_write=490))
        self.assertEqual(energy.mid_mwh(t), 2120)

    def test_multiplier_scales_every_class(self):
        t = tokens(fresh=2000, cr=10000, cw=1000, out=500)
        self.assertAlmostEqual(energy.mid_mwh(t, 0.6), 1272, 9)
        self.assertAlmostEqual(energy.parts(t, 0.3)['output'], 210, 9)

    def test_unknown_counters_count_as_zero(self):
        self.assertEqual(energy.mid_mwh(dict(fresh_input=None, output=1000, cache_read=None)), 1400)
        self.assertEqual(energy.mid_mwh({}), 0)

    def test_range_is_a_factor_of_three_each_way(self):
        self.assertEqual(energy.bounds(900), (300, 2700))


class Tiers(unittest.TestCase):
    def test_claude_tiers_are_weighted(self):
        for model, mult in (('claude-haiku-4-5', 0.3), ('claude-sonnet-4-6', 0.6), ('claude-opus-4-8', 1.0), ('Claude-Opus-4', 1.0)):
            self.assertEqual(energy.multiplier('anthropic', model), (mult, True), model)

    def test_everything_else_is_unweighted(self):
        for provider, model in (('anthropic', 'claude-x'), ('anthropic', None), ('openrouter', 'anthropic/claude-opus-4'), ('openai', 'gpt-a'), (None, 'claude-opus-4'), ('anthropic', '')):
            self.assertEqual(energy.multiplier(provider, model), (1.0, False), (provider, model))
            self.assertIsNone(energy.tier(provider, model))


class Format(unittest.TestCase):
    def test_snaps_to_one_two_five_per_decade(self):
        for mwh, shown in ((1, '~1 mWh'), (1.4, '~1 mWh'), (1.5, '~2 mWh'), (3.1, '~2 mWh'), (3.2, '~5 mWh'), (7, '~5 mWh'), (7.1, '~10 mWh'),
                           (71580, '~100 Wh'), (23860, '~20 Wh'), (500000, '~500 Wh')):
            self.assertEqual(energy.fmt(mwh), shown, mwh)

    def test_edge_values(self):
        self.assertEqual(energy.fmt(0), '0 mWh')
        self.assertEqual(energy.fmt(-1), '0 mWh')
        self.assertEqual(energy.fmt(0.999), '<1 mWh')
        self.assertEqual(energy.snap(0.4), 0.4)

    def test_exact_decade_boundaries_and_units(self):
        for mwh, shown in ((10, '~10 mWh'), (100, '~100 mWh'), (1000, '~1 Wh'), (10000, '~10 Wh'), (1e6, '~1 kWh'), (1e9, '~1 MWh'), (999, '~1 Wh'), (999999, '~1 kWh')):
            self.assertEqual(energy.fmt(mwh), shown, mwh)


def dataset():
    # sonnet: 100k fresh, 1M read, 20k write, 10k out = (39,000 + 15,000 + 9,800 + 14,000) x 0.6 = 46,680 mWh
    # haiku: 50k out = 70,000 x 0.3 = 21,000; gpt-a (unweighted): 10k fresh = 3,900
    return [ob('s', model='claude-sonnet-4-6', fresh=100000, read=1000000, write=20000, out=10000, **CLAUDE),
            ob('h', model='claude-haiku-4-5', out=50000, **CLAUDE),
            ob('g', model='gpt-a', fresh=10000)]


class Fact(unittest.TestCase):
    def fact(self, rows=None, **kw):
        return by_id(cost_facts(dataset() if rows is None else rows, TABLE, **kw))['energy']

    def test_values(self):
        v = self.fact()['values']
        self.assertAlmostEqual(v['mid_mwh'], 71580, 6)
        self.assertAlmostEqual(v['low_mwh'], 23860, 6)
        self.assertAlmostEqual(v['high_mwh'], 214740, 6)
        parts = {p['part']: p for p in v['parts']}
        self.assertEqual(list(parts), ['fresh_input', 'cache_write', 'cache_read', 'output'])
        self.assertAlmostEqual(parts['fresh_input']['mwh'], 39000 * .6 + 3900, 6)
        self.assertAlmostEqual(parts['output']['mwh'], 14000 * .6 + 21000, 6)
        self.assertAlmostEqual(sum(p['share'] for p in v['parts']), 1, 9)
        self.assertEqual((v['requests'], v['unweighted_requests'], v['tiers']), (3, 1, {'haiku': 1, 'sonnet': 1}))
        self.assertFalse(v['lower_bound'])
        self.assertEqual(self.fact()['provenance'], 'computed')

    def test_unpriced_requests_still_have_energy(self):
        v = self.fact([ob('u', model='mystery', fresh=1000)])['values']  # no price in the table: energy needs none
        self.assertAlmostEqual(v['mid_mwh'], 390, 9)
        self.assertEqual(v['unweighted_requests'], 1)

    def test_zero_tokens_still_state_the_requests_and_unweighted_count(self):
        v = by_id(cost_facts([ob('z', model='gpt-a')], TABLE))['energy']['values']  # as on the page: 0 mWh, but the counts are shown
        self.assertEqual((v['mid_mwh'], v['requests'], v['unweighted_requests']), (0, 1, 1))
        self.assertEqual([p['share'] for p in v['parts']], [0, 0, 0, 0])
        self.assertIn('0 mWh', insights.render_text(cost_facts([ob('z', model='gpt-a')], TABLE)))
        self.assertNotIn('energy', by_id(cost_facts([], TABLE)))

    def test_incomplete_observation_makes_it_a_lower_bound(self):
        rows = dataset()
        rows[1]['complete'] = False
        f = self.fact(rows)
        self.assertTrue(f['values']['lower_bound'])
        self.assertEqual(f['values']['lower_bound_requests'], 1)
        self.assertIn('ins_a_lower', f['assumption_keys'])
        self.assertIn('≥~100 Wh', insights.render_text(cost_facts(rows, TABLE)))
        self.assertNotIn('≥', insights.render_text(cost_facts(dataset(), TABLE)))

    def test_ambiguous_observation_is_left_out(self):
        rows = dataset()
        rows[2]['id_synthetic'] = True
        v = self.fact(rows)['values']
        self.assertAlmostEqual(v['mid_mwh'], 67680, 6)
        self.assertEqual((v['requests'], v['unweighted_requests'], v['ambiguous_requests']), (2, 0, 1))

    def test_window_applies(self):
        rows = dataset()
        rows[0]['ts'] = '2026-08-01T00:00:00+00:00'
        self.assertAlmostEqual(self.fact(rows, start='2026-09-01T00:00:00+00:00')['values']['mid_mwh'], 24900, 6)

    def test_assumptions_in_text_and_both_languages(self):
        f = self.fact()
        text = ' '.join(f['assumptions'])
        for needle in ('not a measurement', '390', '1,400', '15,', '490', 'haiku 0.3', 'sonnet 0.6', 'opus 1', 'factor of 3', 'unweighted: 1 requests', 'not counted again'):
            self.assertIn(needle, text)
        self.assertNotIn('{', f['computation'] + text)
        self.assertEqual(f['price_assumptions'], [])
        self.assertNotIn('ins_a_list', f['assumption_keys'])
        for key in (f['title_key'], f['computation_key'], *f['assumption_keys']):
            for lang in ('sv', 'en'):
                self.assertTrue(TEXTS[lang].get(key), (lang, key))
            self.assertEqual(set(re.findall(r'\{(\w+)\}', TEXTS['sv'][key])), set(re.findall(r'\{(\w+)\}', TEXTS['en'][key])), key)
        self.assertIn('inte en mätning', TEXTS['sv']['ins_a_energy_proxy'])

    def test_render_text_states_range_split_and_unweighted_count(self):
        text = insights.render_text(cost_facts(dataset(), TABLE))
        for needle in ('Energy estimate (order of magnitude)', 'mid estimate (order of magnitude, not a measurement): ~100 Wh', 'range (mid / 3 to mid x 3): ~20 Wh to ~200 Wh',
                       'output: ', 'requests: 3; without model weighting (counted with multiplier 1): 1'):
            self.assertIn(needle, text)

    def test_public_form_keeps_the_keys_only(self):
        f = insights.public(cost_facts(dataset(), TABLE))['facts'][-1]
        self.assertEqual(f['id'], 'energy')
        self.assertNotIn('computation', f)
        self.assertIn('unweighted', f['params'])


class ReportPage(unittest.TestCase):
    def report(self, redact=False):
        rows = dataset() + [ob('x', model='internal-secret-model', provider='openrouter', fresh=1000)]
        # A fixed 'now' keeps the fixture (2026-09-03) inside the 30-day insight window whatever today's date is.
        return build_report(rows, {}, redact=redact, table=TABLE, now=datetime(2026, 9, 10, tzinfo=timezone.utc))

    def test_payload_carries_constants_and_a_multiplier_per_claude_model(self):
        e = self.report()['energy']
        self.assertEqual(e['per_1k'], energy.PER_1K)
        self.assertEqual((e['uncertainty'], e['tier_multipliers']), (3, energy.TIERS))
        self.assertEqual(e['multipliers'], {'anthropic': {'claude-sonnet-4-6': 0.6, 'claude-haiku-4-5': 0.3}})  # the others are unweighted: no entry

    def test_payload_is_aggregate_only_and_per_model(self):
        for redact in (False, True):
            rep = self.report(redact)
            self.assertNotIn('energy', rep['columns'])  # no per-row field
            self.assertNotIn('internal-secret-model', json.dumps(rep['energy']))
            self.assertEqual(payload(render_report(rep))['energy'], rep['energy'])

    def test_energy_fact_is_in_both_insight_windows(self):
        for w in self.report()['insights']['windows']:
            self.assertIn('energy', by_id(w))

    def test_template_has_the_card_and_follows_the_filters(self):
        for hook in ('id="energy"', 'id="energy-value"', 'id="energy-range"', 'id="energy-split"', 'id="energy-unweighted"', 'id="energy-lower"', 'id="energy-proxy"',
                     'id="energy-how"', 'data-t="nrg_title"', 'function renderEnergy()', 'energyOf(selected)', 'renderSessions();renderEnergy();renderEnvironment()}'):
            self.assertIn(hook, TEMPLATE)
        self.assertNotIn('energy:v=>', TEMPLATE)  # energy has its own filter-following card; it is not a cost fact on the page
        render = re.search(r'function render\(\)\{.*?\n', TEMPLATE).group(0)
        self.assertIn('renderEnergy()', render)  # render() runs on every filter change

    def test_card_texts(self):
        self.assertEqual((TEXTS['sv']['nrg_title'], TEXTS['en']['nrg_title']), ('Energi (uppskattning)', 'Energy (estimate)'))
        for lang in ('sv', 'en'):
            for key in ('nrg_e', 'nrg_p', 'nrg_range', 'nrg_split', 'nrg_unw', 'nrg_lower', 'nrg_proxy', 'nrg_how', 'ins_energy'):
                self.assertTrue(TEXTS[lang][key], (lang, key))
            self.assertIn('README', TEXTS[lang]['nrg_proxy'])
            self.assertIn('{e_out}', TEXTS[lang]['nrg_how'])
        self.assertIn('not a measurement', TEXTS['en']['nrg_proxy'])
        self.assertIn('inte en mätning', TEXTS['sv']['nrg_proxy'])
        for key in (k for k in TEXTS['en'] if k.startswith('nrg_')):
            self.assertEqual(set(re.findall(r'\{(\w+)\}', TEXTS['sv'][key])), set(re.findall(r'\{(\w+)\}', TEXTS['en'][key])), key)


if __name__ == '__main__':
    unittest.main()
