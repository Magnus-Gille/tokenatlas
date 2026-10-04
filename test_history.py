import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from tokenatlas.history import History, _encode, _decode, _owned_by_current_user, normalize, summarize, merge_observations
from tokenatlas.why import AttributionRecord
from test_why_harnesses import create_opencode_db, pi_message, write_pi_session


def record(call_id='req', **overrides):
    args = dict(harness='claude', provider='anthropic',
                timestamp=datetime(2026, 9, 3, 10, tzinfo=timezone.utc),
                session_id='session', call_id=call_id, model='test-model', effort='high',
                project='app', entrypoint='cli', thread_kind='main', agent='main',
                fresh_input=10, cache_read=20, cache_write=0, output=5, reasoning=0)
    args.update(overrides)
    return AttributionRecord(**args)


def write_claude(path, request='req', output=5):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows=[{'type':'user','uuid':'prompt-1','message':{'role':'user','content':'PRIVATE PROMPT'}},
          {'type':'assistant','uuid':'response-'+request,'requestId':request,
           'sessionId':'session','cwd':'/work/client/app','version':'test-version',
           'timestamp':'2026-09-03T10:00:00Z','message':{'id':'msg-'+request,'model':'test-model','stop_reason':'end_turn',
             'usage':{'input_tokens':10,'cache_read_input_tokens':20,
                      'cache_creation_input_tokens':0,'output_tokens':output}}}]
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db=self.root/'state/history.sqlite3'

    def test_claude_revision_changes_fingerprint_and_forces_one_reread(self):
        import tokenatlas.history as history
        self.assertEqual(history.HARNESS_REVISION.get('claude'), 3)
        source=self.root/'logs/a.jsonl';write_claude(source)
        with patch.dict(history.HARNESS_REVISION,clear=False):
            history.HARNESS_REVISION.pop('claude')
            old=History.fingerprint(source,harness='claude')
        new=History.fingerprint(source,harness='claude')
        self.assertNotEqual(old,new)
        self.assertNotEqual(History.fingerprint(source,harness='opencode'),History.fingerprint(source))
        with patch.dict(history.HARNESS_REVISION,clear=False):
            history.HARNESS_REVISION.pop('claude')
            with History(self.db) as h:
                self.assertEqual(h.refresh('claude',source.parent)['files_skipped'],0)
                self.assertEqual(h.refresh('claude',source.parent)['files_skipped'],1)
        with History(self.db) as h:
            self.assertEqual(h.refresh('claude',source.parent)['files_skipped'],0)
            self.assertEqual(h.refresh('claude',source.parent)['files_skipped'],1)

    def test_codex_revision_changes_fingerprint_and_forces_one_reread(self):
        import tokenatlas.history as history
        from test_why_codex import _write_rollout,_meta,_context,_tokens,_tier
        self.assertEqual(history.HARNESS_REVISION.get('codex'), 7)
        source=self.root/'logs/rollout-one.jsonl'
        counts={'input_tokens':10,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':2}
        _write_rollout(source,[_meta(),_context('2026-09-03T10:00:00Z','model','high','/work/project'),
            _tier('2026-09-03T10:00:00Z','priority'),_tokens('2026-09-03T10:00:01Z',1,counts,counts)])
        with patch.dict(history.HARNESS_REVISION,clear=False):
            history.HARNESS_REVISION['codex']=1
            old=History.fingerprint(source,harness='codex')
        self.assertNotEqual(old,History.fingerprint(source,harness='codex'))
        # Revision 1 is the old collector, which never read the tier: the stored observation has no tariff.
        with patch.dict(history.HARNESS_REVISION,clear=False),patch('tokenatlas.why._codex_tariff',return_value=None):
            history.HARNESS_REVISION['codex']=1
            with History(self.db) as h:
                self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],0)
                self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],1)
                self.assertEqual([r['tariff'] for r in h.records()],[None])
        # Revision 2 re-reads the unchanged file once and enriches the stored observation with the tier.
        with History(self.db) as h:
            self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],0)
            self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],1)
            self.assertEqual([r['tariff'] for r in h.records()],[{'service_tier':'fast'}])

    def test_codex_revision_3_rereads_once_and_adds_quota(self):
        import tokenatlas.history as history
        from test_why_codex import _write_rollout,_meta,_context,_quota_call,_limits,_window
        source=self.root/'logs/rollout-quota.jsonl'
        _write_rollout(source,[_meta(),_context('2026-09-03T10:00:00Z','model','high','/work/project'),
            _quota_call('2026-09-03T10:00:01Z',1,_limits(primary=_window(4,10080,1791580407)))])
        # Revision 2 is the old collector, which never read rate_limits: the stored observation has no quota.
        with patch.dict(history.HARNESS_REVISION,clear=False),patch('tokenatlas.why._codex_quota',return_value=None):
            history.HARNESS_REVISION['codex']=2
            with History(self.db) as h:
                self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],0)
                self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],1)
                self.assertEqual([r['quota'] for r in h.records()],[None])
        with History(self.db) as h:
            self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],0)
            self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],1)  # unchanged logs: nothing re-read, no new rows
            records=h.records()
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]['quota']['windows'][0]['resets_at'],'2026-10-09T21:13:27+00:00')
            self.assertEqual(h.refresh('codex',source.parent)['files_skipped'],1)
            self.assertEqual(len(h.records()),1)

    def test_zero_or_empty_partial_usage_is_retained_as_unknown(self):
        for usage in ({'output_tokens': 0}, {}):
            with self.subTest(usage=usage):
                source=self.root/'logs/a.jsonl';write_claude(source)
                rows=[json.loads(x) for x in source.read_text().splitlines()]
                rows[-1]['message']['usage']=usage
                source.write_text(''.join(json.dumps(x)+'\n' for x in rows))
                with History(self.db) as h:
                    h.refresh('claude',source.parent)
                    self.assertEqual(len(h.records()),1)
                    self.assertFalse(h.records()[0]['complete'])
                    self.assertIn('missing_token_fields',h.records()[0]['warnings'])

    def test_refresh_replaces_partial_claude_request_with_final_row(self):
        source=self.root/'logs/a.jsonl';write_claude(source,output=3)
        rows=[json.loads(x) for x in source.read_text().splitlines()]
        rows[-1]['message']['stop_reason']=None
        source.write_text(''.join(json.dumps(x)+'\n' for x in rows))
        with History(self.db) as h:
            h.refresh('claude',source.parent)
            [r]=h.records()
            self.assertIn('output_not_final',r['warnings']);self.assertFalse(r['complete'])
            final=json.loads(json.dumps(rows[-1]));final['timestamp']='2026-09-03T10:00:05Z'
            final['message']['stop_reason']='end_turn';final['message']['usage']['output_tokens']=90
            with source.open('a') as f: f.write(json.dumps(final)+'\n')
            h.refresh('claude',source.parent)
            [r]=h.records()
            self.assertTrue(r['output_final']);self.assertTrue(r['complete'])
            self.assertNotIn('output_not_final',r['warnings']);self.assertEqual(r['tokens']['output'],90)

    def test_ownership_check_degrades_safely_without_posix_getuid(self):
        with patch.object(os,'getuid',None,create=True):
            self.assertTrue(_owned_by_current_user(type('Info',(),{'st_uid':123})()))

    def test_invalid_iteration_container_does_not_claim_completeness(self):
        from tokenatlas import why
        for invalid in ('bad', {'type':'message','output_tokens':999}):
            raw=why._sanitize_usage({'input_tokens':1,'output_tokens':2,
                'cache_read_input_tokens':0,'cache_creation_input_tokens':0,'iterations':invalid})
            item=normalize(record(raw_usage=raw),'m')
            self.assertFalse(item['complete'])
            self.assertIn('invalid_iteration',item['warnings'])

    def test_missing_tokens_are_unknown_and_iterations_not_assumed_complete(self):
        item=normalize(record(), 'machine')
        self.assertIsNone(item['tokens']['fresh_input'])
        self.assertIsNone(item['tokens']['reasoning'])
        self.assertFalse(item['complete'])
        raw={'input_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':0,'output_tokens':5,
             'iterations':[{'type':'message','output_tokens':5}, {'type':'advisor_message','model':'other','output_tokens':40}]}
        item=normalize(record(raw_usage=raw), 'machine')
        self.assertIn('nontrivial_iterations',item['warnings'])
        self.assertFalse(item['complete'])
        self.assertEqual(item['raw_usage']['iterations'][1]['output_tokens'],40)

    def test_import_is_idempotent_copies_and_retention_preserve_history(self):
        source=self.root/'claude/project/a.jsonl';write_claude(source)
        with History(self.db) as h:
            first=h.refresh('claude',source.parent.parent)
            second=h.refresh('claude',source.parent.parent)
            self.assertEqual(first['files_parsed'],1)
            self.assertEqual(second['files_parsed'],0)
            duplicate=source.with_name('copy.jsonl');duplicate.write_bytes(source.read_bytes())
            h.refresh('claude',source.parent.parent)
            self.assertEqual(len(h.records()),1)
            self.assertEqual(len(h.records()[0]['sources']),2)
            self.assertNotIn('PRIVATE PROMPT',json.dumps(h.records()))
            source.unlink();duplicate.unlink()
            h.refresh('claude',source.parent.parent)
            self.assertEqual(len(h.records()),1)
            self.assertGreater(h.doctor()['missing_source_files'],0)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.db.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(self.db.parent.stat().st_mode),0o700)

    def test_streaming_update_never_regresses_and_partial_line_is_retried(self):
        source=self.root/'logs/a.jsonl';write_claude(source,output=1)
        with History(self.db) as h:
            h.refresh('claude',source.parent)
            write_claude(source,output=10)
            with source.open('a') as f:f.write('{"type":')
            result=h.refresh('claude',source.parent)
            self.assertEqual(result['partial_lines'],1)
            self.assertEqual(h.records()[0]['tokens']['output'],10)
            self.assertEqual(h.refresh('claude',source.parent)['files_parsed'],1)
            write_claude(source,output=2)
            h.refresh('claude',source.parent)
            self.assertEqual(h.records()[0]['tokens']['output'],10)

    def test_missing_root_is_visible_not_zero_coverage(self):
        with History(self.db) as h:
            result=h.refresh('codex',self.root/'absent')
            self.assertEqual(result['status'],'missing')
            self.assertIsNone(result['last_success'])
            self.assertFalse(h.doctor()['coverage_complete'])

    def test_unreadable_directory_is_partial_not_success(self):
        source=self.root/'logs/a.jsonl';write_claude(source)
        def denied_walk(root, onerror):
            onerror(PermissionError('unreadable child directory'))
            return []
        with History(self.db) as h:
            with patch('tokenatlas.history.os.walk', side_effect=denied_walk):
                result=h.refresh('claude',source.parent)
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['read_errors'],1)
            self.assertIsNone(result['last_success'])

    def test_iteration_replay_does_not_invent_raw_snapshots(self):
        from tokenatlas.history import merge_usage
        first={'iterations':[{'type':'message','input_tokens':10,'output_tokens':5}]}
        second={'iterations':[{'type':'message','input_tokens':5,'output_tokens':10}]}
        merged=merge_usage(first,second)
        for _ in range(3):
            merged=merge_usage(merged,second)
        self.assertCountEqual(merged['iteration_snapshots'],[first['iterations'],second['iterations']])
        self.assertEqual(merged['iterations'][0]['output_tokens'],10)
        self.assertEqual(merged['iterations'][0]['input_tokens'],10)

    def test_usage_without_timestamp_is_reported_as_omitted(self):
        source=self.root/'logs/a.jsonl';write_claude(source)
        rows=[json.loads(x) for x in source.read_text().splitlines()]
        rows[-1].pop('timestamp')
        source.write_text(''.join(json.dumps(x)+'\n' for x in rows))
        with History(self.db) as h:
            for _ in range(2):
                result=h.refresh('claude',source.parent)
                self.assertEqual(result['status'],'partial')
                self.assertEqual(result['unparsed_usage_lines'],1)
                self.assertIsNone(result['last_success'])
            self.assertEqual(h.records(),[])

    def test_invalid_iteration_entries_are_not_dropped_into_complete_totals(self):
        from tokenatlas import why
        raw=why._sanitize_usage({'input_tokens':1,'output_tokens':2,
            'cache_read_input_tokens':0,'cache_creation_input_tokens':0,'iterations':[None]})
        self.assertEqual(raw['iterations'],[{}])
        item=normalize(record(raw_usage=raw),'m')
        self.assertFalse(item['complete'])
        self.assertIn('invalid_iteration',item['warnings'])

    def test_schema_version_refused(self):
        with History(self.db) as h:
            h.connection.execute('PRAGMA user_version=999')
        with self.assertRaisesRegex(ValueError,'version'):
            with History(self.db): pass

    def test_concurrent_imports_lose_no_records(self):
        first=self.root/'one/a.jsonl';second=self.root/'two/b.jsonl'
        write_claude(first,'one');write_claude(second,'two')
        command=[sys.executable,'-B','-m','tokenatlas','--db',str(self.db),'refresh','--harness','claude','--root']
        children=[subprocess.Popen(command+[str(p.parent)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for p in (first,second)]
        results=[(p, p.communicate(timeout=30)) for p in children]
        for p, (out,err) in results:
            self.assertEqual(p.returncode,0,err)
        with History(self.db) as h:
            self.assertEqual(len(h.records()),2)

    def test_refresh_failure_rolls_back_observations_and_checkpoint(self):
        source=self.root/'logs/a.jsonl';write_claude(source)
        with History(self.db) as h:
            with patch('tokenatlas.history.normalize', side_effect=RuntimeError('injected normalization failure')):
                with self.assertRaisesRegex(RuntimeError,'injected'):
                    h.refresh('claude', source.parent)
            self.assertEqual(h.records(),[])
            self.assertEqual(h.connection.execute('SELECT count(*) FROM files').fetchone()[0],0)
            self.assertEqual(h.refresh('claude',source.parent)['files_parsed'],1)
            self.assertEqual(len(h.records()),1)

    def test_unknown_usage_can_be_completed_without_duplicate(self):
        source=self.root/'logs/a.jsonl';write_claude(source)
        rows=[json.loads(x) for x in source.read_text().splitlines()]
        del rows[-1]['message']['usage']['cache_read_input_tokens']
        source.write_text(''.join(json.dumps(x)+'\n' for x in rows))
        with History(self.db) as h:
            h.refresh('claude',source.parent)
            self.assertIsNone(h.records()[0]['tokens']['cache_read'])
            write_claude(source)
            h.refresh('claude',source.parent)
            self.assertEqual(len(h.records()),1)
            self.assertEqual(h.records()[0]['tokens']['cache_read'],20)
            self.assertTrue(h.records()[0]['complete'])

    def test_malformed_diagnostics_survive_unchanged_refresh(self):
        source=self.root/'logs/a.jsonl';write_claude(source)
        with source.open('a') as f:f.write('bad json\n')
        with History(self.db) as h:
            first=h.refresh('claude',source.parent)
            second=h.refresh('claude',source.parent)
            self.assertEqual(first['status'],'partial')
            self.assertEqual(second['status'],'partial')
            self.assertEqual(second['malformed_lines'],1)
            self.assertEqual(second['files_parsed'],0)
            self.assertEqual(len(h.records()),1)

    def test_file_changed_while_reading_does_not_commit_or_checkpoint(self):
        source=self.root/'logs/a.jsonl';write_claude(source)
        with History(self.db) as h:
            with patch.object(h,'fingerprint',side_effect=['before','after']):
                result=h.refresh('claude',source.parent)
            self.assertEqual(result['changed_during_read'],1)
            self.assertEqual(result['status'],'partial')
            self.assertEqual(h.records(),[])
            self.assertEqual(h.connection.execute('SELECT count(*) FROM files').fetchone()[0],0)
            h.refresh('claude',source.parent)
            self.assertEqual(len(h.records()),1)

    def test_iteration_reordering_retains_both_original_snapshots(self):
        base={'input_tokens':1,'cache_read_input_tokens':0,'cache_creation_input_tokens':0,'output_tokens':20}
        first=[{'type':'message','model':'executor','output_tokens':10}]
        second=[{'type':'advisor_message','model':'advisor','output_tokens':5},
                {'type':'message','model':'executor','output_tokens':20}]
        a=normalize(record(raw_usage={**base,'iterations':first}),'m')
        b=normalize(record(raw_usage={**base,'iterations':second}),'m')
        for merged in (merge_observations(a,b),merge_observations(b,a)):
            self.assertIn(first,merged['raw_usage']['iteration_snapshots'])
            self.assertIn(second,merged['raw_usage']['iteration_snapshots'])
            self.assertEqual(merged['raw_usage']['iterations'],second)
            self.assertFalse(merged['complete'])

    def test_second_pass_read_error_does_not_checkpoint_file(self):
        source=self.root/'logs/a.jsonl';write_claude(source)
        original=Path.open
        def failing_open(path,*args,**kwargs):
            if path==source and kwargs.get('errors')=='replace':
                raise OSError('injected second-pass failure')
            return original(path,*args,**kwargs)
        with History(self.db) as h:
            with patch.object(Path,'open',failing_open):
                result=h.refresh('claude',source.parent)
            self.assertEqual(result['read_errors'],1)
            self.assertEqual(h.records(),[])
            self.assertEqual(h.connection.execute('SELECT count(*) FROM files').fetchone()[0],0)
            self.assertEqual(h.refresh('claude',source.parent)['files_parsed'],1)
            self.assertEqual(len(h.records()),1)

    def test_claude_row_uuid_identity_is_explicitly_incomplete(self):
        source=self.root/'logs/a.jsonl';write_claude(source)
        rows=[json.loads(x) for x in source.read_text().splitlines()]
        assistant=rows[-1];assistant.pop('requestId');assistant['message'].pop('id')
        duplicate=json.loads(json.dumps(assistant));duplicate['uuid']='other-row'
        rows.append(duplicate);source.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        with History(self.db) as h:
            h.refresh('claude',source.parent)
            self.assertTrue(all(r['id_synthetic'] for r in h.records()))
            totals=summarize(h.records())['totals']
            self.assertIsNone(totals['tokens'])
            self.assertEqual(totals['known_tokens'],0)
            self.assertEqual(totals['ambiguous_identity_tokens'],70)
            self.assertEqual(totals['ambiguous_identity_observations'],2)
            self.assertIsNone(totals['token_fields']['output'])

    def test_codex_without_ordinal_survives_non_usage_line_inserted_in_copy(self):
        from test_why_codex import _write_rollout,_meta,_context,_tokens
        first=self.root/'logs/rollout-one.jsonl'
        counts={'input_tokens':10,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':2}
        rows=[_meta(),_context('2026-09-03T10:00:00Z','model','high','/work/project'),
              _tokens('2026-09-03T10:00:01Z',None,counts,counts)]
        _write_rollout(first,rows)
        with History(self.db) as h:
            h.refresh('codex',first.parent)
            _write_rollout(first.with_name('rollout-copy.jsonl'),[{'type':'harmless'}]+rows)
            h.refresh('codex',first.parent)
            self.assertEqual(len(h.records()),1)

    def test_opencode_sqlite_refresh_is_idempotent_and_private(self):
        db = self.root/'opencode.db'
        connection=create_opencode_db(db)
        connection.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?)',
            ('session','project',None,'/work/app','1.18.32',1,2))
        data={'role':'assistant','providerID':'openai','modelID':'gpt-test','agent':'build',
              'variant':'high','time':{'created':1788429600000,'completed':1788429601000},
              'tokens':{'input':10,'output':5,'reasoning':3,'cache':{'read':20,'write':2},'total':37},
              'content':'PRIVATE ASSISTANT TEXT'}
        connection.execute('INSERT INTO message VALUES (?,?,?,?,?)',
            ('message','session',1788429600000,1788429601000,json.dumps(data)))
        connection.commit();connection.close()
        with History(self.db) as h:
            first=h.refresh('opencode',db)
            second=h.refresh('opencode',db)
            self.assertEqual(first['observations_seen'],1)
            self.assertEqual(second['files_skipped'],1)
            self.assertEqual(len(h.records()),1)
            saved=json.dumps(h.records())
            self.assertNotIn('PRIVATE ASSISTANT TEXT',saved)
            self.assertEqual(h.records()[0]['source_type'],'database')

    def test_sqlite_fingerprint_ignores_shm_but_tracks_db_and_wal(self):
        db=self.root/'fp.db'
        wal=Path(str(db)+'-wal'); shm=Path(str(db)+'-shm')
        for f in (db,wal,shm): f.write_bytes(b'one')
        fp=lambda: History.fingerprint(db,include_sqlite_sidecars=True)
        base=fp()
        shm.write_bytes(b'different content')
        os.utime(shm,ns=(1,10**18))
        self.assertEqual(fp(),base)
        wal.write_bytes(b'one-more')
        changed_wal=fp()
        self.assertNotEqual(changed_wal,base)
        os.utime(wal,ns=(1,2*10**18))
        self.assertNotEqual(fp(),changed_wal)
        db.write_bytes(b'db changed')
        self.assertNotEqual(fp(),changed_wal)

    def test_opencode_wal_refresh_with_shm_rebuild_is_ok_and_then_skipped(self):
        db=self.root/'wal-opencode.db'
        connection=create_opencode_db(db)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?)',
            ('session','project',None,'/work/app','1.18.32',1,2))
        data={'role':'assistant','providerID':'openai','modelID':'gpt-test','agent':'build',
              'time':{'created':1788429600000,'completed':1788429601000},
              'tokens':{'input':10,'output':5,'reasoning':3,'cache':{'read':20,'write':2},'total':37}}
        connection.execute('INSERT INTO message VALUES (?,?,?,?,?)',
            ('message','session',1788429600000,1788429601000,json.dumps(data)))
        connection.commit()  # writer stays open: WAL is un-checkpointed
        try:
            self.assertGreater(Path(str(db)+'-wal').stat().st_size,0)
            with History(self.db) as h:
                first=h.refresh('opencode',db)
                second=h.refresh('opencode',db)
        finally:
            connection.close()
        self.assertEqual(first['status'],'ok')
        self.assertEqual(first['changed_during_read'],0)
        self.assertEqual(first['observations_seen'],1)
        self.assertEqual(second['files_skipped'],1)
        self.assertEqual(second['files_parsed'],0)

    def test_opencode_schema_error_is_reported_as_partial_import(self):
        db=self.root/'unsupported-opencode.db'
        connection=sqlite3.connect(db)
        connection.execute('CREATE TABLE unrelated (id INTEGER)')
        connection.close()
        with History(self.db) as h:
            result=h.refresh('opencode',db)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['read_errors'],1)
        self.assertIn('no such table',result['errors'][0])

    def test_malformed_opencode_rows_are_reported_as_partial_import(self):
        db=self.root/'malformed-opencode.db'
        connection=create_opencode_db(db)
        connection.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?)',
            ('session','project',None,'/work/app','1.0',1,2))
        rows=[
            ('invalid-json','{'),
            ('missing-tokens',json.dumps({'role':'assistant','time':{'completed':1788429601000}})),
            ('invalid-time',json.dumps({'role':'assistant','time':{'completed':'bad'},
                                        'tokens':{'input':1,'output':1,'cache':{'read':0,'write':0}}})),
        ]
        connection.executemany('INSERT INTO message VALUES (?,?,?,?,?)',
            [(message_id,'session',1788429600000,1788429601000,data) for message_id,data in rows])
        connection.commit();connection.close()
        with History(self.db) as h:
            result=h.refresh('opencode',db)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['malformed_lines'],1)
        self.assertEqual(result['unparsed_usage_lines'],2)
        self.assertIsNone(result['last_success'])

    def test_pi_refresh_deduplicates_forks_and_retains_original_session(self):
        usage={'input':10,'output':5,'cacheRead':20,'cacheWrite':2,'reasoning':1}
        copied=pi_message('entry','response','2026-09-03T10:00:00Z',usage)
        root=self.root/'pi'
        write_pi_session(root/'a-fork.jsonl','fork','2026-09-03T09:00:00Z','/work/fork',[copied])
        write_pi_session(root/'z-original.jsonl','original','2026-09-02T09:00:00Z','/work/original',[copied])
        with History(self.db) as h:
            result=h.refresh('pi',root)
            records=h.records()
            self.assertEqual(result['observations_seen'],2)
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]['session'],'original')
            self.assertEqual(records[0]['project_id'],'/work/original')
            self.assertEqual(records[0]['source_type'],'session')
            self.assertEqual(len(records[0]['sources']),2)

    def test_time_buckets_dst_and_unknowns(self):
        samples=[]
        for index,ts in enumerate(('2026-10-25T00:30:00+00:00','2026-10-25T01:30:00+00:00')):
            item=normalize(record(str(index),timestamp=datetime.fromisoformat(ts)), 'm')
            item['tokens']={'fresh_input':10,'cache_read':20,'cache_write':0,'output':5,'reasoning':None}
            item['complete']=True
            samples.append(item)
        reports=[summarize(samples,unit,'Europe/Stockholm') for unit in ('day','hour','minute')]
        self.assertEqual([r['totals']['known_tokens'] for r in reports],[70,70,70])
        self.assertEqual(len(reports[1]['buckets']),2)
        self.assertNotEqual(reports[1]['buckets'][0]['time'],reports[1]['buckets'][1]['time'])
        self.assertIsNone(summarize([normalize(record(),'m')],'day','UTC')['totals']['tokens'])

    def test_summary_scopes_equal_session_ids_by_harness(self):
        claude=normalize(record('claude',raw_usage={
            'input_tokens':1,'cache_read_input_tokens':0,
            'cache_creation_input_tokens':0,'output_tokens':1}), 'm')
        pi=normalize(record('pi',harness='pi',provider='test',raw_usage={
            'input':1,'cacheRead':0,'cacheWrite':0,'output':1,'reasoning':0}), 'm')
        names={row['name'] for row in summarize([claude,pi])['groups']['session']}
        self.assertEqual(names,{'claude:session','pi:session'})

    def test_scoped_session_filter_round_trips_summary_name(self):
        samples=[
            normalize(record('claude'), 'm'),
            normalize(record('pi',harness='pi',provider='test',raw_usage={
                'input':1,'cacheRead':0,'cacheWrite':0,'output':1,'reasoning':0}), 'm'),
        ]
        with History(self.db) as h:
            for index,item in enumerate(samples):
                h._insert(h.connection,_encode(item))
            self.assertEqual([row['harness'] for row in h.records(session='pi:session')],['pi'])
            self.assertEqual({row['harness'] for row in h.records(session='session')},{'claude','pi'})

class HistoryReviewTests(unittest.TestCase):
    def test_normalize_rejects_non_string_metadata(self):
        secret = {'prompt': 'SECRET'}
        item = normalize(record(provider=secret, model=secret, effort=['SECRET'], agent=secret,
                                entrypoint=secret, thread_kind=secret, session_id=secret,
                                turn_id=secret, cwd=secret, parent_session_id=secret,
                                harness_version=secret), 'm')
        self.assertNotIn('SECRET', json.dumps(item))
        self.assertEqual(normalize(record(harness_version=3), 'm')['harness_version'], '3')
        self.assertEqual((item['model'], item['effort'], item['session']), (None, None, 'unknown'))
        self.assertEqual(normalize(record(model='x' * 300), 'm')['model'], None)

    def test_iteration_model_and_type_are_bounded_printable_strings(self):
        from tokenatlas import why
        raw = {'input_tokens': 1, 'output_tokens': 1, 'iterations': [
            {'type': 'message', 'model': 'x' * 300}, {'type': {'p': 'SECRET'}, 'model': 'ok-model'}]}
        item = normalize(record(raw_usage=raw), 'm')
        self.assertEqual(item['raw_usage']['iterations'],
                         [{'type': 'message'}, {'model': 'ok-model'}])
        self.assertNotIn('SECRET', json.dumps(item))
        self.assertEqual(why._sanitize_usage(raw)['iterations'],
                         [{'type': 'message'}, {'model': 'ok-model'}])

    def test_output_not_final_is_a_lower_bound_and_merges_in_both_orders(self):
        usage = {'input_tokens': 10, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 0,
                 'output_tokens': 5}
        def obs(final, output=5, **kw):
            item = normalize(record(raw_usage=dict(usage, output_tokens=output), output=output,
                                    output_final=final, **kw), 'm')
            return item
        partial, done, unknown = obs(False), obs(True, 90), obs(None, 7)
        self.assertFalse(partial['complete'])
        self.assertFalse(partial['output_final'])
        self.assertIn('output_not_final', partial['warnings'])
        self.assertEqual((partial['confidence']['output'], partial['confidence']['reasoning']),
                         ('lower_bound', 'absent'))
        self.assertEqual(partial['confidence']['fresh_input'], 'observed')
        self.assertTrue(done['complete'])
        self.assertEqual(done['confidence']['output'], 'observed')
        self.assertIsNone(unknown['output_final'])
        self.assertTrue(unknown['complete'])
        for a, b in ((partial, done), (done, partial)):
            merged = merge_observations(a, b)
            self.assertTrue(merged['output_final'])
            self.assertTrue(merged['complete'])
            self.assertEqual(merged['tokens']['output'], 90)
        for a, b in ((partial, unknown), (unknown, partial)):
            merged = merge_observations(a, b)
            self.assertFalse(merged['output_final'])
            self.assertIn('output_not_final', merged['warnings'])
            self.assertEqual(merged['confidence']['output'], 'lower_bound')
        legacy = {k: v for k, v in partial.items() if k != 'output_final'}
        legacy['warnings'] = []
        merged = merge_observations(legacy, unknown)
        self.assertIsNone(merged['output_final'])
        self.assertTrue(merged['complete'])
        merged = merge_observations(legacy, partial)
        self.assertFalse(merged['output_final'])

    def test_collector_version_change_invalidates_file_fingerprints(self):
        import tokenatlas.history as history
        self.assertGreaterEqual(history.COLLECTOR_VERSION, 5)

    def test_total_token_counters_survive_persistence(self):
        item = normalize(record(raw_usage={'input_tokens': 1, 'output_tokens': 1,
                                           'total_input_tokens': 7, 'total_output_tokens': 3}), 'm')
        self.assertEqual(item['raw_usage']['total_input_tokens'], 7)
        self.assertEqual(item['raw_usage']['total_output_tokens'], 3)

    def test_copied_request_owner_is_earliest_then_stored_regardless_of_ids(self):
        early = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)
        late = datetime(2026, 9, 3, 11, tzinfo=timezone.utc)
        def obs(session, ts, parent=None, turn=None):
            return normalize(record(session_id=session, timestamp=ts, parent_session_id=parent, turn_id=turn), 'm')
        for first, second in (('aaa', 'zzz'), ('zzz', 'aaa')):
            with self.subTest(first=first):
                # equal time: an import picks the smaller session whatever the order; a local re-read keeps the stored one
                self.assertEqual(merge_observations(obs(first, early), obs(second, early))['session'], 'aaa')
                self.assertEqual(merge_observations(obs(first, early), obs(second, early), authoritative_turns=True)['session'], first)
                self.assertEqual(merge_observations(obs(first, early), obs(second, late))['session'], first)
                self.assertEqual(merge_observations(obs(first, late), obs(second, early))['session'], second)
        merged = merge_observations(obs('zzz', early, 'zzz'), obs('aaa', late, None))
        self.assertEqual((merged['session'], merged['parent_session']), ('zzz', 'zzz'))

    def test_equal_time_import_merge_session_and_turn_fields_do_not_depend_on_order(self):
        when = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)
        a = normalize(record(session_id='sess-b', timestamp=when, parent_session_id='pb', turn_id='tb'), 'm')
        b = normalize(record(session_id='sess-a', timestamp=when, parent_session_id='pa', turn_id='ta'), 'm')
        for x, y in ((a, b), (b, a)):
            merged = merge_observations(x, y)
            self.assertEqual((merged['session'], merged['parent_session'], merged['turn_id']), ('sess-a', 'pa', 'ta'))

    def test_refresh_same_request_in_two_sessions_is_deterministic(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / 'state/history.sqlite3'
        def put(path, session, ts):
            write_claude(path)
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            rows[-1].update(sessionId=session, timestamp=ts)
            path.write_text(''.join(json.dumps(x) + '\n' for x in rows))
        a, b = self.root / 'logs/a.jsonl', self.root / 'logs/b.jsonl'
        tie = '2026-09-03T10:00:00Z'
        put(a, 'sess-a', tie)
        with History(self.db) as h:
            h.refresh('claude', a.parent)
            put(b, 'sess-b', tie)
            h.refresh('claude', a.parent)
            (item,) = h.records()
            # On a tie the stored (first imported) session wins, whatever the ids are.
            self.assertEqual(item['session'], 'sess-a')
            self.assertEqual(item['sources'], sorted([str(a), str(b)]))
            self.assertEqual(item['tokens']['output'], 5)
            put(b, 'sess-b', '2026-09-03T09:00:00Z')
            h.refresh('claude', a.parent)
            self.assertEqual(h.records()[0]['session'], 'sess-b')
        put(a, 'sess-b', tie); put(b, 'sess-a', tie)
        with History(self.root / 'state/fresh.sqlite3') as h:
            h.refresh('claude', a.parent)
            (item,) = h.records()
            # A fresh import reads a.jsonl first, so the tie goes to it: owners can differ
            # between fresh and incremental imports on an exact tie (accepted, documented).
            self.assertEqual(item['session'], 'sess-b')
            self.assertEqual(item['sources'], sorted([str(a), str(b)]))
            self.assertEqual(item['tokens']['output'], 5)

    def test_hour_buckets_are_ordered_by_instant_across_dst_fall_back(self):
        rows = [normalize(record(str(i), timestamp=ts, raw_usage={
                    'input_tokens': 1, 'cache_read_input_tokens': 0,
                    'cache_creation_input_tokens': 0, 'output_tokens': 1}), 'm')
                for i, ts in enumerate((datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc),
                                        datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
                                        datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc)))]
        for granularity in ('hour', 'minute'):
            times = [b['time'] for b in summarize(rows, granularity, 'Europe/Stockholm')['buckets']]
            self.assertEqual([t[11:16] + t[19:] for t in times],
                             ['02:00+02:00', '02:00+01:00', '03:00+01:00'] if granularity == 'hour'
                             else ['02:30+02:00', '02:30+01:00', '03:30+01:00'])

    def test_all_unknown_field_reports_null_not_zero(self):
        item = normalize(record(raw_usage={'input_tokens': 1, 'output_tokens': 1}), 'm')
        totals = summarize([item])['totals']
        self.assertIsNone(totals['known_token_fields']['cache_write'])
        self.assertIsNone(totals['known_token_fields']['reasoning'])
        self.assertEqual(totals['known_token_fields']['output'], 1)

    def test_opencode_output_survives_absent_reasoning(self):
        base = dict(harness='opencode', provider='test')
        usage = {'input': 1, 'cache': {'read': 0, 'write': 0}, 'output': 4}
        item = normalize(record(raw_usage=usage, **base), 'm')
        self.assertEqual((item['tokens']['output'], item['tokens']['reasoning']), (4, None))
        item = normalize(record(raw_usage=dict(usage, reasoning=3), **base), 'm')
        self.assertEqual((item['tokens']['output'], item['tokens']['reasoning']), (7, 3))


def _v1_database(path, records, files):
    """Write a schema v1 database (JSON text observations, text-keyed sources) as the old code did."""
    from tokenatlas.history import _key
    connection = sqlite3.connect(str(path))
    for sql in (
        'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
        'CREATE TABLE observations (key TEXT PRIMARY KEY, data TEXT NOT NULL)',
        'CREATE TABLE sources (observation TEXT, harness TEXT, path TEXT, PRIMARY KEY(observation,harness,path))',
        'CREATE TABLE files (harness TEXT, path TEXT, root TEXT, fingerprint TEXT, diagnostics TEXT, PRIMARY KEY(harness,path))',
        'CREATE TABLE imports (harness TEXT, root TEXT, data TEXT, PRIMARY KEY(harness,root))'):
        connection.execute(sql)
    connection.execute("INSERT INTO meta VALUES ('machine','m-a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1')")
    for record in records:
        item = {k: v for k, v in record.items() if k != 'sources'}
        key = _key(item)
        connection.execute('INSERT INTO observations VALUES (?,?)', (key, json.dumps(item, sort_keys=True)))
        for source in record['sources']:
            connection.execute('INSERT INTO sources VALUES (?,?,?)', (key, item['harness'], source))
    for harness, source, root, fingerprint in files:
        connection.execute('INSERT INTO files VALUES (?,?,?,?,?)', (harness, source, root, fingerprint,
                           json.dumps(dict(malformed_lines=0, partial_lines=0, unparsed_usage_lines=0))))
    connection.execute("INSERT INTO imports VALUES ('claude','/r','{\"status\":\"ok\"}')")
    connection.execute('PRAGMA user_version=1')
    connection.commit()
    connection.close()


def _synthetic_items():
    utc = timezone.utc
    claude_usage = {'input_tokens': 3, 'output_tokens': 9, 'cache_read_input_tokens': 7,
                    'cache_creation_input_tokens': 5,
                    'cache_creation': {'ephemeral_5m_input_tokens': 2, 'ephemeral_1h_input_tokens': 3},
                    'output_tokens_details': {'thinking_tokens': 4},
                    'iterations': [{'input_tokens': 3, 'output_tokens': 9, 'type': 'message', 'model': 'm'}],
                    'iteration_snapshots': [[{'input_tokens': 1}], [{'input_tokens': 3, 'output_tokens': 9}]]}
    samples = [
        record('c1', raw_usage=claude_usage, output_final=True, parent_session_id='p', turn_id='t1',
               turn_confidence='observed', cwd='/work/x', project_id='/work/x', harness_version='1.2',
               timestamp=datetime(2026, 9, 3, 10, 0, 0, 123456, tzinfo=utc)),
        record('c2', raw_usage={'input_tokens': None, 'output_tokens': 0, 'cache_creation': {},
                                'iterations': None}, output_final=False),
        record('c3', raw_usage={'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 1,
                                'cache_creation_input_tokens': 0, 'cache_creation': {'ephemeral_1h_input_tokens': 0}},
               output_final=None, id_synthetic=True),
        record('x1', harness='codex', provider='openai', raw_usage={
            'input_tokens': 10, 'cached_input_tokens': 4, 'cache_write_input_tokens': 0,
            'output_tokens': 2, 'reasoning_output_tokens': 1, 'total_tokens': 12}),
        record('x2', harness='codex', provider='openai', raw_usage={'input_tokens': 1, 'cached_input_tokens': 4}),
        record('p1', harness='pi', provider='test', raw_usage={
            'input': 1, 'cacheRead': 0, 'cacheWrite': 0, 'output': 1, 'reasoning': 0, 'totalTokens': 2},
            session_started_at=datetime(2026, 9, 3, 9, 0, tzinfo=utc)),
        record('o1', harness='opencode', provider='test', raw_usage={
            'input': 1, 'output': 4, 'reasoning': 3, 'cache': {'read': 2, 'write': 0}}),
        record('o2', harness='opencode', provider='test', raw_usage={'input': 1, 'cache': {'read': None}},
               model='unknown', effort='unknown'),
        record('e1', raw_usage={}),
        record('t1', tariff={'speed': 'fast', 'service_tier': 'standard'}, raw_usage={'input_tokens': 1}),
    ]
    return [normalize(r, 'm-test') for r in samples]


class HistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_codec_round_trips_every_harness_and_raw_shape(self):
        items = _synthetic_items()
        self.assertEqual({i['harness'] for i in items}, {'claude', 'codex', 'pi', 'opencode'})
        self.assertEqual({i['output_final'] for i in items}, {True, False, None})
        for item in items:
            with self.subTest(id=item['id']):
                self.assertEqual(_decode(_encode(item)), item)
        raw = {i['id']: i['raw_usage'] for i in items}
        self.assertEqual(raw['c2'], {'input_tokens': None, 'output_tokens': 0, 'cache_creation': {}, 'iterations': None})
        self.assertNotIn('cache_creation_input_tokens', raw['c2'])

    def test_tariff_round_trips_and_merges_per_key(self):
        with_tariff = normalize(record(tariff={'speed': 'fast', 'bogus': 'x', 'inference_geo': 'us'}), 'm')
        self.assertEqual(with_tariff['tariff'], {'speed': 'fast', 'inference_geo': 'us'})
        self.assertEqual(_decode(_encode(with_tariff)), with_tariff)
        self.assertIsNone(normalize(record(), 'm')['tariff'])
        later = normalize(record(tariff={'service_tier': 'standard'}, timestamp=datetime(2026, 9, 3, 11, tzinfo=timezone.utc)), 'm')
        for pair in ((with_tariff, later), (later, with_tariff)):
            self.assertEqual(merge_observations(*pair)['tariff'],
                             {'speed': 'fast', 'inference_geo': 'us', 'service_tier': 'standard'})

    def test_v2_database_without_tariff_column_gains_it_and_keeps_records(self):
        path = self.root / 'h.sqlite3'
        with History(path) as h:
            h.refresh('claude', self.root / 'none')  # creates the schema
            for item in _synthetic_items()[:3]:
                h._insert(h.connection, _encode(dict(item, tariff=None)))
            before = h.records()
        self.assertEqual(len(before), 3)
        connection = sqlite3.connect(str(path))
        try:
            connection.execute('ALTER TABLE observations DROP COLUMN tariff')
        except sqlite3.OperationalError:
            self.skipTest('SQLite without DROP COLUMN')
        self.assertNotIn('tariff', [r[1] for r in connection.execute('PRAGMA table_info(observations)')])
        connection.commit(); connection.close()
        with History(path) as h:
            self.assertIn('tariff', [r[1] for r in h.connection.execute('PRAGMA table_info(observations)')])
            self.assertEqual(h.records(), before)
            self.assertEqual({r['tariff'] for r in h.records()}, {None})
            self.assertEqual(h.connection.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_quota_round_trips_is_revalidated_and_merges_whole(self):
        quota={'limit_id':'codex','plan_type':'pro','reached':None,'windows':[
            {'slot':'primary','minutes':300,'used_percent':12.0,'resets_at':'2026-10-09T21:13:27+00:00'}]}
        item=normalize(record(quota=quota),'m')
        self.assertEqual(item['quota'],quota)
        self.assertEqual(_decode(_encode(item)),item)
        self.assertIsNone(normalize(record(),'m')['quota'])
        hostile=dict(quota,credits={'balance':'1'},windows=quota['windows']+[{'slot':'x','minutes':1,'used_percent':1,'resets_at':None},
            {'slot':'secondary','minutes':True,'used_percent':1,'resets_at':None},
            {'slot':'secondary','minutes':60,'used_percent':float('nan'),'resets_at':None},
            {'slot':'secondary','minutes':60,'used_percent':10**400,'resets_at':None}])
        self.assertEqual(normalize(record(quota=hostile),'m')['quota'],quota)
        self.assertIsNone(normalize(record(quota='junk'),'m')['quota'])
        self.assertIsNone(normalize(record(quota={'windows':[],'reached':None}),'m')['quota'])
        other=dict(quota,plan_type='team',windows=[dict(quota['windows'][0],used_percent=50.0)])
        later=normalize(record(quota=other,timestamp=datetime(2026,9,3,11,tzinfo=timezone.utc)),'m')
        bare=normalize(record(timestamp=datetime(2026,9,3,12,tzinfo=timezone.utc)),'m')
        for pair in ((item,later),(later,item)):
            self.assertEqual(merge_observations(*pair)['quota'],other)
        for pair in ((item,bare),(bare,item)):
            self.assertEqual(merge_observations(*pair)['quota'],quota)

    def test_v2_database_without_quota_column_gains_it_and_keeps_records(self):
        path = self.root / 'h.sqlite3'
        with History(path) as h:
            h.refresh('claude', self.root / 'none')
            for item in _synthetic_items()[:3]:
                h._insert(h.connection, _encode(dict(item, quota=None)))
            before = h.records()
        connection = sqlite3.connect(str(path))
        try:
            connection.execute('ALTER TABLE observations DROP COLUMN quota')
        except sqlite3.OperationalError:
            self.skipTest('SQLite without DROP COLUMN')
        self.assertNotIn('quota', [r[1] for r in connection.execute('PRAGMA table_info(observations)')])
        connection.commit(); connection.close()
        with History(path) as h:
            self.assertIn('quota', [r[1] for r in h.connection.execute('PRAGMA table_info(observations)')])
            self.assertEqual(h.records(), before)
            self.assertEqual({r['quota'] for r in h.records()}, {None})
            self.assertEqual(h.connection.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_snapshot_import_keeps_quota(self):
        quota={'limit_id':'codex','plan_type':'pro','reached':'workspace_owner_credits_depleted','windows':[
            {'slot':'secondary','minutes':10080,'used_percent':101.0,'resets_at':None}]}
        item=normalize(record('q1',harness='codex',provider='openai',quota=quota,raw_usage={'input_tokens':1}),'m-remote')
        with History(self.root/'remote.sqlite3') as remote:
            remote._insert(remote.connection,_encode(item))
            remote.connection.commit()
            remote.snapshot(self.root/'remote.snap')
        with History(self.root/'local.sqlite3') as local:
            self.assertEqual(local.import_snapshot(self.root/'remote.snap','laptop')['new'],1)
            self.assertEqual([r['quota'] for r in local.records()],[quota])

    def _interrupted_log(self, name='logs/a.jsonl'):
        source=self.root/name;write_claude(source)
        with source.open('a') as f:
            f.write(json.dumps({'type':'user','uuid':'stop-1','message':{'role':'user','content':[{'type':'text','text':'[Request interrupted by user]'}]}})+'\n')
        return source

    def test_flags_round_trip_normalize_and_merge_as_a_union(self):
        stopped=normalize(record(flags=['interrupted','b','interrupted']),'m')
        self.assertEqual(stopped['flags'],['b','interrupted'])
        self.assertEqual(_decode(_encode(stopped)),stopped)
        plain=normalize(record(),'m')
        self.assertIsNone(plain['flags']);self.assertEqual(_decode(_encode(plain)),plain)
        later=normalize(record(flags=['x'],timestamp=datetime(2026,9,3,11,tzinfo=timezone.utc)),'m')
        for pair in ((stopped,plain),(plain,stopped),(stopped,later),(later,stopped)):
            merged=merge_observations(*pair)
            self.assertEqual(merged['flags'],sorted(set(pair[0]['flags'] or ())|set(pair[1]['flags'] or ())) or None)
        self.assertEqual(merge_observations(plain,plain)['flags'],None)
        # A re-read keeps the union too: a stale copy without the flag never erases a recorded stop, whatever the file order.
        for pair in ((stopped,plain),(plain,stopped)):
            self.assertEqual(merge_observations(*pair,authoritative_turns=True)['flags'],['b','interrupted'])

    def test_refresh_stores_the_interrupt_flag_and_an_unchanged_refresh_adds_nothing(self):
        source=self._interrupted_log()
        with History(self.root/'mine.sqlite3') as h:
            h.refresh('claude',source.parent)
            self.assertEqual([r['flags'] for r in h.records()],[['interrupted']])
            before=h.connection.execute('SELECT count(*) FROM observations').fetchone()[0]
            h.refresh('claude',source.parent);os.utime(source,(1,1));h.refresh('claude',source.parent)
            self.assertEqual(h.connection.execute('SELECT count(*) FROM observations').fetchone()[0],before)
            self.assertEqual([r['flags'] for r in h.records()],[['interrupted']])

    def test_v2_database_without_flags_column_gains_it_and_keeps_records(self):
        path=self.root/'h.sqlite3';source=self._interrupted_log()
        with History(path) as h:
            h.refresh('claude',source.parent);before=h.records()
        connection=sqlite3.connect(str(path))
        try:
            connection.execute('ALTER TABLE observations DROP COLUMN flags')
        except sqlite3.OperationalError:
            self.skipTest('SQLite without DROP COLUMN')
        connection.commit();connection.close()
        with History(path) as h:
            self.assertIn('flags',[r[1] for r in h.connection.execute('PRAGMA table_info(observations)')])
            self.assertEqual({r['flags'] and tuple(r['flags']) for r in h.records()},{None})
            self.assertEqual(len(h.records()),len(before))

    def test_snapshot_import_keeps_flags_and_old_snapshots_import(self):
        source=self._interrupted_log('src/a.jsonl');snap=self.root/'snap.sqlite3';old=self.root/'old.sqlite3'
        with History(self.root/'other.sqlite3') as other:
            other.refresh('claude',source.parent);other.snapshot(snap);other.snapshot(old)
        raw=sqlite3.connect(str(old))
        try:
            raw.execute('ALTER TABLE observations DROP COLUMN flags')
        except sqlite3.OperationalError:
            self.skipTest('SQLite without DROP COLUMN')
        raw.commit();raw.close()
        with History(self.root/'mine.sqlite3') as h:
            self.assertEqual(h.import_snapshot(snap,'laptop')['new'],1)
            self.assertEqual([r['flags'] for r in h.records()],[['interrupted']])
        with History(self.root/'third.sqlite3') as h:
            self.assertEqual(h.import_snapshot(old,'laptop')['new'],1)
            self.assertEqual([r['flags'] for r in h.records()],[None])

    def test_codec_refuses_fields_it_cannot_restore(self):
        item = _synthetic_items()[0]
        with self.assertRaisesRegex(ValueError, 'unstorable'):
            _encode(dict(item, surprise=1))
        with self.assertRaisesRegex(ValueError, 'billing_verified'):
            _encode(dict(item, billing_verified=True))

    def test_v2_layout_is_typed_and_deduplicated(self):
        source = self.root / 'logs/a.jsonl'; write_claude(source)
        with History(self.root / 'h.sqlite3') as h:
            h.refresh('claude', source.parent)
            c = h.connection
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(h.doctor()['schema_version'], 2)
            self.assertEqual({r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {'meta', 'strings', 'files', 'observations', 'sources', 'imports'})
            row = c.execute('SELECT ts_us,fresh_input,raw_usage,extra FROM observations').fetchone()
            self.assertEqual(tuple(row), (1788429600000000, 10, '[10,-1,5,-1,-1,-1,20,0]', None))
            self.assertEqual(c.execute('SELECT typeof(harness) FROM observations').fetchone()[0], 'integer')
            h.refresh('claude', source.parent)
            self.assertEqual(c.execute('SELECT count(*) FROM sources').fetchone()[0], 1)

    def test_records_filters_run_in_sql_and_keep_order(self):
        for name, request, ts in (('a', 'r2', '2026-09-03T11:00:00Z'), ('b', 'r1', '2026-09-03T10:00:00Z')):
            path = self.root / f'logs/{name}.jsonl'; write_claude(path, request)
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            rows[-1]['timestamp'] = ts
            path.write_text(''.join(json.dumps(x) + '\n' for x in rows))
        with History(self.root / 'h.sqlite3') as h:
            h.refresh('claude', self.root / 'logs')
            self.assertEqual([r['id'] for r in h.records()], ['r1', 'r2'])
            at = datetime(2026, 9, 3, 11, tzinfo=timezone.utc)
            self.assertEqual([r['id'] for r in h.records(start=at)], ['r2'])
            self.assertEqual([r['id'] for r in h.records(end=at)], ['r1'])
            self.assertEqual(h.records(harness='codex'), [])
            self.assertEqual(h.records(session='nope'), [])
            self.assertEqual(len(h.records(session='claude:session', turn=None, project=None)), 2)

    def test_doctor_counts_in_sql_match_records(self):
        with History(self.root / 'empty.sqlite3') as h:
            d = h.doctor()
            self.assertEqual((d['observations'], d['first_event'], d['last_event'], d['incomplete_observations'],
                              d['unlinked_turns']), (0, None, None, 0, 0))
        for name, request, ts, stop in (('a', 'r2', '2026-09-03T11:00:00.250Z', 'end_turn'),
                                        ('b', 'r1', '2026-09-03T10:00:00Z', None)):
            path = self.root / f'logs/{name}.jsonl'; write_claude(path, request)
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            rows[-1]['timestamp'] = ts
            rows[-1]['message']['stop_reason'] = stop
            path.write_text(''.join(json.dumps(x) + '\n' for x in rows))
        with History(self.root / 'h.sqlite3') as h:
            h.refresh('claude', self.root / 'logs')
            records, d = h.records(), h.doctor()
            self.assertEqual((d['observations'], d['first_event'], d['last_event']),
                             (len(records), records[0]['ts'], records[-1]['ts']))
            self.assertEqual(d['incomplete_observations'], sum(not r['complete'] for r in records))
            self.assertEqual(d['unlinked_turns'], sum(r['turn_id'] is None for r in records))
            self.assertEqual(d['incomplete_observations'], 1)

    def test_migrates_v1_keeping_records_sources_and_deleted_files(self):
        gone, kept = self.root / 'logs/gone.jsonl', self.root / 'logs/kept.jsonl'
        write_claude(gone, 'old'); write_claude(kept, 'new')
        with History(self.root / 'reference.sqlite3') as h:
            h.refresh('claude', kept.parent)
            expected = h.records()
        self.assertEqual({len(r['sources']) for r in expected}, {1})
        expected[0]['sources'] = sorted([str(gone), str(kept)])
        legacy = self.root / 'state/legacy.sqlite3'
        legacy.parent.mkdir()
        # Both observations come from both files; a third only exists in the file that is then deleted.
        extra = dict(expected[0], id='only-gone', sources=[str(gone)])
        records = expected + [extra]
        _v1_database(legacy, records, [('claude', str(gone), str(gone.parent), 'f1'),
                                       ('claude', str(kept), str(kept.parent), None)])
        os.chmod(legacy, 0o600)
        size_before = legacy.stat().st_size
        gone.unlink()
        with History(legacy) as h:
            self.assertEqual(h.machine, 'm-a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1')
            self.assertEqual(sorted(h.records(), key=lambda r: r['id']), sorted(records, key=lambda r: r['id']))
            self.assertEqual([r['sources'] for r in h.records() if r['id'] == 'only-gone'], [[str(gone)]])
            self.assertEqual(h.connection.execute('PRAGMA user_version').fetchone()[0], 2)
            tables = {r[0] for r in h.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse({t for t in tables if t.startswith('v1_')})
            self.assertEqual(tables, {'meta', 'strings', 'files', 'observations', 'sources', 'imports'})
            self.assertEqual(h.connection.execute("SELECT count(*) FROM imports").fetchone()[0], 1)
            self.assertEqual(h.doctor()['missing_source_files'], 1)
            self.assertEqual(h.connection.execute("SELECT fingerprint FROM files WHERE path=?", (str(gone),)).fetchone()[0], 'f1')
            # The migrated database keeps refreshing incrementally.
            h.refresh('claude', kept.parent)
            self.assertEqual(len(h.records()), 3)
        with History(legacy) as h:
            self.assertEqual(len(h.records()), 3)
        if os.name != 'nt':  # Windows has no POSIX permission bits
            self.assertLessEqual(stat.S_IMODE(legacy.stat().st_mode), 0o600)
        self.assertGreater(size_before, 0)

    def test_failed_migration_leaves_v1_untouched(self):
        item = _synthetic_items()[0]
        legacy = self.root / 'legacy.sqlite3'
        _v1_database(legacy, [dict(item, sources=[])], [])
        connection = sqlite3.connect(str(legacy))
        connection.execute("UPDATE observations SET data=json_set(data,'$.surprise',1)")
        connection.commit(); connection.close()
        os.chmod(legacy, 0o600)
        with self.assertRaisesRegex(ValueError, 'cannot migrate'):
            History(legacy).__enter__()
        connection = sqlite3.connect(str(legacy))
        self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(connection.execute('SELECT count(*) FROM observations').fetchone()[0], 1)
        connection.close()

    def test_size_is_at_most_300_bytes_per_observation(self):
        import random
        from test_why_codex import _write_rollout, _meta, _context, _tokens
        rng = random.Random(7)
        files, per_file, turns_per_file = 200, 100, 10
        logs = self.root / 'logs'
        for f in range(files):
            session = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            rows = [_meta(session)]
            total = 0
            for n in range(per_file):
                second = n * 7
                stamp = f'2026-09-{1 + f % 28:02d}T{(second // 3600) % 24:02d}:{second // 60 % 60:02d}:{second % 60:02d}Z'
                if n % (per_file // turns_per_file) == 0:
                    context = _context(stamp, 'gpt-5.5', 'high', f'/work/project-{f % 9}')
                    context['payload']['turn_id'] = str(uuid.UUID(int=rng.getrandbits(128), version=4))
                    rows.append(context)
                inp = rng.randrange(2000, 90000); cached = rng.randrange(0, inp); out = rng.randrange(20, 3000)
                usage = {'input_tokens': inp, 'cached_input_tokens': cached, 'cache_write_input_tokens': 0,
                         'output_tokens': out, 'reasoning_output_tokens': rng.randrange(0, out),
                         'total_tokens': inp + out}
                total += inp
                rows.append(_tokens(stamp, n, usage, dict(usage, input_tokens=total, total_tokens=total + out)))
            _write_rollout(logs / f'{f % 28:02d}/rollout-{session}.jsonl', rows)
        with History(self.root / 'h.sqlite3') as h:
            h.refresh('codex', logs)
            count = len(h.records())
            self.assertEqual(count, files * per_file)
            self.assertEqual(len({r['turn_id'] for r in h.records()}), files * turns_per_file)
            h.connection.execute('VACUUM')
        per_observation = (self.root / 'h.sqlite3').stat().st_size / count
        print(f'\nhistory size: {per_observation:.1f} bytes per observation over {count} observations')
        self.assertLessEqual(per_observation, 300)




class PromptProvenanceTests(unittest.TestCase):
    """#132: only locally collected sources reach prompt and context capture."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'logs/a.jsonl';write_claude(self.source)

    def capture(self, h):
        from tokenatlas import prompt_store
        from test_pricing import TABLE
        store=self.root/'top.json'
        prompt_store.update(store,h.records(),TABLE,h.machine,k=3,local=h.local_source_paths())
        return [e['text'] or e.get('context') for e in json.loads(store.read_bytes())['entries'] if e['text'] or any((e.get('context') or {}).values())]

    def snapshot_with_machine(self):
        """Snapshot of another machine's history."""
        snap=self.root/'other.snap'
        with History(self.root/'other.sqlite3') as other:
            other.refresh('claude',self.source.parent)
            other.snapshot(snap)
        return snap

    def test_local_source_still_yields_preview(self):
        with History(self.root/'mine.sqlite3') as h:
            h.refresh('claude',self.source.parent)
            self.assertEqual(self.capture(h),['PRIVATE PROMPT'])

    def test_imported_source_never_yields_preview_even_with_forged_machine(self):
        with History(self.root/'mine.sqlite3') as h:
            snap=self.snapshot_with_machine()
            self.assertEqual(h.import_snapshot(snap,'laptop')['new'],1)
            self.assertTrue(all(s.startswith(('m-',)) for r in h.records() for s in r['sources']))
            self.assertEqual(self.capture(h),[])
            # forge: the imported observation claims this machine's id; its source is still an imported reference
            h.connection.execute('UPDATE observations SET machine=?',(h._sid(h.connection,h.machine),))
            h.connection.commit()
            self.assertEqual({r['machine'] for r in h.records()},{h.machine})
            self.assertEqual(self.capture(h),[])

    @unittest.skipIf(os.name=='nt','a directory named m-<id>: cannot exist on Windows')
    def test_forged_prefixed_reference_resolving_relative_to_cwd_is_rejected(self):
        from tokenatlas.provenance import local_file
        from tokenatlas.prompt_text import extract_prompt
        from tokenatlas.turn_context import turn_context
        cwd=os.getcwd();self.addCleanup(os.chdir,cwd)
        os.chdir(self.root)
        (self.root/('m-'+'e'*0+'0'*32+':')).mkdir()
        (self.root/('m-'+'0'*32+':')/'a.jsonl').write_text(self.source.read_text())
        ref='m-'+'0'*32+':/a.jsonl'
        self.assertTrue(Path(ref).is_file())  # the relative path really would be readable
        self.assertIsNone(local_file(ref))
        self.assertIsNone(extract_prompt('claude',ref,'session','prompt-1'))
        self.assertEqual(turn_context('claude',[ref],'session','prompt-1')['inputs']['first'],None)

    def test_relative_and_non_regular_sources_are_rejected(self):
        from tokenatlas.provenance import local_file, local_sources
        cwd=os.getcwd();self.addCleanup(os.chdir,cwd)
        os.chdir(self.root)
        self.assertIsNone(local_file('logs/a.jsonl'))
        self.assertIsNone(local_file(self.root/'logs'))  # a directory
        self.assertIsNone(local_file(self.root/'missing.jsonl'))
        self.assertEqual(local_file(self.source),self.source)
        self.assertEqual(local_sources(['logs/a.jsonl','m-'+'0'*32+':/a',str(self.source)]),[str(self.source)])

    def test_import_and_reimport_still_merge_and_dedupe(self):
        with History(self.root/'mine.sqlite3') as h:
            h.refresh('claude',self.source.parent)
            snap=self.snapshot_with_machine()
            first=h.import_snapshot(snap,'laptop')
            self.assertEqual((first['new'],first['merged']),(0,1))  # same provider request id: merges with the local copy
            before=h.records(),h.revision
            again=h.import_snapshot(snap,'laptop')
            self.assertEqual((again['new'],again['merged']),(0,1))
            self.assertEqual((h.records(),h.revision),before)
            self.assertEqual(len(before[0]),1)
            local=[s for s in before[0][0]['sources'] if not s.startswith('m-')]
            self.assertEqual(local,[str(self.source)])
        with History(self.root/'third.sqlite3') as h:
            self.assertEqual(h.import_snapshot(snap,'laptop')['new'],1)
            self.assertEqual(h.import_snapshot(snap,'laptop')['new'],0)
            self.assertEqual(len(h.records()),1)

    def test_pre_upgrade_history_quarantines_rows_without_local_refresh_evidence(self):
        """A schema-2 database without files.origin: a forged import row passes every syntax check but is never read."""
        from tokenatlas import prompt_store
        from test_pricing import TABLE
        forged=self.root/'forged/f.jsonl';write_claude(forged,request='reqf')
        db=self.root/'old.sqlite3'
        with History(db) as h:
            h.refresh('claude',self.source.parent)
            h.refresh('claude',forged.parent)
            # what an earlier import left: no root/fingerprint (import never sets them) on a drive-style, absolute path
            h.connection.execute('UPDATE files SET root=NULL,fingerprint=NULL,diagnostics=NULL WHERE path=?',(str(forged),))
            h.connection.commit()
            try:
                h.connection.execute('ALTER TABLE files DROP COLUMN origin');h.connection.commit()
            except sqlite3.OperationalError:
                self.skipTest('SQLite without DROP COLUMN')
        with History(db) as h:
            self.assertEqual(h.local_source_paths(),{str(self.source)})
            self.assertIn(str(forged),{s for r in h.records() for s in r['sources']})  # still reported and deduped
            seen=[]
            def extract(harness,source,session,turn_id,limit=200):seen.append(source);return None
            def context(harness,sources,session,turn_id,start=None,end=None):seen.extend(sources);return None
            records=[dict(r,machine=h.machine) for r in h.records()]  # observation identity claims to be local
            with patch('tokenatlas.provenance._plain',lambda text:True):  # and the syntax check accepts a drive-style path
                prompt_store.update(self.root/'t.json',records,TABLE,h.machine,k=5,extract=extract,context=context,
                                    local=h.local_source_paths())
            self.assertNotIn(str(forged),seen)
            self.assertEqual(set(seen),{str(self.source)})
            self.assertEqual(self.capture(h),['PRIVATE PROMPT'])  # the genuine local source still works
            h.refresh('claude',forged.parent)  # a local refresh of a genuinely local file re-marks it local
            self.assertEqual(h.local_source_paths(),{str(self.source),str(forged)})

    def test_import_marks_sources_as_not_local(self):
        with History(self.root/'mine.sqlite3') as h:
            h.import_snapshot(self.snapshot_with_machine(),'laptop')
            self.assertEqual(h.local_source_paths(),set())
            h.refresh('claude',self.source.parent)
            self.assertEqual(h.local_source_paths(),{str(self.source)})

    def test_snapshot_with_a_non_conforming_machine_id_is_rejected_and_imports_nothing(self):
        for n,forged in enumerate(('C','c:','m-x','M-'+'0'*32,'m-'+'A'*32,'m-'+'0'*31,'m-'+'0'*32+':','m-'+'0'*32+'\n','')):
            db=self.root/f'other{n}.sqlite3';snap=self.root/f'forged{n}.snap'
            with History(db) as other:
                other.refresh('claude',self.source.parent)
                other.connection.execute("UPDATE meta SET value=? WHERE key='machine'",(forged,))
                other.connection.commit()
            raw=sqlite3.connect(str(db));out=sqlite3.connect(str(snap))
            raw.backup(out);out.close();raw.close()
            with History(self.root/f'mine{n}.sqlite3') as h:
                with self.assertRaisesRegex(ValueError,'machine id'):
                    h.import_snapshot(snap,'laptop')
                self.assertEqual(h.records(),[])

    def test_snapshot_with_a_trigger_or_view_is_rejected_and_imports_nothing(self):
        for n,ddl in enumerate((
                "CREATE TRIGGER t BEFORE INSERT ON meta BEGIN UPDATE meta SET value='C' WHERE key='machine'; END",
                "CREATE VIEW v AS SELECT 1")):
            db=self.root/f'trig{n}.sqlite3';snap=self.root/f'trig{n}.snap'
            with History(db) as other:
                other.refresh('claude',self.source.parent)
                other.connection.execute(ddl);other.connection.commit()
            raw=sqlite3.connect(str(db));out=sqlite3.connect(str(snap))
            raw.backup(out);out.close();raw.close()
            with History(self.root/f'mine{n}.sqlite3') as h:
                with self.assertRaisesRegex(ValueError,'trigger|view'):
                    h.import_snapshot(snap,'laptop')
                self.assertEqual(h.records(),[])
                self.assertEqual(h.connection.execute('SELECT count(*) FROM sources').fetchone()[0],0)

    @unittest.skipIf(os.name=='nt','a drive-letter path is a legitimate local path on Windows')
    def test_drive_like_and_prefixed_references_are_never_local(self):
        from tokenatlas.provenance import local_file, local_sources
        refs=['C:/Users/alice/x.jsonl','C:\\Users\\alice\\x.jsonl','m-x:/a','x:/a']
        self.assertEqual(local_sources(refs+[str(self.source)]),[str(self.source)])
        self.assertTrue(all(local_file(r) is None for r in refs))


if __name__=='__main__':unittest.main()
