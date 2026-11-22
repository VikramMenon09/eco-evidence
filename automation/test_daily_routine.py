import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('daily_routine.py')
spec = importlib.util.spec_from_file_location('daily_routine', SCRIPT)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def state(target='2026-11-23T17:09:00-08:00'):
    return {'version': 1, 'automation_id': 'test', 'repository': r.REPOSITORY,
            'timezone': str(r.ZONE), 'next_at': target, 'active': None,
            'history': [], 'schedule_sync_pending': False}


def clock(value):
    return r.local_now(value)


def finish(s, owner, now='2026-11-23T18:10:00-08:00', outcome='published', **kwargs):
    evidence = {'pr_url': r.REPOSITORY + '/pull/2', 'commit': 'a' * 40,
                'published_at': '2026-11-23T18:00:00-08:00'} if outcome == 'published' else {}
    evidence.update(kwargs)
    return r.finish(s, clock(now), owner['run_id'], owner['lease_id'], outcome, **evidence)


class ScheduleTests(unittest.TestCase):
    def test_random_target_uses_next_day_and_a_different_clock_minute(self):
        now = clock('2026-11-22T23:59:00-08:00')
        first = r.next_target(now, '2026-11-22T08:00:00-08:00', chooser=lambda v: v[0])
        last = r.next_target(now, chooser=lambda v: v[-1])
        self.assertEqual(first.isoformat(), '2026-11-23T08:01:00-08:00')
        self.assertEqual(last.isoformat(), '2026-11-23T22:30:00-08:00')

    def test_dst_transitions_and_year_rollover_use_pacific_calendar_days(self):
        for before, expected in [
            ('2027-03-13T22:00:00-08:00', '2027-03-14T08:00:00-07:00'),
            ('2027-11-06T22:00:00-07:00', '2027-11-07T08:00:00-08:00'),
            ('2026-12-31T23:59:00-08:00', '2027-01-01T08:00:00-08:00'),
        ]:
            with self.subTest(before=before):
                self.assertEqual(r.next_target(clock(before), chooser=lambda v: v[0]).isoformat(), expected)

    def test_elapsed_lease_time_uses_utc_across_dst_fold(self):
        self.assertEqual(r.elapsed(clock('2027-11-07T01:30:00-08:00'),
                                   clock('2027-11-07T01:30:00-07:00')), 3600)

    def test_recovery_uses_remaining_today_with_schedule_headroom(self):
        target = r.next_target(clock('2026-11-24T12:00:01-08:00'), after_date='2026-11-23', chooser=lambda v: v[0])
        self.assertEqual(target.isoformat(), '2026-11-24T12:06:00-08:00')

    def test_recovery_uses_tomorrow_if_today_consumed_or_too_late(self):
        for now, consumed in [('2026-11-24T12:00:00-08:00', ['2026-11-24']),
                              ('2026-11-24T22:26:00-08:00', []),
                              ('2026-11-24T23:59:00-08:00', [])]:
            with self.subTest(now=now):
                target = r.next_target(clock(now), after_date='2026-11-23', consumed_dates=consumed, chooser=lambda v: v[0])
                self.assertEqual(target.isoformat(), '2026-11-25T08:00:00-08:00')

    def test_native_rrule_has_clock_only_and_preserves_date_guard(self):
        result = r.schedule(state())
        self.assertEqual(result['rrule'], 'FREQ=DAILY;BYHOUR=17;BYMINUTE=9;BYSECOND=0')
        self.assertEqual(result['next_at'], '2026-11-23T17:09:00-08:00')
        self.assertNotIn('DTSTART', result['rrule'])


class OwnershipTests(unittest.TestCase):
    def test_early_late_and_not_due_claims_do_not_change_state(self):
        for now, reason in [
            ('2026-11-22T17:09:00-08:00', 'not_due_yet'),
            ('2026-11-23T07:59:00-08:00', 'outside_publication_window'),
            ('2026-11-24T00:00:00-08:00', 'outside_publication_window'),
            ('2026-11-23T22:31:00-08:00', 'too_late_to_start_safely'),
        ]:
            with self.subTest(now=now):
                s = state(); before = copy.deepcopy(s)
                self.assertEqual(r.claim(s, clock(now))['reason'], reason)
                self.assertEqual(s, before)

    def test_live_lease_blocks_duplicate_and_stale_reclaim_fences_old_owner(self):
        s = state(); first = r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        self.assertEqual(r.claim(s, clock('2026-11-23T17:10:00-08:00'))['action'], 'skip')
        resumed = r.claim(s, clock('2026-11-23T20:09:00-08:00'))
        self.assertEqual(resumed['action'], 'resume')
        for key in ('run_id', 'date', 'branch', 'started_at'):
            self.assertEqual(first[key], resumed[key])
        self.assertNotEqual(first['lease_id'], resumed['lease_id'])
        before = copy.deepcopy(s)
        with self.assertRaises(ValueError):
            r.check_publish(s, clock('2026-11-23T20:10:00-08:00'), first['run_id'], first['lease_id'])
        with self.assertRaises(ValueError):
            finish(s, first, '2026-11-23T20:10:00-08:00')
        self.assertEqual(s, before)
        self.assertEqual(r.check_publish(s, clock('2026-11-23T20:10:00-08:00'), resumed['run_id'], resumed['lease_id'])['action'], 'publication_allowed')

    def test_expired_owner_cannot_publish_or_finish_without_reclaim(self):
        s = state(); owner = r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        with self.assertRaises(ValueError):
            r.check_publish(s, clock('2026-11-23T20:09:00-08:00'), owner['run_id'], owner['lease_id'])
        with self.assertRaises(ValueError):
            finish(s, owner, '2026-11-23T20:09:00-08:00')

    def test_prepublication_check_refreshes_same_owner_and_reports_midnight_cutoff(self):
        s = state(); owner = r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        check = r.check_publish(s, clock('2026-11-23T20:08:00-08:00'), owner['run_id'], owner['lease_id'])
        self.assertEqual(check['lease_id'], owner['lease_id'])
        self.assertEqual(check['publish_before'], '2026-11-24T00:00:00-08:00')
        self.assertEqual(r.claim(s, clock('2026-11-23T20:10:00-08:00'))['reason'], 'another_run_has_a_live_lease')

    def test_cross_day_recovery_preserves_work_identity_and_cannot_publish(self):
        s = state(); original = r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        recovered = r.claim(s, clock('2026-11-24T12:00:00-08:00'))
        self.assertEqual(recovered['action'], 'recover')
        for key in ('run_id', 'date', 'branch', 'started_at'):
            self.assertEqual(original[key], recovered[key])
        with self.assertRaises(ValueError):
            r.check_publish(s, clock('2026-11-24T12:01:00-08:00'), recovered['run_id'], recovered['lease_id'])
        record = finish(s, recovered, '2026-11-24T12:01:00-08:00')
        self.assertEqual(record['published_date'], '2026-11-23')
        self.assertEqual(record['finished_date'], '2026-11-23')
        target = clock(s['next_at'])
        self.assertEqual(target.date().isoformat(), '2026-11-24')
        self.assertGreaterEqual(target.hour * 60 + target.minute, 12 * 60 + 6)

    def test_out_of_hours_recovery_only_allows_bookkeeping(self):
        s = state(); r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        owner = r.claim(s, clock('2026-11-24T00:10:00-08:00'))
        self.assertEqual(owner['action'], 'recover')
        with self.assertRaises(ValueError):
            r.check_publish(s, clock('2026-11-24T00:11:00-08:00'), owner['run_id'], owner['lease_id'])
        record = finish(s, owner, '2026-11-24T00:11:00-08:00', 'blocked')
        self.assertEqual(record['finished_date'], '2026-11-23')
        self.assertEqual(clock(s['next_at']).date().isoformat(), '2026-11-24')

    def test_backwards_clock_and_wrong_owner_are_rejected(self):
        s = state(); owner = r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        with self.assertRaises(ValueError):
            r.claim(s, clock('2026-11-23T17:08:00-08:00'))
        with self.assertRaises(ValueError):
            r.check_publish(s, clock('2026-11-23T17:10:00-08:00'), 'b' * 24, owner['lease_id'])


class CompletionTests(unittest.TestCase):
    def test_success_records_evidence_and_blocks_same_day_even_after_schedule_change(self):
        s = state(); owner = r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        record = finish(s, owner)
        self.assertEqual(record['published_at'], '2026-11-23T18:00:00-08:00')
        self.assertEqual(record['finished_at'], '2026-11-23T18:10:00-08:00')
        self.assertIsNone(s['active']); self.assertTrue(s['schedule_sync_pending'])
        self.assertEqual(r.claim(s, clock('2026-11-23T21:00:00-08:00'))['reason'], 'day_already_finished')
        self.assertEqual(clock(s['next_at']).date().isoformat(), '2026-11-24')

    def test_2359_publication_can_be_recorded_after_midnight_from_utc_evidence(self):
        s = state('2026-11-23T22:30:00-08:00')
        owner = r.claim(s, clock('2026-11-24T06:30:00+00:00'))
        r.check_publish(s, clock('2026-11-24T07:59:00+00:00'), owner['run_id'], owner['lease_id'])
        record = finish(s, owner, '2026-11-24T08:10:00+00:00', published_at='2026-11-24T07:59:30Z')
        self.assertEqual(record['date'], '2026-11-23')
        self.assertEqual(record['published_date'], '2026-11-23')
        self.assertEqual(record['finished_date'], '2026-11-23')
        self.assertEqual(record['finished_at'], '2026-11-24T00:10:00-08:00')
        self.assertEqual(clock(s['next_at']).date().isoformat(), '2026-11-24')

    def test_blocked_after_midnight_consumes_original_day_only(self):
        s = state('2026-11-23T22:30:00-08:00')
        owner = r.claim(s, clock('2026-11-23T22:30:00-08:00'))
        record = finish(s, owner, '2026-11-24T00:10:00-08:00', 'blocked')
        self.assertEqual(record['finished_date'], '2026-11-23')
        self.assertEqual(clock(s['next_at']).date().isoformat(), '2026-11-24')
        self.assertEqual(r.claim(s, clock(s['next_at']))['action'], 'run')

    def test_late_overdue_recovery_schedules_tomorrow(self):
        s = state(); r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        owner = r.claim(s, clock('2026-11-25T22:26:00-08:00'))
        finish(s, owner, '2026-11-25T22:26:00-08:00', 'blocked')
        self.assertEqual(clock(s['next_at']).date().isoformat(), '2026-11-26')

    def test_invalid_or_foreign_publication_evidence_never_mutates_state(self):
        for evidence in [
            {'published_at': ''}, {'published_at': None},
            {'published_at': '2026-11-23T18:00:00'},
            {'published_at': '2026-11-23T07:59:00-08:00'},
            {'published_at': '2026-11-24T00:00:00-08:00'},
            {'published_at': '2026-11-23T18:11:00-08:00'},
            {'pr_url': 'https://github.com/elsewhere/eco-evidence/pull/2'},
            {'commit': 'a' * 39},
        ]:
            with self.subTest(evidence=evidence):
                s = state(); owner = r.claim(s, clock('2026-11-23T17:09:00-08:00'))
                before = copy.deepcopy(s)
                with self.assertRaises((ValueError, TypeError)):
                    finish(s, owner, **evidence)
                self.assertEqual(s, before)

    def test_blocked_cannot_hide_publication_evidence(self):
        s = state(); owner = r.claim(s, clock('2026-11-23T17:09:00-08:00'))
        with self.assertRaises(ValueError):
            finish(s, owner, outcome='blocked', pr_url=r.REPOSITORY + '/pull/2')


class CorruptionTests(unittest.TestCase):
    def test_invalid_flags_history_and_timestamp_types_fail_closed(self):
        for field, value in [('version', True), ('schedule_sync_pending', 'false'),
                             ('history', [{}]), ('history', ['bad']),
                             ('next_at', None), ('next_at', ''), ('active', {})]:
            with self.subTest(field=field, value=value):
                s = state(); s[field] = value
                with self.assertRaises(ValueError):
                    r.claim(s, clock('2026-11-23T18:00:00-08:00'))

    def test_missing_or_mismatched_history_fields_cannot_bypass_daily_guard(self):
        good = state(); owner = r.claim(good, clock('2026-11-23T17:09:00-08:00')); finish(good, owner)
        for key in r.HISTORY_FIELDS:
            with self.subTest(missing=key):
                s = copy.deepcopy(good); del s['history'][0][key]
                with self.assertRaises(ValueError):
                    r.claim(s, clock('2026-11-23T18:20:00-08:00'))
        for key, value in [('finished_date', '2026-11-24'), ('published_date', '2026-11-24'),
                           ('claimed_at', None), ('branch', 'daily/2026-11-24')]:
            with self.subTest(key=key):
                s = copy.deepcopy(good); s['history'][0][key] = value
                with self.assertRaises(ValueError): r.validate_state(s)

    def test_duplicate_history_or_inconsistent_active_identity_is_rejected(self):
        s = state(); owner = r.claim(s, clock('2026-11-23T17:09:00-08:00')); finish(s, owner)
        s['history'].append(copy.deepcopy(s['history'][0]))
        with self.assertRaises(ValueError): r.validate_state(s)
        s = state(); r.claim(s, clock('2026-11-23T17:09:00-08:00')); s['active']['date'] = '2026-11-24'
        with self.assertRaises(ValueError): r.validate_state(s)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='eco-evidence-routine-test-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.json'

    def cli(self, now, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.path), '--now', now, *args],
                              text=True, capture_output=True)

    def initialize(self):
        result = self.cli('2026-11-22T20:00:00-08:00', 'initialize', '--automation-id', 'test',
                          '--first-at', '2026-11-23T17:09:00-08:00')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_concurrent_cli_claims_produce_exactly_one_owner(self):
        self.initialize()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.cli('2026-11-23T17:09:00-08:00', 'claim'), range(8)))
        self.assertTrue(all(result.returncode == 0 for result in results))
        actions = [json.loads(result.stdout)['action'] for result in results]
        self.assertEqual(actions.count('run'), 1)
        self.assertEqual(actions.count('skip'), 7)
        r.validate_state(json.loads(self.path.read_text()))

    def test_status_does_not_disclose_another_owners_active_lease(self):
        self.initialize()
        owner = json.loads(self.cli('2026-11-23T17:09:00-08:00', 'claim').stdout)
        status = self.cli('2026-11-23T17:10:00-08:00', 'status')
        self.assertEqual(status.returncode, 0, status.stderr)
        active = json.loads(status.stdout)['active']
        self.assertEqual(active['run_id'], owner['run_id'])
        self.assertNotIn('lease_id', active)
        self.assertNotIn(owner['lease_id'], status.stdout)

    def test_full_cli_claim_check_finish_and_native_ack_protocol(self):
        self.initialize()
        claimed = json.loads(self.cli('2026-11-23T17:09:00-08:00', 'claim').stdout)
        ids = ['--run-id', claimed['run_id'], '--lease-id', claimed['lease_id']]
        checked = self.cli('2026-11-23T18:00:00-08:00', 'check-publish', *ids)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        done = self.cli('2026-11-23T18:10:00-08:00', 'finish', *ids, '--outcome', 'published',
                        '--pr-url', r.REPOSITORY + '/pull/2', '--commit', 'a' * 40,
                        '--published-at', '2026-11-24T02:01:00Z')
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertTrue(result['schedule_sync_pending'])
        wrong = self.cli('2026-11-23T18:11:00-08:00', 'ack-schedule', '--next-at', '2026-11-25T08:00:00-08:00')
        self.assertEqual(wrong.returncode, 2)
        right = self.cli('2026-11-23T18:11:00-08:00', 'ack-schedule', '--next-at', result['next_at'])
        self.assertEqual(right.returncode, 0, right.stderr)
        self.assertFalse(json.loads(right.stdout)['schedule_sync_pending'])
        repeated = self.cli('2026-11-23T21:00:00-08:00', 'claim')
        self.assertEqual(json.loads(repeated.stdout)['reason'], 'day_already_finished')

    def test_bad_json_and_naive_clock_fail_without_rewriting_state(self):
        self.initialize(); self.path.write_text('{"version":1,')
        before = self.path.read_bytes()
        for now in ['2026-11-23T17:09:00-08:00', '2026-11-23T17:09:00']:
            result = self.cli(now, 'claim')
            self.assertEqual(result.returncode, 2)
            self.assertIn('Routine state error:', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(self.path.read_bytes(), before)

    def test_reinitialization_cannot_bind_state_to_another_automation(self):
        self.initialize(); before = self.path.read_bytes()
        result = self.cli('2026-11-22T20:00:00-08:00', 'initialize', '--automation-id', 'different',
                          '--first-at', '2026-11-23T08:00:00-08:00')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_atomic_replace_preserves_original_and_removes_temp_file(self):
        self.initialize(); before = self.path.read_bytes()
        with r.locked_state(self.path) as s:
            s['schedule_sync_pending'] = False
            with patch.object(r.os, 'replace', side_effect=OSError('simulated failure')):
                with self.assertRaises(OSError): r.atomic_write(self.path, s)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['state.json', 'state.json.lock'])

    def test_plan_next_refuses_active_work(self):
        self.initialize(); self.cli('2026-11-23T17:09:00-08:00', 'claim')
        before = self.path.read_bytes()
        result = self.cli('2026-11-23T18:00:00-08:00', 'plan-next')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_plan_next_preserves_future_target_and_recovers_stale_target(self):
        self.initialize(); before = self.path.read_bytes()
        result = self.cli('2026-11-22T21:00:00-08:00', 'plan-next')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.path.read_bytes(), before)
        result = self.cli('2026-11-23T22:31:00-08:00', 'plan-next')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(clock(json.loads(result.stdout)['next_at']).date().isoformat(), '2026-11-24')

    def test_delayed_midnight_wake_plans_available_today_instead_of_skipping_it(self):
        self.initialize()
        result = self.cli('2026-11-24T00:10:00-08:00', 'plan-next')
        self.assertEqual(result.returncode, 0, result.stderr)
        target = clock(json.loads(result.stdout)['next_at'])
        self.assertEqual(target.date().isoformat(), '2026-11-24')
        self.assertGreaterEqual(target.hour, 8)
        self.assertLessEqual(target.hour * 60 + target.minute, r.LATEST)


if __name__ == '__main__':
    unittest.main()
