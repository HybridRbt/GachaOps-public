import argparse
import json
from pathlib import Path
import sys
import uuid

from .planner import ConfigError, load, plan
from .supervisor import simulate
from .preflight import desktop_report
from .execution_lock import ExecutionBusy
from .ok_inventory import inspect_installation
from .ok_adapter import prepare, verify_manifest
from .probe import probe_runtime
from .recovery import read_state, save, RecoveryRequired
from .ok_trial import execute_trial
from .assistants import load_assistants, check_assistants, run_assistants
from .app_paths import state_directory, workspace_root, preview_migration, migrate_data


def main():
    parser = argparse.ArgumentParser(description="GachaOps: inspect, prepare, simulate, or explicitly authorized Windows trial")
    parser.add_argument("action", choices=["plan", "simulate", "doctor", "inspect-ok", "prepare-ok",
                                         "verify-installation", "probe-ok", "recovery-status", "trial-ok",
                                         "check-assistants", "run-assistants", "recover-assistants",
                                         "migration-preview", "migrate-data", "export-diagnostics"])
    parser.add_argument("--approved-plan-sha256", help="Explicit approval of exact prepare-ok file; trial only")
    parser.add_argument("--installation", help="Existing OK-WW installation root; read-only")
    parser.add_argument("--config")
    parser.add_argument('--data-root', help='Absolute data directory shared by GUI, CLI and worker')
    parser.add_argument("--output")
    parser.add_argument('--migration-source')
    parser.add_argument('--migration-target')
    parser.add_argument('--approved-preview', help='Path to explicitly reviewed migration preview JSON')
    parser.add_argument('--run', help='Run directory for a sanitized diagnostic export')
    parser.add_argument("--scenario", help="JSON object mapping task IDs to mock behavior lists")
    parser.add_argument("--cancel-file", help="GUI cooperative stop request; run-assistants only")
    args = parser.parse_args()
    try:
        if args.action == 'export-diagnostics':
            from .diagnostics import build_diagnostic_preview, export_diagnostics
            if not args.run or not args.output:
                raise ConfigError('diagnostic export requires explicit --run and --output ZIP')
            preview = build_diagnostic_preview(args.run)
            print(json.dumps({'files': preview.files, 'missing': preview.missing,
                              'notice': 'Only sanitized summaries and hashes; no raw logs, screenshots or configuration.'}))
            path = export_diagnostics(preview, args.output)
            print(json.dumps({'status': 'exported', 'output': str(path)}, ensure_ascii=False))
            return 0
        state = state_directory(args.data_root) if args.data_root is not None else state_directory()
        if args.action == 'migration-preview' and args.output is None:
            raise ConfigError('migration-preview requires explicit --output outside the migration source and target')
        if args.output is None:
            args.output = str(state.parent / 'runs')
        if args.action in {'migration-preview', 'migrate-data'}:
            if not args.migration_source or not args.migration_target:
                raise ConfigError('migration requires explicit source and target')
            if args.action == 'migration-preview':
                output = Path(args.output).resolve()
                if (output.is_relative_to(Path(args.migration_source).resolve())
                        or output.is_relative_to(Path(args.migration_target).resolve())):
                    raise ConfigError('migration preview output must be outside source and target')
                report = preview_migration(args.migration_source, args.migration_target)
                output.mkdir(parents=True, exist_ok=True)
                save(output / 'migration-preview.json', report)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            if not args.approved_preview:
                raise ConfigError('review a preview first; --approved-preview required')
            approved = json.loads(Path(args.approved_preview).read_text(encoding='utf-8-sig'))
            migrate_data(args.migration_source, args.migration_target, approved)
            print(json.dumps({'status': 'migrated', 'data_root': args.migration_target}, ensure_ascii=False))
            return 0
        if args.action == 'recover-assistants':
            from .recovery_check import recover_workspace
            report = recover_workspace(workspace_root(args.data_root) if args.data_root is not None else workspace_root())
            output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
            save(output / 'recovery.json', report)
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report['status'] in {'recovered', 'already_ready'} else 2
        if args.action in {"check-assistants", "run-assistants"}:
            if not args.config:
                raise ConfigError("--config required for assistants")
            data = load_assistants(args.config)
            if args.action == "check-assistants":
                report = check_assistants(data)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 2 if any(r['status'] == 'blocked' for r in report['assistants']) else 0
            cancel = Path(args.cancel_file) if args.cancel_file else None
            report, directory = run_assistants(data, args.output, state,
                                               stop_requested=cancel.exists if cancel else None)
            print(json.dumps({"status": report['status'], "summary": str(directory / 'summary.json')}, ensure_ascii=False))
            return 0 if report['status'] in {'success', 'nothing_enabled'} else 1
        if args.action == "trial-ok":
            if not args.config:
                raise ConfigError("--config required for trial-ok")
            report, directory = execute_trial(args.config, args.approved_plan_sha256, args.output, state)
            print(json.dumps({"status": report["status"], "summary": str(directory / "summary.json")}, ensure_ascii=False))
            return 0 if report["status"] == "success" else 1
        if args.action == "recovery-status":
            print(json.dumps(read_state(state), ensure_ascii=False, indent=2))
            return 0
        if args.action == "verify-installation":
            if not args.config:
                raise ConfigError("--config required for verify-installation")
            descriptor = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
            if not isinstance(descriptor, dict) or descriptor.get("mode") != "prepare_only":
                raise ConfigError("expected prepare-ok descriptor")
            report = verify_manifest(descriptor["installation_root"], descriptor["installation_manifest"])
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "unchanged" else 2
        if args.action in {"prepare-ok", "probe-ok"}:
            if not args.installation:
                raise ConfigError("--installation required")
            if args.action == "probe-ok":
                report, directory = probe_runtime(args.installation, args.output, state)
                print(json.dumps({"status": report["status"], "summary": str(directory / "summary.json")}, ensure_ascii=False))
                return 0 if report["status"] == "success" else 1
            report = prepare(args.installation)
            directory = Path(args.output)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / ("ok-prepared-" + uuid.uuid4().hex + ".json")
            save(path, report)
            print(json.dumps({"descriptor": str(path), "status": report["status"],
                              "blockers": report["blockers"], "file_count": len(report["installation_manifest"]["files"])}, ensure_ascii=False))
            return 0  # Preparation completed; descriptor remains blocked for game execution.
        if args.action == "inspect-ok":
            if not args.installation:
                raise ConfigError("--installation required for inspect-ok")
            print(json.dumps(inspect_installation(args.installation), ensure_ascii=False, indent=2))
            return 0
        if args.action == "doctor":
            report = desktop_report()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "observed_available" else 2
        if not args.config:
            raise ConfigError("--config required for plan/simulate")
        data = load(args.config)
        if args.action == "plan":
            print(json.dumps(plan(data), ensure_ascii=False, indent=2))
            return 0
        behaviors = json.loads(Path(args.scenario).read_text(encoding="utf-8")) if args.scenario else None
        if behaviors is not None and not isinstance(behaviors, dict):
            raise ConfigError("scenario must be an object")
        report, directory = simulate(data, args.output, behaviors)
        print(json.dumps({"summary": str(directory / "summary.json"), "status": report["status"]}, ensure_ascii=False))
        return 0 if report["status"] == "success" else 1
    except (ConfigError, OSError, ValueError, SyntaxError, KeyError, ExecutionBusy, RecoveryRequired) as exc:
        print(f"gachaops: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
