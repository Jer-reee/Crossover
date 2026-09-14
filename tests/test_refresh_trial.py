"""Regression tests with isolated files and mocked macOS commands.

Run from the repository root: python3 -m unittest discover -s tests -v.
No real preferences, processes, CrossOver applications, or automations are changed.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
HELPER = HERE.parent / "scripts" / "refresh_trial.py"
sys.path.insert(0, str(HELPER.parent))
spec = importlib.util.spec_from_file_location("refresh_trial_audit_subject", HELPER)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 25, 11, 56, 18, tzinfo=UTC)


class RefreshFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crossover-refresh-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.pref = self.home / "Library/Preferences/com.codeweavers.CrossOver.plist"
        self.pref.parent.mkdir(parents=True)
        self.reg = self.home / "Library/Application Support/CrossOver/Bottles/Steam/system.reg"
        self.reg.parent.mkdir(parents=True)
        self.reg.write_bytes(b"WINE REGISTRY Version 2\nfixture registry\n")
        self.initial = {"FirstRunDate": (NOW - dt.timedelta(days=13)).replace(tzinfo=None),
                        "UserSetting": "must survive", "Nested": {"a": [1, 2, 3]}}
        self.save(self.initial)
        self.before = self.pref.read_bytes()
        self.backups = self.tmp / "backups"
        self.calls = []
        self.ps_calls = 0
        self.ps_mode = "idle"
        self.write_mode = "success"
        self.export_mode = "success"
        self.export_values = None
        self.ps_hook = None
        self.write_hook = None
        self.app = {"path": str(self.home / "Applications/CrossOver.app"),
                    "bundle_id": subject.DOMAIN, "version": "26.3", "info_plist_sha256": "fixture"}
        patches = [
            mock.patch.object(subject.sys, "platform", "darwin"),
            mock.patch.object(subject.pwd, "getpwuid", return_value=types.SimpleNamespace(pw_dir=str(self.home))),
            mock.patch.object(subject, "utc_now", return_value=NOW),
            mock.patch.object(subject, "discover_app", return_value=self.app),
            mock.patch.object(subject.subprocess, "run", side_effect=self.fake_subprocess),
            mock.patch.dict(subject.os.environ, {"HOME": str(self.home)}, clear=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        if hasattr(subject, "routine_state") and hasattr(subject.routine_state, "utc_now"):
            clock_patch = mock.patch.object(subject.routine_state, "utc_now", side_effect=lambda: subject.utc_now())
            clock_patch.start()
            self.addCleanup(clock_patch.stop)
        with subject.routine_state.Session(self.home) as session:
            session.configure("test-automation", "test-thread")

    def save(self, values):
        self.pref.write_bytes(plistlib.dumps(values, fmt=plistlib.FMT_BINARY))

    def fake_subprocess(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == "/bin/ps":
            self.ps_calls += 1
            if self.ps_hook:
                self.ps_hook(self.ps_calls)
            own = f"{os.getuid()} {os.getpid()} /usr/bin/python3\n"
            if self.ps_mode == "failure":
                return subprocess.CompletedProcess(command, 1, "", "fixture error")
            if self.ps_mode == "malformed":
                return subprocess.CompletedProcess(command, 0, own + "malformed\n", "")
            if self.ps_mode == "missing_self":
                return subprocess.CompletedProcess(command, 0, "0 1 /sbin/launchd\n", "")
            if self.ps_mode == "busy":
                own += f"{os.getuid()} 99991 C:\\Program Files\\Steam\\steam.exe\n"
            return subprocess.CompletedProcess(command, 0, own, "")
        if command[:2] == ["/usr/bin/defaults", "export"]:
            if self.export_mode == "error":
                return subprocess.CompletedProcess(command, 1, b"", b"fixture service error")
            if self.export_mode == "invalid":
                return subprocess.CompletedProcess(command, 0, b"not a plist", b"")
            values = self.export_values
            data = self.pref.read_bytes() if values is None else plistlib.dumps(values)
            return subprocess.CompletedProcess(command, 0, data, b"")
        if command[:2] != ["/usr/bin/defaults", "write"]:
            raise AssertionError(f"Unexpected subprocess: {command!r}")
        if self.write_mode == "timeout":
            raise subprocess.TimeoutExpired(command, 10)
        if self.write_mode == "error":
            return subprocess.CompletedProcess(command, 1, b"", b"fixture error")
        values = plistlib.loads(self.pref.read_bytes())
        values["FirstRunDate"] = dt.datetime.strptime(command[-1], "%Y-%m-%d %H:%M:%S %z").replace(tzinfo=None)
        self.save(values)
        if self.write_hook:
            self.write_hook()
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def invoke(self, apply=True, extra=()):
        stream = io.StringIO()
        args = ["--backup-root", str(self.backups), *extra]
        if apply:
            args.append("--apply")
        with contextlib.redirect_stdout(stream):
            code = subject.main(args)
        return code, json.loads(stream.getvalue())

    def writes(self):
        return [call for call in self.calls if call[:2] == ["/usr/bin/defaults", "write"]]

    def assert_no_write(self, output):
        self.assertEqual(self.writes(), [])
        self.assertFalse(output["write_attempted"])

    def schedule_receipt(self, plan, **overrides):
        routine = subject.routine_state
        values = {"id": "test-automation", "kind": "heartbeat",
                  "target_thread_id": "test-thread",
                  "status": "PAUSED" if plan["action"] == "pause" else "ACTIVE"}
        if plan["action"] == "schedule":
            values["rrule"] = plan["rrule"]
        values.update(overrides)
        path = self.tmp / "fixture-automation.toml"
        path.write_text("".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items()))
        return path

    def test_due_dry_run_never_writes_or_creates_backups(self):
        code, output = self.invoke(apply=False)
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "would_refresh")
        self.assertEqual(self.pref.read_bytes(), self.before)
        self.assertFalse(self.backups.exists())
        self.assert_no_write(output)

    def test_due_apply_changes_only_date_with_private_exact_backup(self):
        registry = self.reg.read_bytes()
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "refreshed")
        after = plistlib.loads(self.pref.read_bytes())
        self.assertEqual(after["FirstRunDate"], NOW.replace(tzinfo=None))
        self.assertEqual({k:v for k,v in after.items() if k != "FirstRunDate"},
                         {k:v for k,v in self.initial.items() if k != "FirstRunDate"})
        self.assertEqual(Path(output["preference_backup"]).read_bytes(), self.before)
        self.assertEqual(stat.S_IMODE(Path(output["preference_backup"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(output["backup_directory"]).stat().st_mode), 0o700)
        self.assertEqual(self.reg.read_bytes(), registry)
        self.assertTrue(output["default_registries_unchanged"])
        self.assertTrue(Path(output["result_record"]).is_file())

    def test_not_due_does_not_write_or_backup(self):
        self.initial["FirstRunDate"] = (NOW - dt.timedelta(days=12)).replace(tzinfo=None)
        self.save(self.initial)
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "not_due")
        self.assertFalse(self.backups.exists())
        self.assert_no_write(output)

    def test_future_date_refused(self):
        self.initial["FirstRunDate"] = (NOW + dt.timedelta(seconds=1)).replace(tzinfo=None)
        self.save(self.initial)
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_uninitialized_preference_refused(self):
        self.initial.pop("FirstRunDate")
        self.save(self.initial)
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_string_date_refused(self):
        self.initial["FirstRunDate"] = "2026-09-12T11:56:18Z"
        self.save(self.initial)
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_malformed_plist_refused(self):
        self.pref.write_bytes(b"not a plist")
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_ps_failure_fails_closed(self):
        self.ps_mode = "failure"
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_ps_malformed_row_fails_closed(self):
        self.ps_mode = "malformed"
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_ps_missing_self_fails_closed(self):
        self.ps_mode = "missing_self"
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_running_windows_program_defers_without_backup(self):
        self.ps_mode = "busy"
        code, output = self.invoke()
        self.assertEqual(output["status"], "deferred_busy")
        self.assertFalse(self.backups.exists())
        self.assert_no_write(output)

    def test_app_started_during_backup_is_caught(self):
        def hook(count):
            if count == 2:
                self.ps_mode = "busy"
        self.ps_hook = hook
        code, output = self.invoke()
        self.assertEqual(output["status"], "deferred_busy")
        self.assertTrue(Path(output["preference_backup"]).is_file())
        self.assert_no_write(output)

    def test_unrelated_preference_changed_during_backup_defers(self):
        real_backup = subject.create_backup
        def backup(*args):
            real_backup(*args)
            changed = dict(self.initial, UserSetting="concurrent user update")
            self.save(changed)
        with mock.patch.object(subject, "create_backup", side_effect=backup):
            code, output = self.invoke()
        self.assertEqual(output["status"], "deferred_changed")
        self.assertEqual(plistlib.loads(self.pref.read_bytes())["UserSetting"], "concurrent user update")
        self.assert_no_write(output)

    def test_registry_changed_during_backup_defers(self):
        real_backup = subject.create_backup
        def backup(*args):
            real_backup(*args)
            self.reg.write_bytes(b"concurrent registry update")
        with mock.patch.object(subject, "create_backup", side_effect=backup):
            code, output = self.invoke()
        self.assertEqual(output["status"], "deferred_changed")
        self.assert_no_write(output)

    def test_app_changed_during_backup_defers(self):
        with mock.patch.object(subject, "discover_app", side_effect=[self.app, dict(self.app, version="26.4")]):
            code, output = self.invoke()
        self.assertEqual(output["status"], "deferred_changed")
        self.assert_no_write(output)

    def test_backup_creation_failure_blocks_write(self):
        with mock.patch.object(subject, "secure_write", side_effect=OSError("fixture disk full")):
            code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_write_error_is_uncertain_and_keeps_backup(self):
        self.write_mode = "error"
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(output["status"], "validation_failed")
        self.assertTrue(output["write_attempted"])
        self.assertTrue(output["may_have_changed"])
        self.assertFalse(output["rollback_performed"])
        self.assertTrue(Path(output["preference_backup"]).is_file())

    def test_write_timeout_is_uncertain_and_keeps_backup(self):
        self.write_mode = "timeout"
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(output["status"], "validation_failed")
        self.assertTrue(output["may_have_changed"])
        self.assertTrue(Path(output["preference_backup"]).is_file())

    def test_unrelated_preference_change_after_write_fails_without_rollback(self):
        def hook():
            changed = plistlib.loads(self.pref.read_bytes())
            changed["UserSetting"] = "concurrent update"
            self.save(changed)
        self.write_hook = hook
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(output["status"], "validation_failed")
        self.assertFalse(output["rollback_performed"])
        self.assertEqual(plistlib.loads(self.pref.read_bytes())["UserSetting"], "concurrent update")

    def test_missing_preference_after_write_is_validation_failure(self):
        self.write_hook = self.pref.unlink
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(output["status"], "validation_failed")
        self.assertTrue(output["may_have_changed"])

    def test_registry_change_after_write_is_validation_failure(self):
        self.write_hook = lambda: self.reg.write_bytes(b"registry changed after write")
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(output["status"], "validation_failed")
        self.assertFalse(output["default_registries_unchanged"])

    def test_result_record_failure_never_has_success_exit(self):
        real_write = subject.secure_write
        def failing_result(path, contents):
            if path.name == "result.json":
                raise OSError("fixture disk full")
            return real_write(path, contents)
        with mock.patch.object(subject, "secure_write", side_effect=failing_result):
            code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertIn("result_record_error", output)
        self.assertTrue(Path(output["preference_backup"]).is_file())

    def test_symlink_preferences_are_not_written(self):
        real = self.tmp / "real.plist"
        self.pref.rename(real)
        self.pref.symlink_to(real)
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(real.read_bytes(), self.before)
        self.assert_no_write(output)

    def test_symlink_backup_root_blocks_write(self):
        real = self.tmp / "real-backups"
        real.mkdir()
        self.backups.symlink_to(real, target_is_directory=True)
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assert_no_write(output)

    def test_expired_state_is_outside_active_trial_scope_and_must_not_write(self):
        self.initial["FirstRunDate"] = (NOW - dt.timedelta(days=14)).replace(tzinfo=None)
        self.save(self.initial)
        code, output = self.invoke()
        self.assert_no_write(output)
        self.assertNotEqual(output["status"], "refreshed")

    def test_custom_bottles_are_either_verified_or_blocked_before_write(self):
        alternate = self.home / "ExternalBottleFixtures"
        custom_reg = alternate / "Game/system.reg"
        custom_reg.parent.mkdir(parents=True)
        custom_reg.write_bytes(b"custom bottle registry")
        self.initial["BottleDir"] = str(alternate)
        self.save(self.initial)
        config = self.home / "Library/Application Support/CrossOver/CrossOver.conf"
        config.write_text('[CrossOver]\n"BottlePath" = "' + str(alternate) + '"\n')
        code, output = self.invoke()
        if self.writes():
            all_snapshots = json.dumps(output)
            self.assertIn(str(custom_reg), all_snapshots,
                          "An applied custom-bottle case must include its registry in verification evidence")
        else:
            self.assertNotEqual(output["status"], "refreshed")

    def test_expiry_crossed_during_backup_blocks_write(self):
        real_backup = subject.create_backup
        def backup(*args):
            real_backup(*args)
            subject.utc_now.return_value = NOW + dt.timedelta(days=1)
        with mock.patch.object(subject, "create_backup", side_effect=backup):
            code, output = self.invoke()
        self.assert_no_write(output)
        self.assertNotEqual(output["status"], "refreshed")

    def test_service_error_blocks_write(self):
        self.export_mode = "error"
        code, output = self.invoke()
        self.assert_no_write(output)
        self.assertNotEqual(code, 0)

    def test_invalid_service_export_blocks_write(self):
        self.export_mode = "invalid"
        code, output = self.invoke()
        self.assert_no_write(output)
        self.assertNotEqual(code, 0)

    def test_service_and_disk_disagreement_blocks_write(self):
        self.export_values = dict(self.initial, UserSetting="unflushed preference")
        code, output = self.invoke()
        self.assert_no_write(output)
        self.assertNotEqual(output["status"], "refreshed")

    def test_service_type_difference_is_not_ignored(self):
        self.initial["TypedSetting"] = True
        self.save(self.initial)
        self.export_values = dict(self.initial, TypedSetting=1)
        code, output = self.invoke()
        self.assert_no_write(output)

    def test_unrelated_preference_type_change_after_write_fails(self):
        self.initial["TypedSetting"] = True
        self.save(self.initial)
        def hook():
            changed = plistlib.loads(self.pref.read_bytes())
            changed["TypedSetting"] = 1
            self.save(changed)
        self.write_hook = hook
        code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(output["status"], "validation_failed")

    def test_shared_lock_blocks_second_invocation_despite_different_backup_root(self):
        with subject.routine_state.Session(self.home):
            code, output = self.invoke(extra=["--backup-root", str(self.tmp / "alternative-backups")])
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "overlap_skipped")
        self.assertFalse(output["preference_write_allowed"])
        self.assertEqual(output["automation_plan"]["action"], "none")
        self.assert_no_write(output)
        self.assertEqual(self.pref.read_bytes(), self.before)

    def test_interrupted_write_intent_resumes_without_new_preference_write(self):
        prior = {"run_id": "fixture-interrupted", "old_first_run_utc": subject.iso(NOW-dt.timedelta(days=13)),
                 "write_attempted": False, "requested_first_run_utc": subject.iso(NOW)}
        with subject.routine_state.Session(self.home) as session:
            session.write_intent(prior)
        code, output = self.invoke()
        self.assertEqual(output["status"], "pending_followup")
        self.assertEqual(output["previous_status"], "uncertain_previous_write")
        self.assertTrue(output["may_have_changed"])
        self.assertTrue(output["alert_required"])
        self.assertFalse(output["write_attempted_this_invocation"])
        self.assertFalse(output["preference_write_allowed"])
        self.assertEqual(self.writes(), [])

    def test_result_log_failure_persists_across_rerun(self):
        real_write = subject.secure_write
        def failing_result(path, contents):
            if path.name == "result.json":
                raise OSError("fixture disk full")
            return real_write(path, contents)
        with mock.patch.object(subject, "secure_write", side_effect=failing_result):
            first_code, first = self.invoke()
        self.assertNotEqual(first_code, 0)
        self.assertEqual(len(self.writes()), 1)
        second_code, second = self.invoke()
        self.assertNotEqual(second_code, 0)
        self.assertEqual(second["status"], "pending_followup")
        self.assertIn("result_record_error", second)
        self.assertEqual(second["automation_plan"]["action"], "pause")
        self.assertFalse(second["write_attempted_this_invocation"])
        self.assertEqual(len(self.writes()), 1)

    def test_post_write_state_save_failure_leaves_recoverable_intent(self):
        real_atomic = subject.routine_state.atomic_json
        saves = []
        def fail_after_intent(path, values):
            saves.append(path)
            if len(saves) >= 2:
                raise OSError("fixture state disk full")
            return real_atomic(path, values)
        with mock.patch.object(subject.routine_state, "atomic_json", side_effect=fail_after_intent):
            first_code, first = self.invoke()
        self.assertNotEqual(first_code, 0)
        self.assertIn("state_record_error", first)
        self.assertEqual(len(self.writes()), 1)
        second_code, second = self.invoke()
        self.assertEqual(second["status"], "pending_followup")
        self.assertEqual(second["previous_status"], "uncertain_previous_write")
        self.assertTrue(second["may_have_changed"])
        self.assertEqual(second["automation_plan"]["action"], "pause")
        self.assertEqual(len(self.writes()), 1)

    def test_not_due_run_requires_schedule_receipt_without_later_write(self):
        self.initial["FirstRunDate"] = (NOW-dt.timedelta(days=12)).replace(tzinfo=None)
        self.save(self.initial)
        first_code, first = self.invoke()
        self.assertEqual(first["status"], "not_due")
        second_code, second = self.invoke()
        self.assertEqual(second["status"], "pending_followup")
        self.assertEqual(second["previous_status"], "not_due")
        self.assertEqual(second["automation_plan"]["next_run_at_utc"], subject.iso(NOW+dt.timedelta(days=1)))
        self.assertEqual(self.writes(), [])
        receipt = self.schedule_receipt(second["automation_plan"])
        with subject.routine_state.Session(self.home) as session:
            completed = session.complete(second["run_id"], receipt)
            self.assertNotIn("pending", session.state)
        self.assertEqual(completed["status"], "followup_complete")

    def test_manual_zero_minimum_age_still_schedules_thirteen_days(self):
        self.initial["FirstRunDate"] = (NOW-dt.timedelta(days=2)).replace(tzinfo=None)
        self.save(self.initial)
        code, output = self.invoke(extra=["--minimum-age-days", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "refreshed")
        expected = subject.iso(NOW+dt.timedelta(days=13))
        self.assertEqual(output["next_due_at_utc"], expected)
        self.assertEqual(output["automation_plan"]["next_run_at_utc"], expected)

    def test_completed_success_allows_next_due_cycle(self):
        code, first = self.invoke()
        self.assertEqual(first["status"], "refreshed")
        receipt = self.schedule_receipt(first["automation_plan"])
        with subject.routine_state.Session(self.home) as session:
            completed = session.complete(first["run_id"], receipt, ui_days=14)
            self.assertNotIn("pending", session.state)
        self.assertEqual(completed["status"], "followup_complete")
        subject.utc_now.return_value = NOW+dt.timedelta(days=13)
        code, second = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(second["status"], "refreshed")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(self.writes()), 2)

    def test_failed_validation_cannot_be_hidden_by_not_due_rerun(self):
        self.write_hook = lambda: self.reg.write_bytes(b"unexpected registry change")
        first_code, first = self.invoke()
        self.assertEqual(first["status"], "validation_failed")
        self.assertNotEqual(first_code, 0)
        self.write_hook = None
        second_code, second = self.invoke()
        self.assertEqual(second["status"], "pending_followup")
        self.assertEqual(second["previous_status"], "validation_failed")
        self.assertEqual(second["automation_plan"]["action"], "pause")
        self.assertTrue(second["alert_required"])
        self.assertNotEqual(second_code, 0)
        self.assertEqual(len(self.writes()), 1)

    def test_ui_failure_is_retained_through_pause_and_rerun(self):
        code, first = self.invoke()
        receipt = self.schedule_receipt(first["automation_plan"])
        with subject.routine_state.Session(self.home) as session:
            pending = session.complete(first["run_id"], receipt, ui_days=13)
            self.assertEqual(pending["previous_status"], "ui_verification_failed")
            paused_receipt = self.schedule_receipt(pending["automation_plan"])
            completed = session.complete(first["run_id"], paused_receipt)
            self.assertEqual(completed["status"], "pause_verified")
        code, next_run = self.invoke()
        self.assertEqual(next_run["status"], "pending_followup")
        self.assertEqual(next_run["previous_status"], "ui_verification_failed")
        self.assertEqual(next_run["automation_plan"]["action"], "pause")
        self.assertEqual(len(self.writes()), 1)

    def test_schedule_mismatch_retains_ui_observation_without_rewrite(self):
        code, first = self.invoke()
        wrong = self.schedule_receipt(first["automation_plan"], status="PAUSED")
        with subject.routine_state.Session(self.home) as session:
            with self.assertRaises(ValueError):
                session.complete(first["run_id"], wrong, ui_days=14)
        code, second = self.invoke()
        self.assertEqual(second["status"], "pending_followup")
        self.assertEqual(second["previous_status"], "schedule_verification_failed")
        self.assertEqual(second["ui_observed_days"], 14)
        self.assertFalse(second["automation_plan"]["requires_ui"])
        self.assertTrue(second["alert_required"])
        self.assertEqual(len(self.writes()), 1)

    def test_app_started_while_write_intent_is_saved_prevents_write(self):
        real_intent = subject.routine_state.Session.write_intent
        def journal(session, result):
            real_intent(session, result)
            self.ps_mode = "busy"
        with mock.patch.object(subject.routine_state.Session, "write_intent", journal):
            code, output = self.invoke()
        self.assert_no_write(output)
        self.assertEqual(output["status"], "deferred_busy")

    def test_preference_changed_while_write_intent_is_saved_prevents_write(self):
        real_intent = subject.routine_state.Session.write_intent
        def journal(session, result):
            real_intent(session, result)
            self.save(dict(self.initial, UserSetting="concurrent update during fsync"))
        with mock.patch.object(subject.routine_state.Session, "write_intent", journal):
            code, output = self.invoke()
        self.assert_no_write(output)
        self.assertEqual(output["status"], "deferred_changed")

    def test_main_resuming_elapsed_success_plan_pauses_without_new_write(self):
        code, first = self.invoke()
        self.assertEqual(first["status"], "refreshed")
        subject.utc_now.return_value = NOW + dt.timedelta(days=13, seconds=1)
        code, second = self.invoke()
        self.assertEqual(second["status"], "pending_followup")
        self.assertEqual(second["automation_plan"]["action"], "pause")
        self.assertEqual(second["issue_code"], "followup_schedule_elapsed")
        self.assertTrue(second["alert_required"])
        self.assertFalse(second["write_attempted_this_invocation"])
        self.assertEqual(len(self.writes()), 1)

    def test_main_resuming_elapsed_busy_retry_bounds_new_anchor_by_deadline(self):
        self.ps_mode = "busy"
        code, first = self.invoke()
        self.assertEqual(first["status"], "deferred_busy")
        subject.utc_now.return_value = NOW + dt.timedelta(hours=23, minutes=50)
        code, second = self.invoke()
        self.assertEqual(second["status"], "pending_followup")
        self.assertEqual(second["automation_plan"]["action"], "schedule")
        self.assertEqual(second["automation_plan"]["next_run_at_utc"], subject.iso(NOW+dt.timedelta(days=1)))
        self.assertEqual(self.writes(), [])
        subject.utc_now.return_value = NOW + dt.timedelta(days=1)
        code, expired = self.invoke()
        self.assertEqual(expired["status"], "pending_followup")
        self.assertEqual(expired["automation_plan"]["action"], "pause")
        self.assertEqual(expired["issue_code"], "retry_deadline_reached")
        self.assertTrue(expired["alert_required"])
        self.assertEqual(self.writes(), [])

    def test_main_resuming_elapsed_not_due_receipt_pauses_without_write(self):
        self.initial["FirstRunDate"] = (NOW-dt.timedelta(days=12)).replace(tzinfo=None)
        self.save(self.initial)
        code, first = self.invoke()
        self.assertEqual(first["status"], "not_due")
        subject.utc_now.return_value = NOW + dt.timedelta(days=1, seconds=1)
        code, second = self.invoke()
        self.assertEqual(second["status"], "pending_followup")
        self.assertEqual(second["automation_plan"]["action"], "pause")
        self.assertEqual(second["issue_code"], "followup_schedule_elapsed")
        self.assertTrue(second["alert_required"])
        self.assertEqual(self.writes(), [])

    def test_resume_io_failure_preserves_existing_pending_evidence(self):
        prior = {"run_id": "fixture-uncertain-write", "old_first_run_utc": subject.iso(NOW-dt.timedelta(days=13)),
                 "write_attempted": False, "requested_first_run_utc": subject.iso(NOW)}
        with subject.routine_state.Session(self.home) as session:
            session.write_intent(prior)
            state_path = session.path
        original_state = state_path.read_bytes()
        with mock.patch.object(subject.routine_state.Session, "resume", side_effect=OSError("fixture readback failure")):
            code, output = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(state_path.read_bytes(), original_state)
        self.assertEqual(self.writes(), [])
        self.assertFalse(output["write_attempted"])
        self.assertEqual(output["automation_plan"]["action"], "pause")
        self.assertTrue(output["alert_required"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
