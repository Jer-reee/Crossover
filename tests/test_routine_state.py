"""Scheduler and durable-state tests; all state lives in temporary directories."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

TARGET = Path(__file__).resolve().parents[1] / 'scripts' / 'routine_state.py'
BINDING = {'automation_id': 'test-automation', 'thread_id': 'test-thread'}
spec = importlib.util.spec_from_file_location('routine_under_test', TARGET)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = dt.datetime(2026, 9, 25, 11, 56, 18, tzinfo=dt.timezone.utc)


def busy(deadline=NOW + dt.timedelta(days=1), **updates):
    result = dict(status='deferred_busy', reason='Applications are running',
                  old_first_run_utc=m.timestamp(NOW-dt.timedelta(days=13)),
                  retry_deadline_at_utc=m.timestamp(deadline), exit_code=0,
                  active_processes=[dict(pid=111, command='/Applications/CrossOver.app/CrossOver')])
    result.update(updates)
    return result


def refreshed(**updates):
    result = dict(status='refreshed', observed_first_run_utc=m.timestamp(NOW),
                  exit_code=0, reason='Only the intended value changed')
    result.update(updates)
    return result


def config(path, plan, **updates):
    values = dict(id=BINDING['automation_id'], kind='heartbeat', status='PAUSED' if plan['action']=='pause' else 'ACTIVE',
                  rrule=plan.get('rrule', 'RRULE:FREQ=DAILY;INTERVAL=13'), target_thread_id=BINDING['thread_id'])
    values.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(k+' = '+json.dumps(v) for k,v in values.items())+'\n')


class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.clock_patch=patch.object(m,'utc_now',return_value=NOW)
        self.clock_patch.start()
        self.temp = tempfile.TemporaryDirectory(prefix='crossover-schedule-test-')
        self.home = Path(self.temp.name)
        self.config = self.home / '.codex/automations' / BINDING['automation_id'] / 'automation.toml'
        with m.Session(self.home) as session:
            session.configure(**BINDING)

    def tearDown(self):
        self.clock_patch.stop()
        self.temp.cleanup()

    def test_utc_rrule_exact(self):
        p = m.plan_result(refreshed(), NOW)
        self.assertEqual(p['rrule'], 'DTSTART:20261008T115618Z\nRRULE:FREQ=DAILY;INTERVAL=13')

    def test_schedule_at_converts_offset_before_formatting(self):
        dubai = NOW.astimezone(dt.timezone(dt.timedelta(hours=4)))
        self.assertEqual(m.schedule_at(dubai)['rrule'], m.schedule_at(NOW)['rrule'])

    def test_retry_is_capped_at_deadline(self):
        deadline=NOW+dt.timedelta(minutes=10)
        self.assertEqual(m.plan_result(busy(deadline), NOW)['next_run_at_utc'], m.timestamp(deadline))

    def test_retry_cutoff_pauses_at_exact_deadline(self):
        self.assertEqual(m.plan_result(busy(NOW), NOW)['action'], 'pause')

    def test_retry_without_deadline_pauses(self):
        value=busy(); value.pop('retry_deadline_at_utc')
        self.assertEqual(m.plan_result(value, NOW)['action'], 'pause')

    def test_partial_failure_pauses_and_alerts(self):
        for key,value in [('exit_code',1),('result_record_error','failed'),('state_record_error','failed'),('may_have_changed',True)]:
            with self.subTest(key=key):
                r=refreshed(**{key:value})
                self.assertEqual(m.plan_result(r,NOW)['action'],'pause')
                self.assertIsNotNone(m.issue_fingerprint(r))

    def test_process_ids_do_not_change_issue_identity(self):
        r=busy(); other=busy(); other['active_processes'][0]['pid']=987
        self.assertEqual(m.issue_fingerprint(r),m.issue_fingerprint(other))

    def test_lock_rejects_overlap(self):
        with m.Session(self.home):
            with self.assertRaises(m.Overlap):
                with m.Session(self.home): pass

    def test_write_intent_survives_new_session(self):
        with m.Session(self.home) as s:
            r=refreshed(run_id='r1'); s.write_intent(r)
        with m.Session(self.home) as s:
            r=s.resume()
            self.assertEqual(r['automation_plan']['action'],'pause')
            self.assertTrue(r['may_have_changed'])
            self.assertFalse(r['preference_write_allowed'])
            self.assertTrue(r['alert_required'])

    def test_ui_failure_is_durable_and_requires_pause(self):
        with m.Session(self.home) as s:
            r=refreshed(); s.record(r,NOW)
            out=s.complete(r['run_id'], self.config, ui_days=13)
            self.assertTrue(out['alert_required'])
            self.assertEqual(out['automation_plan']['action'],'pause')
        with m.Session(self.home) as s:
            self.assertEqual(s.resume()['previous_status'],'ui_verification_failed')

    def test_schedule_mismatch_survives_as_issue(self):
        with m.Session(self.home) as s:
            r=refreshed(); s.record(r,NOW)
            config(self.config,r['automation_plan'],status='PAUSED')
            with self.assertRaises(ValueError):
                s.complete(r['run_id'],self.config,ui_days=14)
        with m.Session(self.home) as s:
            out=s.resume()
            self.assertTrue(out['alert_required'])
            self.assertIsNotNone(out['issue_fingerprint'])

    def test_complete_validates_identity_target_muting_and_recurrence(self):
        p=m.schedule_at(NOW)
        for updates in [dict(id='different'),dict(target_thread_id='different'),dict(kind='cron'),dict(status='PAUSED'),dict(rrule='wrong'),dict(notification_policy='failed_runs_only')]:
            with self.subTest(updates=updates):
                config(self.config,p,**updates)
                with self.assertRaises(ValueError):m.verify_schedule(self.config,p,BINDING)

    def test_successful_complete_does_not_leave_pending(self):
        with m.Session(self.home) as s:
            r=refreshed(); s.record(r,NOW); config(self.config,r['automation_plan'])
            result=s.complete(r['run_id'],self.config,ui_days=14)
            self.assertEqual(result['status'],'followup_complete')
            self.assertIsNone(s.resume())

    def test_known_issue_can_be_acknowledged_after_record(self):
        with m.Session(self.home) as s:
            r=busy(); s.record(r,NOW)
            issue=r['issue_fingerprint']
        with patch.object(m.pwd,'getpwuid',return_value=types.SimpleNamespace(pw_dir=str(self.home))), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['ack-report','--issue-id',issue]),0)
        with m.Session(self.home) as s:
            self.assertFalse(s.resume()['alert_required'])

    def test_unknown_issue_cannot_be_acknowledged(self):
        with patch.object(m.pwd,'getpwuid',return_value=types.SimpleNamespace(pw_dir=str(self.home))), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['ack-report','--issue-id','missing']),1)

    def test_recovered_then_recurring_issue_alerts_again(self):
        with m.Session(self.home) as s:
            first=busy();s.record(first,NOW)
            s.state['last_reported_issue']=first['issue_fingerprint'];s.save()
            config(self.config,first['automation_plan']);s.complete(first['run_id'],self.config)
            good=refreshed();s.record(good,NOW)
            config(self.config,good['automation_plan']);s.complete(good['run_id'],self.config,ui_days=14)
            recurring=busy(deadline=NOW+dt.timedelta(days=14), old_first_run_utc=m.timestamp(NOW))
            s.record(recurring,NOW+dt.timedelta(days=13))
            self.assertEqual(recurring['automation_plan']['action'],'schedule')
            self.assertTrue(recurring['alert_required'])

    def test_cutoff_worsening_realerts_acknowledged_busy_issue(self):
        with m.Session(self.home) as s:
            first=busy();s.record(first,NOW)
            s.state['last_reported_issue']=first['issue_fingerprint'];s.save()
            config(self.config,first['automation_plan']);s.complete(first['run_id'],self.config)
            overdue=busy();s.record(overdue,NOW+dt.timedelta(days=1))
            self.assertEqual(overdue['automation_plan']['action'],'pause')
            self.assertTrue(overdue['alert_required'])

    def test_ui_receipt_survives_schedule_retry_without_reopening_ui(self):
        with m.Session(self.home) as s:
            r=refreshed();s.record(r,NOW)
            config(self.config,r['automation_plan'],status='PAUSED')
            with self.assertRaises(ValueError):s.complete(r['run_id'],self.config,ui_days=14)
        with m.Session(self.home) as s:
            resumed=s.resume()
            self.assertEqual(resumed['ui_observed_days'],14)
            self.assertFalse(resumed['automation_plan']['requires_ui'])
            self.assertTrue(s.state['pending']['ui_verified_at_utc'])
            config(self.config,resumed['automation_plan'])
            completed=s.complete(r['run_id'],self.config)
            self.assertEqual(completed['status'],'followup_complete')
            self.assertNotIn('pending',s.state)
            self.assertEqual(s.state['last_completed']['result']['ui_observed_days'],14)

    def test_read_json_rejects_existing_and_dangling_symlinks(self):
        for existing in [False,True]:
            with self.subTest(existing=existing):
                target=self.home/('target-'+str(existing))
                if existing:target.write_text('{"schema_version":1}')
                link=self.home/('link-'+str(existing));link.symlink_to(target)
                with self.assertRaises(OSError):m.read_json(link)

    def test_read_json_allows_missing_regular_path(self):
        self.assertEqual(m.read_json(self.home/'missing'),{'schema_version':1})

    def test_schedule_rejects_naive_timestamp(self):
        with self.assertRaises(ValueError):m.schedule_at(NOW.replace(tzinfo=None))

    def test_contradictory_ui_receipt_rejected_without_clearing_pending(self):
        with m.Session(self.home) as s:
            r=refreshed();s.record(r,NOW)
            config(self.config,r['automation_plan'])
            with self.assertRaises(ValueError):s.complete(r['run_id'],self.config,ui_days=14,ui_unavailable=True)
            self.assertTrue(s.state['pending']['plan']['requires_ui'])

    def test_pending_busy_past_retry_reanchors_from_current_time(self):
        with m.Session(self.home) as s:
            r=busy();s.record(r,NOW)
        later=NOW+dt.timedelta(hours=2)
        with m.Session(self.home) as s:
            out=s.resume(later)
            self.assertEqual(out['automation_plan']['next_run_at_utc'],m.timestamp(later+dt.timedelta(hours=1)))
            self.assertEqual(out['previous_status'],'deferred_busy')
            self.assertFalse(out['preference_write_allowed'])

    def test_pending_changed_retry_caps_at_saved_deadline(self):
        with m.Session(self.home) as s:
            r=busy(status='deferred_changed');s.record(r,NOW)
            later=NOW+dt.timedelta(hours=23,minutes=50)
            out=s.resume(later)
            self.assertEqual(out['automation_plan']['next_run_at_utc'],r['retry_deadline_at_utc'])

    def test_pending_busy_at_deadline_pauses_and_realerts(self):
        with m.Session(self.home) as s:
            r=busy();s.record(r,NOW)
            s.state['last_reported_issue']=r['issue_fingerprint'];s.save()
            out=s.resume(NOW+dt.timedelta(days=1))
            self.assertEqual(out['automation_plan']['action'],'pause')
            self.assertEqual(out['issue_code'],'retry_deadline_reached')
            self.assertTrue(out['alert_required'])
            issue=out['issue_fingerprint']
        with m.Session(self.home) as s:
            self.assertEqual(s.resume(NOW+dt.timedelta(days=2))['issue_fingerprint'],issue)

    def test_elapsed_success_pauses_and_preserves_helper_evidence(self):
        with m.Session(self.home) as s:
            r=refreshed(preference_backup='/fake/preferences-before.plist');s.record(r,NOW)
            out=s.resume(NOW+dt.timedelta(days=13))
            self.assertEqual(out['automation_plan']['action'],'pause')
            self.assertEqual(out['issue_code'],'followup_schedule_elapsed')
            self.assertEqual(out['helper_status'],'refreshed')
            self.assertEqual(out['observed_first_run_utc'],r['observed_first_run_utc'])
            self.assertEqual(out['preference_backup'],r['preference_backup'])
            self.assertEqual(s.state['pending']['result']['status'],'refreshed')

    def test_future_pending_plan_is_not_rewritten(self):
        with m.Session(self.home) as s:
            r=refreshed();s.record(r,NOW)
            original=s.path.read_bytes()
            out=s.resume(NOW+dt.timedelta(days=1))
            self.assertEqual(s.path.read_bytes(),original)
            self.assertEqual(out['automation_plan'],r['automation_plan'])

    def test_complete_refuses_matching_but_elapsed_active_schedule(self):
        with m.Session(self.home) as s:
            r=refreshed();s.record(r,NOW);config(self.config,r['automation_plan'])
            out=s.complete(r['run_id'],self.config,ui_days=14,now=NOW+dt.timedelta(days=13))
            self.assertEqual(out['status'],'pending_followup')
            self.assertEqual(out['issue_code'],'followup_schedule_elapsed')
            self.assertEqual(out['automation_plan']['action'],'pause')
            self.assertNotIn('schedule_readback_verified',s.state['pending'])

    def test_complete_detects_schedule_elapsed_during_readback(self):
        with m.Session(self.home) as s:
            r=refreshed();s.record(r,NOW);config(self.config,r['automation_plan'])
            before=NOW+dt.timedelta(days=13)-dt.timedelta(seconds=1)
            after=NOW+dt.timedelta(days=13)
            with patch.object(m,'utc_now',side_effect=[before,before,before,after,after]):
                out=s.complete(r['run_id'],self.config,ui_days=14)
            self.assertEqual(out['automation_plan']['action'],'pause')
            self.assertEqual(out['ui_observed_days'],14)
            self.assertNotIn('schedule_readback_verified',s.state['pending'])


if __name__=='__main__':unittest.main(verbosity=2)
