# Skill 安装长时间不结束：运行记录与源码核查

日期：2026-09-04。证据来自用户提供的运行记录、当前 checkout 和离线契约测试。

后续升级：本文记录第一阶段修复。完整目录安装和免重启加载现已由
[原生 Skill 安装升级](2026-09-04-native-skill-installation.md) 接续；下文中的“逐文件下载、重启发现”
是第一阶段交付时的限制，不再代表当前 `install_skill` 的行为。

## 结论

这次记录支持“工具能力缺口导致模型承担文件传输，文本修改验证 bug 又放大了重试”的判断，
不支持“GitHub 下载本身卡住一个小时”或“完成门禁无限重试”的判断。记录没有逐请求时间戳，
不能据此精确分解用户报告的一小时耗时，也不能推断 Provider 内部行为。

## 已确认的因果链

1. 用户只要求从 GitHub 安装 Skill。`SkillLoader` 已支持项目 `.lumen/skills/`，并不要求全局安装。
   但模型先选择工作区以外的 `~/.lumen/skills/`，随后花费大量输出讨论网络与写权限。
2. 当时项目配置只启用 read/list/search/write/edit/run，没有原始文件直接下载工具；命令说明
   明确提供 `network_allowed=False`。模型走 MCP 网页提取，发现 HTML 标签被清理后又获取
   GitHub base64 blob。网页提取可以用于阅读，但不是可信的字节传输 Interface。
3. 87,253 字节的原始文件变成约 116 KB base64，模型又将 base64 作为生成内容逐块写回。
   记录包含重复推导编码、拆块和补字符。安装因此变成了昂贵且容易出错的模型转写任务。
4. `edit_file` 使用原始 `find` 做替换；`TextResourceAdapter.locate` 却对整个 selector
   `.strip()`，删除了 `anchor:<find>` 尾部换行。执行和验证的字符范围不同。
   例如文件 `prefix\nbase64\n`，find=`base64\n`、replace=`base64suffix\n`：实际结果
   正确包含一个末尾换行，但旧验证范围没有消费原来的换行，因此期望结果多出一个换行。
5. 所以替换已写入后，验证报告 `result does not equal the requested isolated text replacement`。
   用户记录第 1582、1704 行出现该错误；随后旧 find 消失或字符数变化，模型继续修补。
   FAILED receipt 不代表回滚，旧实现也未声称自动回滚；不能把“工具失败”当成“文件未改变”。
6. 既有 request/tool count 上限只在循环步骤间检查；`LoopStallObserved` 只是重复调用诊断。
   单个持续输出的 Provider stream 没有总耗时限制。工具 timeout 不约束模型思考和生成。

## 修复

- 字面 anchor 保留全部空白。`edit_file` 从原始 bytes 解码，避免 `read_text` 将 CRLF
  归一化后导致 revision/非目标区域不一致；空 find 在写入前拒绝。
- 新增显式启用的 `download_file`：公共 URL 的 UTF-8 原始字节直接落到工作区，模型只接收
  路径、字节数和 SHA-256。保留原始 HTML 标签和换行，不接受 HTML 页面代替 raw source。
  下载流受到大小与时间限制，可校验调用者指定的 SHA-256；失败时不发布部分文件。
- 下载属于 `Risk.EXTERNAL`、`EffectKind.MUTATION`，经过既有审批；写入复用 Workspace
  原子发布、TaskWorkspace 快照与 prepared/applied/verified journal，Gateway 不重复建账。
  取消发生在发布期间时，等待原子发布和 journal 收尾，避免调用结束后后台继续写入。
- 提示词明确项目安装目录和直接下载路径；无授权传输能力时及时报告具体阻塞，不再通过
  模型反复转写载荷，也不尝试通过其他工具越过工作区写权限。
- `model_request_timeout_seconds` 默认 300 秒，包含单次模型请求中的活动流和 Provider 重试，
  不包括工具执行或用户审批。到期关闭 stream，不执行尚未交付的工具调用，以失败事件结束。
  超时的 PartialRunOutcome 保留文本、请求证据及已收到的 usage。
- 当前项目和示例配置已启用 `download_file`。现有全局 Lumen 安装指向此 checkout，重启进程
  即加载代码；Skill catalog 仍在启动时发现，安装后重启生效。

没有删除独立生产实现，没有修改历史 Session、旧 FAILED receipt 或用户的 `.skill-staging/`。
旧失败会话需要按已有恢复路径核查，不能自动把失败副作用改成成功；建议从新任务重试安装。

## 验证

- 最终全量 `pytest`：**978 passed，60 snapshots passed**（68.70 秒）；Ruff、Pyright、
  contract catalog check 和 Architecture Atlas check 均通过。未修改前端或打包入口，未运行 Web
  build 或 wheel build；本次没有提交、推送或发布。
- 契约测试覆盖带末尾换行、空白和 CRLF 的真实 journaled edit，检查目标 bytes、VERIFIED
  receipt 和完成门禁；覆盖大 Skill 下载、哈希、目录发现、审批、错误不发布及网络取消。
- Loop 测试覆盖静默挂起和持续 thinking，两者都按时停止且不交付已收集的工具调用。
  Runtime 测试覆盖失败事件、部分文本、usage 与 request receipt 的保留。
- 真实网络 smoke：通过新的工具函数把
  `https://raw.githubusercontent.com/leonxlnx/taste-skill/main/skills/taste-skill/SKILL.md`
  下载到临时工作区，再用真实 `SkillLoader` 扫描。耗时 **0.53 秒**，**87,253 字节**，发现
  **design-taste-frontend**；SHA-256 为
  `aa194351b246b8b4799099d4ed7b033d29eab6e6e3d58d8d2172978be7b3ec89`。
  该 smoke 验证传输与发现链路，没有调用收费模型，也没有把临时文件安装进用户的真实目录。

`download_file` 目前只支持 UTF-8 文件；Git clone、ZIP 解包、二进制资源和全局安装不在本次
新增 Interface 中。多文件 Skill 需按仓库实际目录逐文件下载。单请求时限不是整个 run 的时限，
它不承诺任意任务都能在五分钟内完成。
