"""Counter labels are scoped inference; raw status remains provider data."""
import unittest
from datetime import datetime, timezone
from tokenatlas import quota_share, quota_status
import test_quota_share as fixtures
from test_quota_share import TABLE, iso, quota, req

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

if __name__ == '__main__':unittest.main()
