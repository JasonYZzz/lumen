# Lumen macOS 桌面版

桌面版采用 Tauri 2 + 内置 Python 3.13，复用 Lumen Web 和 WorkspaceHost。
目前是 Apple Silicon 开源 Beta；目标为 macOS 14 及以上，最低版本和实际媒体仍需实机验收。

实现复核：2026-10-10，代码交付 `28d1679`；此前的构建与模拟服务验收发生于 2026-10-09，
详见[验收记录](research/2026-10-09-macos-desktop-validation.md)。应用版本仍为 `0.1.0`。

## 安装与打开

打开 DMG，将 Lumen 拖入 Applications，再启动应用。应用内置 Python、Web、网页提取与 Live
客户端依赖，不需要安装 Python、Node.js、uv 或 pnpm。外部 MCP 服务、git、uvx、npx 和
用户命令仍需自行安装；应用不自动安装这些程序。

当前安装包使用 **ad-hoc 签名，未经过 Apple 公证**。从网络下载后可能被系统拦截；核对来源与
发行文件 SHA256 后，按“系统设置 → 隐私与安全性 → 仍要打开”流程明确允许。
不要关闭 Gatekeeper，也不需要执行清除 quarantine 的命令。参见
[Apple 的安全打开说明](https://support.apple.com/en-gb/102445)。

## 第一次使用

1. 选择项目文件夹。项目配置或技能未被信任时，先确认其来源；取消不会执行项目代码。
2. 没有配置时选择服务商与模型，自动填入接口地址、协议和凭据变量名。默认 GPT‑6.1 Sol；
   高级设置可修改地址与模型 ID。“自定义 / 本地模型”支持 `ollama:<model>` 等既有配置。
3. 配置保存到 `~/.lumen/agent.yaml`，已有文件不覆盖。默认读取工具与 CLI 初始模板一致。
4. 输入配置要求的凭据，可选保存到 macOS Keychain。配置只保存变量名；模型连接在发送消息时发生。
5. 项目 MCP 分别选择仅本次、始终允许或拒绝。定义改变后重新授权；工具调用仍经过 Gateway。

已有配置与 Session 保持原格式，不自动改写用户 YAML。启动页“管理已保存的凭据”可按变量名
删除，重新打开项目即可输入并保存替代值。新增不同的凭据变量时，先在用户配置中声明，再重新
打开项目；应用内模型编辑可复用本次启动已解析的变量。凭据不会进入全局环境；只有 MCP 显式
env/header 插值可得到对应值。

### 首次配置模型预设

2026-10-09 核实的公开模型选项，定义在 `src/desktop/bootstrap/model-presets.js`。
只保存用户所选的一个模型，不同时启用全部服务商，也不附带凭据或模型权重。

| 服务商 | 模型 ID（配置使用 `openai:` 前缀） | 协议 | 凭据变量 |
| --- | --- | --- | --- |
| OpenAI | `gpt-6.1-sol`、`gpt-6-astra`、`gpt-6-luna` | Responses | `OPENAI_API_KEY` |
| DeepSeek | `deepseek-flash`、`deepseek-v4-pro` | Responses | `DEEPSEEK_API_KEY` |
| 阿里云百炼 | `qwen3.8-max`、`qwen3.8-flash` | Chat Completions | `DASHSCOPE_API_KEY` |
| Moonshot | `kimi-k3` | Chat Completions | `MOONSHOT_API_KEY` |
| 智谱 | `glm-5.3` | Chat Completions | `ZHIPU_API_KEY` |

官方依据：[OpenAI](https://developers.openai.com/api/docs/guides/latest-model)、
[DeepSeek](https://api-docs.deepseek.com/api/create-response/)、
[Qwen](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、
[Kimi](https://platform.kimi.com/docs/guide/kimi-k3-quickstart)、
[GLM](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)。
Qwen 默认使用仍受支持的中国内地公共域名；其他地域与业务空间专属地址需要按官方文档修改。
Kimi/GLM 预设为开放平台按用量计费接口，Coding 订阅有独立地址与凭据，不能混用。
预设沿用现有 Provider Adapter；新增型号未单独声明的推理能力使用服务商默认值，
不根据模型名称猜测能力。真实账户是否有权限、余额和当地网络可用性仍需实际调用验证。

## 窗口、退出和更新

- 关闭窗口保留应用与活动任务；点击 Dock 或“Lumen → 显示 Lumen”恢复。
- 有实时语音时，关闭前确认结束；不会隐藏窗口后继续录音。
- Cmd+Q 有序取消活动 run、结束 Live、关闭 Host；忙碌时先确认，未处理记录在项目中保留。
- “项目 → 切换项目…”先关闭旧项目，再回到启动页。
- “设置 → 添加外部工具目录…”添加执行器目录，下次打开项目生效；不会执行 shell 启动脚本。
- “设置 → 下载新版本…”打开本仓库 Releases。当前人工退出后替换整个应用，不删除用户
  `~/.lumen` 或项目 `.lumen`，不自动降级 journal。

## 构建

在 Apple Silicon Mac 安装 Rust 1.98.0、Node.js 22、Corepack、uv 0.12.5 和 Command Line Tools。
工具使用锁文件，Python 3.13.15 安装在 `build/desktop/.venv`，不替换开发用 `.venv`。

```sh
python3 scripts/build_desktop.py
```

已有 Web 导出可加 `--skip-web`；只构建冻结后台可加 `--backend-only`。产物位于
`src/desktop/target/aarch64-apple-darwin/release/bundle/`，DMG 同目录包含 `SHA256SUMS.txt`，
构建清单位于 `build/desktop/manifest.json`。
第三方许可证与清单位于应用 `Contents/Resources/backend/third-party/`。
应用标识为 `io.github.lumen.agent`，首次公开发行后保持稳定。

构建顺序：Web → wheel → 隔离安装与 CLI 校验 → PyInstaller onedir → 冻结冒烟 → Tauri app/DMG →
签名/镜像校验 → 包内后台冒烟与摘要。tiktoken 标准编码在构建时下载，按其内置 SHA256 校验后随包使用。
PyInstaller 的 `codesign_identity=None` 表示 ad-hoc 签名；传入字符串 `"-"` 会误启用其
Hardened Runtime 分支，无 Team ID 的 Python 依赖会被 Library Validation 拒绝。

`.github/workflows/desktop.yml` 执行 arm64 Rust 检查、构建和后台冒烟，并上传 Actions artifact；
只需要仓库读取权限，不含证书，不创建 Release。runner 架构显式检查，标签含义参见
[GitHub 官方说明](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。
该工作流由手动运行或匹配路径的 PR 触发，推送 `main` 只触发通用 CI，不自动生成或发布 DMG。

## 验证与限制

冻结冒烟使用本地 OpenAI Chat 模拟服务，通过真实 SDK、HTTP API 和 Host 验证流式回复、落盘、
一次性 token、Cookie、Origin 与父管道 EOF 退出；不代表真实 Provider、OAuth、性能或语音验收。
发布前仍需干净 macOS 用户、最低 macOS 14、实际模型、MCP、两条 Live 链路、麦克风授权、
中文输入法、VoiceOver、quarantine 和 N→N+1 数据兼容验收。

冻结包支持标准库与随包依赖的 Python Plugin/Hook；缺少其他依赖时需使用独立 MCP 或重新构建，
不在应用包内执行 pip。新凭据在重新打开项目时供给；SDK 自行读取的其他环境认证或平台登录
不属于桌面自动支持的路径。应用没有自动更新器；人工升级时 ad-hoc 身份变化可能再次触发
系统与 Keychain 的授权提示。

启动失败提供脱敏提示，第三方 stderr 不落盘，以免输出凭据。检查 YAML、执行器目录和
Keychain 权限后重新打开。数据损坏使用原有恢复功能，桌面壳不自动重放未知工具。
开发启动可用 `cargo run --manifest-path src/desktop/Cargo.toml`，需要根 `.venv` 和已构建 Web。
