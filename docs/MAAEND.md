<!-- gachaops-public-evidence-v1 -->
# MaaEnd：公开契约与证据边界

支持 v2.34.0 LaunchGamePC 及明确的 Global/Auto 默认安装布局；自定义或不明确的 launcher 路径阻断。新生命周期尚未完成真实运行验收，手动模式保留借用游戏边界。

此页是公开导出的接口摘要，不复制内部会话、宿主盘点或账号证据。版本和证据等级以[兼容矩阵](SUPPORT_MATRIX.md)及程序内原始检查为准，不改写其证据日期。

离线验证使用合成 fixture，入口为 tests/test_endfield_native_start.py、tests/test_manual_game.py；完整命令 `python -m unittest discover -s tests -v`。离线测试和历史链路不代表当前版本的实机验收。

保留全局锁、共享 journal、Job 所有权及 borrowed_game；完成、排他或清理不确定停止全链。真实助手、游戏及账号操作必须有对应授权。新增适配器见[开发契约](ADDING_ADAPTER.md)。
