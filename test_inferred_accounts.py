"""Counter labels are scoped inference; raw status remains provider data."""
import unittest
import json
import tempfile
from pathlib import Path
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

    def test_old_quota_event_cannot_replace_a_current_request(self):
        rows = self.rows()
        reset = datetime.fromisoformat(RESET)
        next_reset=(reset+timedelta(days=7)).isoformat()
        rows.append(req('current',(reset+timedelta(seconds=2)).isoformat(),q=quota(week=1,resets=next_reset)))
        old=req('old-event',(reset+timedelta(seconds=4)).isoformat(),q=dict(quota(week=99),status='event'))
        snapshots=quota_share.snapshots_from_records(rows,events=[old])
        self.assertTrue(snapshots.events[0].get('straggler'))
        status=quota_status.collect(rows,events=[old],snapshots=snapshots,now=reset+timedelta(seconds=10))
        self.assertEqual([(r['used_percent'],r['resets_at']) for r in status],[(1,next_reset)])

    def test_new_instance_reported_only_as_event_replaces_stale_counters(self):
        rows=self.rows();reset=datetime.fromisoformat(RESET);next_reset=(reset+timedelta(days=7)).isoformat()
        event=req('new-event',(reset+timedelta(seconds=2)).isoformat(),q=dict(quota(week=1,resets=next_reset),status='event'))
        snapshots=quota_share.snapshots_from_records(rows,events=[event])
        status=quota_status.collect(rows,events=[event],snapshots=snapshots,now=reset+timedelta(seconds=10))
        self.assertEqual([(r['used_percent'],r['resets_at']) for r in status],[(1,next_reset)])
        self.assertNotIn('inferred_account',status[0])
        unknown=req('unmapped',(reset+timedelta(seconds=4)).isoformat(),q=dict(quota(week=45,resets=None),status='event'))
        status=quota_status.collect(rows,events=[event,unknown],snapshots=snapshots,now=reset+timedelta(seconds=10))
        self.assertEqual([(r['used_percent'],r['resets_at']) for r in status],[(45,None)])

    def test_claude_sidecar_straggler_cannot_replace_current_window(self):
        for session in ('s',None,''):
            with self.subTest(session=session):self.claude_straggler(session)

    def claude_straggler(self,session):
        reset=datetime.fromisoformat(RESET);next_reset=(reset+timedelta(days=7)).isoformat()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'claude-quota.jsonl'
            values=[(reset-timedelta(hours=1),99,RESET),(reset+timedelta(seconds=2),1,next_reset),(reset+timedelta(seconds=4),99,RESET)]
            path.write_text(''.join(json.dumps(dict(ts=t.isoformat(),seven_day=dict(used_percent=used,resets_at=due),**({'session':session} if session is not None else {})))+'\n' for t,used,due in values))
            snapshots=quota_share.snapshots_from_records([],claude=path)
            self.assertTrue(snapshots[-1].get('straggler'))
            status=quota_status.collect(claude=path,snapshots=snapshots,now=reset+timedelta(seconds=10))
            self.assertEqual([(r['used_percent'],r['resets_at']) for r in status],[(1,next_reset)])

    def test_claude_sidecar_counters_keep_separate_latest_raw_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'claude-quota.jsonl'
            values=[]
            for i in range(20):
                for offset,session,used in ((0,'a',30+i),(1,'b',90)):
                    values.append(dict(ts=iso(2*i+offset),session=session,seven_day=dict(used_percent=used,resets_at=RESET)))
            path.write_text(''.join(json.dumps(row)+'\n' for row in values))
            snapshots=quota_share.snapshots_from_records([],claude=path)
            status=quota_status.collect(claude=path,snapshots=snapshots,now=datetime(2026,9,4,tzinfo=timezone.utc))
            self.assertEqual([(r.get('inferred_account'),r['used_percent']) for r in status],[(1,49),(2,90)])

if __name__ == '__main__':unittest.main()
