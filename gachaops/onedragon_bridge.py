"""Run inside the assistant's Python, never import its dependencies in Supervisor.

Only the saved official application entry and its public result/callback API are
used. No native task, instance or account configuration is edited.
"""
import argparse
import json
import os
from pathlib import Path
import sys


def creation_ticks():
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetCurrentProcess.restype = w.HANDLE
    api.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    api.GetProcessTimes.restype = w.BOOL
    created, exited, kernel, user = (w.FILETIME() for _ in range(4))
    if not api.GetProcessTimes(api.GetCurrentProcess(), ctypes.byref(created),
                               ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def execute(root, output, token):
    output = Path(output)
    if output.exists():
        raise ValueError('result destination must be fresh')
    os.chdir(root)
    sys.path.insert(0, str(Path(root) / 'src'))
    evidence = {'schema_version': 1, 'token': token, 'pid': os.getpid(),
                'creation_ticks': creation_ticks(),
                'shutdown_completed': False, 'application_results': []}
    try:
        from zzz_od.application.zzz_application_launcher import ZApplicationLauncher

        class ObservedLauncher(ZApplicationLauncher):
            def create_context(self):
                ctx = super().create_context()
                factory = ctx.run_context.get_application

                def observed_factory(*args, **kwargs):
                    app = factory(*args, **kwargs)
                    if app is not None and not getattr(app, '_gachaops_observed', False):
                        callback = app.op_callback

                        def observed_result(result):
                            evidence['application_results'].append({
                                'app_id': app.app_id, 'success': result.success})
                            if callback is not None:
                                callback(result)

                        app.op_callback = observed_result
                        app._gachaops_observed = True
                    return app

                ctx.run_context.get_application = observed_factory
                return ctx

        # Close-game is a transient lifecycle request. No instance/task override
        # and no shutdown option: GUI remains the authority on what to execute.
        sys.argv = ['zzz_application_launcher', '--close-game']
        launcher = ObservedLauncher()
        launcher.run()
        run = launcher.ctx.run_context.last_run_result
        result = launcher.ctx.run_context.last_application_result
        evidence.update(shutdown_completed=True,
                        finish_reason=str(run.finish_reason) if run else None,
                        app_id=run.app_id if run else None,
                        group_id=run.group_id if run else None,
                        operation_success=result.success if result else None)
        code = 0 if (evidence['finish_reason'] == 'COMPLETED'
                     and evidence['operation_success'] is True) else 1
    except BaseException as exc:
        # Keep diagnostics local; do not copy credentials or arbitrary exception
        # strings from the native context into the orchestration report.
        evidence['error_type'] = type(exc).__name__
        code = 2
    temporary = output.with_suffix('.tmp')
    temporary.write_text(json.dumps(evidence, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, output)
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    return execute(args.root, args.output, args.token)


if __name__ == '__main__':
    raise SystemExit(main())
