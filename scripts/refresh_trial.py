#!/usr/bin/env python3
"""Conservatively refresh one CrossOver preference; dry-run unless --apply.

This helper has only been checked against an active CrossOver 26.3 trial. It
does not establish that expired bottles will work or that future versions are
compatible. It never edits bottles, terminates processes, or starts CrossOver.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import routine_state


DOMAIN = "com.codeweavers.CrossOver"
TESTED_VERSIONS = {"26.3", "26.3.0"}
UTC = dt.timezone.utc
SUBPROCESS_TIMEOUT = 10
PREFERENCE_LIMIT = 16 * 1024 * 1024
TRIAL_DAYS = 14
REFRESH_INTERVAL_DAYS = 13


class Halt(Exception):
    def __init__(self, status, reason, exit_code=1, **details):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.exit_code = exit_code
        self.details = details


def utc_now():
    return dt.datetime.now(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def as_utc(value):
    if not isinstance(value, dt.datetime):
        raise Halt("refused", "FirstRunDate is missing or is not a plist date.", 2)
    # plistlib represents XML/binary plist UTC dates as naive datetimes.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def regular_bytes(path, maximum=PREFERENCE_LIMIT):
    """Read an ordinary file without following a final-component symlink."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            initial = os.fstat(stream.fileno())
            if not stat.S_ISREG(initial.st_mode) or initial.st_size > maximum:
                raise Halt("refused", "An input is not a supported regular file.", 2,
                           path=str(path))
            data = stream.read(maximum + 1)
            final = os.fstat(stream.fileno())
        if len(data) > maximum or file_signature(initial) != file_signature(final):
            raise Halt("deferred_changed", "An input changed while being read.", 0,
                       path=str(path))
        return data, initial
    except OSError as exc:
        raise Halt("error", "Could not safely read an input file.",
                   path=str(path), errno=exc.errno) from exc


def file_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns)


def read_preferences(path):
    raw, info = regular_bytes(path)
    try:
        values = plistlib.loads(raw)
    except Exception as exc:
        raise Halt("refused", "The preferences plist could not be parsed.", 2) from exc
    if not isinstance(values, dict):
        raise Halt("refused", "The preferences plist is not a dictionary.", 2)
    date = as_utc(values.get("FirstRunDate"))
    return {"raw": raw, "info": info, "values": values, "date": date}


def typed_equal(left, right):
    """Python equates bool/int/float; plist types must agree as well as values."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            typed_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            typed_equal(a, b) for a, b in zip(left, right))
    return left == right


def export_preferences():
    """Read the authoritative preferences API as typed plist, never print values."""
    try:
        completed = subprocess.run(
            ["/usr/bin/defaults", "export", DOMAIN, "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Halt("error", "The preferences service could not be inspected.",
                   issue_code="preference_export_failed") from exc
    if completed.returncode or len(completed.stdout) > PREFERENCE_LIMIT:
        raise Halt("error", "The preferences service returned an unusable export.",
                   issue_code="preference_export_failed")
    try:
        values = plistlib.loads(completed.stdout)
        if not isinstance(values, dict):
            raise ValueError("not a dictionary")
        as_utc(values.get("FirstRunDate"))
    except Exception as exc:
        raise Halt("refused", "The preferences service export is not a valid initialized plist.",
                   2, issue_code="invalid_preference_export") from exc
    return values


def export_representation(values):
    # defaults export emits XML, which truncates fractional plist dates.
    # Compare using that exact typed representation, while keeping disk checks exact.
    return plistlib.loads(plistlib.dumps(values, fmt=plistlib.FMT_XML))


def require_preference_service_match(original):
    if not typed_equal(export_preferences(), export_representation(original["values"])):
        raise Halt("deferred_changed", "The preferences service and saved plist disagree.",
                   0, issue_code="preference_service_mismatch")


def require_active_trial(original, result):
    deadline = original["date"] + dt.timedelta(days=TRIAL_DAYS)
    result["retry_deadline_at_utc"] = iso(deadline)
    if utc_now() >= deadline:
        raise Halt("refused", "The saved trial date has reached 14 days; expired-state behavior is unverified.",
                   2, issue_code="trial_window_elapsed")


def bottle_location_snapshot(home, app, bottles):
    """Support the observed default layout; fail closed on unverified overrides."""
    cx_root = Path(app["path"]) / "Contents" / "SharedSupport" / "CrossOver"
    configs = [cx_root / "etc" / "crossover.conf",
               home / "Library" / "Application Support" / "CrossOver" / "CrossOver.conf"]
    sections = {}
    sources = {}
    quoted = re.compile(r'^"((?:[^\\"]|\\.)*)"\s*=\s*"((?:[^\\"]|\\.)*)"(?:\s*;.*)?$')
    bare = re.compile(r'^([^=;][^=]*?)\s*=\s*(.*?)\s*$')
    for config in configs:
        if not os.path.lexists(config):
            sources[str(config)] = None
            continue
        raw, _ = regular_bytes(config)
        sources[str(config)] = hashlib.sha256(raw).hexdigest()
        try:
            lines = raw.decode("utf-8-sig").splitlines()
        except UnicodeError as exc:
            raise Halt("refused", "Bottle configuration encoding is unsupported.", 2,
                       issue_code="bottle_configuration_ambiguous", path=str(config)) from exc
        section = ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue
            header = re.fullmatch(r"\[(.*)\]\s*(?:;[^\]]*)?", line)
            if header:
                section = header[1].lower()
                continue
            if section not in {"crossover", "environmentvariables"}:
                continue
            match = quoted.fullmatch(line)
            if match:
                key, value = (part.replace('\\"', '"').replace('\\\\', '\\') for part in match.groups())
            else:
                match = bare.fullmatch(line)
                if not match:
                    raise Halt("refused", "Bottle configuration syntax could not be verified.", 2,
                               issue_code="bottle_configuration_ambiguous", path=str(config))
                key, value = match.groups()
            fields = sections.setdefault(section, {})
            if value == "<undef>":
                fields.pop(key.lower(), None)
            else:
                fields[key.lower()] = value
    environment = dict(os.environ)
    # Defaults' user domain and this helper's paths must describe the same home.
    if environment.get("HOME") and Path(environment["HOME"]).resolve() != home.resolve():
        raise Halt("refused", "The environment home differs from the account home.", 2,
                   issue_code="bottle_configuration_ambiguous")
    if environment.get("CX_ROOT") and Path(environment["CX_ROOT"]).resolve() != cx_root.resolve():
        raise Halt("refused", "An unverified CrossOver root override is configured.", 2,
                   issue_code="bottle_configuration_ambiguous")
    environment.update(HOME=str(home), CX_ROOT=str(cx_root))
    environment_fields = sections.get("environmentvariables", {})
    for key in ("home", "cx_root", "wineprefix", "cx_bottle"):
        if key in environment_fields:
            raise Halt("refused", "A bottle environment override cannot be safely resolved.", 2,
                       issue_code="bottle_configuration_ambiguous")
    for name in ("CX_BOTTLE_PATH", "CX_MANAGED_BOTTLE_PATH"):
        value = environment_fields.get(name.lower())
        if value is None:
            continue
        def substitute(match):
            if match[1] not in environment:
                raise Halt("refused", "A bottle path uses an unresolved environment variable.", 2,
                           issue_code="bottle_configuration_ambiguous")
            return environment[match[1]]
        value = re.sub(r"\$\{([^}]*)\}", substitute, value)
        if value.startswith("~/"):
            value = str(home) + value[1:]
        environment[name] = value
    private = environment.get("CX_BOTTLE_PATH") or sections.get("crossover", {}).get("bottlepath") or str(bottles)
    managed_default = Path("/Library/Application Support/CrossOver/Bottles")
    managed = environment.get("CX_MANAGED_BOTTLE_PATH") or str(managed_default)
    if private != str(bottles) or managed != str(managed_default):
        raise Halt("refused", "Custom bottle locations are outside the verified registry coverage.", 2,
                   issue_code="custom_bottle_location", custom_bottle_directory_configured=True)
    if os.path.lexists(managed_default):
        if managed_default.is_symlink() or not managed_default.is_dir() or any(managed_default.iterdir()):
            raise Halt("refused", "Managed bottles are outside the verified registry coverage.", 2,
                       issue_code="uncovered_managed_bottles", custom_bottle_directory_configured=True)
    if os.path.lexists(bottles):
        if bottles.is_symlink() or not bottles.is_dir():
            raise Halt("refused", "The default bottle root is not an ordinary directory.", 2,
                       issue_code="bottle_configuration_ambiguous")
        if any(path.is_symlink() for path in bottles.iterdir()):
            raise Halt("refused", "A linked bottle is outside the verified default layout.", 2,
                       issue_code="custom_bottle_location", custom_bottle_directory_configured=True)
    return {"configuration_sha256": sources, "private_directory": str(bottles),
            "managed_directory": str(managed_default),
            "environment_path_sources": sorted(name for name in ("CX_BOTTLE_PATH", "CX_MANAGED_BOTTLE_PATH")
                                               if os.environ.get(name) or name.lower() in environment_fields)}


def discover_app(home):
    candidates = [Path("/Applications/CrossOver.app"),
                  home / "Applications" / "CrossOver.app"]
    installed = [path for path in candidates if path.exists()]
    if not installed:
        raise Halt("refused", "CrossOver was not found in a supported location.", 2)
    # Multiple installations can make the intended version ambiguous.
    if len(installed) > 1:
        resolved = {str(path.resolve()) for path in installed}
        if len(resolved) > 1:
            raise Halt("refused", "Multiple CrossOver installations were found.", 2,
                       candidates=[str(path) for path in installed])
    app = installed[0]
    raw, _ = regular_bytes(app / "Contents" / "Info.plist")
    try:
        metadata = plistlib.loads(raw)
        bundle_id = metadata.get("CFBundleIdentifier")
        version = metadata.get("CFBundleShortVersionString")
    except Exception as exc:
        raise Halt("refused", "CrossOver bundle metadata could not be parsed.", 2) from exc
    if bundle_id != DOMAIN:
        raise Halt("refused", "The app bundle identifier is not supported.", 2)
    if version not in TESTED_VERSIONS:
        raise Halt("refused", "This CrossOver version has not been tested.", 2,
                   version=version, tested_versions=sorted(TESTED_VERSIONS))
    return {"path": str(app), "bundle_id": bundle_id, "version": version,
            "info_plist_sha256": hashlib.sha256(raw).hexdigest()}


def relevant_process(command):
    lower = command.lower()
    basename = lower.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    exact = {"wine", "wine64", "wine32", "wine32on64", "wine-preloader",
             "wine64-preloader", "wineserver", "wineloader", "cxbottle",
             "cxstart", "cxrun", "cxinstall", "cxuninstall", "cxoffice"}
    return (basename in exact or basename.endswith(".exe")
            or basename.startswith("crossover")
            or "/crossover.app/" in lower
            or "/crossover/crossover-hosted application/" in lower
            or "/crossOver/bottles/".lower() in lower)


def inspect_processes(uid):
    """Inspect command names only, never user command-line arguments."""
    try:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "uid=,pid=,comm="],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=SUBPROCESS_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise Halt("error", "Process inspection failed; no write is permitted.") from exc
    if completed.returncode != 0:
        raise Halt("error", "Process inspection failed; no write is permitted.",
                   process_inspection_exit_code=completed.returncode)
    matches = []
    seen_self = False
    rows = 0
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(.+?)\s*", line)
        if match is None:
            raise Halt("error", "Process inspection returned an unexpected format.")
        process_uid, pid = int(match[1]), int(match[2])
        command = match[3]
        rows += 1
        if process_uid == uid and pid == os.getpid():
            seen_self = True
        if process_uid == uid and relevant_process(command):
            matches.append({"pid": pid, "command": command})
    if not rows or not seen_self:
        raise Halt("error", "Process inspection could not verify the current user.")
    return sorted(matches, key=lambda item: item["pid"])


def require_idle(uid):
    matches = inspect_processes(uid)
    if matches:
        raise Halt("deferred_busy", "CrossOver or Windows/Wine processes are active.",
                   0, active_processes=matches)


def registry_snapshot(directory):
    """Hash only system.reg in default bottle directories, without parsing it."""
    snapshots = {}
    if not directory.exists():
        return snapshots
    try:
        bottles = sorted(directory.iterdir(), key=lambda path: path.name)
        for bottle in bottles:
            if not bottle.is_dir():
                continue
            registry = bottle / "system.reg"
            if not registry.exists():
                continue
            fd = os.open(str(registry), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                initial = os.fstat(stream.fileno())
                if not stat.S_ISREG(initial.st_mode):
                    raise Halt("refused", "A bottle registry is not a regular file.", 2,
                               path=str(registry))
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                final = os.fstat(stream.fileno())
            if file_signature(initial) != file_signature(final):
                raise Halt("deferred_changed", "A bottle registry changed while read.",
                           0, path=str(registry))
            snapshots[str(registry)] = digest.hexdigest()
    except OSError as exc:
        raise Halt("error", "Bottle registry inspection failed; no write is permitted.",
                   errno=exc.errno) from exc
    return snapshots


def check_preferences_stable(path, original):
    current = read_preferences(path)
    if (current["raw"] != original["raw"]
            or file_signature(current["info"]) != file_signature(original["info"])):
        raise Halt("deferred_changed", "Preferences changed before the write.", 0)


def secure_write(path, contents):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def create_backup(root, original, result):
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise Halt("refused", "The backup root must be a real directory.", 2)
        run = Path(tempfile.mkdtemp(prefix=utc_now().strftime("%Y%m%dT%H%M%SZ-"),
                                    dir=str(root)))
        result["backup_directory"] = str(run)
        pref_backup = run / "preferences-before.plist"
        secure_write(pref_backup, original["raw"])
        result["preference_backup"] = str(pref_backup)
        metadata = {
            "created_at_utc": iso(utc_now()), "app": result["app"],
            "preference_path": result["preference_path"],
            "old_first_run_utc": iso(original["date"]),
            "original_preference_sha256": hashlib.sha256(original["raw"]).hexdigest(),
            "original_mode": oct(stat.S_IMODE(original["info"].st_mode)),
            "original_owner_uid": original["info"].st_uid,
            "default_registry_sha256": result["default_registry_sha256"],
            "minimum_age_days": result["minimum_age_days"],
            "validation_limitations": result["validation_limitations"],
        }
        metadata_path = run / "metadata.json"
        secure_write(metadata_path, (json.dumps(metadata, indent=2) + "\n").encode())
        result["backup_metadata"] = str(metadata_path)
    except OSError as exc:
        raise Halt("error", "The required backup could not be completed.",
                   errno=exc.errno) from exc


def preference_changes(before, after):
    return {
        "changed_keys": sorted(key for key in before.keys() & after.keys()
                               if not typed_equal(before[key], after[key])),
        "added_keys": sorted(after.keys() - before.keys()),
        "removed_keys": sorted(before.keys() - after.keys()),
    }


def validate_after(path, original, expected, bottles, registry_before, result):
    deadline = time.monotonic() + 5
    # defaults/cfprefsd may take a short time to flush the on-disk plist.
    while True:
        current = read_preferences(path)
        service = export_preferences()
        changes = preference_changes(original["values"], current["values"])
        service_changes = preference_changes(export_representation(original["values"]), service)
        result["preference_validation"] = changes
        result["preference_service_validation"] = service_changes
        if (service_changes["added_keys"] or service_changes["removed_keys"]
                or set(service_changes["changed_keys"]) - {"FirstRunDate"}):
            raise Halt("validation_failed", "Unrelated service preferences changed after the write.",
                       issue_code="unrelated_preferences_changed")
        if (changes["added_keys"] or changes["removed_keys"]
                or set(changes["changed_keys"]) - {"FirstRunDate"}):
            raise Halt("validation_failed", "Unrelated preferences changed after the write.")
        if (current["date"] == expected and changes["changed_keys"] == ["FirstRunDate"]
                and typed_equal(service, export_representation(current["values"]))):
            result["preference_service_matches_disk"] = True
            break
        if time.monotonic() >= deadline:
            raise Halt("validation_failed", "The requested FirstRunDate was not verified.")
        time.sleep(0.1)
    result["observed_first_run_utc"] = iso(current["date"])
    registry_after = registry_snapshot(bottles)
    result["default_registries_unchanged"] = registry_after == registry_before
    if registry_after != registry_before:
        raise Halt("validation_failed", "Default bottle registry hashes changed after the write.",
                   changed_registry_paths=sorted(path for path in
                       registry_before.keys() | registry_after.keys()
                       if registry_before.get(path) != registry_after.get(path)))


def nonnegative_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def ordinary_home():
    uid = os.getuid()
    if uid == 0 or os.geteuid() == 0 or uid != os.geteuid():
        raise Halt("refused", "Run as the ordinary user, without sudo or setuid.", 2)
    if sys.platform != "darwin":
        raise Halt("refused", "This helper supports macOS only.", 2)
    home = Path(pwd.getpwuid(uid).pw_dir)
    if not home.is_absolute():
        raise Halt("refused", "The real user home could not be determined.", 2)
    return home


def run(args, result):
    home = ordinary_home()
    uid = os.getuid()
    preference_path = home / "Library" / "Preferences" / (DOMAIN + ".plist")
    bottles = home / "Library" / "Application Support" / "CrossOver" / "Bottles"
    result["app"] = discover_app(home)
    result["preference_path"] = str(preference_path)
    original = read_preferences(preference_path)
    if original["info"].st_uid != uid:
        raise Halt("refused", "The preferences file is not owned by this user.", 2)
    age = (utc_now() - original["date"]).total_seconds() / 86400
    result["old_first_run_utc"] = iso(original["date"])
    result["retry_deadline_at_utc"] = iso(original["date"] + dt.timedelta(days=TRIAL_DAYS))
    result["next_due_at_utc"] = iso(original["date"] + dt.timedelta(days=REFRESH_INTERVAL_DAYS))
    result["age_days"] = round(age, 6)
    if age < 0:
        raise Halt("refused", "FirstRunDate is in the future; no write is permitted.", 2)
    require_active_trial(original, result)
    if age < args.minimum_age_days:
        result.update(status="not_due", reason="FirstRunDate is younger than the minimum age.")
        return
    require_idle(uid)
    location_before = bottle_location_snapshot(home, result["app"], bottles)
    result["bottle_location_validation"] = location_before
    result["custom_bottle_directory_configured"] = False
    require_preference_service_match(original)
    registry_before = registry_snapshot(bottles)
    result["default_registry_sha256"] = registry_before
    result["default_bottle_directory"] = str(bottles)
    check_preferences_stable(preference_path, original)
    if not args.apply:
        result.update(status="would_refresh", reason="Preflight passed; dry-run made no changes.")
        return
    if args.backup_root is None:
        backup_root = home / "Documents" / "Codex" / "CrossOver-trial-backups"
    elif args.backup_root == "~":
        backup_root = home
    elif args.backup_root.startswith("~/"):
        backup_root = home / args.backup_root[2:]
    else:
        backup_root = Path(args.backup_root).absolute()
    require_active_trial(original, result)
    create_backup(backup_root, original, result)
    if registry_snapshot(bottles) != registry_before:
        raise Halt("deferred_changed", "Default bottle registries changed before the write.", 0)
    if discover_app(home) != result["app"]:
        raise Halt("deferred_changed", "The CrossOver app changed before the write.", 0)
    if bottle_location_snapshot(home, result["app"], bottles) != location_before:
        raise Halt("deferred_changed", "Bottle configuration changed before the write.", 0,
                   issue_code="bottle_configuration_changed")
    target = utc_now().replace(microsecond=0)
    if target == original["date"]:
        result.update(status="not_due", reason="FirstRunDate already equals the current UTC second.")
        return
    result["requested_first_run_utc"] = iso(target)
    # A durable intent survives termination between the preference write and its
    # verification. The next invocation must finish/review that run, not write again.
    args.journal(result)
    # Journal I/O can take time. Recheck inputs and running programs afterward.
    require_preference_service_match(original)
    check_preferences_stable(preference_path, original)
    require_idle(uid)
    require_active_trial(original, result)
    result["write_attempted"] = True
    try:
        completed = subprocess.run(
            ["/usr/bin/defaults", "write", DOMAIN, "FirstRunDate", "-date",
             target.strftime("%Y-%m-%d %H:%M:%S +0000")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Halt("validation_failed", "The preference write did not complete reliably; inspect the backup.") from exc
    if completed.returncode:
        raise Halt("validation_failed", "The preference write returned an error; inspect the backup.",
                   preference_write_exit_code=completed.returncode)
    validate_after(preference_path, original, target, bottles, registry_before, result)
    result["retry_deadline_at_utc"] = iso(target + dt.timedelta(days=TRIAL_DAYS))
    result["next_due_at_utc"] = iso(target + dt.timedelta(days=REFRESH_INTERVAL_DAYS))
    result.update(status="refreshed", reason="Only FirstRunDate changed; default registry hashes are unchanged.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Perform the single preference write after all checks.")
    parser.add_argument("--minimum-age-days", type=nonnegative_int, default=13,
                        help="Minimum full age in days (default: 13); 0 is an explicit manual override.")
    parser.add_argument("--backup-root", help="Backup directory (default: real user's Documents/Codex/CrossOver-trial-backups).")
    args = parser.parse_args(argv)
    result = {
        "checked_at_utc": iso(utc_now()), "mode": "apply" if args.apply else "dry_run",
        "minimum_age_days": args.minimum_age_days, "write_attempted": False,
        "validation_limitations": [
            "Tested with an active 26.3 trial; expired-bottle recovery is unverified.",
            "No Windows application or CrossOver UI is launched by this helper.",
            "Registry hash coverage is limited to default user bottles; detected custom/managed layouts are refused.",
            "Concurrent app launches between process inspection and writing cannot be excluded.",
        ],
    }
    exit_code = 0
    session = None
    configured_session = False
    resumed = False
    pending_existed = False
    try:
        if args.apply:
            session = routine_state.Session(ordinary_home())
            session.__enter__()
            session.require_configuration()
            configured_session = True
            pending_existed = bool(session.state.get("pending"))
            previous = session.resume()
            if previous:
                result.update(previous, write_attempted_this_invocation=False)
                resumed = True
                exit_code = result.get("exit_code", 0)
            else:
                result["run_id"] = uuid.uuid4().hex
                args.journal = session.write_intent
        if not resumed:
            run(args, result)
    except routine_state.Overlap:
        result.update(status="overlap_skipped", reason="Another invocation holds the user-wide lock.",
                      alert_required=False, preference_write_allowed=False,
                      automation_plan={"action": "none", "requires_ui": False})
        session = None
    except routine_state.ConfigurationError as exc:
        result.update(status="setup_required", reason=str(exc), alert_required=True,
                      preference_write_allowed=False,
                      automation_plan={"action": "none", "requires_ui": False})
        exit_code = 2
    except Halt as exc:
        result.update(status=exc.status, reason=exc.reason, **exc.details)
        exit_code = exc.exit_code
        if result["write_attempted"]:
            result["status"] = "validation_failed"
            result["rollback_performed"] = False
            result["may_have_changed"] = True
            exit_code = 1
    except Exception as exc:
        # Do not include exception text, which may contain unrelated plist data.
        result.update(status="validation_failed" if result["write_attempted"] else "error",
                      reason="Unexpected failure; no automatic rollback was attempted.",
                      error_type=type(exc).__name__, rollback_performed=False)
        if result["write_attempted"]:
            result["may_have_changed"] = True
        exit_code = 1
    result["exit_code"] = exit_code
    if result.get("backup_directory") and not resumed:
        result_path = Path(result["backup_directory"]) / "result.json"
        result["result_record"] = str(result_path)
        try:
            secure_write(result_path, (json.dumps(result, indent=2, sort_keys=True) + "\n").encode())
        except OSError:
            result.pop("result_record", None)
            result["result_record_error"] = "Could not save the final result; retain this output and the existing backup."
            exit_code = 1
            result["exit_code"] = exit_code
    if (configured_session and session is not None and session.fd is not None
            and not resumed and not pending_existed):
        try:
            session.record(result, utc_now())
        except Exception as exc:
            result.update(state_record_error="Could not save routine state; retain the backup and this output.",
                          state_error_type=type(exc).__name__, alert_required=True,
                          automation_plan={"action": "pause", "requires_ui": False}, exit_code=1)
            exit_code = 1
    elif args.apply and not resumed and result.get("status") not in {"overlap_skipped", "setup_required"}:
        # A failure to acquire or read state must never look like a healthy skip.
        result.update(alert_required=True,
                      automation_plan={"action": "pause", "requires_ui": False})
    if session is not None:
        session.__exit__()
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
