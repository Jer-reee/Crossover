#!/usr/bin/env python3
"""Local coordination and receipts for the CrossOver routine; never edits CrossOver."""

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import tempfile
import uuid

UTC = dt.timezone.utc
INTERVAL = dt.timedelta(days=13)


class ConfigurationError(ValueError):
    pass


def validate_configuration(config):
    """Accept identifiers, never paths or shell fragments, from an explicit binding."""
    if not isinstance(config, dict):
        raise ConfigurationError(
            "Routine setup is required. Create a paused Codex heartbeat for your own task, then run "
            "routine_state.py configure --automation-id ID --thread-id ID before using --apply.")
    for key in ("automation_id", "thread_id"):
        value = config.get(key)
        if (not isinstance(value, str) or len(value) > 200
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None):
            raise ConfigurationError(
                "Routine configuration has an invalid " + key
                + ". Review the binding and rerun configure with valid Codex identifiers.")
    return {key: config[key] for key in ("automation_id", "thread_id")}


def automation_path(home, config):
    config = validate_configuration(config)
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    root = Path(codex_home).expanduser() if codex_home else Path(home) / ".codex"
    return root / "automations" / config["automation_id"] / "automation.toml"


def utc_now():
    return dt.datetime.now(UTC)


def timestamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_date(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A UTC offset is required")
    return parsed.astimezone(UTC)


def schedule_at(at):
    if at.tzinfo is None:
        raise ValueError("A UTC offset is required")
    at = at.astimezone(UTC)
    return {"action": "schedule", "next_run_at_utc": timestamp(at),
            "rrule": "DTSTART:" + at.strftime("%Y%m%dT%H%M%S")
                     + "Z\nRRULE:FREQ=DAILY;INTERVAL=13", "requires_ui": False}


def plan_result(result, now):
    """Plan a bounded follow-up from observed state, never from wall-clock guesses."""
    status = result.get("status")
    if (result.get("exit_code", 0) != 0 or result.get("result_record_error")
            or result.get("may_have_changed") or result.get("state_record_error")):
        return {"action": "pause", "requires_ui": False}
    if status == "refreshed":
        plan = schedule_at(parse_date(result["observed_first_run_utc"]) + INTERVAL)
        plan["requires_ui"] = True
        return plan
    if status == "not_due":
        due = parse_date(result["old_first_run_utc"]) + INTERVAL
        if due <= now:
            return {"action": "pause", "requires_ui": False,
                    "reason": "An inconsistent due date needs review."}
        return schedule_at(due)
    if status in {"deferred_busy", "deferred_changed"}:
        deadline = result.get("retry_deadline_at_utc")
        if deadline and now < parse_date(deadline):
            return schedule_at(min(now + dt.timedelta(hours=1), parse_date(deadline)))
        return {"action": "pause", "requires_ui": False,
                "issue_code": "retry_deadline_reached" if deadline else "retry_deadline_missing",
                "reason": "The retry deadline has been reached; the unresolved blocker needs review."
                          if deadline else "No reliable retry deadline is available; the blocker needs review."}
    return {"action": "pause", "requires_ui": False}


def issue_fingerprint(result):
    if (result.get("status") in {"not_due", "refreshed"}
            and not result.get("exit_code") and not result.get("result_record_error")
            and not result.get("state_record_error") and not result.get("may_have_changed")):
        return None
    stable = {key: result.get(key) for key in
              ("status", "reason", "issue_code", "error_type", "version",
               "changed_registry_paths", "may_have_changed", "exit_code",
               "result_record_error", "state_record_error")}
    stable["processes"] = sorted({p["command"].replace("\\", "/").rsplit("/", 1)[-1]
                                  for p in result.get("active_processes", [])})
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:12]


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Control directory must be owned by the current user and not be a symlink")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)


def atomic_json(path, value):
    fd, temp = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def read_json(path):
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"schema_version": 1}
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 2**20:
            raise ValueError("Unsupported state file")
        value = json.load(stream)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Unsupported state schema")
    return value


class Overlap(Exception):
    pass


class Session:
    def __init__(self, home):
        self.home = Path(home)
        self.root = self.home / "Documents/Codex/CrossOver-trial-backups/_routine"
        self.path = self.root / "state.json"
        self.fd = None
        self.state = None

    def __enter__(self):
        private_directory(self.root)
        self.fd = os.open(str(self.root / "lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Unsupported lock file")
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Overlap("Another routine is still running") from exc
            self.state = read_json(self.path)
            return self
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def __exit__(self, *ignored):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def save(self):
        atomic_json(self.path, self.state)

    def require_configuration(self):
        return validate_configuration(self.state.get("configuration"))

    def configure(self, automation_id, thread_id, replace=False):
        config = validate_configuration({"automation_id": automation_id, "thread_id": thread_id})
        existing = self.state.get("configuration")
        if existing == config:
            return {"status": "already_configured", "configuration": config,
                    "automation_file": str(automation_path(self.home, config))}
        if self.state.get("pending"):
            raise ConfigurationError("Finish or review the unresolved pending run before configuring a different binding.")
        if existing is not None and not replace:
            raise ConfigurationError("A different routine is already configured. Review the new target, then use configure --replace explicitly.")
        self.state["configuration"] = config
        self.state["configured_at_utc"] = timestamp(utc_now())
        self.save()
        return {"status": "configured", "configuration": config,
                "automation_file": str(automation_path(self.home, config))}

    def decorate(self, result):
        fingerprint = issue_fingerprint(result)
        result["issue_fingerprint"] = fingerprint
        result["alert_required"] = bool(fingerprint and fingerprint != self.state.get("last_reported_issue"))
        result["state_file"] = str(self.path)
        return result

    def pending_fingerprint(self):
        pending = self.state["pending"]
        issue = dict(pending["result"])
        if pending.get("schedule_error"):
            issue.update(status="schedule_verification_failed",
                         reason=pending["schedule_error"]["reason"],
                         issue_code="schedule_readback_failed",
                         error_type=pending["schedule_error"]["error_type"])
        if pending.get("followup_error"):
            issue.update(pending["followup_error"])
        return issue_fingerprint(issue)

    def refresh_elapsed_plan(self, now):
        """Persist a new bounded plan when an unfinished schedule has elapsed."""
        pending = self.state.get("pending")
        if (not pending or pending["plan"]["action"] != "schedule"
                or parse_date(pending["plan"]["next_run_at_utc"]) > now):
            return False
        if pending["result"].get("status") in {"deferred_busy", "deferred_changed"}:
            plan = plan_result(pending["result"], now)
        else:
            plan = {"action": "pause", "requires_ui": False,
                    "issue_code": "followup_schedule_elapsed",
                    "reason": "The recorded follow-up schedule elapsed before completion; review the saved outcome before resuming."}
        pending["elapsed_plan"] = pending["plan"]
        pending["plan"] = plan
        pending["result"]["automation_plan"] = plan
        if plan["action"] == "pause":
            pending["followup_error"] = {
                "status": "orchestration_error", "issue_code": plan.get("issue_code", "followup_schedule_elapsed"),
                "reason": plan.get("reason", "The elapsed follow-up could not be safely rescheduled.")}
        pending["issue_fingerprint"] = self.pending_fingerprint()
        self.save()
        return True

    def resume(self, now=None):
        self.refresh_elapsed_plan(utc_now() if now is None else now)
        pending = self.state.get("pending")
        if not pending:
            return None
        result = dict(pending["result"])
        result["previous_status"] = result.get("status")
        result["status"] = "pending_followup"
        result["run_id"] = pending["run_id"]
        result["automation_plan"] = pending["plan"]
        result["followup_only"] = True
        result["preference_write_allowed"] = False
        result["reason"] = "Finish the recorded verification/scheduling work before any new preference change."
        if pending["phase"] == "write_intent":
            result.update(previous_status="uncertain_previous_write", may_have_changed=True,
                          reason="The previous process stopped after recording write intent; inspect its backup.")
        if pending.get("schedule_error"):
            result.update(helper_status=pending["result"].get("status"),
                          previous_status="schedule_verification_failed",
                          schedule_readback_error=pending["schedule_error"],
                          reason=pending["schedule_error"]["reason"])
        if pending.get("followup_error"):
            result.update(helper_status=pending["result"].get("status"),
                          previous_status=pending["followup_error"]["status"],
                          issue_code=pending["followup_error"]["issue_code"],
                          reason=pending["followup_error"]["reason"])
        # Preserve the original issue identity; routine resumes must not create duplicate alerts.
        result["issue_fingerprint"] = pending.get("issue_fingerprint")
        result["alert_required"] = bool(result["issue_fingerprint"] and
                                        result["issue_fingerprint"] != self.state.get("last_reported_issue"))
        result["state_file"] = str(self.path)
        return result

    def write_intent(self, result):
        self.require_configuration()
        snapshot = dict(result)
        snapshot.update(status="uncertain_previous_write", may_have_changed=True,
                        reason="A preference write was about to be attempted; completion is unconfirmed.")
        self.state["pending"] = {"run_id": result["run_id"], "phase": "write_intent",
                                 "result": snapshot, "plan": {"action": "pause", "requires_ui": False},
                                 "issue_fingerprint": issue_fingerprint(snapshot)}
        self.save()

    def record(self, result, now):
        result.setdefault("run_id", uuid.uuid4().hex)
        result["automation_plan"] = plan_result(result, now)
        if result["automation_plan"].get("reason"):
            result.update(status="orchestration_error", reason=result["automation_plan"]["reason"])
            result["issue_code"] = result["automation_plan"].get("issue_code")
        self.decorate(result)
        self.state["pending"] = {"run_id": result["run_id"], "phase": "result_ready",
                                 "result": result, "plan": result["automation_plan"],
                                 "issue_fingerprint": result["issue_fingerprint"]}
        self.save()

    def complete(self, run_id, automation_path, ui_days=None, ui_unavailable=False, now=None):
        config = self.require_configuration()
        read_clock = utc_now if now is None else lambda: now
        pending = self.state.get("pending")
        if not pending or pending["run_id"] != run_id:
            raise ValueError("The run ID does not match the pending run")
        if ui_days is not None and ui_unavailable:
            raise ValueError("UI observation and UI unavailable are mutually exclusive")
        if self.refresh_elapsed_plan(read_clock()):
            return self.resume(read_clock())
        if pending["plan"].get("requires_ui"):
            if ui_days != 14:
                result = pending["result"]
                result.update(status="ui_verification_failed", exit_code=1,
                              reason="UI verification was unavailable." if ui_unavailable else "The UI did not verify 14 days.",
                              ui_observed_days=ui_days)
                pending.update(plan={"action": "pause", "requires_ui": False},
                               issue_fingerprint=issue_fingerprint(result))
                result["automation_plan"] = pending["plan"]
                self.save()
                return self.resume(read_clock())
            pending["result"]["ui_observed_days"] = 14
            pending["ui_verified_at_utc"] = timestamp(read_clock())
            pending["plan"]["requires_ui"] = False
            pending["result"]["automation_plan"] = pending["plan"]
            # Persist the observed counter independently of schedule availability.
            self.save()
        try:
            if self.refresh_elapsed_plan(read_clock()):
                return self.resume(read_clock())
            verify_schedule(automation_path, pending["plan"], config)
        except (OSError, ValueError) as exc:
            pending["schedule_error"] = {"reason": str(exc), "error_type": type(exc).__name__}
            issue = dict(pending["result"])
            issue.update(status="schedule_verification_failed", reason=str(exc),
                         issue_code="schedule_readback_failed", error_type=type(exc).__name__)
            pending["issue_fingerprint"] = issue_fingerprint(issue)
            self.save()
            raise
        if self.refresh_elapsed_plan(read_clock()):
            return self.resume(read_clock())
        if pending.get("schedule_error"):
            pending["resolved_schedule_error"] = pending.pop("schedule_error")
            pending["issue_fingerprint"] = self.pending_fingerprint()
        pending["schedule_readback_verified"] = True
        pending["completed_at_utc"] = timestamp(read_clock())
        if pending["plan"]["action"] == "pause":
            pending["phase"] = "paused_needs_review"
        else:
            self.state["last_completed"] = pending
            self.state.pop("pending")
            if pending["issue_fingerprint"] is None:
                # A recovered issue must alert again if it recurs in a later cycle.
                self.state.pop("last_reported_issue", None)
        self.save()
        return {"status": "pause_verified" if pending["plan"]["action"] == "pause" else "followup_complete",
                "run_id": run_id, "schedule_readback_verified": True, "state_file": str(self.path)}


def verify_schedule(path, plan, config):
    config = validate_configuration(config)
    values = {}
    for line in Path(path).read_text().splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        if separator and key in {"id", "kind", "status", "rrule", "target_thread_id", "notification_policy"}:
            values[key] = json.loads(value.strip())
    if (values.get("id") != config["automation_id"] or values.get("kind") != "heartbeat"
            or values.get("target_thread_id") != config["thread_id"]):
        raise ValueError("Automation identity or task target does not match")
    expected = "PAUSED" if plan["action"] == "pause" else "ACTIVE"
    if values.get("status") != expected:
        raise ValueError("Automation status does not match the plan")
    if plan["action"] == "schedule" and values.get("rrule") != plan["rrule"]:
        raise ValueError("Automation recurrence does not match the planned UTC schedule")
    if values.get("notification_policy") == "failed_runs_only":
        raise ValueError("Automation issue notifications are muted")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "configure", "complete", "ack-report", "clear-reviewed-pause"])
    parser.add_argument("--automation-id", help="Your own Codex heartbeat automation ID.")
    parser.add_argument("--thread-id", help="The Codex task targeted by that heartbeat.")
    parser.add_argument("--replace", action="store_true", help="Explicitly replace a binding after all pending work is resolved.")
    parser.add_argument("--run-id")
    parser.add_argument("--ui-days", type=int)
    parser.add_argument("--ui-unavailable", action="store_true")
    parser.add_argument("--issue-id")
    args = parser.parse_args(argv)
    if os.getuid() == 0 or os.getuid() != os.geteuid():
        parser.error("Run as the ordinary macOS user")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    session = Session(home)
    try:
        if args.action == "status":
            print(json.dumps(read_json(session.path), indent=2))
            return 0
        with session:
            if args.action == "configure":
                value = session.configure(args.automation_id, args.thread_id, args.replace)
            elif args.action == "complete":
                config = session.require_configuration()
                value = session.complete(args.run_id, automation_path(home, config),
                                         args.ui_days, args.ui_unavailable)
            elif args.action == "ack-report":
                known = [session.state.get(k, {}) for k in ("pending", "last_completed")]
                if not args.issue_id or args.issue_id not in [x.get("issue_fingerprint") for x in known]:
                    raise ValueError("Issue ID does not match a recorded run")
                session.state["last_reported_issue"] = args.issue_id
                session.save()
                value = {"status": "report_acknowledged", "issue_fingerprint": args.issue_id}
            else:
                pending = session.state.get("pending", {})
                if pending.get("run_id") != args.run_id or pending.get("phase") != "paused_needs_review":
                    raise ValueError("Only a reviewed, verified pause can be cleared")
                session.state["last_reviewed"] = session.state.pop("pending")
                session.save()
                value = {"status": "reviewed_pause_cleared", "run_id": args.run_id}
            print(json.dumps(value, indent=2))
            return 0
    except (OSError, ValueError, Overlap) as exc:
        print(json.dumps({"status": "orchestration_error", "reason": str(exc), "alert_required": True}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
