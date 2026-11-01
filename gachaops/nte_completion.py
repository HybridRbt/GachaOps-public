"""Read-only OK-NTE v1.4.6 daily-result contract; saved task choices stay opaque."""
import ast
import re

from .ok_completion import LINE, correlated_lines, initialization_pids

START = re.compile(r'^enabled task <src\.tasks\.daily\.DailyRoutineTask\.DailyRoutineTask object at 0x[0-9a-fA-F]+>$')
EMPTY = '日常任务没有已选任务，跳过执行'
EXIT = 'Successfully Executed Task, Exiting Game and App!'


def evaluate(lines, exit_code, *, writer_verified=False):
    starts = begins = ends = 0
    empty = executor_completed = False
    results, failures = {}, []
    invalid = False
    for number, line in enumerate(lines, 1):
        event = LINE.fullmatch(line)
        if not event:
            continue
        level, thread, source, message = (event[k] for k in ('level', 'thread', 'source', 'message'))
        if level == 'INFO' and source == 'start_controller' and START.fullmatch(message):
            starts += 1
        if not starts or thread != 'TaskExecutor':
            continue
        if source == 'DailyRoutineTask':
            if level == 'ERROR':
                failures.append({'line': number, 'message': message})
            if level == 'INFO':
                if message == '开始执行日常任务':
                    begins += 1
                elif message == '结束执行日常任务':
                    ends += 1
                    invalid |= begins != 1 or set(results) != {'success', 'failed', 'skipped'}
                elif message == EMPTY:
                    empty = True
                elif message.startswith('任务失败: '):
                    failures.append({'line': number, 'message': message})
                elif message.startswith('info_set '):
                    match = re.fullmatch(r'info_set (success|failed|skipped) (.*)', message)
                    if match:
                        key = match[1]
                        try:
                            value = ast.literal_eval(match[2])
                            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                                raise ValueError('invalid task result list')
                            invalid |= key in results or begins != 1 or ends != 0
                            results[key] = value
                        except (ValueError, SyntaxError):
                            invalid = True
        if source == 'TaskExecutor':
            if level == 'ERROR' and 'exception stopped' in message:
                failures.append({'line': number, 'message': message})
            if level == 'INFO' and message == EXIT and (ends == 1 or empty):
                executor_completed = True
    completed = (empty and begins == ends == 0 and not results) or (begins == ends == 1 and not empty)
    confirmed = writer_verified and starts == 1 and exit_code is not None
    status = 'completion_unconfirmed'
    if confirmed and exit_code != 0:
        status = 'failed'
    elif confirmed and completed and executor_completed and not invalid:
        status = 'partial_failure' if failures or results.get('failed') else 'success'
    elif confirmed and failures:
        status = 'failed'
    return {'status': status, 'exit_code': exit_code, 'writer_verified': writer_verified,
            'task_starts': starts, 'task_completed': completed, 'executor_completed': executor_completed,
            'empty_preset': empty, 'results': results, 'failures': failures,
            'failed_applications': results.get('failed', []), 'invalid_result_sequence': invalid}


def observe(cursor, exit_code, root_pid, owned):
    # The packaged pythonw is the root interpreter; no unbound child writer is accepted.
    try:
        lines, association = correlated_lines(cursor, root_pid)
        pids = initialization_pids(lines)
        report = evaluate(lines, exit_code, writer_verified=pids == [root_pid])
        report['initialization_pids'] = pids
        report['log_association'] = association
        return report
    except (OSError, ValueError) as exc:
        return {'status': 'completion_unconfirmed', 'reason': str(exc), 'exit_code': exit_code}
