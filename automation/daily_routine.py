#!/usr/bin/env python3
"""Daily contribution guard and random schedule planner; never edits Codex config."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('America/Los_Angeles')
EARLIEST = 8 * 60
LATEST = 22 * 60 + 30
LEASE_SECONDS = 3 * 3600
SCHEDULE_LEAD_MINUTES = 5
REPOSITORY = 'https://github.com/VikramMenon09/eco-evidence'
ACTIVE_FIELDS = {'run_id', 'lease_id', 'date', 'branch', 'started_at', 'claimed_at'}
HISTORY_FIELDS = ACTIVE_FIELDS | {
    'finished_at', 'finished_date', 'outcome', 'pr_url', 'commit',
    'published_at', 'published_date', 'note',
}
_CURRENT_TIME = object()


def local_now(value=_CURRENT_TIME):
    if value is _CURRENT_TIME:
        return datetime.now(ZONE)
    if not isinstance(value, str) or not value:
        raise ValueError('A timestamp must be a nonempty offset-aware ISO string.')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('A supplied clock must include its UTC offset.')
    return result.astimezone(ZONE)


def calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('A work date must use YYYY-MM-DD.')
    return date.fromisoformat(value)


def elapsed(now, then):
    """Elapsed seconds, including offset changes across a DST transition."""
    return (now.astimezone(timezone.utc) - then.astimezone(timezone.utc)).total_seconds()


def next_target(now, previous=None, chooser=secrets.choice, *, after_date=None, consumed_dates=()):
    """Plan after a consumed day; recovered work can leave time to start today."""
    now = now.astimezone(ZONE)
    previous_minute = None
    if previous:
        old = local_now(previous)
        previous_minute = old.hour * 60 + old.minute
    day = now.date() + timedelta(days=1) if after_date is None else max(
        calendar_date(after_date) + timedelta(days=1), now.date())
    consumed = {calendar_date(value) for value in consumed_dates}
    while True:
        earliest = EARLIEST
        if day == now.date():
            ready = now + timedelta(minutes=SCHEDULE_LEAD_MINUTES)
            earliest = max(earliest, ready.hour * 60 + ready.minute + bool(ready.second or ready.microsecond))
            if ready.date() != day:
                earliest = LATEST + 1
        choices = [m for m in range(earliest, LATEST + 1) if m != previous_minute]
        if day not in consumed and choices:
            minute = chooser(choices)
            return datetime.combine(day, time(minute // 60, minute % 60), ZONE)
        day += timedelta(days=1)


def schedule(state):
    target = local_now(state['next_at'])
    return {
        'automation_id': state['automation_id'],
        'next_at': target.isoformat(),
        'timezone': str(ZONE),
        'rrule': f'FREQ=DAILY;BYHOUR={target.hour};BYMINUTE={target.minute};BYSECOND=0',
        'schedule_sync_pending': state['schedule_sync_pending'],
    }


def validate_work(record, fields):
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError('Incomplete or unknown work-record fields; preserve the state and investigate.')
    if not isinstance(record['run_id'], str) or not re.fullmatch(r'[0-9a-f]{24}', record['run_id']):
        raise ValueError('Invalid stable work identifier.')
    if not isinstance(record['lease_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', record['lease_id']):
        raise ValueError('Invalid lease identifier.')
    work_date = calendar_date(record['date'])
    started, claimed = local_now(record['started_at']), local_now(record['claimed_at'])
    if record['branch'] != 'daily/' + record['date'] or started.date() != work_date:
        raise ValueError('Work date, original start, and branch must agree.')
    if not EARLIEST <= started.hour * 60 + started.minute <= LATEST or elapsed(claimed, started) < 0:
        raise ValueError('Invalid original start or lease timestamp.')
    return started, claimed


def validate_publication(record, started, finished):
    if not isinstance(record['pr_url'], str) or not re.fullmatch(
            re.escape(REPOSITORY) + r'/pull/[1-9][0-9]*', record['pr_url']):
        raise ValueError('A verified pull-request URL in this repository is required.')
    if not isinstance(record['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', record['commit']):
        raise ValueError('The verified full commit SHA is required.')
    published = local_now(record['published_at'])
    if (published.hour < 8 or published.date().isoformat() != record['date']
            or elapsed(published, started) < 0 or elapsed(finished, published) < 0):
        raise ValueError('Publication evidence must be within the work date and between start and finish.')
    if record['published_date'] != published.date().isoformat():
        raise ValueError('Publication date does not match the evidence timestamp.')


def validate_state(state):
    required = {'version', 'automation_id', 'repository', 'timezone', 'next_at',
                'active', 'history', 'schedule_sync_pending'}
    if not isinstance(state, dict) or set(state) != required or type(state.get('version')) is not int or state['version'] != 1:
        raise ValueError('Unknown routine state format; preserve it and investigate.')
    if state['repository'] != REPOSITORY or state['timezone'] != str(ZONE):
        raise ValueError('Routine repository or timezone does not match.')
    if not isinstance(state['automation_id'], str) or not state['automation_id'].strip():
        raise ValueError('Missing native automation identifier.')
    if type(state['schedule_sync_pending']) is not bool:
        raise ValueError('Invalid schedule synchronization flag.')
    target = local_now(state['next_at'])
    if not EARLIEST <= target.hour * 60 + target.minute <= LATEST or target.second or target.microsecond:
        raise ValueError('Invalid planned contribution time.')
    if not isinstance(state['history'], list) or len(state['history']) > 120:
        raise ValueError('Invalid contribution history.')
    dates, identifiers = set(), set()
    for record in state['history']:
        started, claimed = validate_work(record, HISTORY_FIELDS)
        finished = local_now(record['finished_at'])
        if elapsed(finished, claimed) < 0 or not isinstance(record['note'], str) or len(record['note']) > 2000:
            raise ValueError('Invalid completion timestamp or note.')
        if record['outcome'] == 'published':
            validate_publication(record, started, finished)
            consumed_date = record['published_date']
        elif record['outcome'] == 'blocked':
            if any(record[key] != '' for key in ('pr_url', 'commit', 'published_at', 'published_date')):
                raise ValueError('Blocked work must not contain publication evidence.')
            consumed_date = record['date']
        else:
            raise ValueError('Invalid recorded outcome.')
        if record['finished_date'] != consumed_date or consumed_date in dates or record['run_id'] in identifiers:
            raise ValueError('Invalid or duplicate consumed work day.')
        dates.add(consumed_date)
        identifiers.add(record['run_id'])
    if state['active'] is not None:
        validate_work(state['active'], ACTIVE_FIELDS)
        if state['active']['date'] in dates or state['active']['run_id'] in identifiers:
            raise ValueError('Active work has already been completed.')
    return state


def disposition(state, now):
    """New work uses the daily guard; stale work must first be reconciled."""
    validate_state(state)
    now = now.astimezone(ZONE)
    active = state['active']
    if active:
        age = elapsed(now, local_now(active['claimed_at']))
        if age < 0:
            raise ValueError('Clock predates the active lease.')
        if age < LEASE_SECONDS:
            return {'action': 'skip', 'reason': 'another_run_has_a_live_lease'}
        action = 'resume' if active['date'] == now.date().isoformat() and now.hour >= 8 else 'recover'
        return {'action': action, 'reason': 'stale_lease_requires_reconciliation'}
    if now.hour < 8:
        return {'action': 'skip', 'reason': 'outside_publication_window'}
    if any(h['finished_date'] == now.date().isoformat() for h in state['history']):
        return {'action': 'skip', 'reason': 'day_already_finished'}
    if now < local_now(state['next_at']):
        return {'action': 'skip', 'reason': 'not_due_yet'}
    if now.hour * 60 + now.minute > LATEST:
        return {'action': 'skip', 'reason': 'too_late_to_start_safely'}
    return {'action': 'run', 'reason': 'due'}


def claim(state, now):
    now = now.astimezone(ZONE)
    result = disposition(state, now)
    if result['action'] == 'skip':
        return result
    if state['active'] is None:
        state['active'] = {
            'run_id': secrets.token_hex(12), 'date': now.date().isoformat(),
            'branch': 'daily/' + now.date().isoformat(), 'started_at': now.isoformat(),
            'lease_id': secrets.token_hex(16), 'claimed_at': now.isoformat(),
        }
    else:
        # Work identity never changes; a stale owner's token is permanently invalid.
        state['active']['lease_id'] = secrets.token_hex(16)
        state['active']['claimed_at'] = now.isoformat()
    return {**result, **state['active']}


def require_owner(state, now, run_id, lease_id):
    validate_state(state)
    active = state['active']
    if (not active or not isinstance(run_id, str) or not isinstance(lease_id, str)
            or not secrets.compare_digest(active['run_id'], run_id)
            or not secrets.compare_digest(active['lease_id'], lease_id)):
        raise ValueError('Work identifier or lease identifier does not match the current owner.')
    if not 0 <= elapsed(now, local_now(active['claimed_at'])) < LEASE_SECONDS:
        raise ValueError('Lease expired or clock moved backwards; reclaim and reconcile before proceeding.')
    return active


def check_publish(state, now, run_id, lease_id):
    """Call immediately before each external write; this does not publish anything."""
    now = now.astimezone(ZONE)
    active = require_owner(state, now, run_id, lease_id)
    if now.hour < 8 or active['date'] != now.date().isoformat():
        raise ValueError('Publication is allowed only from 08:00 until midnight on the immutable work date.')
    active['claimed_at'] = now.isoformat()
    return {'action': 'publication_allowed', **active,
            'publish_before': datetime.combine(now.date() + timedelta(days=1), time(), ZONE).isoformat()}


def finish(state, now, run_id, lease_id, outcome, pr_url='', commit='', published_at='', note=''):
    """Record server-verified evidence; bookkeeping time never consumes another day."""
    now = now.astimezone(ZONE)
    active = require_owner(state, now, run_id, lease_id)
    if not isinstance(note, str):
        raise ValueError('Completion note must be text.')
    published = local_now(published_at) if outcome == 'published' else None
    record = {**active, 'finished_at': now.isoformat(),
              'finished_date': published.date().isoformat() if published else active['date'],
              'outcome': outcome, 'pr_url': pr_url, 'commit': commit,
              'published_at': published.isoformat() if published else published_at,
              'published_date': published.date().isoformat() if published else '', 'note': note[:2000]}
    # Validate a candidate copy so rejected evidence cannot partially mutate the state.
    candidate = {**state, 'history': (state['history'] + [record])[-120:], 'active': None}
    validate_state(candidate)
    candidate['next_at'] = next_target(
        now, state['next_at'], after_date=record['finished_date'],
        consumed_dates=[h['finished_date'] for h in candidate['history']]).isoformat()
    candidate['schedule_sync_pending'] = True
    validate_state(candidate)
    state.update(candidate)
    return record


@contextmanager
def locked_state(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = validate_state(json.loads(path.read_text())) if path.exists() else None
        yield state


def atomic_write(path, state):
    validate_state(state)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True, help='Untracked local state JSON path')
    parser.add_argument('--now', help='Offset-aware ISO time for reproducible tests only')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('initialize')
    init.add_argument('--automation-id', required=True)
    init.add_argument('--first-at', required=True)
    sub.add_parser('status')
    sub.add_parser('claim')
    preflight = sub.add_parser('check-publish')
    preflight.add_argument('--run-id', required=True)
    preflight.add_argument('--lease-id', required=True)
    done = sub.add_parser('finish')
    done.add_argument('--run-id', required=True)
    done.add_argument('--lease-id', required=True)
    done.add_argument('--outcome', choices=['published', 'blocked'], required=True)
    done.add_argument('--pr-url', default='')
    done.add_argument('--commit', default='')
    done.add_argument('--published-at', default='')
    done.add_argument('--note', default='')
    sub.add_parser('plan-next')
    ack = sub.add_parser('ack-schedule')
    ack.add_argument('--next-at', required=True)
    args = parser.parse_args(argv)
    try:
        now = local_now() if args.now is None else local_now(args.now)
        with locked_state(args.state) as state:
            if args.command == 'initialize':
                if state is None:
                    state = {'version': 1, 'automation_id': args.automation_id, 'repository': REPOSITORY,
                             'timezone': str(ZONE), 'next_at': local_now(args.first_at).isoformat(),
                             'active': None, 'history': [], 'schedule_sync_pending': True}
                    if local_now(state['next_at']) <= now:
                        raise ValueError('Initial target must be in the future.')
                    atomic_write(args.state, state)
                elif state['automation_id'] != args.automation_id:
                    raise ValueError('Existing state belongs to a different native automation.')
                result = {'action': 'initialized'}
            else:
                if state is None:
                    raise ValueError('Initialize this routine before running it.')
                if args.command == 'status':
                    active = ({k: v for k, v in state['active'].items() if k != 'lease_id'}
                              if state['active'] else None)
                    result = {**disposition(state, now), 'active': active, 'recent_history': state['history'][-5:]}
                elif args.command == 'claim':
                    result = claim(state, now)
                    if result['action'] != 'skip':
                        atomic_write(args.state, state)
                elif args.command == 'check-publish':
                    result = check_publish(state, now, args.run_id, args.lease_id)
                    atomic_write(args.state, state)
                elif args.command == 'finish':
                    result = {'action': 'finished', 'record': finish(
                        state, now, args.run_id, args.lease_id, args.outcome,
                        args.pr_url, args.commit, args.published_at, args.note)}
                    atomic_write(args.state, state)
                elif args.command == 'plan-next':
                    if state['active']:
                        raise ValueError('Finish or recover the active run before planning another.')
                    if local_now(state['next_at']) > now:
                        raise ValueError('The current target is still in the future; keep the existing plan.')
                    state['next_at'] = next_target(
                        now, state['next_at'], after_date=(now.date() - timedelta(days=1)).isoformat(),
                        consumed_dates=[h['finished_date'] for h in state['history']]).isoformat()
                    state['schedule_sync_pending'] = True
                    result = {'action': 'planned'}
                    atomic_write(args.state, state)
                else:
                    if args.next_at != state['next_at']:
                        raise ValueError('Acknowledgement target does not match the latest plan.')
                    state['schedule_sync_pending'] = False
                    result = {'action': 'schedule_acknowledged'}
                    atomic_write(args.state, state)
            print(json.dumps({**result, **schedule(state)}, indent=2))
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
        parser.exit(2, f'Routine state error: {exc}\n')


if __name__ == '__main__':
    main()
