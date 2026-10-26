# 九尾狐成长形象升级验证

日期：2026-10-22。范围为 Web 形象、页面配色与成长设置反馈；不更改 Runtime、权限或 Provider 请求。

## 实现

- 用户提供的五张透明 PNG 保留来源；转换为原尺寸 1600×1600、480px、160px 共 15 张 WebP，
  quality 95、alphaQuality 100，不裁剪或放大。原 PNG 合计 8.10 MiB，全部 WebP 2.56 MiB。
- 默认/off/minimal 为 01，low 为 02，medium 为 03，high 为 04，xhigh/max 为 05。
  明确设置按 Host effective level 映射；默认、未校验或不支持设置不推测档位。
  成长只表示设置反馈，不累计经验、不根据时长升级，不代表任务进度或模型能力。
- 首页、导航、工作状态头像、空状态、思考图标及 Web favicon 使用新形象；思考面板预览
  当前/拖动形态并可展开五阶段说明。保存失败不改变主页面的已保存设置投影。
- `/mascot-preview.html` 提供五阶段、深色背景、静态模式与原尺寸 WebP 下载；不写会话设置。
- 原视频/分层/Rive/实验 3D 组件、专属测试、素材和制作脚本在生产代码零引用后移除。
  共 57 个文件的原始快照保存在 `local-backups/identity-before-snow-fox-2026-10-22/`，
  含 SHA-256 清单；根 `.gitignore` 排除，备份不进入 public 或静态导出。
  保留共享的可见性/动态偏好 Hook、通用 GLB 文件预览、语音场景和旧研究链接入口。
- DESIGN、PRODUCT、设计 sidecar 与品牌/命令说明同步；Atlas/TUI/原生启动与应用图标继续
  使用 Fold。已有不相关的 dirty/untracked 内容和原素材的暂存状态保留。未提交或推送。

## 实际验证

- 素材转换脚本断言：五张输入、透明通道、输出格式及尺寸全部通过。
- Web 全量 Vitest：30 文件、274 测试通过。随后增加“已知 high 默认仍保持基础形象”断言，
  设置面板最小回归 11 项通过。覆盖档位/alias、默认与未校验、图像回退、静态交互及定时器清理。
- `pnpm --dir src/web typecheck` 与最终 `build` 通过。
- `uv run ruff check .` 通过；`uv run pyright` 为 0 error / warning。
- `git diff --check`、备份 57 文件摘要复核、`git check-ignore`、文档本地链接检查通过。
- 静态导出含新形象及 favicon，不含旧狐狸素材；运行代码/脚本无旧渲染器引用。
- Chrome 实际页面：1440×1000 首页与设置、390×844 首页与设置，预览页实际 1920×848
  及 390×844。五阶段逐一点击、图像 loaded、高清下载链接随阶段改变；手机 scrollWidth=390，
  静态模式 computed animationName=none。没有创建模型 run 或修改真实会话设置。
- 单次设计 detector 只有字号/预览专用色的 advisory，已同步设计文档。独立 finish review
  确认七张截图有效；唯一必要修复为深色焦点圈，改用 `line-strong` 后对背景对比度 8.12:1。
  复核桌面与手机真实键盘聚焦截图后，该项评分 resolved，disposition: ship，限于该修复。

截图位于 `.impeccable/review/snow-fox-2026-10-22/`：`desktop.jpg`、`mobile.jpg`、
`desktop-settings.jpg`、`mobile-settings.jpg`、`preview-desktop.jpg`、`preview-dark.jpg`、
`preview-mobile.jpg`；`detector.json` 为本轮原始扫描结果。

## 未验收边界

未运行真实 Provider 请求、Safari/iOS/Android 真机、完整 Python pytest 或桌面安装包构建。
组件测试与 Chrome 响应式模拟不等于这些环境的实机验收；本次没有新增 API 或运行时行为。
