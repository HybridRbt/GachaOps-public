"""Explicitly authorized single-node trial. Never used by simulate/probe/prepare.

No retries or fallback. An unowned game/runner is recorded and left untouched;
uncertain ownership keeps the durable journal quarantined for manual inspection.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .ok_adapter import prepare, runner_environment, verify_manifest
from .ok_completion import LogCursor, capture_cursor, observe_root
from .recovery import ExecutionSession, RecoveryRequired, save
from .windows_observation import find_matching_processes, owned_processes, verify_observed_membership
from .windows_process import WindowsProcess, CleanupError
from .foreground import request_foreground, allow_foreground
from .preflight import runtime_desktop_report


def load_approved_descriptor(path, approved_sha256):
    content = Path(path).read_bytes()
    if not approved_sha256 or hashlib.sha256(content).hexdigest() != approved_sha256:
        raise ValueError("explicit approval of this exact prepared descriptor is required")
    descriptor = json.loads(content.decode("utf-8-sig"))
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != 1 or descriptor.get("mode") != "prepare_only":
        raise ValueError("expected prepare-ok descriptor")
    for key in ("installation_root", "preset_sha256", "cwd"):
        if not isinstance(descriptor.get(key), str) or not descriptor[key]:
            raise ValueError(f"descriptor missing {key}")
    if not isinstance(descriptor.get("installation_manifest"), dict) or not isinstance(descriptor.get("argv_candidate"), list):
        raise ValueError("descriptor missing manifest or candidate command")
    return descriptor


def execute_trial(descriptor_path, approved_sha256, output, state, timeout=1800):
    descriptor = load_approved_descriptor(descriptor_path, approved_sha256)
    if os.name != "nt":
        raise OSError("Windows required for real trial")
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
        raise ValueError("trial timeout must be positive and at most 3600 seconds")
    # The locked region covers drift, process checks, launch, observation and cleanup.
    with ExecutionSession(state, "authorized_ok_ww_single_trial") as session:
        try:
            root = descriptor["installation_root"]
            drift = verify_manifest(root, descriptor["installation_manifest"])
            current = prepare(root)
            if (drift["status"] != "unchanged" or current["preset_sha256"] != descriptor["preset_sha256"]
                    or current["argv_candidate"] != descriptor["argv_candidate"] or current["cwd"] != descriptor["cwd"]):
                raise ValueError("installation or preset changed; prepare and approve again")
            blockers = [b for b in current["blockers"] if b not in {
                "daily_preset_not_authorized", "real_entry_not_verified", "game_job_inheritance_not_verified"}]
            if blockers:
                raise ValueError("trial blocked: " + ", ".join(blockers))
            directory = Path(output).resolve() / ("ok-trial-" + uuid.uuid4().hex)
            directory.mkdir(parents=True)
            log_path = Path(current["cwd"]) / "logs/ok-script.log"
            cursor = capture_cursor(log_path)
        except BaseException:
            session.finish("prelaunch_blocked", cleanup_verified=True)  # No process created.
            raise
        return run_prepared(current, session, directory, cursor, timeout,
                            metadata={"approved_descriptor_sha256": approved_sha256,
                                      "preset_sha256": current["preset_sha256"]},
                            post_check=lambda: verify_manifest(root, descriptor["installation_manifest"]),
                            desktop_check=runtime_desktop_report)


def post_cleanup_observation(targets, borrowed, known_pids, grace=5, owned=()):
    """Wait only for known exiting identities; unknown outsiders still block."""
    def settling(observed):
        if any(r['pid'] not in known_pids for r in observed.get('uncertain', [])):
            return False
        for row in observed.get('matches', []):
            if not any(row['pid'] == old['pid'] and row['creation_ticks'] == old['creation_ticks']
                       and os.path.normcase(row['image']) == os.path.normcase(old['image']) for old in list(borrowed) + list(owned)):
                return False
        return bool(observed.get('uncertain') or observed.get('matches'))
    observed = find_matching_processes(targets)
    if grace <= 0 or not settling(observed):
        return observed
    deadline = time.monotonic() + grace
    while settling(observed) and time.monotonic() < deadline:
        time.sleep(0.1)
        observed = find_matching_processes(targets)
    return observed


def run_prepared(current, session, directory, cursor, timeout, *, metadata=None,
                 post_check=None, finish_session=True, progress=None, exit_grace=5,
                 completion_observer=None, stop_requested=None, desktop_check=None):
    """Monitor an owned assistant; caller holds durable admission throughout."""
    report = {"schema_version": 1, "status": "starting", "game_execution": True,
              "run_id": session.record["run_id"],
              "argv": current["argv_candidate"], "cwd": current["cwd"], "retries": 0,
              "cursor_offset": cursor.offset,
              "ownership_observations": [], "game_observed_in_job": False, "cleanup_verified": False}
    report.update(metadata or {})
    report['game_path'] = current['game_path']
    report['observation_targets'] = current.get('observation_targets') or [
        current['argv_candidate'][0], current.get('launcher', str(Path(current['installation_root']) / 'ok-ww.exe')),
        current['game_path']]
    if isinstance(cursor, LogCursor):
        report['log_baseline'] = {
            'path': cursor.path, 'identity': list(cursor.identity), 'offset': cursor.offset,
            'prefix_sha256': cursor.prefix_sha256, 'captured_at_ns': cursor.captured_at_ns,
            'expected_archive_name': cursor.expected_archive_name,
            'archive_identities': [[name, list(identity)] for name, identity in cursor.archive_identities]}
    borrowed = current.get('borrowed_game', [])
    report['borrowed_game'] = borrowed
    report['game_observed_external'] = False
    def same_borrowed(row):
        return any(row['pid'] == old['pid'] and row['creation_ticks'] == old['creation_ticks']
                   and os.path.normcase(row['image']) == os.path.normcase(old['image']) for old in borrowed)
    save(directory / "summary.json", report)
    started = time.monotonic()
    next_progress = started + 30
    next_focus = started
    next_desktop_check = started
    code, root_pid = None, None
    uncertain = None
    history = {}
    targets = current.get('observation_targets') or [current["argv_candidate"][0], current.get("launcher", str(Path(current["installation_root"]) / "ok-ww.exe")), current["game_path"]]
    try:
        from .compatibility import verify_installation_stamp
        try:
            verify_installation_stamp(current)
        except ValueError as error:
            report.update(status='blocked', game_execution=False, cleanup_verified=True,
                          repeat_blocked=True, error=str(error), duration_seconds=0, exit_code=None)
            if finish_session: session.finish('prelaunch_blocked', cleanup_verified=True)
            save(directory / 'summary.json', report)
            return report, directory
        if stop_requested and stop_requested():
            report.update(status='cancelled', cleanup_verified=True, game_execution=False,
                          duration_seconds=0, exit_code=None)
            if finish_session:
                session.finish('cancelled', cleanup_verified=True)
            save(directory / 'summary.json', report)
            return report, directory
        launch_options = {'additional_roots': current['additional_roots']} if current.get('additional_roots') else {}
        if desktop_check:
            desktop = desktop_check()
            if desktop.get('status') != 'observed_available':
                report.update(status='desktop_environment_interrupted', desktop_interruption=desktop,
                              cleanup_verified=True, game_execution=False, duration_seconds=0, exit_code=None)
                if finish_session:
                    session.finish(report['status'], cleanup_verified=True)
                save(directory / 'summary.json', report)
                return report, directory
        with WindowsProcess(current["argv_candidate"], cwd=current["cwd"], env=runner_environment(), **launch_options) as process:
            root_pid = process.pid
            allow_foreground(root_pid)
            report.update(status="running", root_pid=root_pid)
            while True:
                if desktop_check and time.monotonic() >= next_desktop_check:
                    next_desktop_check = time.monotonic() + 1
                    desktop = desktop_check()
                    if desktop.get('status') != 'observed_available':
                        report.update(status='desktop_environment_interrupted', desktop_interruption=desktop)
                        save(directory / 'summary.json', report)
                        if progress:
                            progress('Desktop environment interrupted: ' + desktop.get('reason', 'unknown') +
                                     '; cleaning owned processes and stopping the chain')
                        break
                try:
                    rows = owned_processes(process)
                    observed = find_matching_processes(targets)
                except OSError as exc:
                    # The retained root handle remains authoritative when a
                    # fresh PID query loses access during Windows teardown.
                    report['observation_warning'] = str(exc)
                    try:
                        code = process.wait(min(2, max(0, timeout - (time.monotonic() - started))))
                        break
                    except subprocess.TimeoutExpired:
                        uncertain = 'matching_process_observation_uncertain'
                        break
                if observed["status"] != "observed":
                    report['observation_warning'] = observed
                    known_ids = {root_pid} | {r['pid'] for r in history.values()} | {r['pid'] for r in borrowed}
                    if any(r['pid'] not in known_ids for r in observed.get('uncertain', [])):
                        uncertain = 'matching_process_observation_uncertain'
                        break
                    try:
                        code = process.wait(min(2, max(0, timeout - (time.monotonic() - started))))
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    uncertain = "matching_process_observation_uncertain"
                    break
                try:
                    checked = verify_observed_membership(process, observed["matches"])
                except OSError as exc:
                    report['observation_warning'] = str(exc)
                    prior = list(history.values()) + borrowed
                    if any(r['pid'] != root_pid and not any(
                            r['pid'] == old['pid'] and r['creation_ticks'] == old['creation_ticks']
                            and os.path.normcase(r['image']) == os.path.normcase(old['image'])
                            for old in prior) for r in observed['matches']):
                        uncertain = 'matching_process_observation_uncertain'
                        break
                    try:
                        code = process.wait(min(2, max(0, timeout - (time.monotonic() - started))))
                        break
                    except subprocess.TimeoutExpired:
                        uncertain = 'matching_process_observation_uncertain'
                        break
                outside = [r for r in checked if r.get("in_owned_job") is not True and not same_borrowed(r)]
                if outside:
                    report["unowned_processes"] = outside
                    uncertain = "matching_game_or_runner_outside_owned_job"
                    break
                for row in rows + checked:
                    if same_borrowed(row):
                        report['game_observed_external'] = True
                    if row.get("in_owned_job") is True:
                        history[(row["pid"], row["creation_ticks"])] = row
                    if (row.get("in_owned_job") is True and os.path.normcase(row.get("image", ""))
                            == os.path.normcase(current["game_path"])):
                        report["game_observed_in_job"] = True
                if list(history.values()) != report["ownership_observations"]:
                    report["ownership_observations"] = list(history.values())
                    try:
                        session.observe_owned(list(history.values()))
                    except OSError:
                        report['failure_stage'] = 'execution_journal_write'
                        raise
                    save(directory / "summary.json", report)
                now = time.monotonic()
                if now >= next_focus:
                    next_focus = now + 2
                    focus = request_foreground(list(history.values()) + borrowed,
                                               current.get('focus_targets') or [current['game_path']])
                    if focus != report.get('foreground'):
                        report['foreground'] = focus
                        save(directory / 'summary.json', report)
                        if progress and focus['status'] in {'foreground', 'foreground_denied'}:
                            progress(f"Foreground: {focus['status']}; pid={focus['pid']}")
                if stop_requested and stop_requested():
                    report['status'] = 'cancelled'
                    break
                remaining = timeout - (now - started)
                if progress and now >= next_progress:
                    progress(f"Running: {int(now - started)}s elapsed; game detected: {report['game_observed_in_job'] or report['game_observed_external']}")
                    next_progress = now + 30
                if remaining <= 0:
                    report["status"] = "timeout"
                    break
                if current.get('native_startup'):
                    from .maagf2 import advance_startup
                    startup = advance_startup(current, cursor, process, list(history.values()), directory,
                                              now - started, stop_requested=stop_requested)
                    if startup != report.get('native_startup'):
                        report['native_startup'] = dict(startup)
                        save(directory / 'summary.json', report)
                        if progress and startup['status'] == 'acknowledged':
                            progress('Native game connection ready; saved daily preset commanded once')
                        if progress and startup['status'] == 'native_auto_started':
                            progress('Native saved preset started automatically; no start command sent')
                    if startup.get('task_queue_verified'):
                        try:
                            code = process.wait(0)
                        except subprocess.TimeoutExpired:
                            cursor.command['owned_stop'] = True
                            save(directory / 'native-owned-stop.json', {
                                'root_pid': root_pid, 'reason': 'verified_native_business_queue_completed',
                                'startup_mode': cursor.startup_mode, 'start_commands_sent': 0})
                        break
                try:
                    code = process.wait(min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        report["owned_job_cleanup_verified"] = True
    except CleanupError as exc:
        uncertain = "owned_job_cleanup_failed"
        report["error"] = str(exc)
    except (OSError, ValueError) as exc:
        uncertain = ("execution_journal_write_failed" if report.get('failure_stage') == 'execution_journal_write'
                     else "launch_or_observation_failed")
        report["error"] = str(exc)
        # An exception in the body still goes through the process context's
        # verified cleanup. Its cleanup failures use CleanupError above.
        report['owned_job_cleanup_verified'] = root_pid is not None
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        # Reaching this handler after entering the process context means its
        # __exit__ verified Job cleanup; CleanupError is handled separately.
        report['owned_job_cleanup_verified'] = root_pid is not None
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        save(directory / "summary.json", report)
        raise
    try:
        if root_pid is None:
            completion = {"status": "launch_failed"}
        elif completion_observer:
            completion = completion_observer(cursor, code, root_pid, list(history.values()))
        else:
            completion = observe_root(cursor, code, root_pid)
    except (OSError, ValueError, UnicodeError) as exc:
        completion = {"status": "completion_unconfirmed", "error": str(exc)}
    report["completion"] = completion
    try:
        known_pids = {root_pid} | {r['pid'] for r in history.values()} | {r['pid'] for r in borrowed}
        remaining_processes = post_cleanup_observation(targets, borrowed, known_pids, grace=exit_grace,
                                                       owned=list(history.values()) if report.get('owned_job_cleanup_verified') else ())
        known_remaining = [r for r in remaining_processes['matches'] if same_borrowed(r)]
        unknown_remaining = [r for r in remaining_processes['matches'] if not same_borrowed(r)]
        report['external_game_left_running'] = bool(known_remaining)
        if remaining_processes["status"] != "observed" or unknown_remaining:
            uncertain = uncertain or "matching_processes_remain_after_cleanup"
        report["remaining_processes"] = remaining_processes
    except OSError as exc:
        uncertain = uncertain or "post_cleanup_observation_failed"
        report["post_cleanup_error"] = str(exc)
    if (not report["game_observed_in_job"] and not report['game_observed_external']
            and report['status'] != 'desktop_environment_interrupted'):
        uncertain = uncertain or "game_job_inheritance_unconfirmed"
    try:
        report["post_execution_drift"] = post_check() if post_check else {"status": "not_required"}
    except (OSError, ValueError) as exc:
        report["post_execution_drift"] = {"status": "unknown", "error": str(exc)}
    if uncertain:
        report.update(status="ownership_or_cleanup_unconfirmed", reason=uncertain)
    else:
        report["cleanup_verified"] = report.get("owned_job_cleanup_verified") is True
        if report["status"] not in {"timeout", "cancelled", "desktop_environment_interrupted"}:
            report["status"] = completion["status"]
            if post_check and report["post_execution_drift"]["status"] != "unchanged":
                report["status"] = "installation_changed_or_unconfirmed"
        if finish_session:
            session.finish(report["status"], cleanup_verified=report["cleanup_verified"])
    report["exit_code"] = code
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    save(directory / "summary.json", report)
    return report, directory
