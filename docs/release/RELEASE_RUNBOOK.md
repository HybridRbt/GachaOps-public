# 候选包构建、审查与交付

公开 v1 尚未发布。干净002042c/Python3.12.14本地候选及其确切源码/材料已获维护者限范围批准；两份正式批准记录适用于该候选。DISTRIBUTION_REVIEW_PROPOSAL保持false，是不可变的原提交审阅稿，不代表正式记录失效，也不代表公开已获批准。旧提案和历史批准仅保留当时范围。

## 构建与产物审查分开

使用干净提交和 requirements-desktop.txt 的固定依赖。Build-Desktop.ps1 的输出必须是新目录；完整保留 GachaOps/ 目录和 build-manifest.json。新构建的 commit/EXE 及所有 runtime 字节形成独立身份，不能继承旧 policy 或材料批准。不为嵌入批准而重新构建已审 EXE。

先跑核心/Qt、兼容矩阵、源码隐私检查及无游戏 smoke，再保存确切构建产物供审查。私有 build.log 和现场原始证据不随公共包交付。本地测试不替代远端 CI 或完整 Windows 验收；CI 额度受限时只做离线验证。

## 必要材料和包布局

源码归档清单在 source-delivery/SOURCE_BUNDLE_MANIFEST.json，实际交付须包含全部六份归档，不只提供 URL。使用 prepare_release_materials.py 从已核对的本地缓存按完整摘要恢复归档；缺失、长度或摘要变化即停止，不联网补齐。

必要文档由 stage_release.py 的 REQUIRED 定义，源路径与交付目标一起审查。docs/QUICKSTART.md 保持 docs 目录布局；使用声明、数据目录说明、兼容矩阵、贡献接口和安全说明直接可读，根 README 指向快速开始。第三方原文放 THIRD_PARTY_LICENSES，源码与替换材料放 SOURCE_DELIVERY。普通使用者无需先解压应用源码 ZIP 才能阅读声明。

新材料摘要、源码/目标映射和实际 app 身份须独立审查。维护者已确认微软 DLL 下游条款方案 A 及具体文案，本地候选已接入；实际公开展示、远端 CI 及公开决定继续独立保留，不因本地技术验证通过就写整体许可已清关。应用 AGPL 权利与第三方独立许可分别保留。

应用源码必须匹配当前候选的提交与EXE身份：APPLICATION_SOURCE_DELIVERY、应用源码ZIP内SOURCE_MANIFEST及其逐项Git输入摘要相互对应。仅归档自身摘要不变，不能证明它适用于新构建；缺失、旧提交、新旧EXE身份错配或缺少必要构建输入均阻断。

## 暂存和 ZIP

`python scripts/stage_release.py --source <完整已审材料目录> --app-dir <已审app> --output <新目录> --binary-dispositions <精确已审policy>`

暂存前冻结完整 app 文件集合/摘要、必要说明、分发材料和精确 binary policy；检查源码声明隐私及实际包内相对链接。暂存复制后再次检查全部字节、文件集合及 binary raw/accepted/unresolved，最后生成 checksums.json。任何变化阻断；失败目录只是私有诊断，不是可用候选。ZIP 另核对 CRC、无重复 entry、完整 entry 集合及每项 SHA256。新材料未获批准时，实际暂存应失败，不使用合成批准代替真实批准。

## 发布门禁

发布必须默认关闭。需要实际来源提交、匹配版本的既有 tag、确切产物身份、完整材料、已通过的验收、PUBLIC_RELEASE_ENABLED 及受保护 public-release 环境和明确维护者决定。环境必须预先配置人工审核。未实际远端运行，不宣称远端通过。新私有仓库首推前关闭 Actions，启用需另有执行范围。

不得自动推送共享分支、改仓库可见性、重写旧历史、发 tag/Release、启用 Pages 或运行真实助手/游戏。公开源码规则见[公开范围](../PUBLIC_SOURCE.md)，验证覆盖见[验收矩阵](../VALIDATION.md)。

## 三次手动调度

1. phase=build：只构建并上传 GachaOps-unreviewed-build，其中 build/GachaOps/ 是确切 app，review-input.json 默认 approved=false。完整 app 文件集合及 build-manifest 都纳入审查；记录 run ID 并在30天过期前留存。
2. 维护者审查这组产物、源码归档及材料，分别准备精确 BINARY_DISPOSITIONS.json、DISTRIBUTION_APPROVAL.json 和 CANDIDATE_REVIEW.json。CANDIDATE_REVIEW 以 capture 输出为模板，app_files 不得重算后自动接受；sidecar_sha256 为 distribution_approval、source_bundle_manifest、binary_dispositions 原始文件的 SHA256，真正无 policy 时才可填 null。scope 固定 local_candidate_only，只有维护者批准后才 approved=true。审核必须包含代码提交/EXE、完整字节、原始命中、源码对应、变更说明和未完成发布条件。JSON 是经审阅的记录，不是数字签名。
3. phase=materials：从已审材料提交准备 GachaOps-reviewed-materials。该提交必须已经提供清单中的全部六归档（包括对应本次 build 的应用源码 ZIP）；工具不联网恢复、不替换旧包。大归档不宜放产品常规分支，可用专用私有材料分支。旧9e归档不能冒充新build对应源码。归档缺失、摘要错误或审批字段不齐即失败，维护者须先补齐确切输入；此阶段不重新构建 EXE。
4. phase=stage：选择含 CANDIDATE_REVIEW.json 的审查提交，显式输入 build_run_id 和 materials_run_id。下载这两次的确切 artifact；重新校验完整 app、三 sidecar、全部归档和实际暂存，再生成 ZIP 并全 entry 校验。不会调用 Build-Desktop，也不从‘最新运行’猜测输入。
5. 只有本轮 stage 成功才可能进入默认关闭的 publish。既有 tag 指向实际 build 提交，版本与包内 manifest、审查记录、pyproject 一致。candidate-review 和 public-release 环境须设置审核人；PUBLIC_RELEASE_ENABLED 未明确启用时不发布。

本地等效命令：

```sh
python scripts/release_candidate.py capture --app-dir <app> --output <新待审JSON>
python scripts/release_candidate.py finalize --source <已审材料源码树> --app-dir <原app> --review <维护者已审JSON> --archive-dir <本地归档缓存> --output <新输出目录>
python scripts/release_candidate.py verify --zip <输出>/GachaOps-candidate.zip --summary <输出>/release-summary.json
```

材料含深层上游文件，Windows 未启用长路径时应使用较短的工作/输出路径。路径写入失败会阻断，保留诊断目录且不得当作成功包。Actions 的显式 run-id 下载接口见[官方文档](https://github.com/actions/download-artifact#download-artifacts-from-other-workflow-runs-or-repositories)。三平台core-tests与Windows build已实际远端通过；materials/stage/publish未远端执行。云端3.12.10构建不替代已审3.12.14本地候选。


## 本轮发布提案的执行方式

待维护者明确批准的提案是：不重建已审本地ZIP，以人工上传该确切产物发布v0.1.0预发布；候选代码tag指向002042c，后续材料同步提交只更新元数据。此方式需维护者明确选择，不能冒充云端stage/publish通过。上述云端自动发布门禁保持关闭，不修改受保护环境或PUBLIC_RELEASE_ENABLED。公开决定和执行结果另行记录；尚未执行公开、tag或Release。
