# 原生 Skill 安装：实现与体验对齐

日期：2026-09-04。接续 [安装失败根因分析](2026-09-04-skill-install-failure-audit.md)。

后续真实使用发现的全局 scope、澄清数组与 Web 呈现问题，已由
[全局安装与澄清交互修复](2026-09-04-skill-scope-clarification-ui.md) 接续验证。

## 目标与事实

用户只需给 GitHub 地址或工作区本地目录，模型调用安装工具，Lumen 完成真实字节传输、目录安装、
验证和发现。87 KB 正文不经过模型转写，安装不执行 Skill 本身的领域流程。

对照资料为 [Codex 官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)、本机 Codex
`skill-installer` 的 `install-skill-from-github.py` 实现，以及
[Claude Code 官方 Skill 文档](https://code.claude.com/docs/en/skills)。两者具体安装入口、权限机制
和目录约定并不相同；“体验对齐”指独立 Skill 的核心安装与使用流程，不等于所有插件生态功能相同。

| 用户操作 | 当前实现 |
| --- | --- |
| 给 GitHub 仓库链接 | 自动查默认分支、固定 commit，再下载该 commit 的 ZIP |
| 指定某个 Skill | 支持 tree/blob/raw URL、path/ref、工作区本地目录 |
| 仓库包含多个 Skill | 返回有界候选清单，指定精确路径后安装，不猜目标 |
| 安装引用资源 | 完整复制文本、二进制、空目录和脚本可执行标记 |
| 安装后使用 | 刷新摘要，同一轮即可 list/load；手动专用 Skill 仍需用户调用 |
| 重复安装和更新 | 相同内容 no-op；显式覆盖；受管目录本地修改保护 |
| 撤销与恢复 | 既有 Work Product 目录 snapshot、恢复及中断 journal 对账 |
| 项目和用户范围 | 项目需信任；用户范围由 user_skill_install_enabled 独立授权，保留原全局权限兼容 |
| 私有仓库 | 使用 GH_TOKEN/GITHUB_TOKEN；不把凭据放进模型参数和回执 |

## Implementation 与权威

- `SkillInstaller` 负责来源解析、网络传输、候选识别与安装策略，返回有界元数据。
- `DirectoryResourceAdapter` 实现既有 ResourceAdapter Interface。整个目录清单、文件 bytes 和
  executable 标记存为单个内容寻址 artifact；不会把二进制 base64 当作模型生成参数。
- `TaskWorkspace` 保持 mutation journal、revision、恢复和完成门禁的唯一权威，未增加第二套
  安装事务系统。暂存目录与目标在同一文件系统；发布失败恢复旧树，崩溃由 journal 对账。
  更新使用两次 rename，二者之间可能短暂无目标目录；不是跨进程数据库事务。异常保留的备份
  使用隐藏暂存目录，不参与 Skill 发现，不能未经核查删除。
- Gateway 沿用 `Risk.EXTERNAL` / `EffectKind.MUTATION`，拒绝未审批调用；Plan 继续只读。
- `ResourceManager` 刷新发现，`AgentRuntime` 每次请求获取最新目录摘要。启动时为空也保留
  发现/加载工具；已激活 Skill 的 Session artifact 不因源文件变化而被重写。

当前和示例 agent 配置已启用 `install_skill`。其他配置仍需显式加入 `tools.builtins`，保持原有
只读配置的能力约束。TUI、Web、headless 共用 ResourceManager/Runtime，无客户端专用安装实现。
子 Agent 不转发父工作区的安装闭包，避免借父级工具修改父目录或全局目录；安装由根 Agent 执行。

## 兼容与明确限制

保留项目覆盖用户及内置同名 Skill 的原有优先级，保留 manual-only、项目信任、审批和 Sandbox
规则，保留旧 Session 加载和 append-only journal。不重写旧失败会话，不把原 FAILED receipt
自动改成成功。没有删除独立生产实现；网络 URL 校验和目录 fsync 改为两个实际调用者共享的
公开 helper，旧调用点同步迁移；原 UTF-8 `download_file` 继续保留。

这次没有实现 marketplace/plugin 管理、MCP/Hook 安装或自动执行、Git/SSH 凭据回退。独立 Skill
不是 plugin 的别名。下载与展开各限 32 MiB，文件和目录各限 4096；拒绝 symlink、特殊文件、
逃逸和大小写冲突，大仓库可能需要先准备工作区本地 Skill 目录。网络获取总期限 110 秒，工具
上限 120 秒；发布若正在收尾，取消会等待 journal 一致后返回，避免后台继续写入。

## 验证记录

真实网络：从 `https://github.com/leonxlnx/taste-skill` 安装 `skills/taste-skill` 到临时工作区，
固定 commit `ccbc15639c97057cbfcf32ecebc38ef716e4bb37`。完整安装耗时 **13.38 秒**，源文件
**87,253 字节**，真实 SkillLoader 发现 `design-taste-frontend`，目录验证通过，无完成阻塞。
这是本次网络测量，不是所有网络和模型的耗时承诺；没有调用收费模型或改变用户实际 Skill 目录。

新增安装测试覆盖完整二进制目录、CRLF、可执行标记、空目录、默认分支与 commit、多候选、
tree/blob/raw、含斜线分支、本地更新、同内容重装、本地修改保护、全局权限、项目信任、审批拒绝、
路径与 symlink 拒绝、凭据不跟随未知重定向、网络取消、目录交换失败恢复、prepared/applied 中断
恢复。真实 Runtime + 流式 FunctionModel 契约验证“安装 → 新摘要可见 → 加载 → 正常结束”，
不是只调用安装工具做成功判断。

全量 Python **1000 项测试**和 **60 个快照**通过；Web **127 项测试**、TypeScript typecheck 与 Next 生产构建通过。
Ruff、严格 Pyright 和契约目录检查通过。OpenAPI/TypeScript schema 与 Architecture Atlas 均通过
项目生成命令更新，保留工作树中已有改动；未提交、推送或发布。
`uv build --wheel` 成功；使用生成 wheel 的独立临时环境执行 `lumen --version`，输出 `lumen 0.1.0`。
