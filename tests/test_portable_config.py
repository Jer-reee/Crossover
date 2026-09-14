"""Portable binding tests; all state is temporary and no CrossOver process is used."""

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refresh_trial as helper
import routine_state as state


CONFIG = {"automation_id": "test-automation", "thread_id": "test-thread"}
NOW = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)


class PortableConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def configured_session(self):
        session = state.Session(self.home)
        session.__enter__()
        self.addCleanup(session.__exit__)
        session.configure(**CONFIG)
        return session

    def automation_file(self, plan, config=None):
        config = CONFIG if config is None else config
        path = self.home / "automation.toml"
        values = {"id": config["automation_id"], "kind": "heartbeat",
                  "target_thread_id": config["thread_id"],
                  "status": "ACTIVE" if plan["action"] == "schedule" else "PAUSED"}
        if plan["action"] == "schedule":
            values["rrule"] = plan["rrule"]
        path.write_text("\n".join(key + " = " + json.dumps(value)
                                   for key, value in values.items()))
        return path

    def test_unconfigured_apply_stops_before_preflight_without_pending_work(self):
        output = io.StringIO()
        with mock.patch.object(helper, "ordinary_home", return_value=self.home), \
                mock.patch.object(helper, "run") as run, \
                mock.patch.object(helper.subprocess, "run") as process, \
                contextlib.redirect_stdout(output):
            code = helper.main(["--apply"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "setup_required")
        self.assertIn("configure --automation-id", result["reason"])
        self.assertFalse(result["write_attempted"])
        self.assertFalse(result["preference_write_allowed"])
        run.assert_not_called()
        process.assert_not_called()
        self.assertNotIn("pending", state.read_json(state.Session(self.home).path))

    def test_dry_run_needs_no_binding_and_creates_no_private_state(self):
        output = io.StringIO()
        with mock.patch.object(helper, "run", side_effect=lambda args, result:
                               result.update(status="not_due")) as run, \
                mock.patch.object(helper, "ordinary_home", return_value=self.home), \
                contextlib.redirect_stdout(output):
            code = helper.main([])
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertFalse(state.Session(self.home).root.exists())

    def test_configuration_persists_and_identical_binding_is_idempotent(self):
        with state.Session(self.home) as session:
            self.assertEqual(session.configure(**CONFIG)["status"], "configured")
            original = session.path.read_bytes()
            self.assertEqual(session.configure(**CONFIG)["status"], "already_configured")
            self.assertEqual(session.path.read_bytes(), original)
        with state.Session(self.home) as session:
            self.assertEqual(session.require_configuration(), CONFIG)

    def test_retargeting_requires_explicit_replace(self):
        session = self.configured_session()
        with self.assertRaisesRegex(state.ConfigurationError, "--replace"):
            session.configure("other-automation", "other-thread")
        self.assertEqual(session.require_configuration(), CONFIG)
        session.configure("other-automation", "other-thread", replace=True)
        self.assertEqual(session.require_configuration()["thread_id"], "other-thread")

    def test_unresolved_run_prevents_retargeting_even_with_replace(self):
        session = self.configured_session()
        session.state["pending"] = {"run_id": "unfinished"}
        session.save()
        with self.assertRaisesRegex(state.ConfigurationError, "unresolved"):
            session.configure("other-automation", "other-thread", replace=True)
        self.assertEqual(session.require_configuration(), CONFIG)

    def test_identical_binding_is_allowed_with_pending_work(self):
        session = self.configured_session()
        session.state["pending"] = {"run_id": "unfinished"}
        self.assertEqual(session.configure(**CONFIG)["status"], "already_configured")
        self.assertEqual(session.state["pending"]["run_id"], "unfinished")

    def test_unbound_pending_run_cannot_be_silently_adopted(self):
        with state.Session(self.home) as session:
            session.state["pending"] = {"run_id": "unknown-target"}
            with self.assertRaisesRegex(state.ConfigurationError, "unresolved"):
                session.configure(**CONFIG)

    def test_invalid_identifiers_are_rejected_without_saving(self):
        with state.Session(self.home) as session:
            for value in (None, "", "../different", "/absolute", "a/b", " a", "a\nb", "x" * 201):
                with self.subTest(value=value):
                    with self.assertRaises(state.ConfigurationError):
                        session.configure(value, CONFIG["thread_id"])
                    with self.assertRaises(state.ConfigurationError):
                        session.configure(CONFIG["automation_id"], value)
            self.assertNotIn("configuration", session.state)

    def test_custom_codex_home_is_used(self):
        custom = self.home / "custom-codex"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom)}):
            actual = state.automation_path(self.home, CONFIG)
        self.assertEqual(actual, custom / "automations/test-automation/automation.toml")

    def test_empty_and_unset_codex_home_use_real_user_home(self):
        for values in ({}, {"CODEX_HOME": ""}, {"CODEX_HOME": "  "}):
            with self.subTest(values=values), mock.patch.dict(os.environ, values, clear=True):
                self.assertEqual(state.automation_path(self.home, CONFIG),
                                 self.home / ".codex/automations/test-automation/automation.toml")

    def test_receipt_verifier_uses_configured_identity(self):
        plan = state.schedule_at(NOW + dt.timedelta(days=1))
        path = self.automation_file(plan)
        state.verify_schedule(path, plan, CONFIG)
        for other in ({"automation_id": "other", "thread_id": CONFIG["thread_id"]},
                      {"automation_id": CONFIG["automation_id"], "thread_id": "other"}):
            with self.subTest(other=other):
                with self.assertRaisesRegex(ValueError, "identity"):
                    state.verify_schedule(path, plan, other)

    def test_completion_uses_binding_for_schedule_readback(self):
        session = self.configured_session()
        session.record({"status": "not_due", "old_first_run_utc": state.timestamp(NOW),
                        "exit_code": 0}, NOW)
        pending = session.state["pending"]
        path = self.automation_file(pending["plan"])
        result = session.complete(pending["run_id"], path, now=NOW)
        self.assertEqual(result["status"], "followup_complete")
        self.assertEqual(session.state["configuration"], CONFIG)

    def test_write_intent_cannot_be_created_without_configuration(self):
        with state.Session(self.home) as session:
            with self.assertRaises(state.ConfigurationError):
                session.write_intent({"run_id": "example"})
            self.assertNotIn("pending", session.state)

    def test_configuration_does_not_change_a_durable_write_intent(self):
        session = self.configured_session()
        session.write_intent({"run_id": "example", "write_attempted": False})
        before = session.path.read_bytes()
        with self.assertRaises(state.ConfigurationError):
            session.configure("other-automation", "other-thread", replace=True)
        self.assertEqual(session.path.read_bytes(), before)
        resumed = session.resume(now=NOW)
        self.assertEqual(resumed["previous_status"], "uncertain_previous_write")
        self.assertFalse(resumed["preference_write_allowed"])


if __name__ == "__main__":
    unittest.main()
