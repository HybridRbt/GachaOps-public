# 第三方组件与许可证告知

GachaOps 自有代码和 OneDragon 桥接采用 AGPL-3.0-only，copyright HybridRbt。第三方文件保留各自许可证。完整原文在 THIRD_PARTY_LICENSES/，源码与替换说明在 SOURCE_DELIVERY/。

| 组件 | 版本与用途 | 许可和材料 |
|---|---|---|
| CPython | 3.12.14，冻结运行时 | PSF 及所含组件许可；installed/CPython-3.12.14/LICENSE.txt 与上游保守告知全集 |
| Qt / PySide6-Essentials / Shiboken6 | 6.10.2，动态库、插件、翻译、绑定 | 本包采用 LGPLv3 路径，附 GPLv3/LGPLv3 原文与版本源码；Qt Copyright The Qt Company Ltd. and other contributors，逐组件年份及作者见原文 |
| Mesa / LLVM 软件 OpenGL | Qt 供应方运行库 | MIT、Boost、LLVM/NCSA 及所含组件原始告知；THIRD_PARTY_LICENSES/upstream/mesa-11.2.2/ 与 THIRD_PARTY_LICENSES/upstream/llvm-3.6.2.src/ 保存许可 |
| PyInstaller | 6.22.0，bootloader 与打包工具 | GPLv2-or-later，含 bootloader exception；完整 COPYING.txt 在 installed/pyinstaller-6.22.0.dist-info/ |
| Microsoft Visual C++ CRT | x64 14.44.35211.0，五个 DLL | Microsoft 独立条款；Community 2022 VC Redist 未改原文件；原始条款及来源在 Microsoft/，不重新许可为 AGPL |
| orbital-ring brand 03 | 程序标识 | 项目自有作品，AGPL-3.0-only |
| packaging、altgraph、pefile、pywin32-ctypes、hooks-contrib、setuptools | 构建环境参考 | 附安装版本原始许可，不表示全部编入运行时 |

允许为调试用户对 LGPL 库的修改而进行逆向工程。本项目允许修改应用、替换兼容动态库和运行修改版本，不要求签名密钥或在线批准。六份源码归档和摘要见 SOURCE_DELIVERY/SOURCE_BUNDLE_MANIFEST.json；替换及重建见同目录“源码与替换交付说明.md”。许可全集保守包含可选和仅构建组件，不是精确编译 SBOM。154 文件及构建后 CRT 替换摘要见 RUNTIME_MANIFEST.json。七张权利未明的游戏 PNG 不随包，界面使用文字后备显示。

## 外部助手

本版只登记用户自行安装的助手及官方入口，不下载、安装或交付助手本体、依赖及游戏素材。外部助手继续适用其许可证。自有桥接源码随包，不复制上游原生任务实现。

OneDragon 桥接在用户安装的助手环境中导入 GPLv3 代码。GPLv3 第 13 节允许与 AGPLv3 作品组合，GPL 部分保留相应条款，组合整体适用 AGPL 的网络交互要求。不能仅凭另起进程推定没有组合义务。另行分发修改助手时须履行该助手的分发义务。

OK-NTE 仓库 GPLv3、网站页脚 AGPLv3 的差异保留在上游核查记录中。本包未交付该助手，此差异不作为无关文件的发布前置。以后捆绑时重新评估。

参考：[Qt LGPLv3](https://doc.qt.io/qt-6.10/lgpl.html)、[GPLv3 第 13 节](https://doc.qt.io/qt-6.10/gpl.html)、[微软分发文件清单](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution)。原始条款优先于本说明。
