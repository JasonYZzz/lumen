# 全局 Skill 安装与澄清交互修复

日期：2026-09-04。证据为用户截图、对应 Session journal、当前代码与隔离验证。

## 根因

1. 用户实际请求明确为“到 lumen 的全局用户使用”。模型选择 `scope=user` 符合请求。
   上一版 ResourceManager 却把受管 Skill 目录写入绑定到 `sandbox.mode=disabled`；因此安装在
   下载前拒绝。不能静默改成项目安装，也不应要求关闭整个命令沙箱才能安装用户级 Skill。
2. 原始模型 ToolCallPart 已把 `choices` 编码为 JSON 字符串，内部文本才是数组。框架的 list
   校验正确拒绝了字符串；没有证据支持模型所称“框架将数组序列化错了”或“中文导致编码错误”。
   缺少对这一明确格式偏差的受限兼容，使同一个澄清调用失败三次。
3. Web 把澄清事件压成灰色普通文字，丢失可操作的结构化选项。快照已有 pendingClarification，
   但页面没有使用。控制工具不显示为普通工具卡，前端仅按“最后一个工具卡”区分过程与答案，
   导致澄清前的长篇排错文本成为正文；等待状态还默认展开全部过程。

## 修改

- 新增 `agent.user_skill_install_enabled`，默认 false；当前项目和示例配置启用。只有受管
  `~/.lumen/skills/<name>` 可写，仍经过 Gateway 审批、目录路径验证、TaskWorkspace journal
  与完整内容验证。通用文件工具和命令 sandbox 不扩大权限，子 Agent 不获得父级安装闭包。
  旧 `sandbox.mode=disabled` 的全局安装能力保持兼容。工具描述明确当前可用 scopes；未启用时
  返回 scope_unavailable、可用 scopes 与简短指引，无写入，不建议关闭整个 sandbox。
- ClarificationGate 的类型校验在现有 list Interface 上，仅为 choices 接受一层合法 JSON
  数组字符串。Schema 仍向模型声明数组，拒绝对象、非字符串元素、任意文本和过大输入。
  不使用 eval，不对写入、执行等其他工具参数做隐式转换。兼容移除条件写在代码中。
- Web 保存服务端已有的 pendingClarification 投影；活动问题显示选项按钮与自由回答框。
  点击经现有 startRun Interface 继续原 Session，防重复提交，错误可见且可重试，不清空原输入草稿。
  新 run 接受后清除卡片；刷新从快照恢复。已回答的问题不再显示“等待补充信息”。
- 澄清前的 assistant 片段按控制事件归入处理过程，等待时默认收起；不靠英文关键词猜测思考，
  不删 Session 原文。提示词要求简短提问，禁止将参数排错和未经证实的框架故障推测公开叙述。

没有删除独立生产实现；替换的是重复的 scope/命令沙箱关联条件以及单一路径的澄清展示。
保留原配置默认值、审批、旧 Session 加载、原始日志、失败恢复、项目范围和 manual-only Skill。
旧会话中已经持久化的无选项长问题不自动改写，新界面仍可用自由回答继续该会话。

## 验证

- Python 全量 1002 项与 60 个快照通过；随后补充 5 项畸形 choices 校验，整个 Runtime 测试
  文件的 60 项再次通过。最终收集 1007 项；全局目录授权测试还在独立项目/模拟 HOME 上复验。
- Ruff、严格 Pyright、契约目录检查通过；OpenAPI/TypeScript 与 Architecture Atlas 通过项目
  生成命令同步。Web 129 项测试、TypeScript typecheck、Next 生产构建通过。
- 真实 GitHub 安装在独立临时 HOME 和项目中验证：`scope=user`、`workspace_write`、专用开关
  启用，经过真实 Gateway 审批和 TaskWorkspace 验证，安装 taste-skill 的 87,253 字节正文，
  用时 **3.21 秒**；用户级发现成功，项目目录未被误写。未改变用户真实 Skill 目录、未调用收费模型。
  初次 smoke 的 macOS 临时目录路径包含 `/var` 符号链接，被预期保护拒绝；测试改用该临时
  目录的真实路径后通过，没有放宽生产路径校验。
- 在隔离本地服务使用真实 Web/Host/Runtime 与流式 FunctionModel，浏览器实际验证：
  ① 字符串数组一次生成可点击卡片；② 刷新保留问题与选项；③ 点击选项继续同一会话；
  ④ 回答后刷新显示“已收到补充信息”。截图检查了卡片、文字层级、按钮与输入布局。
  这验证真实交互与服务端契约；没有宣称所有 Provider 都不会再输出不适当的正文。

需要重启现有 Lumen 进程加载 Python/配置，并刷新 Web 载入构建产物。没有提交、推送或发布。
