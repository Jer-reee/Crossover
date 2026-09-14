---
name: crossover-trial-refresh
description: Manage explicitly requested CrossOver 26.3 trial-counter changes and a personal 13-day Codex routine with backups, verification, and issue reporting. Use for setup, refreshes, and troubleshooting; not uninstalling CrossOver or repairing expired bottles.
---

# CrossOver trial refresh

Use this skill when the current user explicitly requests a preference refresh or recurring routine. Installing the skill alone does not enable a schedule or authorize a preference change. Use the bundled scripts as the ordinary macOS user. The helper changes only `com.codeweavers.CrossOver` → `FirstRunDate`. Preserve bottles, games, caches, unrelated preferences, and running programs. Do not force-quit, reinstall, or broaden a failure into expired-bottle repair.

## Evidence and boundaries

On September 12, CrossOver 26.3 displayed 14 days after previously showing 13. Only FirstRunDate changed; Steam and Ubisoft Connect `system.reg` hashes stayed identical. This is a single observed result; personal preference files and logs are intentionally excluded from this repository.

That establishes the active-trial counter change. Windows program execution, expired-bottle recovery, Trackmania functionality, and indefinite repeatability remain unverified. Never claim those outcomes from a counter check.

The helper enforces the supported version, active trial under 14 days, idle processes, consistent typed preferences, supported default bottle configuration, private backups, and unchanged registry hashes. It refuses custom/managed bottle locations. A shared lock prevents overlapping helper writes; it cannot prevent the user launching an app after a process check.

## First-time setup for this user

Use an available Python 3.9+ interpreter. Read `routine_state.py status` before creating anything; all paths below are relative to the installed skill. The user's private configuration supplies the automation and task IDs. Never copy another person's IDs or trial date.

1. Run `python3 scripts/refresh_trial.py` read-only. Confirm an initialized, supported active trial. A refusal requires resolving the reason before enabling a routine.
2. If setup is requested and no binding exists, inspect existing Codex automations for this skill. Reuse a matching routine or create one **PAUSED** heartbeat attached to this user's current task, with a 13-day interval. Do not create shell cron jobs or launch agents. Use the automation tool's actual returned ID and the real current task ID; do not guess them.
3. Bind that routine locally:

   ```sh
   python3 scripts/routine_state.py configure --automation-id AUTOMATION_ID --thread-id TASK_ID
   ```

   Repeating the same binding is harmless. A different binding requires reviewing the existing routine and explicitly using `--replace`; unresolved pending work blocks retargeting. Treat a matching existing binding as authoritative.
4. Update the saved heartbeat to ACTIVE with an explicit UTC anchor at the returned `next_due_at_utc`. If already due but still inside the 14-day window, use the next minute, capped at the returned deadline, so the helper evaluates the current state at execution. Read back the saved schedule before reporting setup complete. Initial creation may reject an explicit DTSTART; create paused with the simple interval, then update the existing automation with the UTC anchor.
5. Save a concise prompt that tells Codex to load this installed skill, run its helper once with `--apply`, follow the returned plan, verify completion, and report new issues, required action, recovery, or completed refreshes. Keep ordinary early checks and previously reported unchanged blockers quiet. Preserve the user's notification policy through the automation tool.

The configuration uses `$CODEX_HOME/automations` when CODEX_HOME is set, otherwise the real user's `~/.codex/automations`. Never share the generated state, backups, or automation TOML. The scheduled helper refuses to write until it has a local binding.

## Execute once or resume

Resolve script paths relative to this skill directory. For the authorized scheduled run, run **one** command:

```sh
python3 scripts/refresh_trial.py --apply
```

Default minimum age is 13 days. An explicit immediate-refresh request may use `--minimum-age-days 0`; scheduled runs must retain the default. Future scheduling remains 13 days even after a manual override. For a read-only check, omit `--apply`; this does not create backups or routine state. Do not routinely run both preflight and apply.

Inspect the JSON and process exit together. `refreshed` confirms preference and registry checks; UI and schedule completion are separate. `not_due` is normal. `deferred_busy` or `deferred_changed` requires a bounded retry. `overlap_skipped` is a quiet skip because another invocation owns the lock; leave its schedule alone.

`pending_followup` means a previous run has unfinished work. Follow its recorded `automation_plan` and `previous_status`; do not reset again. Interrupted write intent is uncertain, even if the date now looks recent. Preserve the backup and inspect the uncertainty before considering another write.

State is at `~/Documents/Codex/CrossOver-trial-backups/_routine/state.json`. Read it with:

```sh
python3 scripts/routine_state.py status
```

The helper stores timestamped private preference backups and machine-readable results under the same backup root. Never restore an entire plist over newer user changes. A requested undo restores only the backed-up FirstRunDate after the same idle/state checks.

## Finish the recorded run

Use the returned `run_id` and `automation_plan`. Do not calculate dates from local wall time or infer success from exit zero alone.

1. If `requires_ui` is true and no saved UI receipt exists, inspect CrossOver's trial dialog through Computer Use. Confirm 14 days, without launching a Windows program or installer. Bound inspection to a few attempts; unavailable UI is an issue. If you opened CrossOver solely for this check, quit normally afterward, preserving anything the user started. If inspection failed, immediately call `routine_state.py complete --run-id RUN_ID --ui-unavailable` or pass the actual `--ui-days N`. This records the issue and returns a pause plan; do this before changing the schedule.
2. Apply `automation_plan` to the heartbeat identified by the saved local configuration using `automation_update`. Preserve name, prompt, configured task target, and unmuted notification setting. `schedule` means ACTIVE with the exact returned UTC recurrence; `pause` means PAUSED. Never edit automation TOML directly or create another schedule.
3. Verify the saved schedule and record completion:

   ```sh
   python3 scripts/routine_state.py complete --run-id RUN_ID --ui-days 14
   ```

   Pass `--ui-days 14` only for an actually observed counter. Omit UI arguments when not required or when a saved receipt exists, including after applying the pause for failed UI verification.

`complete` reads back the actual automation identity, target, status, recurrence, and notification setting. A mismatch becomes a durable issue. Fix the scheduling problem according to the returned plan and call `complete` again; do not rerun the preference write. If completion returns `pending_followup`, follow its updated plan and complete again. Overdue unfinished plans recalculate a bounded blocked-run retry or pause for review, so a past date is never accepted as completion. `followup_complete` or `pause_verified` confirms the saved schedule, not delivery of a notification.

Retries occur at the earlier of one hour later or the 14-day deadline. At the deadline the routine pauses and escalates. Successful changes re-anchor the next run to the verified date plus 13 days. Unsupported state, partial writes, failed logs, or failed verification pause for review. A verified pause stays recorded and blocks further preference changes. After diagnosing and resolving that specific issue, `clear-reviewed-pause --run-id RUN_ID` clears the reviewed record; do not clear merely to bypass a guard or automatically re-enable an unresolved routine.

## Report issues and completion

When setting up the routine, preserve the user's request for issue reporting. Newly detected blockers, nonzero exits, uncertain writes, missing/invalid JSON, unknown statuses, a dry-run-only scheduled outcome, unavailable UI, failed backups/logging, and schedule failures require a final issue report. A refreshed status does not override an accompanying error. State what failed, whether a preference changed or remains uncertain, backup availability, and the next action or retry. Explicitly report a pause. For heartbeat runs, use `NOTIFY` for a new issue, worsening issue, recovery, or completed refresh; use `DONT_NOTIFY` for healthy early checks, ordinary overlap skips, and already-reported unchanged blockers.

Use `issue_fingerprint` and `alert_required` to suppress duplicates. Only acknowledge a **previous** issue after reading this task's history and confirming its final issue report actually exists:

```sh
python3 scripts/routine_state.py ack-report --issue-id ISSUE_ID
```

Then compare the current fingerprint with state `last_reported_issue` before deciding whether the issue is unchanged; saved result booleans predate the acknowledgment. Never acknowledge merely because a report is drafted; the script cannot verify final-message or OS-notification delivery. Recovery clears the old acknowledgment so a later recurrence alerts again. If state itself cannot be read or saved, report that failure directly and pause through the automation tool.

Local execution requires the Mac and Codex app to be available. This routine cannot issue an immediate alert while its host is unavailable; a run resumed after the 14-day deadline refuses the write and reports the missed window.
