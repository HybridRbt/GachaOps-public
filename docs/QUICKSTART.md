# GachaOps quick start

Public v1 is **not released yet**. A local engineering candidate or CI artifact is not a signed, approved public release. Windows x64 with an unlocked interactive desktop is required for real execution. The GUI opens without elevation; requesting a run can ask for administrator rights, and the elevated window still requires you to start it.

GachaOps is free of charge and licensed under [GNU AGPLv3](../LICENSE) (AGPL-3.0-only), Copyright (C) 2026 HybridRbt. The full [usage notice](USAGE_NOTICE.md) contains the maintainer's account-risk, financial-loss, disclaimer, interpretation and third-party privacy statements. These statements do not change AGPL rights or exclude responsibilities that applicable law makes mandatory.

Read the [usage, risk and third-party notice](USAGE_NOTICE.md) before starting. GachaOps is independent of game operators and assistant teams. Automation may lead to account penalties; open source or visual recognition does not guarantee safety. Starting runs the native presets you selected, including any configured resource spending or account changes. Stopping cannot undo actions already performed. Upstream licenses and privacy rules apply separately; see the [seven-assistant audit](THIRD_PARTY_ASSISTANTS.md).

1. Install and configure a supported assistant using its official upstream instructions. GachaOps does not download, install or update assistants. In each native GUI choose the tasks and completion behavior you want, save, and exit existing assistant instances.
2. For an approved candidate, extract the whole directory including `_internal`. Verify checksums.json. Do not copy only the EXE. Current local candidates are unsigned; verify origin and hashes. No Python/source installation should be required for the frozen app, but clean-machine acceptance is still a release gate.
3. Open GachaOps. A new data directory creates a default combination with seven disabled, unregistered assistants. Nothing starts. Choose “全部” to see them, then “去接入” and select the installed assistant directory and any required entry/game EXE. The official-install button opens documentation only.
4. Read the registration preview. Invalid paths are not saved; unsupported versions/native settings can be saved as a disabled candidate. Resolve the specific message in the native assistant or environment, then recheck. Do not guess account fields or bypass compatibility/cleanup isolation.
5. Select assistants and order, enter time limits, and save a combination. New/rename/delete and explicit import are available; imports show a preview and never start tasks. Deleting a combination requires confirmation and permanently removes only that combination's configuration; it creates no deletion backup. Existing edit backups are retained. Deleting the last combination creates an empty default. Existing configurations are not automatically scanned or overwritten. Repeat limit defaults to zero and remains an explicit user choice.
6. Run the read-only check. If an assistant supports manual game entry, first follow its native preparation instructions and enter the game yourself. A borrowed game is not owned by the GachaOps Job; uncertainty or a game remaining open stops the next game.
7. Only when you intend real account activity, click start. Keep the desktop available. A UAC cancellation or failed elevation does not start the chain. Stop requests clean up only owned processes; never manually delete the journal to bypass an unconfirmed cleanup.
8. Inspect per-game results. `partial_failure` and `completion_unconfirmed` remain distinct from success. “查看原因” offers a diagnostic preview and explicit ZIP save; it does not upload. Raw native logs/screenshots/configuration are excluded from the default diagnostic.

## 隔离后恢复运行资格

“上一轮清理未确认”表示上次运行的进程或清理证据仍待核验，当前轮没有启动。关闭、重开或更新程序不会自动解除隔离。

1. 退出上一轮相关助手、游戏和启动器。
2. 以管理员权限打开同一个 GachaOps。
3. 在“运行情况”或“查看原因”点击“检查并恢复运行资格”。程序负责核对进程身份和清理证据，不启动、停止或重试游戏。
4. 显示恢复成功后，再手动开始运行。

若核验失败，按显示的 PID、路径或权限提示处理后再检查。历史扫描因拒绝访问只保存了 PID 和文件名时，程序会重新查询；只有确认该 PID 已退出，且原 Job 清理证明、其他历史进程身份、当前完整扫描和审计保存均通过，才恢复资格。没有历史创建时间且 PID 仍活跃时，不会猜测它已被其他进程复用。

若提示历史证据缺失或身份无法确认，或处理后仍失败，在“查看原因”点击“导出诊断包”，预览后保存并交给维护者核验。无需反复点击开始，不必自行分析日志；不要删除或修改隔离记录。

See [compatibility](SUPPORT_MATRIX.md), [data directories/migration](DATA_DIRECTORIES.md), [adapter contributions](ADDING_ADAPTER.md), [third-party inventory](../THIRD_PARTY_NOTICES.md) and [security reporting](../SECURITY.md). The historical six-game chain does not prove current-version or BetterGI live acceptance.
