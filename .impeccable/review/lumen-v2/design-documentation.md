# Lumen Web v2 design documentation

本轮为现有产品扩展的 finish documenter，依据已实现结果更新 app scope；没有重新选择视觉方向。

## Evidence read

- Shipped documenter definition: `/Users/admin/.agents/skills/impeccable/agents/impeccable_documenter.toml`。
- Format/sidecar contract: `/Users/admin/.agents/skills/impeccable/reference/document.md`；同时核对 skill 主文件和 metadata 相关说明。
- Existing app context: `src/web/PRODUCT.md`、原 `src/web/DESIGN.md`。
- Approved composition context: `design/lumen-v2/DESIGN.md`（r2）；只作为构图依据，未把原型数据或全部 token 移入生产规范。
- Production: `src/web/src/app/globals.css`（按 tokens、shell、focus、composer、overlay、responsive 采样）、`src/web/src/components/assistant-workspace.css`、`assistant-workspace.tsx`、`personal-preferences.tsx`、`lumen-app.tsx`、`landing-entry.tsx`、`landing-entry.module.css`、`mascot/mascot-scene.tsx`、`mascot/video-mascot-renderer.tsx` 相关路径，以及 `src/web/src/app/layout.tsx` 的字体来源。
- Review: 同目录 `finish-review.md`、`finish-verdict.md`；ship 仅覆盖已列明的两项错误恢复修复。
- Visual evidence inspected: `desktop-final.jpg`、`mobile-final.jpg`、`desktop-detail-error.jpg`、`mobile-detail-error.jpg`、`preferences-error.jpg`、`preferences-recovered.jpg`。截图是验收记录，不是新增生产图片。

## Files written

- `src/web/DESIGN.md`：把既有摘要合并为带规范 token 的文档，保留白灰黑、Geist、原 logo 与动态九尾狐。使用生产 body line-height 1.75、13px/36px actions、235px feed aside 及实际断点；没有照搬 r2 的按钮、semantic chip 或 dark-preview 数值。
- `src/web/.impeccable/design.json`：新增 schemaVersion 2 sidecar，包含 color/type metadata、8-step 色带、实际 shadow/motion/breakpoint 扩展、8 个自包含组件片段及与文档逐字对应的 narrative。色带是按 contract 合成的展示辅助，不是生产 palette 或新增 primitive。Composer 片段只展示其表面，描述明确保留生产 overlay editor/toolbar。
- `.impeccable/review/lumen-v2/design-documentation.md`：本记录。

`src/web/PRODUCT.md` 的 durable product facts 已足够，本轮未修改。根 PRODUCT/DESIGN、根 sidecar、生产代码、原型和原狐狸媒体均未改动。

## Contract validation

PASS — 本地 `uv run python` 使用 PyYAML/JSON 解析，assert 检查：frontmatter 只使用规定 token groups；component props 仅用允许的 8 个属性；全部 token refs 可解析；八个 canonical section 的顺序正确；sidecar schemaVersion、ISO 时间、title、extension groups、color/type metadata keys 与文档对应；每条色带为 8 步；8 个组件引用现有 frontmatter token，HTML class 均以 `ds-` 开头，无外部图片或脚本依赖，交互组件包含 focus；north star、overview、characteristics、rules、do/don't narrative 与 Markdown 一致。

`git diff --check` 通过；因这些 scope 文件当前为 untracked，另按文件内容检查末尾空白和结尾换行，避免只依赖 Git 的 tracked diff。未运行联网官方 linter、浏览器、构建或重复功能测试；本轮仅写文档和 sidecar，schema 检查为本地 contract assertions，不冒充官方 linter 或组件渲染验收。

## Drift not canonized

现有全局 `--sidebar` 为 `#f5f5f3`，实际 sidebar 使用 literal `#fafafa`；frontmatter 以 rendered sidebar 为准并说明差异，没有修改 CSS。既有不同 focus 样式、11px legacy inline-error 文本也未提升为新通用 token；本轮授权是记录并补齐当前系统，未扩展为旧样式整理。没有发现需要新增视觉世界、媒体或 theme 的依据。
