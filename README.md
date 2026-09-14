# CrossOver Trial Refresh for Codex

A macOS Codex skill for a narrowly scoped CrossOver 26.3 trial-counter change, with private backups, checks that preserve bottles, a personal 13-day routine, and issue reporting.

## Install with Codex

Give your friend this repository's URL and ask them to paste this into Codex on their own Mac:

> Install the `crossover-trial-refresh` skill from this GitHub repository. It lives at the repository root; install it with the name `crossover-trial-refresh`. Then read the skill, check my CrossOver installation without changing it, and set up my own 13-day routine with alerts for failures and required action. Create and bind my own automation and task IDs. Run changes only when due and all checks pass.

Codex's skill installer can install the repository root with `--path . --name crossover-trial-refresh`. If the skill was just installed, continue on the next turn so Codex discovers it. Installing the files alone does not activate a routine.

After setup, use:

> Use $crossover-trial-refresh to check my routine and report anything that needs attention.

## Requirements

- macOS, Python 3.9 or later, and Codex desktop with local automation tools.
- An initialized, active CrossOver 26.3 trial with a supported default bottle layout.
- CrossOver and Windows programs closed when a change is due.
- The Mac and Codex available for local scheduled execution and alerts.

No third-party Python packages are required. CrossOver, Steam, Ubisoft Connect, and Trackmania are not included or installed by this repository.

## What it changes

The only CrossOver mutation is the macOS `FirstRunDate` preference. The helper checks eligibility and processes, backs up preferences, changes the date, and compares typed preferences and bottle-registry hashes. Codex checks the displayed counter and verifies the saved next schedule.

It refuses expired state, unsupported versions/layouts, inconsistent preferences, or missing routine configuration. It never force-quits a game, deletes caches or bottles, reinstalls CrossOver, or restores an entire plist over newer settings. Interrupted or partially verified work is recorded and must be resolved before another preference change.

Each user has separate configuration, backups, and state under `~/Documents/Codex/CrossOver-trial-backups`. Existing unresolved records are preserved. Treat those local files as private; do not upload them with bug reports.

## Limits of the evidence

One real test on CrossOver 26.3 showed the displayed counter changing from 13 to 14 days after reopening. Only FirstRunDate changed and two default bottle registry hashes stayed identical. This establishes that observed counter change; expired-bottle recovery, game launches after expiry, and indefinite repeatability have not been established. No successful future cycle is guaranteed.

Local scheduling cannot run or deliver immediate alerts while the computer or Codex is unavailable. If a resumed run is outside the verified active-trial window, it pauses for review. This is an independent project and is not affiliated with CodeWeavers.

## Development and tests

Run the isolated regression suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary files and mocked application commands; they do not modify your actual CrossOver installation. The portability tests cover per-user configuration and prevent accidental reuse of another user's automation. GitHub Actions runs the suite on macOS.

The skill entry point is [SKILL.md](SKILL.md). The two helpers are [refresh_trial.py](scripts/refresh_trial.py) and [routine_state.py](scripts/routine_state.py).

## Sharing and reports

Share the repository URL. Keep reports to the program version, status, and a redacted description of the failure. Do not attach preferences, bottle registries, local state, account details, or private task IDs.
