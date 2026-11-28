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
OWNER_URL = 'https://github.com/VikramMenon09/'
PORTFOLIO_REPOSITORIES = tuple(OWNER_URL + name for name in (
    'eco-evidence', 'carbon-commons', 'circular-economy-lab'))
SLOT_BANDS = {1: (8 * 60, 11 * 60), 2: (13 * 60, 16 * 60),
              3: (18 * 60, 20 * 60 + 30)}
CLAIM_GRACE_MINUTES = 15  # Claims include the entire ending minute; plans never use the grace.
PORTFOLIO_WRITE_CUTOFF = time(23, 45)
MAX_PORTFOLIO_RECORDS = 10_000
MAX_PROJECTS = 1_000
ACTIVE_FIELDS = {'run_id', 'lease_id', 'date', 'branch', 'started_at', 'claimed_at'}
HISTORY_FIELDS = ACTIVE_FIELDS | {
    'finished_at', 'finished_date', 'outcome', 'pr_url', 'commit',
    'published_at', 'published_date', 'note',
}
PORTFOLIO_ACTIVE_FIELDS = ACTIVE_FIELDS | {'slot', 'scheduled_at'}
PORTFOLIO_HISTORY_FIELDS = HISTORY_FIELDS | {'slot', 'scheduled_at'}
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
    result = {
        'automation_id': state['automation_id'],
        'next_at': target.isoformat(),
        'timezone': str(ZONE),
        'rrule': f'FREQ=DAILY;BYHOUR={target.hour};BYMINUTE={target.minute};BYSECOND=0',
        'schedule_sync_pending': state['schedule_sync_pending'],
    }
    if state['version'] == 2:
        result.update({'slot': state['next_slot'], 'scope_version': state['scope_version'],
                       'allowed_repositories': list(state['allowed_repositories'])})
    return result


def validate_work(record, fields):
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError('Incomplete or unknown work-record fields; preserve the state and investigate.')
    if not isinstance(record['run_id'], str) or not re.fullmatch(r'[0-9a-f]{24}', record['run_id']):
        raise ValueError('Invalid stable work identifier.')
    if not isinstance(record['lease_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', record['lease_id']):
        raise ValueError('Invalid lease identifier.')
    work_date = calendar_date(record['date'])
    started, claimed = local_now(record['started_at']), local_now(record['claimed_at'])
    branch = 'daily/' + record['date']
    earliest, latest = EARLIEST, LATEST
    if 'slot' in fields:
        target = validate_plan({'slot': record['slot'], 'scheduled_at': record['scheduled_at']})
        branch += '/slot-' + str(record['slot'])
        earliest, latest = claim_band(record['slot'])
        if target.date() != work_date or elapsed(started, target) < 0:
            raise ValueError('Original slot target and start must agree.')
    if record['branch'] != branch or started.date() != work_date:
        raise ValueError('Work date, original start, and branch must agree.')
    if not earliest <= started.hour * 60 + started.minute <= latest or elapsed(claimed, started) < 0:
        raise ValueError('Invalid original start or lease timestamp.')
    return started, claimed


def validate_publication(record, started, finished, repositories=(REPOSITORY,)):
    if not isinstance(record['pr_url'], str) or not any(re.fullmatch(
            re.escape(repository) + r'/pull/[1-9][0-9]*', record['pr_url']) for repository in repositories):
        raise ValueError('A verified pull-request URL in an allowed repository is required.')
    if not isinstance(record['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', record['commit']):
        raise ValueError('The verified full commit SHA is required.')
    published = local_now(record['published_at'])
    if (published.hour < 8 or published.date().isoformat() != record['date']
            or elapsed(published, started) < 0 or elapsed(finished, published) < 0):
        raise ValueError('Publication evidence must be within the work date and between start and finish.')
    if record['published_date'] != published.date().isoformat():
        raise ValueError('Publication date does not match the evidence timestamp.')


def validate_v1_state(state):
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


def validate_repository(repository):
    if not isinstance(repository, str) or not re.fullmatch(
            re.escape(OWNER_URL) + r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', repository) or repository.casefold().endswith('.git'):
        raise ValueError('Project must use the exact owned GitHub HTTPS URL and a safe repository name.')
    return repository


def claim_band(slot):
    """Allow native startup latency through 15 minutes after the last target minute.

    The whole deadline minute is inclusive: 11:15:59, 16:15:59 or 20:45:59.
    This extends first-claim eligibility only; random targets stay in SLOT_BANDS.
    """
    earliest, latest = SLOT_BANDS[slot]
    return earliest, latest + CLAIM_GRACE_MINUTES


def validate_plan(plan):
    if (not isinstance(plan, dict) or set(plan) != {'slot', 'scheduled_at'}
            or type(plan['slot']) is not int or plan['slot'] not in SLOT_BANDS):
        raise ValueError('Invalid portfolio slot plan.')
    target = local_now(plan['scheduled_at'])
    earliest, latest = SLOT_BANDS[plan['slot']]
    if not earliest <= target.hour * 60 + target.minute <= latest or target.second or target.microsecond:
        raise ValueError('Portfolio target must be a whole minute inside its slot band.')
    return target


def validate_v2_state(state):
    required = {'version', 'scope_version', 'automation_id', 'allowed_repositories', 'timezone',
                'next_at', 'next_slot', 'plans', 'active', 'history', 'schedule_sync_pending'}
    if (not isinstance(state, dict) or set(state) != required or type(state['version']) is not int
            or state['version'] != 2 or type(state['scope_version']) is not int or state['scope_version'] != 1):
        raise ValueError('Unknown portfolio state or scope version; preserve it and investigate.')
    if state['timezone'] != str(ZONE):
        raise ValueError('Portfolio timezone does not match.')
    if not isinstance(state['automation_id'], str) or not state['automation_id'].strip():
        raise ValueError('Missing native automation identifier.')
    if type(state['schedule_sync_pending']) is not bool:
        raise ValueError('Invalid schedule synchronization flag.')
    repositories = state['allowed_repositories']
    if not isinstance(repositories, list) or not 3 <= len(repositories) <= MAX_PROJECTS:
        raise ValueError('Invalid portfolio repository list.')
    for repository in repositories:
        validate_repository(repository)
    if (not set(PORTFOLIO_REPOSITORIES).issubset(repositories)
            or len({repository.casefold() for repository in repositories}) != len(repositories)):
        raise ValueError('Required repositories are missing or repository aliases are duplicated.')
    plans = state['plans']
    if not isinstance(plans, list) or not 1 <= len(plans) <= MAX_PORTFOLIO_RECORDS:
        raise ValueError('Portfolio plans must contain 1–10,000 retained records; no automatic pruning is allowed.')
    planned = {}
    previous_by_slot = {}
    previous = None
    for plan in plans:
        target = validate_plan(plan)
        key = (target.date().isoformat(), plan['slot'])
        if key in planned or previous is not None and elapsed(target, previous) <= 0:
            raise ValueError('Portfolio plans must be chronological and unique by date and slot.')
        old = previous_by_slot.get(plan['slot'])
        if old and (old.hour, old.minute) == (target.hour, target.minute):
            raise ValueError('A slot must change its last recorded clock minute, including across gaps.')
        planned[key] = plan
        previous_by_slot[plan['slot']] = target
        previous = target
    if (type(state['next_slot']) is not int or state['next_slot'] != plans[-1]['slot']
            or state['next_at'] != plans[-1]['scheduled_at']):
        raise ValueError('The current target must match the latest immutable slot plan.')
    history = state['history']
    if not isinstance(history, list) or len(history) > MAX_PORTFOLIO_RECORDS:
        raise ValueError('Portfolio history exceeds 10,000 retained records; preserve and archive explicitly.')
    legacy_dates, completed, identifiers = set(), set(), set()
    published_commits, published_prs = set(), set()
    for record in history:
        legacy = isinstance(record, dict) and set(record) == HISTORY_FIELDS
        started, claimed = validate_work(record, HISTORY_FIELDS if legacy else PORTFOLIO_HISTORY_FIELDS)
        finished = local_now(record['finished_at'])
        if elapsed(finished, claimed) < 0 or not isinstance(record['note'], str) or len(record['note']) > 2000:
            raise ValueError('Invalid completion timestamp or note.')
        if record['outcome'] == 'published':
            validate_publication(record, started, finished, (REPOSITORY,) if legacy else repositories)
            if record['commit'] in published_commits or record['pr_url'] in published_prs:
                raise ValueError('A published commit or pull request cannot be counted again in portfolio history.')
            published_commits.add(record['commit'])
            published_prs.add(record['pr_url'])
            consumed_date = record['published_date']
        elif record['outcome'] == 'blocked':
            if any(record[key] != '' for key in ('pr_url', 'commit', 'published_at', 'published_date')):
                raise ValueError('Blocked work must not contain publication evidence.')
            consumed_date = record['date']
        else:
            raise ValueError('Invalid recorded outcome.')
        if record['finished_date'] != consumed_date or record['run_id'] in identifiers:
            raise ValueError('Invalid or duplicate completed work identity.')
        identifiers.add(record['run_id'])
        if legacy:
            if consumed_date in legacy_dates:
                raise ValueError('Duplicate legacy consumed date.')
            legacy_dates.add(consumed_date)
        else:
            key = (record['date'], record['slot'])
            if (key in completed or key not in planned
                    or record['scheduled_at'] != planned[key]['scheduled_at']):
                raise ValueError('Completed slot must refer to one unique original plan.')
            completed.add(key)
    if any(day in legacy_dates for day, _ in planned):
        raise ValueError('A legacy consumed day cannot also receive portfolio slot plans.')
    active = state['active']
    if active is not None:
        validate_work(active, PORTFOLIO_ACTIVE_FIELDS)
        key = (active['date'], active['slot'])
        if (key in completed or active['run_id'] in identifiers or key not in planned
                or active['scheduled_at'] != planned[key]['scheduled_at']
                or active['scheduled_at'] != state['next_at'] or active['slot'] != state['next_slot']):
            raise ValueError('Active work must own the current uncompleted slot plan.')
    return state


def validate_state(state):
    if not isinstance(state, dict) or type(state.get('version')) is not int:
        raise ValueError('Unknown routine state format; preserve it and investigate.')
    if state['version'] == 1:
        return validate_v1_state(state)
    if state['version'] == 2:
        return validate_v2_state(state)
    raise ValueError('Unknown routine state version; explicit migration is required.')


def _portfolio_target(now, plans, legacy_dates, chooser):
    now = now.astimezone(ZONE)
    ready = (now.astimezone(timezone.utc) + timedelta(minutes=SCHEDULE_LEAD_MINUTES)).astimezone(ZONE)
    by_key = {(local_now(plan['scheduled_at']).date(), plan['slot']): local_now(plan['scheduled_at'])
              for plan in plans}
    previous_by_slot = {plan['slot']: local_now(plan['scheduled_at']) for plan in plans}
    last = local_now(plans[-1]['scheduled_at']) if plans else None
    day = max(now.date(), last.date()) if last else now.date()
    while True:
        if day not in legacy_dates:
            for slot, (earliest, latest) in SLOT_BANDS.items():
                if (day, slot) in by_key:
                    continue
                previous = previous_by_slot.get(slot)
                choices = []
                for minute in range(earliest, latest + 1):
                    target = datetime.combine(day, time(minute // 60, minute % 60), ZONE)
                    if elapsed(target, ready) < 0 or last and elapsed(target, last) <= 0:
                        continue
                    if previous and (target.hour, target.minute) == (previous.hour, previous.minute):
                        continue
                    choices.append(target)
                if choices:
                    return slot, chooser(choices)
        day += timedelta(days=1)


def next_portfolio_target(state, now, chooser=secrets.choice):
    """Select a future unplanned slot without mutating state or retrying misses."""
    validate_state(state)
    if state['version'] != 2:
        raise ValueError('Explicit portfolio migration is required before selecting slots.')
    legacy_dates = {calendar_date(record['date']) for record in state['history'] if 'slot' not in record}
    return _portfolio_target(now, state['plans'], legacy_dates, chooser)


def _advance_portfolio(state, now):
    slot, target = next_portfolio_target(state, now)
    candidate = {**state, 'next_at': target.isoformat(), 'next_slot': slot,
                 'plans': state['plans'] + [{'slot': slot, 'scheduled_at': target.isoformat()}],
                 'schedule_sync_pending': True}
    validate_state(candidate)
    state.update(candidate)


def migrate_to_portfolio(state, now, chooser=secrets.choice):
    """Explicit migration; preserve every legacy record and refuse active work."""
    validate_state(state)
    if state['active'] is not None:
        raise ValueError('Finish or recover active work before migrating; stale leases are still active work.')
    if state['version'] != 1:
        raise ValueError('State already uses the portfolio format; preserve its existing plan.')
    legacy_dates = {calendar_date(record['date']) for record in state['history']}
    slot, target = _portfolio_target(now, [], legacy_dates, chooser)
    candidate = {key: value for key, value in state.items() if key != 'repository'}
    candidate.update({'version': 2, 'scope_version': 1,
                      'allowed_repositories': list(PORTFOLIO_REPOSITORIES),
                      'next_at': target.isoformat(), 'next_slot': slot,
                      'plans': [{'slot': slot, 'scheduled_at': target.isoformat()}],
                      'schedule_sync_pending': True})
    validate_state(candidate)
    state.clear()
    state.update(candidate)
    return state


def add_project(state, repository):
    """Register an intended owned project; registration does not assert its existence."""
    validate_state(state)
    if state['version'] != 2:
        raise ValueError('Explicit portfolio migration is required before adding projects.')
    if state['active'] is not None:
        raise ValueError('Finish or recover active work before changing the portfolio scope.')
    validate_repository(repository)
    if repository in state['allowed_repositories']:
        return state
    if repository.casefold() in {item.casefold() for item in state['allowed_repositories']}:
        raise ValueError('A differently cased alias of this repository is already registered.')
    candidate = {**state, 'allowed_repositories': state['allowed_repositories'] + [repository]}
    validate_state(candidate)
    state.update(candidate)
    return state


def _portfolio_disposition(state, now):
    target = local_now(state['next_at'])
    if elapsed(now, target) < 0:
        return {'action': 'skip', 'reason': 'not_due_yet'}
    slot = state['next_slot']
    if any(record.get('slot') == slot and record['date'] == target.date().isoformat()
           for record in state['history']):
        return {'action': 'skip', 'reason': 'slot_already_finished'}
    earliest, latest = claim_band(slot)
    if target.date() != now.date() or not earliest <= now.hour * 60 + now.minute <= latest:
        return {'action': 'skip', 'reason': 'planned_slot_missed', 'required_command': 'plan-next'}
    return {'action': 'run', 'reason': 'due'}


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
    if state['version'] == 2:
        return _portfolio_disposition(state, now)
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
        if state['version'] == 2:
            state['active'].update({'slot': state['next_slot'], 'scheduled_at': state['next_at']})
            state['active']['branch'] += '/slot-' + str(state['next_slot'])
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
    if state['version'] == 2:
        publish_before = datetime.combine(now.date(), PORTFOLIO_WRITE_CUTOFF, ZONE)
        if elapsed(now, publish_before) >= 0:
            raise ValueError('Portfolio external writes must begin before 23:45 Pacific on the work date.')
    else:
        publish_before = datetime.combine(now.date() + timedelta(days=1), time(), ZONE)
    active['claimed_at'] = now.isoformat()
    return {'action': 'publication_allowed', **active, 'publish_before': publish_before.isoformat()}


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
    history = state['history'] + [record]
    candidate = {**state, 'history': history if state['version'] == 2 else history[-120:], 'active': None}
    validate_state(candidate)
    if state['version'] == 2:
        _advance_portfolio(candidate, now)
    else:
        candidate['next_at'] = next_target(
            now, state['next_at'], after_date=record['finished_date'],
            consumed_dates=[h['finished_date'] for h in candidate['history']]).isoformat()
        candidate['schedule_sync_pending'] = True
    validate_state(candidate)
    state.update(candidate)
    return record


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON state field; preserve the original file.')
        result[key] = value
    return result


@contextmanager
def locked_state(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = validate_state(json.loads(path.read_text(), object_pairs_hook=_unique_fields)) if path.exists() else None
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
    sub.add_parser('migrate-to-portfolio')
    project = sub.add_parser('add-project')
    project.add_argument('--repository', required=True)
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
                if args.command == 'migrate-to-portfolio':
                    migrate_to_portfolio(state, now)
                    atomic_write(args.state, state)
                    result = {'action': 'migrated_to_portfolio'}
                elif args.command == 'add-project':
                    add_project(state, args.repository)
                    atomic_write(args.state, state)
                    result = {'action': 'project_registered', 'repository': args.repository}
                elif args.command == 'status':
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
                    if state['version'] == 2:
                        if disposition(state, now)['action'] == 'run':
                            raise ValueError('The planned slot is still eligible; claim it rather than silently discarding it.')
                        _advance_portfolio(state, now)
                    else:
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
