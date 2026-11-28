# Portfolio contribution routine

The owner's Codex automation targets three useful contributions per Pacific calendar day across the [project portfolio](PORTFOLIO.md). It plans a fresh random start minute in each of three bands:

| Session | Planned start window, America/Los_Angeles |
| --- | --- |
| 1 — morning | 08:00–11:00 |
| 2 — afternoon | 13:00–16:00 |
| 3 — evening | 18:00–20:30 |

Only one session can own an active lease. Initial claims have a 15-minute startup grace after their band's final minute: through 11:15:59, 16:15:59 or 20:45:59 respectively. Targets themselves stay inside the table's bands. An attempt may continue beyond its start band, but version 2 publication preflight rejects new GitHub writes at or after 23:45, with a hard evidence cutoff at midnight. Missed sessions are skipped, not accumulated into a catch-up burst. Each date/session can be attempted once; a blocked attempt consumes that session too. A later day's corresponding session avoids repeating the most recent planned clock minute, including across gaps. Random choices are generated locally, not taken from a fixed weekly timetable.

The native scheduler stores one ordinary daily time: the next pending session. After each attempt or expired start window, the agent uses the **Codex Automations action** to move that same automation to the next planned time. Local date/session guards prevent extra work when the daily rule also wakes on an unintended date. A skipped check must not create a commit or rerandomize a still-future target.

The helper does **not** edit Codex configuration, create OS jobs, publish code or communicate with collaborators. Keep the computer on and Codex running for this local routine. A missed native wake can delay recovery until the same clock time the following day; later slots are not independent background jobs and may also be missed. Pauses and notification preferences remain controlled by the user. Unavailable accounts, reviewers, sources or checks can prevent delivery; report these failures honestly.

## Migration and configuration

Use Python 3.11 or newer on macOS/Linux. State stays untracked outside the repository. Version 1 remains readable with its original one-attempt-per-day behavior. Existing state is never silently migrated or reinitialized.

To enable the portfolio, inspect and back up the existing state, verify there is no active run, then explicitly execute:

```sh
python3 automation/daily_routine.py --state /path/to/local-state.json migrate-to-portfolio
```

Migration preserves the earlier history objects and their original identities. Their dates remain consumed conservatively. Version 2 records a `slot`, a planned timestamp and a distinct `daily/YYYY-MM-DD/slot-N` branch for each new attempt. It also retains plans so missed slots cannot be resampled. Its initial repository allowlist contains EcoEvidence, Carbon Commons and Circular Economy Lab. A later separately scoped project can be registered while no work is active:

```sh
python3 automation/daily_routine.py --state /path/to/local-state.json add-project \
  --repository https://github.com/VikramMenon09/NEW_PROJECT_NAME
```

That URL is a placeholder. Registration accepts only an exact owned GitHub URL with a safe repository name. It does not create a repository or verify its contents. Never use a new empty state to bypass existing history. The `--now` flag is only for reproducible tests; real runs use the real clock. Version 2 retains at most 10,000 plans and 10,000 history records, rejecting further writes at the limit instead of silently discarding history; at three sessions a day this permits roughly nine years of operation before explicit archival maintenance.

## Run protocol

1. Check the native automation's current settings and the helper's `status` output promptly. If paused, stop and preserve the pause. Defer project investigation until after claiming so normal preparation does not consume the start window.
2. If `schedule_sync_pending` is true, apply the returned schedule through the native automation action, preserving the full current prompt, destination, activation state and notification preferences. Verify the stored schedule, then run `ack-schedule --next-at` with the exact returned timestamp.
3. Run `claim`. On `skip`, do no project work. A future target or another owner's live lease is an ordinary no-work result. When the target's start band and grace have expired and no lease is active, use `plan-next` once, synchronize and acknowledge that plan. Never advance an already-future target. Planning chooses the next eligible band at least five minutes ahead. A new owned repository URL may be registered before claim; keep this lightweight and do substantive research after claiming.
4. A successful claim returns the stable `run_id`, work date, slot, scheduled timestamp and branch, plus the current owner's `lease_id`. Use only the token returned by your own successful claim. A stale `resume` retains the work identity and rotates its token; an older-date `recover` permits bookkeeping only. Inspect local changes and existing PRs before editing. Never duplicate a PR, overwrite unrelated work or republish older work as today's contribution.
5. Read `AGENTS.md`, this guide and the selected project's instructions and roadmap. Choose one substantial portfolio task, delegate bounded work, verify primary evidence where needed, implement it, run meaningful tests and the normal user entry point, and obtain actual Claude review. Repair material findings. A feature, sourced study or repaired defect is an outcome; an empty commit is not.
6. Immediately before every repository creation, push, PR creation/update or merge, run `check-publish` with both ownership IDs. Version 2 verifies an unexpired current lease and the same Pacific work date, 08:00 until 23:45, then refreshes ownership. An operation started before the margin can finish later, but publication evidence must still precede midnight. The helper cannot hold its local lock across GitHub actions, so keep only the current owner writing and recheck after interruptions. Use the verified expected head SHA for a merge and retain review protections.
7. Commit with accurate authorship, publish a focused PR and attach it to the chat. Verify CI on the exact head before merging authorized changes. Record the verified PR URL, full head SHA and server publication timestamp with `finish --outcome published`; report merge status separately. For a new PR use its creation time; for an existing PR updated this run use independently verified publication/merge evidence from this work date. Never substitute bookkeeping time or count the same earlier publication again. If no qualifying publication occurred, finish as blocked with the concrete reason.
8. Synchronize the returned next plan through the native automation action, verify it, and acknowledge the exact target. Preserve a pause that occurred during work. If synchronization fails, retain the pending flag and report the failure.

## Ownership and evidence

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

These final arguments are examples, not publication evidence. Version 2 accepts published PRs only from its explicit repository allowlist and rejects a previously published PR URL or commit SHA, including evidence retained from version 1. Choose unpublished work for a new counted session. An already recorded PR can still need maintenance, but closing or updating it is not another qualifying publication; describe that work without recording a second success. Local commits alone are not publication.

A lease lasts three hours. Refresh with `check-publish` before 2.5 hours have elapsed during long work. If it expires, claim again and continue only on a same-run `resume` using the new token. Never take over a live lease or copy an owner token from status, the state file or another worker. Finishing requires a current unexpired lease too.

Bookkeeping can happen after midnight while retaining the original work date and slot. A PR created at 23:59 and recorded at 00:10 belongs to the original date; a PR created after midnight is not a valid publication for the earlier attempt. Record a timing violation as blocked, retain its actual URL/timestamp in the note, and report it. Reconcile an interrupted prior-day attempt before starting another one. Recovery can schedule a remaining slot today without consuming an unrelated day.

Version 1 still uses its original 08:00–22:30 start window, one attempt per date and `daily/YYYY-MM-DD` branches until explicit migration. Do not apply the three-slot protocol to unmigrated state. The native action, not this helper, enforces user pauses; check native status before work and preserve it during every schedule update.
