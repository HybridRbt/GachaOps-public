"""Read-only OK-WW log contract for v3.6.7 and v3.8.9. Never imports or starts the assistant.

v3.8.9 (reviewed offline against ok-ww v3.8.9 with ok-script 3.0.7) only
changes the first completion line through i18n: DailyTask logs
tr('Daily Task Completed') and the zh_CN catalog renders the exact string
「每日任务已完成」. The start line, init line, LINE format, the executor
exit line and the midnight dated rotation are unchanged.

An exit code and a *new* task start are required. Caught additional-task failures
remain partial_failure even when the runner subsequently announces completion.
The caller must separately prove process ownership/exclusive log writer binding.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from types import SimpleNamespace
import hashlib
import os
from pathlib import Path
import re
import time


LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
                  r"(?P<level>\w+) (?P<thread>\S+) (?P<source>[^:]+):(?P<message>.*)$")
START = re.compile(r"^enabled task <src\.task\.DailyTask\.DailyTask object at 0x[0-9A-Fa-f]+>$")
# Both spellings are exact strings, never substrings: the same DailyTask
# thread also logs sub-task lines such as 「每周乐园任务已完成, 已达到上限」
# that must not count as the run completion. The English form keeps older
# builds (and non-zh catalogs) decidable.
TASK_COMPLETED_MESSAGES = frozenset({'Daily Task Completed', '每日任务已完成'})
# Routine ERROR lines, classified by source plus an anchored message
# template. They accompany healthy runs and must not turn one into a
# failure; they are kept in the result as routine_errors instead. Each
# entry: (log source name, anchored template, exempt window in seconds
# after the task start, or None for no window). Add or remove entries only
# here. Sources verified against ok-ww v3.8.9 and ok-script 3.0.7:
ROUTINE_ERROR_PATTERNS = (
    # src/combat/CombatCheck.py:185 -- mid-fight target acquisition retries.
    ('CombatCheck', re.compile(r'target_enemy failed, try recheck break out of combat'), None),
    # src/task/BaseCombatTask.py:1003 (raise_not_in_combat) -- recheck after
    # slipping out of combat; repeated inside healthy fights.
    ('BaseCombatTask', re.compile(r'combat check not in combat'), None),
    # src/char/BaseChar.py:639 shared liberation retry, surfaced through the
    # character's own logger (observed source: Hiyuki).
    ('Hiyuki', re.compile(r'clicked liberation but no effect'), None),
    # ok-script ok/device/capture_methods/bitblt_utils.py:90 -- transient
    # BitBlt capture failures while the game window is coming up; observed
    # within seconds 17-21 of a task start, window bounded at 60.
    ('bitblt_utils', re.compile(r'capture_by_bitblt exception:.*'), 60),
    # ok-script ok/device/interaction_methods/post_message.py:115 -- invalid
    # handle while the window is not ready yet; same bounded window.
    ('post_message', re.compile(r'PostMessage error.*'), 60),
)


@dataclass(frozen=True)
class LogCursor:
    path: str
    identity: tuple
    offset: int
    prefix_sha256: str
    at_line_boundary: bool
    captured_at_ns: int = 0
    archive_identities: tuple = ()
    expected_archive_name: str = ''
    baseline_mtime: float | None = None


def midnight_archive_name(path, mtime, observed_at):
    """Compute the native stdlib handler's suffix without opening a log writer."""
    settings = SimpleNamespace(when='MIDNIGHT', utc=False, atTime=None, interval=86400)
    rollover = TimedRotatingFileHandler.computeRollover(settings, int(mtime))
    start = rollover - settings.interval
    then = time.localtime(start)
    now_dst = time.localtime(int(observed_at)).tm_isdst
    if now_dst != then.tm_isdst:
        then = time.localtime(start + (3600 if now_dst else -3600))
    return f'{path.stem}.{time.strftime("%Y-%m-%d", then)}{path.suffix}'


def dated_archives(path):
    pattern = re.compile(re.escape(path.stem) + r'\.\d{4}-\d{2}-\d{2}' + re.escape(path.suffix))
    return sorted(p for p in path.parent.glob(path.stem + '.*' + path.suffix)
                  if pattern.fullmatch(p.name))


def capture_cursor(path):
    path = Path(path).resolve()
    captured_at_ns = time.time_ns()
    archives = []
    for archive in dated_archives(path):
        if archive.is_symlink():
            raise ValueError('symlink log archive cannot be correlated')
        stat = archive.stat()
        archives.append((archive.name, (stat.st_dev, stat.st_ino)))
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        prefix = stream.read(stat.st_size)
    return LogCursor(str(path), (stat.st_dev, stat.st_ino), len(prefix),
                     hashlib.sha256(prefix).hexdigest(), not prefix or prefix.endswith(b"\n"),
                     captured_at_ns, tuple(archives),
                     f'{path.stem}.{datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d}{path.suffix}',
                     stat.st_mtime)


def appended_lines(cursor):
    """Reject rotation/truncation/rewrite; ignore preexisting or incomplete lines."""
    with Path(cursor.path).open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if (stat.st_dev, stat.st_ino) != cursor.identity or stat.st_size < cursor.offset:
            raise ValueError("log rotated or truncated; completion cannot be correlated")
        prefix = stream.read(cursor.offset)
        if hashlib.sha256(prefix).hexdigest() != cursor.prefix_sha256:
            raise ValueError("log prefix changed; completion cannot be correlated")
        tail = stream.read(stat.st_size - cursor.offset)
    if not cursor.at_line_boundary:
        tail = tail.partition(b"\n")[2]
    tail = tail[:tail.rfind(b"\n") + 1]
    return tail.decode("utf-8", errors="strict").splitlines()


def initialization_pids(lines):
    pids = []
    for line in lines:
        event = LINE.fullmatch(line)
        if event and event['level'] == 'INFO' and event['source'] == 'ok':
            init = re.match(r'ok-script init .*?, pid=(\d+) config:', event['message'])
            if init:
                pids.append(int(init[1]))
    return pids


def correlated_lines(cursor, root_pid):
    """Allow one proved dated rename, with unchanged baseline and fresh root init.

    Generic observe() stays strict. Only observers bound to an owned root may
    read the replacement file. Unknown/multiple rotations remain unconfirmed.
    """
    try:
        return appended_lines(cursor), {'kind': 'append'}
    except ValueError:
        path = Path(cursor.path)
        if path.is_symlink():
            raise ValueError('symlink replacement log cannot be correlated')
        with path.open('rb') as stream:
            current_stat = os.fstat(stream.fileno())
            current_identity = (current_stat.st_dev, current_stat.st_ino)
            if current_identity == cursor.identity:
                raise ValueError('log truncated or prefix changed; completion cannot be correlated')
            replacement = stream.read(current_stat.st_size)
        baseline = dict(cursor.archive_identities)
        changed = []
        for archive in dated_archives(path):
            if archive.is_symlink():
                raise ValueError('symlink log archive cannot be correlated')
            stat = archive.stat()
            identity = (stat.st_dev, stat.st_ino)
            if baseline.get(archive.name) != identity:
                changed.append((archive, identity))
        allowed_names = {cursor.expected_archive_name}
        if cursor.baseline_mtime is not None:
            allowed_names.add(midnight_archive_name(path, cursor.baseline_mtime, time.time()))
        if (len(changed) != 1 or changed[0][1] != cursor.identity
                or changed[0][0].name not in allowed_names):
            raise ValueError('original log archive missing or multiple rotations; completion cannot be correlated')
        archive = changed[0][0]
        # Use the original cursor so identity, length, prefix and line boundary
        # receive exactly the same checks as the ordinary append path.
        archived_tail = appended_lines(replace(cursor, path=str(archive)))
        replacement = replacement[:replacement.rfind(b'\n') + 1]
        lines = archived_tail + replacement.decode('utf-8', errors='strict').splitlines()
        if not cursor.captured_at_ns or initialization_pids(lines) != [root_pid]:
            raise ValueError('rotated log requires unique owned root initialization')
        # Native timestamps use the same host local timezone as the supervisor.
        # One second allows millisecond formatting without admitting an old run.
        lower_bound = cursor.captured_at_ns / 1e9 - 1
        initialized = False
        for line in lines:
            event = LINE.fullmatch(line)
            if not event:
                continue
            stamp = datetime.strptime(line[:23], '%Y-%m-%d %H:%M:%S,%f').timestamp()
            if stamp < lower_bound:
                raise ValueError('rotated log contains preexisting events')
            if event['source'] == 'ok' and re.match(r'ok-script init .*?, pid=', event['message']):
                initialized = True
            if event['source'] == 'start_controller' and event['message'].startswith('enabled task ') and not initialized:
                raise ValueError('rotated task start precedes root initialization')
        # Reject another rotation/replacement while collecting the evidence.
        stat = path.stat()
        if (stat.st_dev, stat.st_ino) != current_identity:
            raise ValueError('log changed during rotation verification')
        return lines, {'kind': 'dated_rotation', 'archive': str(archive),
                       'baseline_prefix_sha256': cursor.prefix_sha256}


def _stamp(line):
    """Local-time epoch of a log line's timestamp, or None when unparseable."""
    try:
        return datetime.strptime(line[:23], '%Y-%m-%d %H:%M:%S,%f').timestamp()
    except ValueError:
        return None


def classify_routine(source, message, stamp, start_stamp):
    """True when an ERROR line matches a routine template.

    Windowed templates are exempt only inside their seconds budget after the
    task start; an unparseable timestamp never exempts."""
    for routine_source, pattern, window in ROUTINE_ERROR_PATTERNS:
        if source == routine_source and pattern.match(message):
            if window is None:
                return True
            if stamp is None or start_stamp is None:
                return False
            return 0 <= stamp - start_stamp <= window
    return False


def evaluate(lines, exit_code, *, writer_verified=False):
    """Classify a single newly started DailyTask; no exit means still unconfirmed.

    Routine ERROR noise is moved to ``routine_errors`` only while the run is
    clean -- exactly one task start, both completion markers, exit code 0.
    Any other outcome keeps every ERROR in ``failures``; none is dropped."""
    starts = 0
    completed = False
    executor_completed = False
    start_stamp = None
    errors = []
    for number, line in enumerate(lines, 1):
        match = LINE.fullmatch(line)
        if not match:
            continue
        level, thread, source, message = (match[k] for k in ("level", "thread", "source", "message"))
        if level == "INFO" and source == "start_controller" and START.fullmatch(message):
            starts += 1
            if start_stamp is None:
                start_stamp = _stamp(line)
        if not starts:
            continue
        if thread == "TaskExecutor" and source == "DailyTask" \
                and level == "INFO" and message in TASK_COMPLETED_MESSAGES:
            completed = True
        if (level == "INFO" and thread == "TaskExecutor" and source == "TaskExecutor"
                and message == "Successfully Executed Task, Exiting Game and App!" and completed):
            executor_completed = True
        if level == "ERROR":
            errors.append((number, source, message, _stamp(line)))
    clean = starts == 1 and completed and executor_completed and exit_code == 0
    failures = []
    routine_errors = []
    for number, source, message, stamp in errors:
        if clean and classify_routine(source, message, stamp, start_stamp):
            routine_errors.append({"line": number, "message": message})
        else:
            failures.append({"line": number, "message": message})
    confirmed = writer_verified and starts == 1 and exit_code is not None
    status = "completion_unconfirmed"
    if confirmed and exit_code != 0:
        status = "failed"
    elif confirmed and completed and executor_completed:
        status = "partial_failure" if failures else "success"
    elif confirmed and failures:
        status = "failed"
    return {"status": status, "exit_code": exit_code, "task_starts": starts,
            "task_completed": completed, "executor_completed": executor_completed,
            "writer_verified": writer_verified, "failures": failures,
            "routine_errors": routine_errors}


def observe(cursor, exit_code, *, writer_verified=False):
    try:
        return evaluate(appended_lines(cursor), exit_code, writer_verified=writer_verified)
    except (OSError, ValueError) as exc:
        return {"status": "completion_unconfirmed", "reason": str(exc), "exit_code": exit_code}


def observe_root(cursor, exit_code, root_pid):
    """Correlate exactly one fresh OK initialization with the Job-owned root PID."""
    try:
        lines, association = correlated_lines(cursor, root_pid)
        pids = initialization_pids(lines)
        report = evaluate(lines, exit_code, writer_verified=pids == [root_pid])
        report["initialization_pids"] = pids
        report['log_association'] = association
        return report
    except (OSError, ValueError) as exc:
        return {"status": "completion_unconfirmed", "reason": str(exc), "exit_code": exit_code}
