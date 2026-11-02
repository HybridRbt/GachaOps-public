from datetime import datetime, timedelta, timezone
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from gachaops.ok_completion import capture_cursor, observe, observe_root
from gachaops.nte_completion import observe as observe_nte
from test_nte import cycle


def fresh(lines):
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
    return [stamp + line[23:] for line in lines]


def write(path, lines):
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


class RotationTests(unittest.TestCase):
    def baseline(self, root):
        path = root / 'ok-script.log'
        write(path, cycle())  # Old success, even with the same PID, must be ignored.
        yesterday = time.time() - 86400
        os.utime(path, (yesterday, yesterday))
        cursor = capture_cursor(path)
        return path, cursor, root / cursor.expected_archive_name

    def test_actual_timed_handler_startup_rollover_retains_partial_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path, cursor, archive = self.baseline(Path(directory))
            handler = TimedRotatingFileHandler(path, when='midnight', backupCount=7, encoding='utf-8')
            handler.namer = lambda name: str(Path(name).with_name('ok-script.' + Path(name).name.split('.log.')[1] + '.log'))
            handler.setFormatter(logging.Formatter('%(message)s'))
            try:
                for line in fresh(cycle(['一咖舍'])):
                    handler.emit(logging.LogRecord('fixture', logging.INFO, '', 0, line, (), None))
                handler.flush()
            finally:
                handler.close()
            # The native handler may choose the preceding date across a DST
            # transition; inspect its actual output instead of assuming mtime.
            archives = list(path.parent.glob('ok-script.*.log'))
            self.assertEqual(len(archives), 1)
            report = observe_nte(cursor, 0, 123, [])
            self.assertEqual(report['status'], 'partial_failure')
            self.assertEqual(report['task_starts'], 1)
            self.assertEqual(report['log_association']['kind'], 'dated_rotation')
            # No root binding means the generic read path remains strict.
            self.assertEqual(observe(cursor, 0, writer_verified=True)['status'], 'completion_unconfirmed')

    def test_midnight_archive_suffix_across_spring_and_fall_transitions(self):
        from gachaops.ok_completion import midnight_archive_name
        def chicago_time(stamp):
            spring = datetime(2026, 3, 8, 8, tzinfo=timezone.utc).timestamp()
            fall = datetime(2026, 11, 1, 7, tzinfo=timezone.utc).timestamp()
            dst = int(spring <= stamp < fall)
            wall = datetime.fromtimestamp(stamp, timezone.utc) - timedelta(hours=5 if dst else 6)
            return time.struct_time((*wall.timetuple()[:8], dst))
        cases = [
            (datetime(2026, 3, 7, 18, tzinfo=timezone.utc), datetime(2026, 3, 8, 17, tzinfo=timezone.utc), 'ok-script.2026-03-07.log'),
            (datetime(2026, 10, 31, 17, tzinfo=timezone.utc), datetime(2026, 11, 1, 18, tzinfo=timezone.utc), 'ok-script.2026-10-30.log')]
        with patch('gachaops.ok_completion.time.localtime', side_effect=chicago_time):
            for old, now, expected in cases:
                with self.subTest(expected=expected):
                    self.assertEqual(midnight_archive_name(Path('ok-script.log'), old.timestamp(), now.timestamp()), expected)

    def test_rollover_during_task_combines_only_new_tail_and_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path, cursor, archive = self.baseline(Path(directory))
            rows = fresh(cycle(['异象家具']))
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\n'.join(rows[:3]) + '\n')
            path.rename(archive)
            write(path, rows[3:])
            result = observe_nte(cursor, 0, 123, [])
            self.assertEqual(result['status'], 'partial_failure')
            self.assertEqual(result['failed_applications'], ['异象家具'])

    def test_unproved_archives_replacements_and_prefix_edits_are_rejected(self):
        for mode in ['missing', 'copy', 'prefix', 'multiple', 'wrong_date', 'truncate']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, cursor, archive = self.baseline(root)
                if mode == 'copy':
                    shutil.copy2(path, archive)
                    # Keep the old inode allocated, preventing inode reuse.
                    path.rename(root / 'unrecognized.log')
                elif mode == 'missing':
                    path.rename(root / 'unrecognized.log')
                elif mode == 'wrong_date':
                    path.rename(root / 'ok-script.2001-01-01.log')
                elif mode != 'truncate':
                    path.rename(archive)
                if mode == 'prefix':
                    archive.write_text('rewritten baseline\n', encoding='utf-8')
                if mode == 'multiple':
                    (root / 'ok-script.2001-01-01.log').write_text('another rotation\n')
                write(path, fresh(cycle()))
                self.assertEqual(observe_nte(cursor, 0, 123, [])['status'], 'completion_unconfirmed')

    def test_old_wrong_duplicate_and_late_initialization_are_rejected(self):
        for mode in ['old', 'wrong', 'duplicate', 'late', 'no_init']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path, cursor, archive = self.baseline(Path(directory))
                path.rename(archive)
                rows = fresh(cycle())
                if mode == 'old':
                    old = datetime.fromtimestamp(time.time() - 86400).strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
                    rows = [old + line[23:] for line in rows]
                elif mode == 'wrong':
                    rows[0] = rows[0].replace('pid=123', 'pid=456')
                elif mode == 'duplicate':
                    rows.insert(1, rows[0])
                elif mode == 'late':
                    rows[0], rows[1] = rows[1], rows[0]
                else:
                    rows = rows[1:]
                write(path, rows)
                self.assertEqual(observe_nte(cursor, 0, 123, [])['status'], 'completion_unconfirmed')

    def test_incomplete_rotated_log_and_nonzero_exit_never_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            path, cursor, archive = self.baseline(Path(directory))
            path.rename(archive)
            rows = fresh(cycle())
            path.write_text('\n'.join(rows), encoding='utf-8')
            self.assertEqual(observe_nte(cursor, 0, 123, [])['status'], 'completion_unconfirmed')
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\n')
            self.assertEqual(observe_nte(cursor, 0, 123, [])['status'], 'success')
            self.assertEqual(observe_nte(cursor, 2, 123, [])['status'], 'failed')

    def test_ww_root_observer_uses_same_proved_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            path, cursor, archive = self.baseline(Path(directory))
            path.rename(archive)
            rows = cycle()[:2]
            rows[1] = rows[1].replace('src.tasks.daily.DailyRoutineTask.DailyRoutineTask', 'src.task.DailyTask.DailyTask')
            rows += ['2026-10-01 12:00:00,000 INFO TaskExecutor DailyTask:Daily Task Completed', cycle()[-1]]
            write(path, fresh(rows))
            self.assertEqual(observe_root(cursor, 0, 123)['status'], 'success')
