# Daily contribution routine

The owner's Codex automation attempts one substantial contribution per Pacific calendar day. It chooses a new random start minute between **08:00 and 22:30 America/Los_Angeles**, leaving time for validation and publication before midnight. It avoids the immediately preceding start minute. Random times are generated locally, rather than repeating a fixed weekly timetable.

The native scheduler stores an ordinary daily time. After each attempted contribution, the agent uses the **Codex Automations action** to move that same automation to the next randomly chosen time. The local `next_at` date prevents a second contribution if a changed daily clock time also produces a scheduler check later on the current day. A skipped check must not pick another time or create a commit.

The scheduling helper only computes plans and maintains local state. It does **not** create an OS cron job, edit Codex configuration, push code, or change an automation itself. Native scheduling remains under the user's control; pausing the automation must remain effective.

## Run protocol

Use Python 3.11 or newer on macOS/Linux. Choose an untracked local state path outside the source tree.

1. Read `AGENTS.md`, the roadmap, the native automation's current settings, and the helper's `status` output. Preserve current user pauses and preferences.
2. If `schedule_sync_pending` is true, apply the returned schedule through the native automation action, preserving the existing prompt, destination, and activation/notification settings, then run `ack-schedule` with the exact `next_at` value.
3. Run `claim`. If it says `skip`, do not perform repository work. In particular, a future target, a completed day, and a live lease are normal no-work outcomes.
4. A successful claim gives a stable `run_id`, work date and branch, plus a separate `lease_id` for the current owner. Use only the token returned by your own successful claim; never adopt a token from the state file, status output, or another worker. A `resume` action reuses today's interrupted work; `recover` only reconciles an earlier day's attempt. Inspect local changes and any existing PR for that branch before editing or pushing. Never duplicate an existing PR or overwrite unrelated work. Never publish from an older work date: record its verified previous publication or blocked outcome first, then follow the returned schedule.
5. Pick one useful change, delegate bounded work, check primary evidence, implement it, run tests and the actual user workflow, and obtain the independent Claude review. Keep the change focused and substantial.
6. Immediately before each push, PR publication, or merge, run `check-publish` with both IDs. It verifies the current owner's unexpired lease, today's work date, and the 08:00–23:59 Pacific window, then refreshes the lease. Do not proceed if it fails. The helper cannot hold a lock across GitHub actions; keep only this owner writing, use the expected head SHA when merging, and recheck ownership after an interruption. Do not begin new external writes after 23:45, leaving a margin before the hard midnight cutoff. If the window ends, preserve unpublished work and record a blocked attempt. Do not backdate commits.
7. Push the feature branch, publish or update its PR, attach the PR to the current chat, verify the exact head's checks, and merge authorized work when ready. Record the real PR URL, full head commit SHA and an independently verified GitHub publication timestamp with `finish --outcome published`. For a newly created PR, use its `createdAt`; for an existing PR completed this run, use the verified publication/merge time for this run, not an old creation date. This records actual publication, not a claim that every published PR was merged; report PR and merge status separately. A blocked attempt uses `finish --outcome blocked` and a concrete reason.
8. Apply the returned schedule through the native automation action, verify it, then acknowledge the exact plan. If rescheduling fails, leave the pending flag in place for recovery and report that failure.

## Example helper commands

```sh
python3 automation/daily_routine.py --state /path/to/local-state.json status
python3 automation/daily_routine.py --state /path/to/local-state.json claim
python3 automation/daily_routine.py --state /path/to/local-state.json check-publish \
  --run-id THE_RECORDED_RUN_ID --lease-id THE_CURRENT_LEASE_ID
python3 automation/daily_routine.py --state /path/to/local-state.json finish \
  --run-id THE_RECORDED_RUN_ID --lease-id THE_CURRENT_LEASE_ID --outcome published \
  --pr-url https://github.com/VikramMenon09/eco-evidence/pull/123 \
  --commit THE_VERIFIED_FULL_COMMIT_SHA --published-at THE_VERIFIED_PUBLICATION_TIME
```

Those final arguments are placeholders in this example, not real contribution evidence. Initialization requires the actual native automation ID and a future offset-aware first target. The `--now` option exists only for reproducible tests; ordinary runs must use the real clock.

A run lease lasts three hours. A stale reclaim retains the branch, original work date and run ID but rotates the lease ID, invalidating the previous owner's token. For lengthy work, call `check-publish` again before 2.5 hours have elapsed to refresh ownership within the allowed window. If ownership only expired, call `claim`: continue publication only for a same-run `resume` with the newly returned lease ID. A `recover` result permits bookkeeping only, and any different run or owner means stop. Finishing also requires a current, unexpired lease; claim the stale run first if its lease expired before bookkeeping. Do not take over a live lease or mark another running agent's work blocked.

Bookkeeping is permitted after midnight. A PR created at 23:59 and recorded at 00:10 belongs to the previous day; a blocked finish at 00:10 also consumes only its original work date. If verified GitHub evidence shows a publication crossed midnight or occurred outside the allowed window, do not record it as a valid same-day publication. Record the original attempt as blocked, put its actual URL and timestamp in the note, and report the timing violation. A PR created inside the window can still count as published if a later merge is blocked. An interrupted prior-day run is reconciled before new work. The helper can then schedule a random time later that same day if a valid window remains, rather than accidentally skipping an extra day.

If a scheduled check arrives after 22:30, do not start new work. With no active lease, use `plan-next` once to recover a stale target, then apply and acknowledge it. An overdue wake before 08:00 can also use `plan-next` to choose a time in the remaining day. Do not advance an already-future target. A missing or invalid state file is a blocker: preserve it and inspect existing branches and PRs before recovering it; never silently initialize a fresh empty history.

## What a successful day means

A success has a useful artifact or behavior, documented evidence, passing checks, the independent review, and a real GitHub PR. A local commit alone is not publication. Keep the computer on and Codex running for this local routine. An unavailable machine, account, data source, reviewer, or CI service can prevent a day's delivery; failures must remain visible instead of being replaced with filler activity.

Paid sponsorship and spending are outside this routine. GitHub achievement awards are separate from the work log and must be verified from GitHub if reported.
