# macOS Apple Silicon 桌面实施验证

日期：2026-10-09。对应[实施计划](../plans/2026-10-09-macos-apple-silicon-desktop-implementation-plan.md)。
交付为可本机构建的开源 Beta，不是已公开发行或经过 Apple 公证的正式版本。
用户没有 Developer ID 证书，因此本次采用 ad-hoc 签名、DMG、只读 GitHub Actions artifact 和人工更新。

## 实现与边界

- `src/desktop/`：Tauri 2 壳、本地启动页、项目选择、信任/MCP 授权、Keychain、原生菜单、
  单实例、最近项目、窗口尺寸、关闭/退出/切换与后台进程生命周期。
- `src/lumen/desktop.py`：有界、版本化私有管道，配置预览与授权后启动已有 FastAPI/WorkspaceHost。
  聊天、Session、Plan、审批、Agent、Live 和工具门禁继续使用原有 Host Interface。
- 配置与凭据：显式变量 Map，不注入进程全局环境；拒绝的 MCP 不请求凭据；共享 CLI 的 MCP
  授权规则；配置创建不覆盖、权限 0600；授权过程中配置变更拒绝；配置报告脱敏插值值。
- 安全边界：后台只监听随机 `127.0.0.1` 端口，复用一次性启动 token、Cookie、Origin/Host
  检查。远程聊天页没有 Tauri 启动 IPC 权限；原生接口再检查窗口来源。token 不广播。
- 关闭/恢复：通过 Host 查询与取消，保留待处理记录；父管道 EOF 关闭 Host。超时只终止仍被
  当前壳持有的子进程组。第三方 stderr 不持久化，启动错误脱敏。
- 打包：冻结 Python 3.13、Web、Live、网页提取、标准 tokenizer 缓存；收集 Python/Rust
  许可证。用户配置的外部 MCP、命令执行器和额外 Plugin 依赖不自动安装。
- `.github/workflows/desktop.yml`：只读权限、arm64 构建、检查、冻结冒烟与 artifact；不发布 Release。

没有更改配置 v2、Session v12 或 Host schema；兼容读取和原有恢复门禁保持原实现。
开始时工作区已含大量其他改动，保留原有 dirty/untracked 文件，未提交、推送或发布。

## 构建基线

| 项目 | 实际环境 |
|---|---|
| Git HEAD | `291348aa956c88e6ccd6f465fd4ea63656956e7c`，包含未提交工作区改动 |
| 本机 | Apple Silicon arm64，macOS 26.5.1，Command Line Tools |
| 目标 | `aarch64-apple-darwin`，最低版本声明 14.0；14.0 尚未实机测试 |
| Python | 冻结 3.13.15；根开发环境 3.12.14 保留 |
| Rust | 1.98.0；Tauri 2.12.2；Tauri CLI 2.12.1 |
| PyInstaller | 6.22.3，onedir，`codesign_identity=None` |
| Node / pnpm | 本机构建 Node 24.19.0 / Corepack pnpm 10.28.2；CI 固定 Node 22 |
| 签名 | ad-hoc；无 Team ID、Developer ID、公证或自动更新签名 |

构建命令：`MACOSX_DEPLOYMENT_TARGET=14.0 python3 scripts/build_desktop.py --skip-web`。
此前已通过 Web 生产构建，跳过只避免重复生成同一静态导出。
四份 lockfile 摘要及 DMG 摘要由构建写入 `build/desktop/manifest.json`；DMG 同目录生成
`SHA256SUMS.txt`。CI 和完整本地构建默认先安装锁定依赖并构建 Web。

本次最终 DMG 为 50.24 MiB，SHA256：
`20a42acfba0cfb4a64d169e77cb6608cf3407d33d370c31692d760efc4270a21`。
包内 16 个 Mach-O 均为 arm64，load command 的最低系统要求不高于 14.0；这不代替最低系统实机测试。

## 实际验证

| 验证 | 结果 |
|---|---|
| `uv run --frozen --extra web --extra live pytest` | **1536 passed、1 skipped，60 snapshots passed**，128.22 秒 |
| `uv run ruff check .` | 通过 |
| Pyright Linux / Darwin / Windows | 各 0 errors、0 warnings |
| Web test / typecheck / build | 219 tests（29 files）通过；类型检查和生产构建通过 |
| Rust fmt / clippy / test | 通过；2 个测试覆盖管道大小、字段/类型、凭据名与精确来源 |
| contracts / Architecture Atlas 新鲜度 | 通过；手写拓扑与能力矩阵补充桌面 Adapter |
| `uv build` | sdist 与 wheel 构建通过 |
| 隔离 wheel CLI | `lumen --version`、隔离配置 `lumen --check-config` 通过 |
| 冻结后台与 app 内后台 | 两处均通过隔离 HOME/Finder PATH 冒烟；没有系统 Python/Node 依赖 |
| 最终 app / DMG | 深层严格签名校验、全部 Mach-O arm64 检查、镜像校验与摘要生成通过 |

`codesign --display` 确认为 `Signature=adhoc`、无 Team ID；`spctl --assess --type execute`
结果为 rejected，符合未公证包的限制，并非 Gatekeeper 自动通过的发行物。

第一轮全量回归在同时执行 Rust LTO 时，已有 `test_process_recovery` 的 20 秒标记等待超时；
单独重跑通过。随后顺序执行上述完整回归全部通过，未修改该测试或放宽时限。

冻结冒烟使用本机 OpenAI Chat 模拟 HTTP/SSE 服务和真实 SDK，验证未认证 401、一次性 token
兑换后 URL 清理、Cookie、错误 Origin 403、Host 创建任务、回复持久化和父管道 EOF 退出。
这是可复现集成验证，不代表真实 Provider、性能或媒体验收。

原生 UI 验证使用隔离配置与模拟服务，未使用真实凭据：文件选择、项目信任、一次性凭据、
中文文本粘贴、流式回复、Cmd+O 切换、取消凭据、关闭/恢复窗口与 Cmd+Q 退出；退出后没有
残留桌面/后台进程。检查默认 1180×820 和最小 800×600 窗口，截图位于
`.impeccable/review/desktop-bootstrap/`。

界面独立复核提出三项修正：删除标题上方多余标签、增加复选框/披露区域点击高度、继承 Web
选中与光标颜色。最终 app 重新构建并同尺寸截图，复核评分三项均 resolved，`disposition: ship`；
该结论仅覆盖三项修正，不代表媒体、VoiceOver 或全部产品验收。
文档复核确认该启动页延续现有 Web 的颜色与 Fold 图标，并使用 macOS 系统字体；Web 正文使用
Geist。保留原有 Atlas 设计文件，没有为桌面局部扩展重写全局设计系统。

测试发现同步 Keychain 调用在隔离 HOME 无登录钥匙串时可能阻塞窗口，已迁移到后台线程，
系统授权不持有桌面状态锁；凭据页面的取消仍可清理后台。本次没有实际创建/修改登录钥匙串。
凭据自动读取、保存与删除的代码路径已实现，真实权限、签名变更和 N→N+1 Keychain 验收未完成。

## 模型预设与首次配置 UI 后续改进

同日按用户要求更新首次配置，默认 GPT‑6.1 Sol，可选择 GPT‑6 Astra/Luna、DeepSeek Flash/V4 Pro、
Qwen 3.8 Max/Flash、Kimi K3 和 GLM 5.3。型号、协议与地址的官方来源见
[桌面指南](../desktop.md#首次配置模型预设)；它们是选择模板，不会同时启用全部服务商。
没有找到可用的 Intelligent UI 技能/插件，按智能引导的方向实现，使用 Impeccable 复核。

服务商和模型选择自动填入接口、地址和凭据变量；高级设置保留自定义模型/本地模型及地域地址。
凭据页按服务商显示 API Key 名称，保存到 Keychain 默认不勾选。修正从已滚动的高级配置进入
下一页时保留滚动位置的问题，页面重新聚焦并回到顶部。保持原有私有管道与配置格式。

本次新增 12 项 DOM 交互测试覆盖全部 9 个预设、单选提交、自定义字段清理、高级覆盖、
密码输入、Keychain 默认关闭和页面切换。Web 全量 **231 tests / 30 files**、typecheck、build 通过；
修正滚动后的 12 项最小回归再次通过。桌面后台 9 tests、Ruff 与 Pyright 通过。
Python 运行时未改动，没有重复前一阶段的全量 Python 回归。

重新生成 app/DMG，最终包深层严格签名、16 个 Mach-O arm64、DMG 镜像校验和包内后台
真实 SDK/本地模拟服务冒烟通过；上方 SHA256 已更新。新的 GitHub 工作流步骤运行引导测试。
原生 UI 用隔离 HOME/空配置完成项目选择、Qwen 预设、自定义本机模拟地址、一次性凭据、
进入聊天与流式回复，验证用户 YAML 只含选中模型、没有 API Key，Session 已落盘。
最后的滚动修正重新打包并以空配置复核，不使用真实账户或登录 Keychain。
默认 1180×820、最小 800×600 的最终截图位于 `.impeccable/review/desktop-onboarding/`；
展开高级配置使用顶部/底部配对截图，最小凭据页的主要操作可见，辅助删除操作可滚动到达。
独立界面复核检查九张最终原生截图与源码，结论 `disposition: ship`，没有要求额外修正；
依据保存在同目录 `finish-review.md`。此结论覆盖本次引导界面，不代表真实账户或 VoiceOver 验收。
独立文档复核同样给出范围内 `ship`，记录为 `documentation-review.md`；纠正了 Web 使用 Geist、
桌面使用 macOS 系统字体的表述，保留既有 Atlas 设计文档及其历史偏差。

## 尚未完成的发行验收

这些需要真实环境或外部服务，不能由 fixture、SDK 模拟或构建结果代替：

- macOS 14 干净用户、实际网络下载 quarantine、安装/卸载/重装与 N→N+1 数据兼容。
- 真正 Provider、外部 MCP/OAuth、实际冻结包内命令沙箱和 Agent worktree 导入。
- 两条 Live 媒体链路、麦克风首次/拒绝/撤销授权、WKWebView 图形与资源占用。
- 中文输入法组合过程、VoiceOver、系统 Keychain 访问和跨版本身份授权。
- GitHub hosted runner 实际执行（本次只生成并本地验证工作流对应命令）。

未公证的下载包可能被 Gatekeeper 拦截。按[桌面指南](../desktop.md)核对来源/摘要后通过系统
“仍要打开”明确允许；不提供关闭 Gatekeeper 或自动清除 quarantine 的脚本。
用户指南、构建脚本与发行物已准备，公开发布仍需用户后续授权。
