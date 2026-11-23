# Skill 脚本编写规范

Skill 仍以 `SKILL.md` 为入口。可选的 `scripts` mapping 声明模型能够通过
`run_skill_script` 调用的脚本：

```yaml
---
name: project-check
description: Run the project-specific validation suite
scripts:
  check: scripts/check.sh
  summarize: scripts/summarize.py
---
先运行 `check`，再根据输出总结失败。
```

脚本路径必须相对 Skill 目录，解析后不能越界；只接受 `.sh`、`.bash`、`.py`。
已激活目录包在校验版本后从临时副本运行，运行目录为副本中的 Skill 根目录，不经过 shell 拼接参数，默认 30 秒超时，并按
EXECUTE 风险进入标准审批流程。环境只保留 `PATH`、`HOME`、locale、终端与临时目录，
不会继承 `*_KEY`、`*_TOKEN` 等凭证；`LUMEN_WORKSPACE` 指向当前工作区。

内置 `commit`、`test-runner`、`review-pr` 随 wheel 分发。为保持旧安装的发现结果不变，
需设置 `agent.builtin_skills_enabled: true`。同名优先级从高到低为项目、用户、内置。同一范围内 `.lumen/skills` 优先于显式启用的 `.agents/skills`。

## 导入与调用兼容

将一个完整 Skill 放在独立目录中，以 `SKILL.md` 声明名称和用途。在 Web 或桌面「设置 → 扩展能力 → Skills」预览后安装。ZIP 能保留空目录和执行位。浏览器文件夹上传只传所选文件及相对路径。也可给出含子目录的 GitHub tree URL。安装 32 MiB、4096 文件的上限包括二进制资产。

Codex 的 `agents/openai.yaml` 中 `policy.allow_implicit_invocation: false` 与正文的 `disable-model-invocation: true` 都会限制模型自动启动。用户用 `/skill:<name>` 明确选择后，本会话可以读取引用和申请运行声明脚本。`user-invocable: false` 则关闭手动调用。Claude 的 fork、agent、hooks、工具预授权及动态命令会产生兼容提示，Lumen 按自身权限运行；外部工具依赖需要操作者配置。

正文按激活时的版本保留。目录中的引用、脚本或 helper 有变化时，用户须再次明确激活。独立单文件或超限目录可以保留正文，使用配套工具需改为可固定版本的目录包。
