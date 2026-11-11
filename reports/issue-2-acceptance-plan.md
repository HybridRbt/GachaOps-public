# Issue #2 验收方案：清理隔离恢复在历史进程查询失败后的行为

- 卡片：验收清理隔离恢复在历史进程查询失败后的行为（research）
- 输入：[HybridRbt/GachaOps-public#2](https://github.com/HybridRbt/GachaOps-public/issues/2)
- 审阅对象：提交 `2e9c358`（`fix: recheck historical cleanup uncertainty and explain recovery steps`，PR #1 分支 `codex/maintenance-active-20261104` 顶端；`main@50b55bd` 是其祖先）
- 环境：本机 Windows 11，Python 3.12.10（`%LOCALAPPDATA%\Programs\Python\Python312\python.exe`，满足 >=3.11）；独立 worktree 检出 + 独立临时测试数据目录

## 一、通读结论（修复在做什么）

修复前的 `recover_workspace` 要求 `remaining_processes.status == 'observed' 且 uncertain == []`，
即历史上任何一次进程访问拒绝（只留下 PID+文件名+错误码的 uncertain 记录）都会在重新查询之前
永久阻断恢复。修复后：

1. 允许格式完好且归属可核验的历史 `uncertain` 记录进入核验（`gachaops/recovery_check.py`，
   `remaining` 校验放宽为 `status ∈ {observed, uncertain}` 且与 uncertain 非空一致）。
2. 对 uncertain 记录做严格结构校验：pid 为正整数、name 为纯文件名且 casefold 后命中本轮
   目标/归属/历史镜像名集合、error 为正错误码或非空字符串、PID 不重复。
3. 重新查询全部 PID（监督进程 + owned + historical + unknown）：查询抛 OSError 时以
   InspectionBlocked 阻断并给出 PID、标签与动作提示（winerror 5 → 管理员重开）；PID 仍活跃时，
   已知身份要求 creation_ticks 变化（确认是复用），**未知身份一律不猜测复用**。
4. 仍要求：原 Job 清理证据（`owned_job_cleanup_verified`、ownership 与 journal 一致）、
   当前全量扫描无 matches/uncertain、journal 未漂移。
5. 顺序不变：审计先写（`fresh-inspection.json` passed → `planned-journal.json`），
   **最后**才提交 `state/execution.json`；任何一步失败保持隔离并落 blocked 证据。
6. 新增 `recovery_instructions()`：只读生成含隔离 run_id、待核验助手名的分步指引，
   `desktop.py` 启动失败解释改用它；QUICKSTART 新增「隔离后恢复运行资格」一节。

Issue 四个场景 → 2e9c358 合成用例映射：

| # | 场景 | 用例 | 预期 |
|---|------|------|------|
| 1 | 历史拒绝访问后确认退出 | `test_historical_access_denial_recovers_only_after_all_fresh_checks` | recovered；PID 222 出现在 process_checks 且 live_identity 为 None；原记录字节不变 |
| 2 | 活跃未知 PID | `test_historical_unknown_live_pid_cannot_be_assumed_reused` | blocked；提示含 PID 与「导出诊断包」；journal 不变 |
| 3 | 当前查询失败 | `test_historical_denial_still_requires_current_empty_scan_and_job_proof`（scan/proof 子测） | blocked；journal 不变 |
| 4 | 审计写入失败 | 同上（audit 子测）+ 既有 `test_audit_failure_before_commit_keeps_quarantine` | blocked；隔离保留 |

另覆盖：当前权限错误指明 PID 与管理员动作（`test_historical_unknown_current_permission_error_identifies_pid_and_action`）、
畸形/无关 uncertain 记录不得被丢弃（`test_malformed_or_unrelated_historical_unknown_cannot_be_dropped`）。

## 二、执行方案

### A. 环境准备（不改任何产品代码）
- A1 在本 worktree 从 `2e9c358` 建本地分支（如 `acceptance/issue-2`）检出，main 分支不动。
- A2 测试数据目录用系统临时目录（源码树外），`PYTHONUTF8=1`（README 对中文路径的要求）。
- A3 记录解释器版本与 HEAD sha。

### B. 离线回归（issue 接手步骤 1）
- B1 指定命令：`python -m unittest discover -s tests -p "test_recovery_check.py" -v`
- B2 全量套件兜底：`python -m unittest discover -s tests -v`（Qt 用例无 PySide6 时按设计跳过，如实记录）
- 通过标准：全部通过；失败则如实记录并在报告中定位（不擅自改产品代码，先报告）。

### C. 代码审阅（issue 接手步骤 2，离线半）
- C1 按「一、通读结论」逐条对照 2e9c358 源码复核，重点：
  - 恢复前置条件顺序与 issue 表述一致（历史记录验证 → 旧 PID 与本轮目标重查 → Job 证据 → 当前扫描 → 审计）；
  - 审计写入严格先于状态提交（`recovery_check.py` 末段 save 顺序）；
  - 阻断路径绝不写 idle、保留 blocked 证据目录；
  - UI 入口：`desktop.py` 「运行情况」卡片与「查看原因」对话框的 `检查并恢复运行资格`、
    提权门槛（未提权只提示不启动核验）、worker 子进程 `recover-assistants`、结果弹窗与恢复后重查。
- C2 审阅中发现的问题只记录为 findings（含定位与影响），是否修复由维护者另开卡决定。
- C3 核对 QUICKSTART 新节与实现行为一致。

### D. Windows 现场验收
- D1（本会话可做）合成现场演练：在临时 `--data-root` 构造与 fixture 同构的隔离工作区，
  运行 `python -m gachaops recover-assistants --data-root <临时目录> --output <临时目录>`，
  走真实 ctypes 查询路径（假 PID → 快照确认不存在 → 视为已退出 → recovered）。
  前提：本机默认部署 `%LOCALAPPDATA%\GachaOps` 若存在未收尾隔离，`--data-root` 会被
  `app_paths` 拒绝——此时不做演练、不动真实工作区，仅如实记录（这正是 issue 要维护者现场做的场景）。
  安全性：全程不启动/不停止任何进程；短暂持有全局命名锁 `Global\GachaOps.Execution.v1`
  （若真实运行中会得到 ExecutionBusy，属预期）；只写临时目录。
- D2（维护者执行）真实现场验收，按 issue：确认目标程序已退出 → 管理员打开同一 GachaOps →
  「检查并恢复运行资格」→ 记录版本、最终提示原文、隔离状态。证据模板见「四」。

### E. 交付
- E1 汇总为验收报告 `reports/issue-2-acceptance-report.md`（本目录），含：环境与提交、
  B/C/D1 实测输出摘要、findings、现场模板与维护者待办。
- E2 卡片 workpad 记录各检查实测状态与 sha；结果通过卡片与回复交付，不在本卡直接改
  GitHub issue（Flowly 只读外部条目）。

## 三、安全边界（全程遵守，来自 issue 与 README）
- 保持全局锁 `Global\GachaOps.Execution.v1`、共享 journal、Windows Job 归属与 borrowed_game 边界；
- 禁止手工改运行记录、禁止按进程名杀进程；
- 恢复核验只读进程身份，绝不启动或停止游戏；
- 公开证据只保留版本、结果与脱敏原因（PID/路径按需脱敏）。

## 四、维护者现场验收模板（D2 用）

| 字段 | 记录 |
|---|---|
| 构建版本/提交 | （如 v0.1.0 候选 / 2e9c358） |
| 隔离起因（脱敏） | （如：收尾扫描一次访问拒绝，PID 已退出） |
| 操作 | 退出目标程序 → 管理员重开 → 检查并恢复运行资格 |
| 最终提示原文 | （复制弹窗全文） |
| 恢复前/后隔离状态 | quarantined → idle（recovery-status 或界面） |
| 结果 | 恢复成功 / 阻断（原因 + 下一责任人，issue 保持开放） |

## 五、风险与开放点
- PySide6 未安装时 UI 用例跳过：UI 入口结论以代码审阅 + （可行时）pip 安装 PySide6 后补跑为准；
- 本机若存在真实未收尾部署，D1 受限（见上），不绕过；
- 审阅若发现缺陷：仅记录与建议，不顺手修改（本卡为 research 验收卡）。
