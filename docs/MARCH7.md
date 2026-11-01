<!-- gachaops-public-evidence-v1 -->
# 三月七助手：公开契约与证据边界

已登记兼容范围为 v2026.9.30、v2026.10.16；版本/配置摘要变化仍阻断。子启动链需确切身份和完成协议，新版本自然日常验收仍单独保留。

此页是公开导出的接口摘要，不复制内部会话、宿主盘点或账号证据。版本和证据等级以[兼容矩阵](SUPPORT_MATRIX.md)及程序内原始检查为准，不改写其证据日期。

离线验证使用合成 fixture，入口为 tests/test_march7.py、tests/test_registration.py；完整命令 `python -m unittest discover -s tests -v`。离线测试和历史链路不代表当前版本的实机验收。

保留全局锁、共享 journal、Job 所有权及 borrowed_game；完成、排他或清理不确定停止全链。真实助手、游戏及账号操作必须有对应授权。新增适配器见[开发契约](ADDING_ADAPTER.md)。
