import unittest

from tokenatlas import pricing, shared


class SharedTests(unittest.TestCase):
    def test_call_is_computed_once_per_scope_and_by_identity(self):
        seen = []

        def work(rows, k):
            seen.append(1)
            return [k]
        rows = [1]
        self.assertEqual(shared.call(work, rows, 1), [1])
        self.assertEqual(shared.call(work, rows, 1), [1])
        self.assertEqual(len(seen), 2)  # outside a scope nothing is shared
        with shared.scope():
            first = shared.call(work, rows, 1)
            self.assertIs(shared.call(work, rows, 1), first)
            shared.call(work, [1], 1)
            shared.call(work, rows, 2)
        self.assertEqual(len(seen), 5)

    def test_history_key_change_drops_derived_results(self):
        with shared.scope():
            self.assertEqual(shared.history('a', lambda: 1), 1)
            self.assertEqual(shared.history('a', lambda: 2), 1)
            self.assertEqual(shared.history('b', lambda: 3), 3)

    def test_pricing_in_a_scope_equals_plain_and_copies(self):
        table = pricing.load_prices()
        m = table['models'][0]
        obs = dict(provider=m['provider'], model=m['model'], harness='codex', tariff={}, raw_usage={},
                   tokens=dict(fresh_input=10, cache_read=5, cache_write=0, output=7))
        plain = pricing.price_observation(obs, table)
        with shared.scope():
            one = pricing.price_observation(obs, table)
            one['parts']['input'] = -1
            two = pricing.price_observation(obs, table)
        self.assertEqual(two, plain)


if __name__ == '__main__':
    unittest.main()
