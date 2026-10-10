# Skill 安装故障、原生实现与澄清修复

原始验收：2026-09-04；合并：2026-10-10。本文合并安装失败、原生安装和全局范围澄清的三份
阶段记录。当前接入规则见[MCP 与 Skills](../architecture-guide/09-mcp-and-skills.md)，
安全边界见[工具与权限](../architecture-guide/04-tools-permissions-security.md)。

## 故障与修复依据

最初安装将 87,253 字节 SKILL.md 经 MCP 提取，再由模型转录约 116 KiB base64；
大文件转录、工具参数和局部替换链路拖长执行。原日志没有每次请求的完整时钟，
不能把等待全部归因为 GitHub 或模型。文本锚点 strip 丢失末尾换行，使写入/验证不同；
failed receipt 本身不等于自动回滚。临时增加请求总时限不是现行方案，
当前默认无模型请求总时限硬墙，见[恢复契约](../architecture-guide/14-long-running-recovery.md)。

原生安装由 SkillInstaller 拉取原始字节，GitHub 默认分支先解析固定 commit，支持受管的
tree/blob/raw、ZIP 与本地目录来源。安装不通过模型重写全文，候选保留二进制、可执行属性及
目录结构；ResourceAdapter 与 TaskWorkspace 记录 prepared/applied/verified。
同文件系统 staging/备份重命名不宣称全局事务；取消等待 I/O 收尾，不遗留半成品。
更新保护本地修改，备份不被 Skill 发现；目录刷新后同 turn 可发现，manual-only 仍需显式调用。

## 安全与全局安装

- scope 与命令 Sandbox 是不同权限。user_skill_install_enabled 默认 false；
  开关启用后只授权受管 ~/.lumen/skills/<name>，仍经 Gateway 审批、路径校验和内容验证。
  不建议关闭整个 sandbox；子 Agent 不获得父级安装闭包。
- 不可用 scope 明确返回可用范围，不静默改装到项目目录。旧 disabled 模式的兼容路径保留，
  通用文件/命令工具权限不扩大。
- 私有 GitHub token 只在安装执行边界读取，不成为模型参数或日志；重定向保护凭据。
  下载与解压各限 32 MiB、文件/目录数限 4096，拒绝 symlink、大小写冲突与目录逃逸；
  网络 110 秒、工具 120 秒是安装边界，不是主 turn 的模型预算。
- 工作区信任、项目/用户范围、审批和 TaskWorkspace 验证继续独立生效。
  macOS smoke 最初因 /var 临时路径的符号链接被拒绝，改用真实路径后通过，
  没有放宽生产保护。

## 澄清问题修复

实际 Provider 返回 choices 的合法 JSON 数组字符串，原 list 契约拒绝它；schema 仍声明数组，
ClarificationGate 只兼容一层合法字符串数组，不接受对象、非字符串元素、任意文本或超大输入，
不用 eval，不把此兼容扩展到写入/执行参数。

Web 使用服务端 pendingClarification 投影，选项按钮与自由回答均经已有 startRun 继续同 Session。
防重复提交，失败可重试并保留草稿，刷新可恢复问题；回答后清除等待状态。
澄清前片段按公开控制事件归入过程，等待时收起，保留 journal 原文。
旧会话中的无选项问题不被自动改写，仍可自由回答。

## 原始验收

| 阶段 | 实际结果 |
| --- | --- |
| 原生项目安装 | 独立临时目录安装 taste-skill，固定 commit ccbc15639c97057cbfcf32ecebc38ef716e4bb37；正文 87,253 字节，用时 13.38 秒。Python 1000 tests / 60 snapshots，Web 127 tests；未改用户全局目录、未调用收费模型 |
| 全局权限与澄清 | 独立临时 HOME/project、workspace_write、专用开关，经真实 Gateway 审批与验证安装同一正文，用时 3.21 秒；用户级发现成功、项目未误写。Python 全量 1002 项后补 5 个输入校验用例，Runtime 60 项复跑，最终收集 1007 项；不能把 1007 当成当次完整重跑 |
| Web 澄清 | Web 129 tests、typecheck/build；真实 Web/Host/Runtime 配合 FunctionModel，浏览器验证字符串选项、刷新恢复、点击继续同 Session、回答后状态。非真实模型端到端质量保证 |

各阶段 Ruff、strict Pyright、契约、OpenAPI/TypeScript 与 Atlas 按当次命令验证。
13.38 秒与 3.21 秒环境/缓存不同，不是受控速度对比，也不能外推任意仓库安装耗时。
原始报告可从[整理前 Git 快照](https://github.com/JasonYZzz/lumen/tree/8940f92b3a61b0f0e3d7dc0e770da7d06facc3ea/docs/research)
追溯；本次只合并文档，没有重新安装 Skill、调用 Provider 或重跑上述测试。
