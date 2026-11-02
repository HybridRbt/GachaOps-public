# GachaOps

在 Windows 交互桌面上串行编排用户已经安装并配置好的游戏助手：登记路径 → 只读检查 → 选择顺序 → 用户开始 → 完成判定与收尾 → 结果与脱敏诊断。

**v0.1.0 预发布版。** 本产品开源免费，采用 [GNU AGPLv3](LICENSE)（AGPL-3.0-only），Copyright (C) 2026 HybridRbt。首次公开预发布保留已知验收限制；不宣称全部 Windows 环境及真实助手版本均已验收。不会自动下载、安装、更新助手，也不会在打开界面或登记时运行游戏。

## 微软组件使用前告知

Windows 二进制包包含微软提供的五个 Visual C++ 运行库 DLL。它们具有独立许可。请在使用这些 DLL 或再分发含这些 DLL 的包前，阅读《微软 CRT 组件条款》及随包微软原文。本条款仅涉及所列微软文件，不改变 GachaOps 的 AGPLv3 权利。源码仓库不交付这些 DLL；以下告知适用于包含这些 DLL 的二进制包。

在取得可阅读的完整条款后，继续使用上述微软 DLL，表示接受仅适用于这些 DLL 的组件条款；再分发含这些 DLL 的包，还须履行其中分发者的承接要求。如果不同意，请勿使用或再分发这些 DLL。你仍可依 AGPLv3 获取、修改和分发 GachaOps 自有源码。

[微软 CRT 组件条款](MICROSOFT_CRT_TERMS.md)

## 使用

先在各原生助手 GUI 保存任务与结束设置，再退出已有实例。候选包需要完整解压，包括 `_internal`；GUI 首启创建七项未登记、未勾选的默认组合。使用“全部 → 去接入”选择助手安装目录和必要入口/游戏路径，读取预览、处理阻碍，再选择并保存组合。支持新建、改名、删除、显式导入和变更备份。

[快速开始](docs/QUICKSTART.md) · [七助手兼容矩阵](docs/SUPPORT_MATRIX.md) · [数据目录与迁移](docs/DATA_DIRECTORIES.md) · [主页源码](docs/index.html)

## 使用与第三方声明

GachaOps 与各游戏运营方及助手团队没有官方隶属或背书关系。自动化与模拟操作可能违反游戏协议并导致账号处罚；开源或视觉识别不保证免封。点击开始会执行原助手已保存的预设，可能产生资源消耗或其他账号变更，停止不能撤销已发生的操作。请在运行前确认原生任务及兼容要求。

使用者须自行了解并承担使用 GachaOps 导致的账号问题及后果。**使用本软件产生的任何后果均与开发者无关；开发者拥有本项目的最终解释权。本产品开源免费，对使用者的金钱损失不负责。** 不对第三方商用或售卖行为作授权裁定。

各助手由用户自行安装，分别遵循其许可证和隐私规则；GachaOps 不捆绑助手安装包。被调用助手自身的信息采集、加密、遥测及服务商承诺归属于助手及其提供方，GachaOps 不代为承诺，也不承担连带责任。上述声明不改变 AGPLv3 已授予的权利，不排除适用法律不可排除的责任。请阅读[完整声明](docs/USAGE_NOTICE.md)和[七助手许可核查](docs/THIRD_PARTY_ASSISTANTS.md)。第三方桥接与分发义务仍须按实际版本及包内容核查。

七个适配器已实现并有无游戏回归：OK-WW、ZZZ-OneDragon、MaaEnd、MaaGF2、OK-NTE、三月七、BetterGI。手动开局仅 OK-WW/MaaEnd。具体版本、配置契约和限制以矩阵及适配器检查为准；历史六游戏验收不覆盖当前更新或 BetterGI。原生日常失败和完成不确定均保留，不因 exit0 改称成功。

源码开发默认使用仓库数据；显式旧绑定保持权威；无绑定独立 EXE 使用 `%LOCALAPPDATA%/GachaOps`。打开 GUI 不要求管理员；请求运行可触发 UAC，取消不开始任务，提权后的窗口也须用户再次开始。迁移必须显式选择、预览，并通过旧 journal/全局锁/文件摘要检查；不通过新目录绕过隔离。

## 源码入口

目标源码仓库：[HybridRbt/GachaOps-public](https://github.com/HybridRbt/GachaOps-public)。预发布与校验文件见 [v0.1.0 发布页](https://github.com/HybridRbt/GachaOps-public/releases/tag/v0.1.0)。下载前阅读上方微软 CRT 告知与完整条款；对应源码归档及第三方源码随包提供。外部助手安装入口仍以各上游项目为准。

```sh
git clone --branch main https://github.com/HybridRbt/GachaOps-public.git
cd GachaOps-public
```

该仓库从审阅后的独立根开始，不包含原私有仓库祖先。开发前阅读[公开源码范围](docs/PUBLIC_SOURCE.md)、[贡献指南](CONTRIBUTING.md)和[适配器契约](docs/ADDING_ADAPTER.md)；源码快照不等于既有 EXE 的构建提交。已有 checkout 先检查本地改动，勿覆盖工作区。

## 开发与验证

Python >=3.11，核心仅标准库；Qt/打包依赖见 requirements-desktop.txt。在仓库根目录：

```sh
python -m unittest discover -s tests -v
python scripts/check_support_matrix.py
python -m gachaops check-assistants --config config/assistants.example.json
python -m gachaops doctor
```

Windows 中文路径测试使用 UTF-8 模式，TEMP 指向源码树外可写目录。Qt fixture 必须注明实际执行或跳过；Linux/macOS仅验证适用核心，不代表可运行游戏。发行构建、日志/校验和及签名状态见 [发布手册](docs/release/RELEASE_RUNBOOK.md)。当前候选未签名，不代表已受 Windows 信任。

任务内容由用户在原助手中管理。保持 `Global\GachaOps.Execution.v1`、共享 journal、Windows Job 所有权及 borrowed_game 边界；归属、排他或收尾不确定停止全链，不按进程名杀进程。重复轮次默认0，只有用户显式设置时生效；不自动重试成功项或切换备用助手。战斗、抽卡、强化、账号切换和原生配置消费不属于编排器。

[贡献指南](CONTRIBUTING.md) · [新增适配器](docs/ADDING_ADAPTER.md) · [安全报告](SECURITY.md) · [第三方清单](THIRD_PARTY_NOTICES.md)

私有历史和账号证据不进入公共交付；旧仓库不能直接切公开。仅从审阅后的独立脱敏源码/历史准备候选，最终公开、tag/Release及网站托管均需维护者决定。
