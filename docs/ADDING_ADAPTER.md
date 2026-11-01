# Adding a built-in adapter

GachaOps v1 accepts repository-reviewed built-ins, not arbitrary user JSON commands or dynamic imports. The synthetic template under tests/fixtures/synthetic_adapter.py is test-only and must never be added to the production catalog.

## Interfaces and integration points

Declare an immutable AdapterSpec in gachaops/adapter_catalog.py: adapter_id, game_id, display_name, verified upstream HTTPS URL, registration_fields, start_modes and a prepare callable. registration_fields are installation_root, optional entry_executable, and game_path only when the installation cannot supply it. Manual mode requires a complete borrowed-game admission/cleanup contract; do not advertise it merely because the UI has a dropdown.

prepare(row) reads the installed entry/version and saved native settings without launching or changing them. It returns the existing descriptor contract used by assistants.py: installation_root, cwd, argv_candidate (an argument list), runner_version, task_configuration, blockers, game_path, observation_targets, game_start, borrowed_game, and a completion_kind when the shared OK observer is unsuitable. Preserve the selected native task preset. The catalog dispatch wraps preparation with an installation drift fingerprint.

Registration is inspect_registration -> user preview -> commit_registration. No inspection may run a native GUI, task or game. Invalid paths fail without saving; unsupported versions/settings can be saved only as disabled candidates. No ready cache bypasses a new run's checks.

If needed, implement bind_execution(current, node_directory) and an observe callback. Add explicit dispatch in assistants.run_assistants for binding and completion observers; adding only a catalog row does not implement those lifecycle paths. For a bridge, use a fixed bundled entry rather than arbitrary argv from user configuration. Keep application resources separate from AppPaths.data_root. Frozen worker resources must be included in GachaOps.spec and tested from a clean package.

## Completion and ownership contract

Associate completion with the current run, owned writer identity including creation time, and bounded fresh output. Preserve partial failures and missing/ambiguous completion. Exit zero, old output, idle UI, a disappearing window or unrelated notifications are insufficient. The synthetic observer demonstrates the shape only: it accepts caller-supplied owned identities and is not a substitute for production Windows Job observations. On non-Windows, the example's creation time is None and must not be advertised as native PID-reuse validation.

The supervisor owns only its Job processes; a manually opened game is borrowed. Do not kill an external process by name or adopt an already running launcher. Unknown identities/exclusivity, cleanup uncertainty or a remaining borrowed game stop the whole chain. New retry/fallback semantics are out of scope; existing explicit repeat_limit defaults to zero and must not repeat successful work.

## Reusable no-game checks

Run `python -m unittest discover -s tests -v`. Relevant layers:

| Contract | Executable tests |
|---|---|
| Array argv, explicit shell=False, Unicode/spaces, run/owned-writer/freshness, success/partial/unknown | adapter_contract/test_synthetic.py |
| Timeout/cancel of only the synthetic child | adapter_contract/test_synthetic.py |
| Global lock, Job trees, parent exit, unrelated-process preservation and cleanup failure | test_windows.py, test_m1.py, test_execution_contract.py |
| Borrowed-game ownership/PID replacement and remaining external game stop | test_manual_game.py |
| Unknown exclusivity, cleanup uncertainty, failed manual preflight stop whole chain | test_assistant_repeats.py |
| Registration without launches, drift before WindowsProcess, schema and capabilities | test_registration.py, test_support_matrix.py, test_adapter_catalog.py |

Reuse these lifecycle assertions for a new integration and add adapter-specific native completion fixtures. Synthetic process tests never open a real installation. Windows-specific tests are skipped on other systems; report that boundary explicitly.

## Promotion

Submit a focused PR with upstream contract evidence, synthetic fixtures, exact supported version policy, support_matrix.json changes and risks. Pass offline core and Qt tests, then Windows no-game process fixtures. Only after explicit user authorization perform a real single-game acceptance, then a serial scenario. Keep version-specific evidence and failures intact; no new adapter is claimed Windows-verified because another game passed. Update docs/VALIDATION.md and the public matrix without private paths or raw evidence.
