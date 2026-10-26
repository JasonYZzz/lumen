# 全仓库质量扫描与修复（2026-10-23）

Lumen 是本地通用 Agent framework，Web、TUI、桌面和 headless 共用 WorkspaceHost。
本轮按单用户、本地工作区、多 Host 互斥执行的实际边界检查，不按多租户服务推定负载。
检查对象是含已有未提交 V2 改动的当前工作区；没有提交、推送、发布或重启用户正在使用的服务。

## 覆盖与结论

结合源码追踪、故障注入、全量测试、生成物校验与浏览器抽查，修复四个行为问题及当前文档漂移。
源码深入检查集中在自动化 Store/Engine 生命周期、Host 启停与终态收录、文件导入、Web 鉴权、
偏好保存、模型设置、TUI 历史分页。其他 Runtime/Loop/Context/Gateway/Provider/Agent 路径
由全量契约回归覆盖；不声称逐行人工审计了所有实现。

## 已修复问题

| 优先级 | 可复现情形与影响 | 最小修复及证据 |
| --- | --- | --- |
| P1 | 自动化取得 owner 后，恢复读取抛出 ValueError。Engine 关闭数据库却仍持有锁且 owner=true；Host.close 随后对已关闭库调用 interrupt_goals，退出失败，其他 Host 无法接管 | 启动失败复用 Engine.close 清理，再进入 storage_error。新增回归验证另一把锁能取得所有权、普通对话可完成、Host 可关闭 |
| P1 | Store.open 检测到缺表或无版本的非空数据库后抛错，但留下连接；同一对象第二次 open 直接返回，绕过布局校验 | 打开流程任何异常都关闭并清空连接后重抛。两种损坏布局各连续打开两次，均拒绝，且不重建数据库 |
| P2 | TUI 在历史顶部前插旧消息时仍可能受已排队的尾部跟随影响，随后相对滚动再次叠加偏移。全量初轮记录原锚点从 y=2 移到 y=12，单独重跑可通过 | 前插前关闭尾部跟随并使旧回调失效，布局后用同一消息绝对定位。已有回归增加后续布局刷新及跟随状态断言；定向和最终全量通过 |
| P2 | 偏好保存或保存后刷新期间，文本、范围与取消编辑仍可操作；成功回调清空用户在等待时新输入的草稿 | busy 期间锁定三个入口，完成/失败后解锁。新增组件回归覆盖写入等待及随后刷新等待 |

两个后端损坏布局用例和启动恢复用例在修复前合计 3 项失败，修复后通过；新增偏好回归也先失败后通过。
没有新增依赖、公开抽象、配置项或数据 schema。保留 Session v1–v11 兼容读取、v12 journal、
助理库 v1 备份迁移、权限审批、损坏状态拒绝重建和普通对话降级路径。

## UI 检查

浏览器只读查看已有本地服务的桌面目标页、偏好设置以及 390×844 手机视口。
手机目标页 document.scrollWidth=390、innerWidth=390，无横向溢出；五个主导航文字可见，
偏好设置有可滚动内容区。已还原浏览器视口，未保存偏好、提交任务或修改用户数据。
运行中服务未重启，浏览器截图证明现有布局；本轮新 busy 行为由组件回归和新构建验证。

| 抽查维度 | 评分（0–4） | 依据与限制 |
| --- | --- | --- |
| 可访问性 | 3 | 可见标签、原生输入、dialog 与焦点管理存在；未做屏幕阅读器及全页面对比度认证 |
| 性能 | 3 | 主路由首屏 JS 290 kB，文件查看器按需加载；未做设备帧率、压力或内存长期测量 |
| 响应式 | 3 | 桌面和 390px 设置/目标页抽查通过；未做物理手机触控、软键盘验收 |
| 主题 | 3 | 五种外观映射与存储同步有测试，当前视觉规范已记录；未穷举全部页面和外观组合 |
| 实现一致性 | 3 | 前端经 Host API 工作，偏好并发编辑缺口已修复；装饰检测不作为行为缺陷证明 |
| 合计 | 15/20 | 有界抽查结果，不是 WCAG 合规或全设备质量认证 |

Impeccable 静态扫描产生 5 项主提示、231 项 advisory。逐项核对主提示：运行中文字渐变
有真实运行状态及 reduced-motion 分支；滚动刻度的微小宽度过渡未有实测卡顿证据；代码区
等宽字体和隔离 Word 预览的 Arial 各有阅读用途。未将这些风格提示冒充功能漏洞，也未为
清空检测器而改写既有视觉系统。advisory 不代表 231 个已确认缺陷，未逐项进行视觉认证。

## 文档与生成物

- 第 16 章把旧暖白/绿调说明改为当前樱粉/灰紫及独立外观偏好，修正仅 UTF-8 导入的过时描述，
  补充打开失败与恢复失败的清理契约、偏好保存期间的交互规则。
- 文档地图指向本次验证；历史验收日期、计数和发布限制保持原意。
- contracts 与 Provider catalog 新鲜度通过。OpenAPI/TypeScript schema 重新生成后与执行前
  SHA-256 完全一致；工作区原有 schema 改动不是本次生成漂移。
- Atlas 原先过期，按正式命令重建并检查；同时核对 index.html 中 Host、owner、SQLite v2、
  目标推进与后台预算的手写图解，本次修复不改变这些架构关系。
- Markdown 本地文件链接目标扫描通过；该扫描不验证外网可达性或 Markdown 锚点。

## 实际验证

环境：macOS arm64，Python 3.12.14；本机 Node 24.19.0 / pnpm 12.9.1，区别于 CI 的 Node 22 / pnpm 10。

| 检查 | 结果 |
| --- | --- |
| `uv run --frozen ruff check .` | 通过 |
| Pyright `--pythonplatform Darwin / Linux / Windows` | 三种平台类型检查均 0 errors |
| 后端定向：automation、TUI、workspace_host、web_api | 140 passed |
| `uv run --frozen --extra web --extra live pytest` 最终全量 | 1614 passed、1 skipped；60 snapshots passed |
| `pnpm --dir src/web test` | 31 files、287 tests passed |
| Web `typecheck`、`build` | 通过；静态导出成功 |
| `cargo test --manifest-path src/desktop/Cargo.toml --locked` | 2 passed |
| `uv build` | sdist 与 wheel 构建成功 |
| 临时独立 venv 安装生成 wheel，从临时目录运行 CLI | `lumen --version` 为 2.0.0；`--check-config` 通过；内置 Web index.html 存在 |
| contracts、Provider catalog、Atlas、schema 重生成一致性、diff whitespace | 通过 |

初轮全量为 1612 passed、1 skipped、1 failed（TUI 滚动）；上述最终结果包含本轮新增回归与修复，
不把首轮失败隐藏为持续全绿。wheel 检查使用无网络模型调用的 fixture 配置；安装依赖由包元数据解析，
不代表锁文件依赖下的另一次全量运行。

## 验收边界

未执行真实 Provider/外部 MCP 请求、真实设备/屏幕阅读器验收、断电与长期 scheduler 压力测试、
Linux/Windows 运行时测试或完整 Python 3.11–3.13 CI 矩阵；未构建签名 DMG 或执行 Keychain 交付验收。
本轮修复和回归已完成，不能据此宣称上述发行门禁完成。正在运行的服务需常规重启后才加载后端修复。
