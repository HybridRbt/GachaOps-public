"""No-game example ONLY. Never register this module in the production catalog."""
import argparse
import json
import os
from pathlib import Path
import time


def creation_ticks():
    if os.name != 'nt': return None
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetCurrentProcess.restype = w.HANDLE
    api.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    api.GetProcessTimes.restype = w.BOOL
    created, exited, kernel, user = (w.FILETIME() for _ in range(4))
    if not api.GetProcessTimes(api.GetCurrentProcess(), *map(ctypes.byref, (created, exited, kernel, user))):
        raise ctypes.WinError(ctypes.get_last_error())
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def observe(cursor, exit_code, root_pid, owned):
    if exit_code != 0: return 'failed'
    try:
        data = json.loads(Path(cursor['output']).read_text(encoding='utf-8'))
        if (data['run_id'] != cursor['run_id'] or data['pid'] != root_pid
                or data['at_ns'] < cursor['started_at_ns']
                or not any(row.get('pid') == data['pid'] and row.get('creation_ticks') == data['creation_ticks'] for row in owned)):
            return 'completion_unconfirmed'
        return data['status'] if data['status'] in {'success', 'partial_failure'} else 'completion_unconfirmed'
    except (OSError, ValueError, KeyError, TypeError):
        return 'completion_unconfirmed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['success', 'partial_failure', 'ambiguous', 'timeout'], required=True)
    args = parser.parse_args()
    if args.mode == 'timeout':
        time.sleep(3600)
        return
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'run_id': args.run_id, 'pid': os.getpid(), 'creation_ticks': creation_ticks(),
                   'at_ns': time.time_ns(), 'status': args.mode}, stream)


if __name__ == '__main__': main()
