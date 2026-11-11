# Application data and explicit migration

Program resources and runtime data are separate. Source development defaults to its repository. A frozen app first honors a valid adjacent `gachaops-workspace.json`; legacy executables inside a source deployment retain that deployment. An unbound standalone executable uses `%LOCALAPPDATA%/GachaOps`. An invalid binding, inaccessible data or missing environment produces an error rather than silently switching journals. Bundle extraction paths are resources only.

`--data-root <absolute-directory>` selects the same authority in GUI, CLI and workers. A conflicting explicit binding is rejected. A known unfinished legacy journal cannot be bypassed by selecting a new directory. Global execution locking remains `Global\GachaOps.Execution.v1`, independent of data directory, user or version. GUI elevation retains the resolved directory and never starts a chain automatically.

Data contains `config`, `state`, `runs` and `backups`. Default CLI output goes under that directory's `runs`. No real user data is migrated merely by opening the app. First-run profiles are handled separately from directory resolution.

Confirmed combination deletion permanently removes only the selected UUID directory under `config/profiles`; it does not create a deletion archive or touch native assistants and games. Saving or renaming can retain earlier edit backups under `backups/profiles`; deleting a combination does not purge those existing backups. They remain until the user explicitly removes them. No automatic expiry or cleanup runs. Canceling deletion makes no change; deleting the last combination creates a default combination. The selected directory is atomically removed from the active list before file cleanup. If cleanup fails partway, a warning identifies the hidden `.deleting-*` residue for manual inspection; it is not a recoverable backup and no background purge runs. Other combinations and the default remain usable. A failure to remove the directory from the active list is reported as a deletion failure.

To migrate, explicitly choose an existing source and a **new, nonexistent target outside source**:

```
python -m gachaops migration-preview --migration-source <old-root> --migration-target <new-root> --output <preview-directory>
python -m gachaops migrate-data --migration-source <old-root> --migration-target <new-root> --approved-preview <preview-directory>/migration-preview.json
```

Read the preview before the second command. Migration takes the global lock, requires an idle source journal with `cleanup_verified=true`, hashes all config/state/run files, rechecks the preview, copies to a sibling staging directory, verifies every byte and rechecks source before one atomic publication. Existing target directories, quarantined/unfinished/corrupt journals, symlinks, changed sources and write failures block migration. Source remains intact; failed staging is retained for inspection. The target stores the preview/journal snapshot under backups. Relative output commands are resolved by the caller; preview files are private evidence and must not ship publicly.

The old binding remains authoritative; after successful copy explicitly select the new root (or update the old binding as a separate deployment action). Merely copying files does not authorize resetting isolation. Rebuilding/replacing an executable must preserve its data and binding. Actual frozen executable acceptance is still required under HYB-124/125.
