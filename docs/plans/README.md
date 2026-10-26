# 活动计划

本目录只保留仍有明确未完成门禁或条件式后续工作的计划。它们是工作记录，不是实现规范；完成状态应以
源码、测试和 [`../architecture-guide/`](../architecture-guide/README.md) 为准。

状态复核：2026-10-23；本次新增 Agent 效果升级计划，既有计划状态及历史验收日期保留。
计划正文保留原始目标；当前状态及未完成门禁见下表和各计划的实施记录。

| 计划 | 当前状态 |
|---|---|
| [Agent 效果与 Harness 改进](2026-10-23-agent-quality-harness-improvement-plan.md) | Proposed；先交付隔离评测和经验证技能，再按收益门禁推进实验经验库与一次配置优化，尚未实施 |
| [v2 目标驱动与资料空间优化](2026-10-21-lumen-v2-product-flow-upgrade.md) | 本地实现已落地；目标／对话／成果、资料版本与集合、受限检索跟进及四项修复已覆盖回归；P4 跨平台／设备／长期发行门禁仍开放，见计划实施记录 |
| [外部 Agent 接入设计与实施计划](2026-09-15-external-agent-integration-plan.md) | Proposed；先验证单个受限 backend，再扩展既有 Factory Seam，尚未实施 |
| [Web Search / Fetch 升级](2026-09-14-web-search-upgrade.md) | Phase 0–2 已实现；SearXNG、可选增强提取／浏览器层与结构化输出，Phase 3 只在真实失败证据满足时启动 |
| [WebUI Three.js 升级](2026-09-18-webui-threejs-upgrade-plan.md) | M0 静态基线、M1–M3、M5 加载接口已实施；用户跳过 M4，正式角色资产及真实 Live 媒体验收待补 |
| [运行时可靠性与性能优化](2026-10-09-runtime-optimization-implementation-plan.md) | 核心切片已实施；冷索引、真实 Provider／跨平台推广与磁盘容量门禁继续跟踪，偏差和取舍见第16节 |
| [macOS Apple Silicon 桌面应用](2026-10-09-macos-apple-silicon-desktop-implementation-plan.md) | 开源 Beta 已实施并完成本地 arm64 构建与首次引导验收；ad-hoc 签名、人工更新，真实 Provider／macOS 14／媒体与 Keychain 等发行验收待补 |

已被 Accepted 决策和当前实现完全替代的计划，在迁移有效门禁后删除；保留的计划必须说明剩余工作。
删除依据见 [当前实现审计](../architecture-guide/12-current-implementation-audit.md)。

旧 Rive 角色计划的条件式资产门槛已并入 Three.js 计划第 10 节；早期 Harness 计划的真实
Provider/性能推广门槛已并入运行时优化计划第 17 节，不再维护重复计划。
