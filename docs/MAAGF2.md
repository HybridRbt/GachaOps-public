<!-- gachaops-public-evidence-v1 -->
# MaaGF2Exilium：公开契约与证据边界

版本范围以 v2.7.3 / MFAAvalonia v2.16.2 及实际 Core 摘要检查为准。原生日常任务失败如实保留；关键回调损坏保持 completion_unconfirmed，不代填账号来源或凭据。

入口按实际 ProductName 分流。MFAAvalonia 继续使用已核验 Core 摘要与旧实例契约；MXU 不读取残留 MFA 配置。缺失、未知或冲突身份均阻止运行。2026-11-01 的身份保护只证明旧版阻断，不能当作后来新增的 MXU 日常验收。

## 精确 MXU 接入范围

后续源码新增 GF2 资源 v2.8.3 / MXU 框架 v2.8.1 的离线契约。两者版本维度不同，不能只看目录名或任一版本字符串放行。已从[官方 GF2 v2.8.3 归档](https://github.com/DarkLingYun/MaaGF2Exilium/releases/tag/v2.8.3)核对主程序、MaaFramework、Python 入口及 agent 主脚本的完整摘要；CLI/配置/日志协议依据 [MXU v2.8.1 固定源码](https://github.com/MaaXYZ/MXU/tree/dbce2f2e124a67d5df9e481730e801fd1dfe2469)。其他 MXU 字节及 v2.8.0 继续阻断。

仅支持原助手按 lastActiveInstanceId 运行最近选中的 PC 实例（保存多个实例不阻断，重复名称/ID 仍拒绝）和唯一直接游戏 EXE 前动作，不新增手动开局。请在原助手中先选中要运行的实例，再保存并退出；GachaOps 启动的就是该实例。启动使用数组参数 `--autostart --instance <实例名称> --quit-after-run`，不改任务选项、不去重原生任务名称。启动前重新核对配置、接口导入、Framework/Python/agent、任务/选项定义、资源及游戏字节；新增、缺失、变化或重解析点均拒绝。完整检查可能比旧版更慢；检查期间取消仍须在创建进程前生效。

须在原助手处理以下阻碍后保存并退出，再回到 GachaOps 重新检查：

- 启用的 MXU 按名称结束进程、外部程序、关机及其他未核验特殊任务。名称杀进程不具有本轮 PID/路径/Job 所有权边界；不把它当作安全收尾。
- 任意实例的启用定时策略、Web 控制、全局热键或独立自动运行入口。GachaOps 不代替用户更改这些设置。
- 重复实例名称/ID、缺失活动实例、错误控制器/PC资源、窗口或游戏路径不匹配、非直接启动/附加参数以及已有助手或游戏进程。

本轮结果要求本轮日志偏移/前缀、准确 root PID/创建时间/镜像和 Job 所有权、唯一实例提交批次、每项 Starting 与完整终态、原生退出标记及正常退出共同闭合。只接受已观测的安装内 Python 作为辅助 writer，其回调不算 GUI 批次完成。损坏/旧日志、未知 writer、PID 复用或不完整批次均保持未确认；取消和超时仍由监督器优先保留。

当前证据包括合成回归和实际安装的只读准备/漂移复验，没有启动原生程序或游戏。真实自动开始、游戏 Job 继承、正常结束、关闭及串行交接仍未验收。**已发布 v0.1.0 ZIP 不包含这次 MXU 实现**；新二进制必须重新构建、审阅并绑定材料，不能继承旧包批准。

此页是公开导出的接口摘要，不复制内部会话、宿主盘点或账号证据。版本和证据等级以[兼容矩阵](SUPPORT_MATRIX.md)及程序内原始检查为准，不改写其证据日期。

离线验证使用合成 fixture，入口为 tests/test_maagf2_mxu.py、tests/test_maagf2_corrupt_logs.py、tests/test_maagf2.py；完整命令 `python -m unittest discover -s tests -v`。离线测试和历史链路不代表当前版本的实机验收。

保留全局锁、共享 journal、Job 所有权及 borrowed_game；完成、排他或清理不确定停止全链。真实助手、游戏及账号操作必须有对应授权。新增适配器见[开发契约](ADDING_ADAPTER.md)。
