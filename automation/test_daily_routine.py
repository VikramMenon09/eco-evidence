import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
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


PORTFOLIO_REPOSITORIES = [
    'https://github.com/VikramMenon09/eco-evidence',
    'https://github.com/VikramMenon09/carbon-commons',
    'https://github.com/VikramMenon09/circular-economy-lab',
]
PORTFOLIO_BANDS = {1: (8 * 60, 11 * 60), 2: (13 * 60, 16 * 60), 3: (18 * 60, 20 * 60 + 30)}


def portfolio_state(target='2026-11-27T08:00:00-08:00', slot=1):
    """A temporary v2 scheduler fixture; no native schedule or live state is used."""
    return {'version': 2, 'automation_id': 'test', 'allowed_repositories': PORTFOLIO_REPOSITORIES.copy(),
            'scope_version': 1, 'timezone': str(r.ZONE), 'next_at': target, 'next_slot': slot,
            'plans': [{'slot': slot, 'scheduled_at': target}], 'active': None,
            'history': [], 'schedule_sync_pending': False}


def legacy_record(day, number=1):
    """Invented completed v1 work, with original v1 fields and branch spelling."""
    started = clock(day + 'T16:00:00+00:00')
    return {'run_id': f'{number:024x}', 'lease_id': f'{number:032x}', 'date': day,
            'branch': 'daily/' + day, 'started_at': started.isoformat(), 'claimed_at': started.isoformat(),
            'finished_at': (started + timedelta(minutes=1)).isoformat(), 'finished_date': day,
            'outcome': 'blocked', 'pr_url': '', 'commit': '', 'published_at': '', 'published_date': '',
            'note': 'Synthetic historical scheduler fixture'}


def portfolio_finish(s, owner, now, *, repository=None, outcome='blocked'):
    evidence = {'pr_url': repository + '/pull/7', 'commit': f"{len(s['history']) + 1:040x}", 'published_at': now} if outcome == 'published' else {}
    return r.finish(s, clock(now), owner['run_id'], owner['lease_id'], outcome, **evidence)


class PortfolioPlanningTests(unittest.TestCase):
    def test_migration_preserves_legacy_history_and_consumes_its_whole_date(self):
        s = state('2026-11-27T17:09:00-08:00')
        s['history'] = [legacy_record('2026-11-26'), legacy_record('2026-11-27', 2)]
        before_history = copy.deepcopy(s['history'])
        result = r.migrate_to_portfolio(s, clock('2026-11-27T12:00:00-08:00'), chooser=lambda choices: choices[0])
        self.assertIs(result, s)
        self.assertEqual(s['history'], before_history)
        self.assertTrue(all(set(item) == r.HISTORY_FIELDS and '/slot-' not in item['branch'] for item in s['history']))
        self.assertEqual((s['version'], s['scope_version'], s['automation_id'], s['timezone']),
                         (2, 1, 'test', 'America/Los_Angeles'))
        self.assertNotIn('repository', s)
        self.assertEqual(set(s['allowed_repositories']), set(PORTFOLIO_REPOSITORIES))
        self.assertEqual((s['next_slot'], s['next_at']), (1, '2026-11-28T08:00:00-08:00'))
        self.assertEqual(s['plans'], [{'slot': 1, 'scheduled_at': s['next_at']}])
        self.assertTrue(s['schedule_sync_pending'])
        self.assertIsNone(s['active'])
        r.validate_state(s)

    def test_migration_refuses_active_invalid_or_already_migrated_state_without_mutation(self):
        active = state()
        r.claim(active, clock('2026-11-23T17:09:00-08:00'))
        invalid = state(); invalid['unknown_configuration'] = True
        bad_history = state(); bad_history['history'] = [legacy_record('2026-11-23')]
        bad_history['history'][0]['branch'] = 'daily/2026-11-24'
        for original in (active, invalid, bad_history, portfolio_state()):
            with self.subTest(state=original):
                before = copy.deepcopy(original)
                with self.assertRaises(ValueError):
                    r.migrate_to_portfolio(original, clock('2026-11-27T07:00:00-08:00'))
                self.assertEqual(original, before)

    def test_first_eligible_band_and_inclusive_boundaries_allow_five_actual_minutes(self):
        cases = [
            ('2026-11-27T07:55:00-08:00', 1, '2026-11-27T08:00:00-08:00'),
            ('2026-11-27T07:55:01-08:00', 1, '2026-11-27T08:01:00-08:00'),
            ('2026-11-27T10:55:00-08:00', 1, '2026-11-27T11:00:00-08:00'),
            ('2026-11-27T10:55:01-08:00', 2, '2026-11-27T13:00:00-08:00'),
            ('2026-11-27T15:55:00-08:00', 2, '2026-11-27T16:00:00-08:00'),
            ('2026-11-27T15:55:01-08:00', 3, '2026-11-27T18:00:00-08:00'),
            ('2026-11-27T20:25:00-08:00', 3, '2026-11-27T20:30:00-08:00'),
            ('2026-11-27T20:25:01-08:00', 1, '2026-11-28T08:00:00-08:00'),
        ]
        for now, expected_slot, expected_time in cases:
            with self.subTest(now=now):
                s = portfolio_state('2026-11-26T18:05:00-08:00', 3)
                before = copy.deepcopy(s)
                slot, target = r.next_portfolio_target(s, clock(now), chooser=lambda choices: choices[0])
                self.assertEqual((slot, target.isoformat()), (expected_slot, expected_time))
                self.assertGreaterEqual(r.elapsed(target, clock(now)), 300)
                self.assertEqual(s, before, 'Selecting a target must not reserve or discard a plan.')
        s = portfolio_state('2026-11-26T18:05:00-08:00', 3)
        for now, expected in [('2026-11-27T07:00:00-08:00', '2026-11-27T11:00:00-08:00'),
                              ('2026-11-27T12:00:00-08:00', '2026-11-27T16:00:00-08:00'),
                              ('2026-11-27T17:00:00-08:00', '2026-11-27T20:30:00-08:00')]:
            self.assertEqual(r.next_portfolio_target(s, clock(now), chooser=lambda choices: choices[-1])[1].isoformat(), expected)

    def test_dst_and_year_rollover_preserve_pacific_bands(self):
        cases = [
            ('2027-03-13T20:26:00-08:00', '2027-03-14T08:00:00-07:00'),
            ('2027-11-06T20:26:00-07:00', '2027-11-07T08:00:00-08:00'),
            ('2026-12-31T23:59:00-08:00', '2027-01-01T08:00:00-08:00'),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                s = portfolio_state('2026-11-26T18:05:00-08:00', 3)
                slot, target = r.next_portfolio_target(s, clock(now), chooser=lambda choices: choices[0])
                self.assertEqual((slot, target.isoformat()), (1, expected))
                self.assertGreaterEqual(r.elapsed(target, clock(now)), 300)

    def test_previous_day_corresponding_minutes_and_all_reserved_slots_are_excluded(self):
        s = portfolio_state('2026-11-26T18:00:00-08:00', 3)
        s['plans'] = [{'slot': slot, 'scheduled_at': f'2026-11-26T{hour}:00:00-08:00'}
                      for slot, hour in ((1, '08'), (2, '13'), (3, '18'))]
        for now, expected in [('2026-11-27T07:00:00-08:00', (1, '2026-11-27T08:01:00-08:00')),
                              ('2026-11-27T12:00:00-08:00', (2, '2026-11-27T13:01:00-08:00')),
                              ('2026-11-27T17:00:00-08:00', (3, '2026-11-27T18:01:00-08:00'))]:
            slot, target = r.next_portfolio_target(s, clock(now), chooser=lambda choices: choices[0])
            self.assertEqual((slot, target.isoformat()), expected)
        s['plans'].append({'slot': 1, 'scheduled_at': '2026-11-27T08:01:00-08:00'})
        s['next_slot'], s['next_at'] = 1, '2026-11-27T08:01:00-08:00'
        slot, target = r.next_portfolio_target(s, clock('2026-11-27T08:02:00-08:00'), chooser=lambda choices: choices[0])
        self.assertEqual((slot, target.isoformat()), (2, '2026-11-27T13:01:00-08:00'))

    def test_most_recent_same_slot_minute_remains_excluded_across_multi_day_gaps(self):
        s = portfolio_state('2026-11-24T18:00:00-08:00', 3)
        s['plans'] = [{'slot': slot, 'scheduled_at': f'2026-11-24T{hour}:00:00-08:00'}
                      for slot, hour in ((1, '08'), (2, '13'), (3, '18'))]
        slot, target = r.next_portfolio_target(s, clock('2026-11-27T07:00:00-08:00'), chooser=lambda choices: choices[0])
        self.assertEqual((slot, target.isoformat()), (1, '2026-11-27T08:01:00-08:00'))
        invalid = copy.deepcopy(s)
        invalid['next_slot'], invalid['next_at'] = 1, '2026-11-27T08:00:00-08:00'
        invalid['plans'].append({'slot': 1, 'scheduled_at': invalid['next_at']})
        with self.assertRaises(ValueError):
            r.validate_state(invalid)
        # Only the most recent minute is excluded, so an older minute becomes
        # available after a different same-slot plan was actually recorded.
        s['next_slot'], s['next_at'] = 1, '2026-11-25T08:01:00-08:00'
        s['plans'].append({'slot': 1, 'scheduled_at': s['next_at']})
        slot, target = r.next_portfolio_target(s, clock('2026-11-27T07:00:00-08:00'), chooser=lambda choices: choices[0])
        self.assertEqual((slot, target.isoformat()), (1, '2026-11-27T08:00:00-08:00'))


class PortfolioOwnershipTests(unittest.TestCase):
    def test_three_same_day_attempts_keep_distinct_identity_and_repository_evidence(self):
        s = portfolio_state()
        for slot, repository in enumerate(PORTFOLIO_REPOSITORIES, 1):
            target = clock(s['next_at'])
            owner = r.claim(s, target)
            self.assertEqual((owner['action'], owner['slot'], owner['scheduled_at']), ('run', slot, target.isoformat()))
            self.assertEqual(owner['branch'], f'daily/2026-11-27/slot-{slot}')
            before = copy.deepcopy(s)
            self.assertEqual(r.claim(s, target + timedelta(seconds=1))['action'], 'skip')
            self.assertEqual(s, before)
            completed_at = (target + timedelta(minutes=1)).isoformat()
            record = portfolio_finish(s, owner, completed_at, repository=repository, outcome='published')
            self.assertEqual(record['pr_url'], repository + '/pull/7')
            self.assertEqual(record['scheduled_at'], target.isoformat())
            self.assertEqual(record['slot'], slot)
            self.assertTrue(s['schedule_sync_pending'])
            before = copy.deepcopy(s)
            with self.assertRaises(ValueError):
                portfolio_finish(s, owner, completed_at)
            self.assertEqual(s, before)
        self.assertEqual([item['slot'] for item in s['history']], [1, 2, 3])
        self.assertEqual(len({item['run_id'] for item in s['history']}), 3)
        self.assertEqual(len(s['plans']), 4)
        self.assertEqual((s['next_slot'], clock(s['next_at']).date().isoformat()), (1, '2026-11-28'))
        before = copy.deepcopy(s)
        self.assertEqual(r.claim(s, clock('2026-11-27T23:00:00-08:00'))['action'], 'skip')
        self.assertEqual(s, before)
        r.validate_state(s)

    def test_migrated_history_is_retained_beyond_the_v1_120_record_limit(self):
        s = state('2026-11-27T17:09:00-08:00')
        first = date(2026, 11, 27) - timedelta(days=120)
        s['history'] = [legacy_record((first + timedelta(days=index)).isoformat(), index + 1) for index in range(120)]
        original = copy.deepcopy(s['history'])
        r.migrate_to_portfolio(s, clock('2026-11-27T07:00:00-08:00'), chooser=lambda choices: choices[0])
        for _ in range(3):
            target = clock(s['next_at'])
            owner = r.claim(s, target)
            portfolio_finish(s, owner, (target + timedelta(minutes=1)).isoformat())
        self.assertEqual(len(s['history']), 123)
        self.assertEqual(s['history'][:120], original)
        self.assertEqual([item['slot'] for item in s['history'][120:]], [1, 2, 3])
        r.validate_state(s)

    def test_retained_record_caps_fail_without_clearing_owner_or_pruning_state(self):
        for history_full in (False, True):
            with self.subTest(full_collection='history' if history_full else 'plans'):
                s = portfolio_state()
                if history_full:
                    s['history'] = [legacy_record('2026-11-26')]
                owner = r.claim(s, clock(s['next_at']))
                before = copy.deepcopy(s)
                # A one-record cap reaches the same validation paths with small
                # fixtures: first while adding history, otherwise the next plan.
                with patch.object(r, 'MAX_PORTFOLIO_RECORDS', 1):
                    r.validate_state(s)
                    with self.assertRaises(ValueError):
                        portfolio_finish(s, owner, '2026-11-27T08:01:00-08:00')
                self.assertEqual(s, before)
                self.assertEqual(s['active']['lease_id'], owner['lease_id'])

    def test_missed_slots_do_not_catch_up_or_mutate_while_last_band_minute_is_allowed(self):
        for now in ('2026-11-27T11:16:00-08:00', '2026-11-28T09:00:00-08:00'):
            s = portfolio_state(); before = copy.deepcopy(s)
            result = r.claim(s, clock(now))
            self.assertEqual((result['action'], result['reason']), ('skip', 'planned_slot_missed'))
            self.assertEqual(result['required_command'], 'plan-next')
            self.assertEqual(s, before)
        for slot, target, wake in [(1, '2026-11-27T08:00:00-08:00', '2026-11-27T11:00:59-08:00'),
                                   (2, '2026-11-27T13:00:00-08:00', '2026-11-27T16:00:59-08:00'),
                                   (3, '2026-11-27T18:00:00-08:00', '2026-11-27T20:30:59-08:00')]:
            with self.subTest(slot=slot):
                owner = r.claim(portfolio_state(target, slot), clock(wake))
                self.assertEqual((owner['action'], owner['slot'], owner['scheduled_at']), ('run', slot, target))

    def test_claim_grace_deadlines_match_work_validation_without_extending_targets(self):
        cases = ((1, '11:00', '11:15:59', '11:16:00'),
                 (2, '16:00', '16:15:59', '16:16:00'),
                 (3, '20:30', '20:45:59', '20:46:00'))
        for slot, end, deadline, expired in cases:
            with self.subTest(slot=slot):
                target = f'2026-11-27T{end}:00-08:00'
                at_deadline = clock(f'2026-11-27T{deadline}-08:00')
                too_late = clock(f'2026-11-27T{expired}-08:00')
                s = portfolio_state(target, slot)
                owner = r.claim(s, at_deadline)
                self.assertEqual((owner['action'], owner['slot'], owner['scheduled_at']), ('run', slot, target))
                r.validate_state(s)
                record = portfolio_finish(s, owner, too_late.isoformat())
                self.assertEqual(record['started_at'], at_deadline.isoformat())
                r.validate_state(s)
                rejected = portfolio_state(target, slot)
                before = copy.deepcopy(rejected)
                self.assertEqual(r.claim(rejected, too_late)['reason'], 'planned_slot_missed')
                self.assertEqual(rejected, before)
                corrupted = portfolio_state(target, slot)
                r.claim(corrupted, clock(target))
                corrupted['active']['started_at'] = too_late.isoformat()
                corrupted['active']['claimed_at'] = too_late.isoformat()
                with self.assertRaises(ValueError):
                    r.validate_state(corrupted)
                with self.assertRaises(ValueError):
                    r.validate_plan({'slot': slot, 'scheduled_at': at_deadline.replace(second=0).isoformat()})

    def test_duplicate_published_commit_or_pr_is_rejected_including_legacy_evidence(self):
        original_url = r.REPOSITORY + '/pull/7'
        original_commit = 'a' * 40
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                s = portfolio_state()
                if legacy:
                    prior = legacy_record('2026-11-26')
                    prior.update({'outcome': 'published', 'pr_url': original_url, 'commit': original_commit,
                                  'published_at': prior['started_at'], 'published_date': prior['date']})
                    s['history'] = [prior]
                else:
                    first = r.claim(s, clock(s['next_at']))
                    r.finish(s, clock('2026-11-27T08:01:00-08:00'), first['run_id'], first['lease_id'],
                             'published', original_url, original_commit, '2026-11-27T08:01:00-08:00')
                target = clock(s['next_at'])
                owner = r.claim(s, target)
                when = target + timedelta(minutes=1)
                before = copy.deepcopy(s)
                for url, commit in ((r.REPOSITORY + '/pull/8', original_commit), (original_url, 'b' * 40)):
                    with self.subTest(url=url, commit=commit), self.assertRaisesRegex(ValueError, 'cannot be counted again'):
                        r.finish(s, when, owner['run_id'], owner['lease_id'], 'published', url, commit, when.isoformat())
                    self.assertEqual(s, before)
                r.finish(s, when, owner['run_id'], owner['lease_id'], 'published',
                         r.REPOSITORY + '/pull/9', 'c' * 40, when.isoformat())
                self.assertEqual(len(s['history']), 2)

    def test_duplicate_legacy_publications_keep_v1_policy_but_refuse_migration_without_rewriting(self):
        for same_commit in (True, False):
            s = state('2026-11-27T17:09:00-08:00')
            for number, day in enumerate(('2026-11-25', '2026-11-26'), 1):
                record = legacy_record(day, number)
                record.update({'outcome': 'published',
                               'pr_url': r.REPOSITORY + '/pull/' + str(number if same_commit else 1),
                               'commit': 'a' * 40 if same_commit else f'{number:040x}',
                               'published_at': record['started_at'], 'published_date': day})
                s['history'].append(record)
            r.validate_state(s)  # Version 1 intentionally retains its original policy.
            before = copy.deepcopy(s)
            with self.assertRaisesRegex(ValueError, 'cannot be counted again'):
                r.migrate_to_portfolio(s, clock('2026-11-27T07:00:00-08:00'))
            self.assertEqual(s, before)

    def test_blocked_unpublished_work_may_continue_in_a_later_slot(self):
        s = portfolio_state()
        first = r.claim(s, clock(s['next_at']))
        intended_pr = r.REPOSITORY + '/pull/7'
        r.finish(s, clock('2026-11-27T08:01:00-08:00'), first['run_id'], first['lease_id'],
                 'blocked', note='Synthetic test: unpublished continuation intended for ' + intended_pr)
        target = clock(s['next_at'])
        owner = r.claim(s, target)
        when = target + timedelta(minutes=1)
        record = r.finish(s, when, owner['run_id'], owner['lease_id'], 'published',
                          intended_pr, 'a' * 40, when.isoformat())
        self.assertEqual(record['pr_url'], intended_pr)
        self.assertEqual([entry['outcome'] for entry in s['history']], ['blocked', 'published'])

    def test_v2_write_margin_rejects_exact_cutoff_without_invalidating_late_completion(self):
        s = portfolio_state('2026-11-27T20:30:00-08:00', 3)
        r.claim(s, clock(s['next_at']))
        owner = r.claim(s, clock('2026-11-27T23:44:58-08:00'))
        checked = r.check_publish(s, clock('2026-11-27T23:44:59-08:00'), owner['run_id'], owner['lease_id'])
        self.assertEqual(checked['publish_before'], '2026-11-27T23:45:00-08:00')
        before = copy.deepcopy(s)
        for when in ('2026-11-27T23:45:00-08:00', '2026-11-27T23:59:59-08:00'):
            with self.subTest(now=when), self.assertRaisesRegex(ValueError, 'before 23:45'):
                r.check_publish(s, clock(when), owner['run_id'], owner['lease_id'])
            self.assertEqual(s, before)
        record = r.finish(s, clock('2026-11-28T00:10:00-08:00'), owner['run_id'], owner['lease_id'],
                          'published', r.REPOSITORY + '/pull/15', 'd' * 40, '2026-11-27T23:59:30-08:00')
        self.assertEqual(record['published_date'], '2026-11-27')
        self.assertEqual(record['finished_date'], '2026-11-27')

    def test_stale_same_day_work_rotates_lease_and_records_late_completion(self):
        s = portfolio_state()
        first = r.claim(s, clock(s['next_at']))
        resumed = r.claim(s, clock('2026-11-27T23:43:00-08:00'))
        self.assertEqual(resumed['action'], 'resume')
        for field in ('run_id', 'date', 'branch', 'started_at', 'slot', 'scheduled_at'):
            self.assertEqual(resumed[field], first[field])
        self.assertNotEqual(resumed['lease_id'], first['lease_id'])
        before = copy.deepcopy(s)
        with self.assertRaises(ValueError):
            r.check_publish(s, clock('2026-11-27T23:44:00-08:00'), first['run_id'], first['lease_id'])
        self.assertEqual(s, before)
        checked = r.check_publish(s, clock('2026-11-27T23:44:00-08:00'), resumed['run_id'], resumed['lease_id'])
        self.assertEqual(checked['publish_before'], '2026-11-27T23:45:00-08:00')
        record = r.finish(s, clock('2026-11-28T00:10:00-08:00'), resumed['run_id'], resumed['lease_id'], 'published',
                          PORTFOLIO_REPOSITORIES[1] + '/pull/9', 'c' * 40, '2026-11-28T07:59:30Z')
        self.assertEqual((record['date'], record['published_date'], record['finished_date']), ('2026-11-27',) * 3)
        self.assertEqual(record['scheduled_at'], '2026-11-27T08:00:00-08:00')
        self.assertEqual((s['next_slot'], clock(s['next_at']).date().isoformat()), (1, '2026-11-28'))

    def test_prior_date_recovery_only_records_old_work_then_plans_remaining_today(self):
        s = portfolio_state()
        first = r.claim(s, clock(s['next_at']))
        recovered = r.claim(s, clock('2026-11-28T12:00:00-08:00'))
        self.assertEqual(recovered['action'], 'recover')
        self.assertEqual((recovered['run_id'], recovered['slot'], recovered['scheduled_at']),
                         (first['run_id'], 1, first['scheduled_at']))
        with self.assertRaises(ValueError):
            r.check_publish(s, clock('2026-11-28T12:01:00-08:00'), recovered['run_id'], recovered['lease_id'])
        record = portfolio_finish(s, recovered, '2026-11-28T12:01:00-08:00')
        self.assertEqual(record['finished_date'], '2026-11-27')
        self.assertEqual((s['next_slot'], clock(s['next_at']).date().isoformat()), (2, '2026-11-28'))
        self.assertEqual(s['plans'][0], {'slot': 1, 'scheduled_at': '2026-11-27T08:00:00-08:00'})

    def test_add_project_and_publication_evidence_enforce_explicit_owner_scope(self):
        s = portfolio_state()
        added = 'https://github.com/VikramMenon09/market-carbon-data'
        self.assertIs(r.add_project(s, added), s)
        self.assertEqual(s['allowed_repositories'], PORTFOLIO_REPOSITORIES + [added])
        self.assertEqual(s['scope_version'], 1)
        before = copy.deepcopy(s)
        self.assertIs(r.add_project(s, added), s)
        self.assertEqual(s, before)
        invalid = ['https://github.com/VikramMenon09/Eco-Evidence',
                   added + '.git', added + '.GIT', added + '.gIt',
                   'http://github.com/VikramMenon09/new-project', 'https://github.com/another-owner/new-project',
                   'https://github.com/person@VikramMenon09/new-project', added + '/', added + '?x=1', added + '#x',
                   'https://github.com/VikramMenon09/../new-project', 'https://github.com/VikramMenon09/.hidden',
                   'https://github.com/VikramMenon09/' + 'x' * 101]
        for repository in invalid:
            before = copy.deepcopy(s)
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                r.add_project(s, repository)
            self.assertEqual(s, before)
        owner = r.claim(s, clock(s['next_at']))
        before = copy.deepcopy(s)
        with self.assertRaises(ValueError):
            r.add_project(s, 'https://github.com/VikramMenon09/another-project')
        self.assertEqual(s, before)
        for repository in ('https://github.com/another-owner/eco-evidence',
                           'https://github.com/VikramMenon09/not-explicitly-added'):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                portfolio_finish(s, owner, '2026-11-27T08:01:00-08:00', repository=repository, outcome='published')
            self.assertEqual(s, before)
        record = portfolio_finish(s, owner, '2026-11-27T08:01:00-08:00', repository=added, outcome='published')
        self.assertEqual(record['pr_url'], added + '/pull/7')

    def test_v2_configuration_and_plan_identity_are_strict(self):
        original = portfolio_state()
        mutations = []
        for field, value in [('scope_version', True), ('scope_version', 2), ('next_slot', True), ('next_slot', 0),
                             ('unknown_field', 1), ('allowed_repositories', []),
                             ('allowed_repositories', PORTFOLIO_REPOSITORIES * 2), ('plans', [])]:
            changed = copy.deepcopy(original); changed[field] = value; mutations.append((field, changed))
        for name, plan in [('duplicate', original['plans'][0]),
                           ('same-slot-new-minute', {'slot': 1, 'scheduled_at': '2026-11-27T08:01:00-08:00'}),
                           ('wrong-band', {'slot': 2, 'scheduled_at': '2026-11-27T08:00:00-08:00'}),
                           ('boolean-slot', {'slot': True, 'scheduled_at': '2026-11-28T08:00:00-08:00'})]:
            changed = copy.deepcopy(original); changed['plans'].append(copy.deepcopy(plan)); mutations.append((name, changed))
        for name, changed in mutations:
            with self.subTest(case=name):
                before = copy.deepcopy(changed)
                with self.assertRaises(ValueError):
                    r.claim(changed, clock('2026-11-27T08:00:00-08:00'))
                self.assertEqual(changed, before)
        completed = portfolio_state()
        owner = r.claim(completed, clock(completed['next_at']))
        portfolio_finish(completed, owner, '2026-11-27T08:01:00-08:00')
        duplicate = copy.deepcopy(completed['history'][0])
        duplicate['run_id'], duplicate['lease_id'] = 'e' * 24, 'f' * 32
        completed['history'].append(duplicate)
        with self.assertRaises(ValueError):
            r.validate_state(completed)


class PortfolioCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='portfolio-routine-test-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'state.json'

    def cli(self, now, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.path), '--now', now, *args],
                              text=True, capture_output=True, timeout=15)

    def test_cli_migration_add_project_and_schedule_ack_preserve_history_and_reject_bad_writes(self):
        original = state('2026-11-27T17:09:00-08:00')
        original['history'] = [legacy_record('2026-11-26')]
        self.path.write_text(json.dumps(original))
        result = self.cli('2026-11-27T07:00:00-08:00', 'migrate-to-portfolio')
        self.assertEqual(result.returncode, 0, result.stderr)
        migrated = json.loads(self.path.read_text())
        self.assertEqual(migrated['history'], original['history'])
        self.assertEqual((migrated['version'], migrated['next_slot']), (2, 1))
        self.assertTrue(migrated['schedule_sync_pending'])
        before = self.path.read_bytes()
        for args in [('migrate-to-portfolio',), ('add-project', '--repository', 'https://github.com/other/repo'),
                     ('ack-schedule', '--next-at', '2026-11-28T08:00:00-08:00')]:
            result = self.cli('2026-11-27T07:01:00-08:00', *args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(self.path.read_bytes(), before)
        result = self.cli('2026-11-27T07:01:00-08:00', 'add-project', '--repository', 'https://github.com/VikramMenon09/resource-data')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli('2026-11-27T07:02:00-08:00', 'ack-schedule', '--next-at', migrated['next_at'])
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.path.read_text())
        self.assertFalse(saved['schedule_sync_pending'])
        self.assertEqual(saved['plans'], migrated['plans'])
        self.assertEqual(saved['history'], original['history'])

    def test_cli_missed_plan_advances_without_replanning_and_eligible_plan_cannot_be_discarded(self):
        self.path.write_text(json.dumps(portfolio_state()))
        before = self.path.read_bytes()
        for now in ('2026-11-27T07:00:00-08:00', '2026-11-27T09:00:00-08:00'):
            result = self.cli(now, 'plan-next')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(self.path.read_bytes(), before)
        result = self.cli('2026-11-27T11:16:00-08:00', 'claim')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['reason'], 'planned_slot_missed')
        self.assertEqual(self.path.read_bytes(), before)
        result = self.cli('2026-11-27T11:16:00-08:00', 'plan-next')
        self.assertEqual(result.returncode, 0, result.stderr)
        planned = json.loads(self.path.read_text())
        self.assertEqual(planned['next_slot'], 2)
        self.assertEqual(len(planned['plans']), 2)
        self.assertEqual(planned['plans'][0], {'slot': 1, 'scheduled_at': '2026-11-27T08:00:00-08:00'})
        self.assertEqual(planned['history'], [])
        self.assertTrue(planned['schedule_sync_pending'])
        before = self.path.read_bytes()
        result = self.cli('2026-11-27T11:17:00-08:00', 'plan-next')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_duplicate_publication_preserves_state_bytes_and_current_lease(self):
        s = portfolio_state()
        first = r.claim(s, clock(s['next_at']))
        original_url, original_commit = r.REPOSITORY + '/pull/7', 'a' * 40
        r.finish(s, clock('2026-11-27T08:01:00-08:00'), first['run_id'], first['lease_id'],
                 'published', original_url, original_commit, '2026-11-27T08:01:00-08:00')
        target = clock(s['next_at'])
        owner = r.claim(s, target)
        self.path.write_text(json.dumps(s))
        before = self.path.read_bytes()
        when = (target + timedelta(minutes=1)).isoformat()
        for url, commit in ((r.REPOSITORY + '/pull/8', original_commit), (original_url, 'b' * 40)):
            result = self.cli(when, 'finish', '--run-id', owner['run_id'], '--lease-id', owner['lease_id'],
                              '--outcome', 'published', '--pr-url', url, '--commit', commit, '--published-at', when)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertIn('cannot be counted again', result.stderr)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(json.loads(self.path.read_text())['active']['lease_id'], owner['lease_id'])

    def test_concurrent_v2_claims_have_one_owner_and_rejected_active_changes_preserve_bytes(self):
        self.path.write_text(json.dumps(portfolio_state()))
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.cli('2026-11-27T08:00:00-08:00', 'claim'), range(8)))
        self.assertTrue(all(result.returncode == 0 for result in results))
        payloads = [json.loads(result.stdout) for result in results]
        self.assertEqual([item['action'] for item in payloads].count('run'), 1)
        self.assertEqual([item['action'] for item in payloads].count('skip'), 7)
        owner = next(item for item in payloads if item['action'] == 'run')
        self.assertEqual((owner['slot'], owner['branch']), (1, 'daily/2026-11-27/slot-1'))
        before = self.path.read_bytes()
        for args in [('migrate-to-portfolio',), ('plan-next',),
                     ('add-project', '--repository', 'https://github.com/VikramMenon09/extra-project')]:
            result = self.cli('2026-11-27T08:01:00-08:00', *args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(self.path.read_bytes(), before)
        result = self.cli('2026-11-27T08:01:00-08:00', 'status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(owner['lease_id'], result.stdout)
        self.assertEqual(json.loads(result.stdout)['active']['slot'], 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), ['state.json', 'state.json.lock'])
        r.validate_state(json.loads(self.path.read_text()))


if __name__ == '__main__':
    unittest.main()
