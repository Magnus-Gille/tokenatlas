"""Counter labels are scoped inference; raw status remains provider data."""
import unittest
from datetime import datetime, timedelta, timezone
from tokenatlas import quota_share, quota_status
import test_quota_share as fixtures
from test_quota_share import TABLE, iso, quota, req
from test_quota_share import RESET

class InferredAccounts(unittest.TestCase):
    def rows(self):
        return fixtures.Shares().orchestration(other_account=True)

    def test_two_counters_have_stable_window_labels_and_separate_raw_status(self):
        rows = self.rows()
        snapshots = quota_share.snapshots_from_records(rows)
        windows = quota_share.windows(snapshots)
        self.assertEqual({w.get('inferred_account') for w in windows}, {1, 2})
        self.assertEqual(quota_share.windows(quota_share.snapshots_from_records(rows)), windows)
        status = quota_status.collect(rows, snapshots=snapshots, now=datetime(2026, 9, 4, tzinfo=timezone.utc))
        self.assertEqual({r.get('inferred_account') for r in status}, {1, 2})
        self.assertEqual(sorted(r['used_percent'] for r in status), [18, 99])
        self.assertEqual({r['plan_type'] for r in status}, {'pro'})

    def test_single_counter_has_no_account_identity_claim(self):
        rows = fixtures.Shares().orchestration()
        snapshots = quota_share.snapshots_from_records(rows)
        self.assertTrue(all('inferred_account' not in w for w in quota_share.windows(snapshots)))
        status = quota_status.collect(rows, snapshots=snapshots, now=datetime(2026, 9, 4, tzinfo=timezone.utc))
        self.assertEqual(len(status), 1)
        self.assertNotIn('inferred_account', status[0])

    def test_latest_raw_reading_can_decrease_without_becoming_peak(self):
        rows = self.rows()
        rows.append(req('last', iso(31), session='s9', q=quota(week=97)))
        snapshots = quota_share.snapshots_from_records(rows)
        status = quota_status.collect(rows, snapshots=snapshots, now=datetime(2026, 9, 4, tzinfo=timezone.utc))
        self.assertEqual(sorted(r['used_percent'] for r in status), [18, 97])

    def test_pre_reset_straggler_cannot_replace_the_current_window(self):
        rows = self.rows()
        reset = datetime.fromisoformat(RESET)
        next_reset = (reset + timedelta(days=7)).isoformat()
        rows += [req('current', (reset+timedelta(seconds=2)).isoformat(), session='s1', q=quota(week=1,resets=next_reset)),
                 req('straggler', (reset+timedelta(seconds=4)).isoformat(), session='s1', q=quota(week=99))]
        snapshots = quota_share.snapshots_from_records(rows)
        self.assertTrue(snapshots[-1].get('straggler'))
        status = quota_status.collect(rows, snapshots=snapshots, now=reset+timedelta(seconds=10))
        self.assertEqual([(r['used_percent'],r['resets_at']) for r in status], [(1,next_reset)])
        self.assertNotIn('inferred_account', status[0])

if __name__ == '__main__':unittest.main()
