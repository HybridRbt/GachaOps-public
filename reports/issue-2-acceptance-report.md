# Issue #2 验收报告：清理隔离恢复在历史进程查询失败后的行为

- 输入：[HybridRbt/GachaOps-public#2](https://github.com/HybridRbt/GachaOps-public/issues/2)（已通读）
- 被验对象：提交 `2e9c358ab479d40ffae250dab434b978f36b2834`（`fix: recheck historical cleanup uncertainty and explain recovery steps`，PR #1 分支顶端）
- 环境：Windows 11 Home 10.0.26200；Python 3.12.10（`%LOCALAPPDATA%\Programs\Python\Python312\python.exe`，
  满足 >=3.11；本机 `python`/`py` 无效，命令以完整解释器路径执行，等价于 issue 中的 `python`）；
  PySide6-Essentials 6.10.2 + PyInstaller 6.22.0 装于临时目录（`pip install --target`），`QT_QPA_PLATFORM=offscreen`
- 检出：独立 worktree 分支 `acceptance/issue-2` @ 2e9c358（main 未动）；测试数据全部在系统临时目录（源码树外）
- 工作区状态：被测代码与 2e9c358 完全一致；仅新增未跟踪的 `reports/` 记录文件，不影响被测代码

## 一、离线回归（issue 接手步骤 1）✅

命令（等价形式，`$PY` = 上述解释器路径，`PYTHONUTF8=1`）：

```sh
$PY -m unittest discover -s tests -p "test_recovery_check.py" -v
# Ran 16 tests in 1.812s — OK（exit 0）
```

issue 要求的四个场景全部通过：

| # | 场景 | 用例（2e9c358 新增） | 结果 |
|---|------|----------------------|------|
| 1 | 历史拒绝访问后确认退出 | `test_historical_access_denial_recovers_only_after_all_fresh_checks` | ok：recovered；PID 222 计入 process_checks 且 live_identity=None；native.exe 与 game.exe 均在重查目标中；原记录字节不变 |
| 2 | 活跃未知 PID | `test_historical_unknown_live_pid_cannot_be_assumed_reused` | ok：blocked；提示含 PID 与「导出诊断包」；journal 保持隔离 |
| 3 | 当前查询失败 | `test_historical_denial_still_requires_current_empty_scan_and_job_proof`（scan/proof 子测） | ok：blocked；journal 不变 |
| 4 | 审计写入失败 | 同上（audit 子测：planned-journal 写入抛 OSError） | ok：blocked；隔离保留 |

其余 11 个用例（含既有 `test_audit_failure_before_commit_keeps_quarantine`、锁占用/状态漂移/外部证据不解除、CLI 不加载助手配置等）全部 ok。

全量兜底（含 Qt UI，offscreen）：

```sh
PYTHONPATH=<gui-deps> QT_QPA_PLATFORM=offscreen $PY -m unittest discover -s tests -v
# Ran 351 tests in 35.880s — OK (skipped=1)（exit 0）
```

- 唯一 skip：`test_windows.PortableTests.test_preflight_unsupported`（non-Windows 行为探针，Windows 上设计内跳过）
- `test_desktop_ui` 29 项全部实跑通过（含 2e9c358 更新的恢复指引断言：原神标签、old-run、管理员、退出、运行情况、检查并恢复运行资格、导出诊断包；且不再出现「可能原因，并非已确认的诊断」）

## 二、代码审阅（issue 接手步骤 2，离线半）✅ 无缺陷，4 条备忘

**恢复前置条件与顺序**（`gachaops/recovery_check.py` @ 2e9c358）：历史记录验证（remaining 结构/归属/唯一性、owned 与 journal 一致、targets 完整）→ 逐 PID 重查（监督进程 + owned + historical + unknown；OSError → InspectionBlocked 带 PID、标签、winerror 5 给管理员提示；unknown 仍活跃一律阻断，不猜测 PID 复用）→ Job 证据（`owned_job_cleanup_verified`）→ 当前全量扫描必须 `{'status':'observed','matches':[],'uncertain':[]}` → journal 漂移复查。与 issue 表述一致。

**审计先于状态提交**（关键项）：

```python
save(evidence / 'fresh-inspection.json', dict(proof, status='passed'))   # 审计
restored = dict(journal, phase='idle', ...)
save(evidence / 'planned-journal.json', restored)                        # 计划态审计
save(state / 'execution.json', restored)                                 # 最后提交
```

任何前步失败（含审计写入 OSError）都落入 except：隔离保持 `quarantined`，证据目录保留 blocked 版 `fresh-inspection.json`，并在提示中引导「导出诊断包」。既有用例 `test_audit_failure_before_commit_keeps_quarantine` 与新增 audit 子测双重覆盖。

**界面入口**（`gachaops/desktop.py` @ 2e9c358）：「运行情况」卡片常驻按钮（:592）与「查看原因」对话框按钮（:1693，`needs_recovery()` 时显示）都走 `begin_recovery()`：未提权只弹「需要管理员权限」不启动核验（:1451-1456）；worker 子进程 `recover-assistants` 写 `recovery.json`，结果弹窗展示（:1820-1834），recovered/already_ready 后恢复定时重查。启动失败解释改用 `recovery_instructions(workspace, NAMES)`（:1894-1896），含隔离 run_id、待核验助手名与分步指引；`cause_unknown` 收窄为 `not error`。QUICKSTART 新节「隔离后恢复运行资格」与实现行为一致（含「不会猜测已被其他进程复用」）。

**备忘（信息级，非缺陷，不阻塞验收）**：
1. uncertain 行的 PID 唯一性只在 uncertain 集合内校验，未与 owned/historical PID 交叉去重——重复时该 PID 会被按两种身份各查一次且都必须通过，安全性不受影响，仅提示可能重复。
2. 若最终状态提交（`execution.json`）本身写失败，except 会用 blocked+error 覆盖已写出的 passed 版 `fresh-inspection.json`——记录语义上合理（恢复未完成），但 passed 证据不保留副本。
3. `recovery_instructions` 在锁外只读 state 与 chain，异常全部吞掉（展示路径，设计如此）。
4. 全量套件在本机 3.12（README 称核心支持 >=3.11）通过；私有 CI 曾针对 3.11 修测试清理（002042c），如需 3.11 矩阵证据以 CI 为准。

## 三、Windows 真实 API 合成演练（D1，本会话）✅

前提检查：本机无默认部署 `%LOCALAPPDATA%\GachaOps`（`--data-root` 不会被既有未收尾部署拒绝）；全程未启动/未停止任何进程，只读进程身份；短暂持有全局锁 `Global\GachaOps.Execution.v1`。工作区构造与合成用例同构（`remaining_processes.status='uncertain'`，uncertain=访问拒绝行 pid+name+error=5，无创建时间）。

| 演练 | 输入 | 结果 |
|---|---|---|
| 活跃未知 PID | uncertain 指向演练进程自身 PID 91704（python.exe） | exit 2 `blocked`：「进程 PID 91704（python.exe）仍在运行或历史身份无法确认，请先退出相关程序。」+ 导出诊断包指引；journal 保持 `quarantined` |
| 确认退出的未知 PID | uncertain 指向 PID 107497（新全量快照证明不存在） | exit 0 `recovered`：journal → `idle`；`fresh-inspection.json` status=passed，该行 `live_identity: null`；当前扫描 `{observed, [], []}` |

（脚本与结果：`reports/evidence/drill.log`、`drill-result.json`；原始提示全文见文件。）

## 四、结论与剩余事项

- **回归通过，成功和阻断路径均符合 issue 条件**（验收项 1 离线部分：达成；三平台 CI 另有共同结果）。
- 代码审阅无缺陷；审计先于状态提交成立；UI 入口与文档一致。
- **剩余（验收项 2）**：维护者按 `reports/issue-2-acceptance-plan.md` 第四节模板做真实 Windows 现场验收（确认目标程序已退出 → 管理员打开同一 GachaOps → 「检查并恢复运行资格」→ 记录版本、最终提示原文、隔离状态），证据回填 issue #2。若阻断：记录原因与下一责任人并保持 issue 开放。
- 本卡未修改任何产品代码；`reports/` 为未跟踪记录文件。若 PR #1 恢复代码后续有新提交，按 sha 补跑第一条回归命令即可。

## 五、证据文件

| 文件 | 内容 |
|---|---|
| `reports/evidence/recovery-tests.log` | issue 指定命令的完整 -v 输出（16/16 OK） |
| `reports/evidence/full-suite.log` | 全量套件输出（351 tests, OK, skipped=1, exit=0） |
| `reports/evidence/drill.log` / `drill-result.json` | 真实 API 演练两场景的完整输出与结构化结果 |
| `reports/evidence/exe-build-manifest.json` / `exe-build.log` | D2 验收构建（2e9c358，pwsh 7）的构建清单与日志 |

## 六、D2 现场发现（2026-11-09 增补，实机）

维护者现场操作中提出两条交互问题，均不涉及恢复路径本身的正确性，记录为改进建议
（已在卡片上提案，另开卡决定）：

1. **管理员权限提示来得晚**：非提权下登记/配置/路径检查均可正常进行，直到点「开始运行」
   才经 UAC 提权重开（`desktop.py:1482-1486` → `elevate()` `desktop.py:1552-1575`）；
   预检警告其实会提前列出该项（`desktop.py:1200-1202`）。安全硬门槛不在界面——隔离态下
   开新运行由 supervisor 以 `RecoveryRequired` 拒绝（`recovery.py:82`），UI 未置灰属引导层
   体验问题。建议：`needs_recovery()` 为真时置灰主动作，只留「以管理员身份重开」。
2. **提权重开新开实例而非替换**：`elevate()` 经 ShellExecuteW `runas` 打开新的提权实例后
   不退出自身（`desktop.py:1572` 提示原文「已打开管理员窗口；请在新窗口中开始运行」），
   出现两个实例并存；在旧窗口再点「开始运行」还会开出第三个。不破坏状态（旧窗口不持有
   全局执行锁；锁由运行中的 supervisor 持有）。恢复路径不受影响：`begin_recovery` 不自动
   提权，仅提示手动以管理员重开（`desktop.py:1490-1491`）。建议：新实例启动后自动退出
   旧窗口或加单实例互斥，并让恢复入口复用一键重开。
3. **版本/身份门真实触发（正向观察）**：现场「启动前检查」两条拦截均按设计生效——
   星铁三月七 v2026.11.8 不在已核验集（`march7.py:24`：v2026.9.30 / v2026.10.16）→
   停止自检并提示「请更新 GachaOps 兼容支持后重新检查」；少前2 MXU 构建身份未经核验
   （`maagf2_mxu.py` guarded adapter，6f5572f）→ 紧急核验节点。同清单显示
   鸣潮/绝区零/终末地/异环/原神已就绪，D2 可用已就绪组合执行，无需这两个被拦助手。
   （三月七新版核验属产品跟进；MXU 构建身份核验归关联卡「增加 MaaGF2 MXU 离线接入」。）

## 七、D2 现场验收结果（2026-11-09/10，实机）✅ 通过

**构建身份**：现场实际运行的 `GachaOps.exe` 来自维护者本机私有开发树的发布构建
（2026-11-04 15:45），其 `build-manifest.json` 记录 commit=`2e9c358ab479…`（即被验提交）、
`dirty=false`、exe sha256 `F6D4C280…A8479B`、python 3.12.14、unsigned、windows-x64
（被验提交公共仓日期 2026-11-04 15:44:20 −0600，构建发生在其后 1 分钟）。
桌面副本 `build/candidates/issue2-d2`（11-08 构建，同 commit）未被该轮使用；
两者同 commit，验收有效性不受影响。

**时间线（当地 = UTC−6）**：

| 时刻 | 事件 |
|------|------|
| 11-09 20:02 | 桌面快捷方式指向 dev 构建并启动 GUI（管理员） |
| 20:04:47 | 运行开始（run_id `c76d8a05…`；6 助手配置，少前2 被版本门禁禁用，5 个实跑） |
| 20:33 / 21:34 / 22:45 / 22:52 | 鸣潮 `completion_unconfirmed`（1726s）→ 绝区零 `timeout`（3600.9s，missing_runner_result）→ 终末地 `partial_failure`（4318.7s）→ 异环 `success`（404.9s，不在需处理列表） |
| 22:52–23:02 | 原神（BetterGI 0.66.0.0）运行 621.2s：原生收尾无法确认（completion=`native_end_unconfirmed`，reason=「原生任务中断、警告或结束记录不足；不能确认全部完成。」，failed=自动秘境：未匹配到任何战斗脚本）；**历史进程查询失败**：`remaining_processes={status:'uncertain', matches:[BetterGI, GenshinImpact], uncertain:[{pid:73388, name:'BetterGI.exe', error:5}]}`（拒绝访问）→ `cleanup_verified=false`（`owned_job_cleanup_verified=true`，Job 归属与全局锁完好） |
| 23:03 | 链收尾 `status=stopped_cleanup_unconfirmed`；journal → `phase=quarantined`（reason=unfinished_execution）；GUI「需处理 4」+ 原神行「进程归属或清理未确认 · 自动脱管」 |
| 11-10 20:56:46 | 维护者确认全部相关程序退出后，在管理员窗口执行「检查并恢复运行资格」→ **核对通过**（详见下）→ journal → `idle` + `recovery_reason`/`recovery_inspection`/`ended_at=核对时刻` |

**恢复核验实测（fresh-inspection.json，status=passed）**：

- 14 项进程身份核验全部满足：监督进程（PID 4824）+ 10 个归属进程 + 2 个历史 matches
  的 `live_identity` 均为 null，或 creation_ticks 与原记录不一致；
- **PID 复用甄别在真实环境生效**：归属进程 PID 70268（原 ZFGameBrowser.exe）在恢复时
  已被 `msedge.exe` 复用，凭 creation_ticks 差异正确判定为不同进程——未按名称处理、
  未做 PID 复用猜测（这正是被验修复的核心行为）；
- 历史不确定行 PID 73388（BetterGI.exe，error=5 那次查询失败的主角）确认已退出
  （`live_identity=null`）；
- 对 BetterGI.exe / GenshinImpact.exe 的当前全量扫描 = `{'observed', [], []}`（无 uncertain
  行，管理员权限下无查询失败）。

**证据核验（本会话只读，未改任何记录、未停止任何进程）**：恢复证据目录 5 文件齐备
（original-journal / original-chain / original-node / fresh-inspection / planned-journal）；
`planned-journal.json` 与提交后的 `state/execution.json` **逐字节一致**（审计先于状态提交
在真实环境复现）；original-* 为隔离原账的字节副本；弹窗与数据逐项吻合（4.png 的
621 秒/第 1 轮/自动秘境/警告列表/「结果未确认的记录」原文 = original-node.json；
5.png = `recovery_check.py:211` recovered 文案）。

**结论**：issue #2 场景（历史进程查询失败 → 清理未确认 → 隔离 → 确认退出后按身份核验
恢复）在真实 Windows 上端到端成立，**D2 现场验收通过**。验收项 2 完成。

**界面残留说明（设计内）**：恢复成功后「运行情况」与上方任务列表仍显示上一轮原神行的
「清理未确认」——运行记录不可变，恢复仅恢复运行资格（弹窗原文亦注明「原失败记录已
保留」），列表展示的是上一轮链报告的原始状态。改进建议已另行提案（恢复成功后在界面
标记「运行资格已恢复」并归档上一轮显示）。

**issue #2 回填用脱敏结论（公开证据只含版本、结果与脱敏原因）**：

> 版本：commit `2e9c358ab479d40ffae250dab434b978f36b2834`（现场运行 exe 的
> build-manifest 为证，dirty=false）。
> 结果：真实运行中原生收尾未确认 + 历史进程查询失败（BetterGI.exe 拒绝访问，error=5）
> → 清理未确认、本轮进入隔离；维护者退出全部相关程序后以管理员执行「检查并恢复运行
> 资格」，14 项进程身份核验通过（含一处 PID 复用被 creation_ticks 正确甄别）、当前全量
> 扫描无残留 → 运行资格恢复，原失败记录保留，证据目录完整。
> 脱敏原因：原神节点「原生任务中断、警告或结束记录不足；不能确认全部完成。」
> （助手输出细节不公开。）
