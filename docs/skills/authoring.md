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
运行目录固定为 Skill 目录，不经过 shell 拼接参数，默认 30 秒超时，并按
EXECUTE 风险进入标准审批流程。环境只保留 `PATH`、`HOME`、locale、终端与临时目录，
不会继承 `*_KEY`、`*_TOKEN` 等凭证；`LUMEN_WORKSPACE` 指向当前工作区。

内置 `commit`、`test-runner`、`review-pr` 随 wheel 分发。为保持旧安装的发现结果不变，
需设置 `agent.builtin_skills_enabled: true`；项目 Skill 和用户 Skill 可按同名覆盖内置版本。
