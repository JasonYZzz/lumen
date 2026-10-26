# 九尾狐高清重绘、品牌恢复与浏览器适配验收

日期：2026-10-22。本轮承接用户的截图反馈，修正 Web 形象清晰度、九尾数量、品牌入口和短屏适配。
前轮记录保留在 `2026-10-22-snow-fox-identity-validation.md`，其素材与品牌说明不再代表当前实现。

## 最终实现

- 左上、右上使用原 Lumen Fold SVG L；网页 metadata 恢复 `/favicon.svg`。
- 内置 `image_gen` 参考原幼灵初醒，重新生成同一角色的五成长阶段及基础闭眼帧。
  六张原生 PNG 均为 1254×1254，包含真正 alpha；没有插值放大或声称 2K/4K 输出。
  五阶段逐张目视确认九个独立尾尖；完整提示词见 `../brand/mascot/fox-v2-prompts.md`，
  原图保存在 `../brand/mascot/sources-v2/`。
- 转换脚本输出原尺寸、960px、480px、160px 无损 WebP，加一张 960px 闭眼帧，共 21 张、约
  12.16 MiB。原生生成 PNG 在 public 之外；运行时按需加载，480w/960w 与真实尺寸一致。
  版本化 URL 避免继续命中上一轮图像缓存。原用户 PNG 输入及其暂存状态保持不变。
- 桌面形象 280px、手机 192px；高度不超过 700px 时使用 160px。首页不压缩内容，允许纵向滚动；
  workspace-shell 明确占满可用宽度并保留 min-width:0，viewport-fit 支持安全区。
- 5.6s 轻呼吸、650ms 轻点回应、输入关注、范围约 ±2.5° 的指针反馈；基础形象每 6.8s
  眨眼约 136ms，只显示配套闭眼帧的眼部。阶段 2–5 使用呼吸与回应，没有声称独立尾巴或骨骼动画。
  inactive/static/reduced 停止动画，眨眼图片不挂载；离开指针区域复位，定时器卸载清理。
- 思考强度映射和 Host 设置保存边界沿用前轮。L 品牌标志不跟随成长；预览页不会修改真实设置。
- 本轮前快照位于 Git 排除的 `local-backups/identity-before-retina-motion-2026-10-22/`，
  42 文件带 SHA-256。前轮 57 文件快照也保留。两组摘要均复核通过；无提交、推送或外部发布。

## 实际验证

- Web 全量 Vitest：30 文件、277 测试通过；覆盖响应式图像路径、五档映射、回退、静态模式、
  隐藏/恢复时的眨眼挂载、指针反馈与离开复位、定时器清理、原有对话与业务流程。
- `pnpm --dir src/web typecheck`、`pnpm --dir src/web build`、转换脚本断言与 `node --check` 通过。
- 单次设计 detector 结果为空数组；`git diff --check`、设计 sidecar JSON、链接与备份排除检查通过。
- Chrome 视口：1913×895、1440×1000、2560×1080、820×700、1366×600、390×844、320×568。
  七组 DOM 的 shell.right、innerWidth、document.scrollWidth 相等，没有横向溢出。
  1913px 实测选用 960px 图，角落两处均为 L SVG，静态导出的 icon 指向 `/favicon.svg`。
  浏览器自动化的临时 favicon 标记不作为产品 favicon 声明。
- 320px 短屏的 stage clientHeight=435、scrollHeight=494；真实滚动到 scrollTop=58.5 后，
  输入区 bottom≈409，小于底栏 top=505，底部说明也可达。测试 viewport 覆盖已 reset。
- 实际点击招呼后 DOM 确认 greet/breathe/blink 动画存在，闭眼帧 ready=true；五阶段逐一切换
  并更新高清下载链接，图像均已加载。静态模式 computed animationName 均为 none。
- 独立 finish reviewer 阅读全部 13 张截图、当前源码和素材，disposition: ship，无必要修复。
  `preview-mobile.jpg` 是 390px 宽的 full-page 捕获，内容高度 850px；测试视口高度为 844px。

截图与 DOM 记录位于 `.impeccable/review/snow-fox-retina-2026-10-22/`，含七个尺寸、短屏滚动后、
桌面/手机设置面板及浅色/深色预览；`responsive-evidence.json` 保存边界与图像加载结果。

## 验收边界

用户原截图右侧红色区域未在当前页面 DOM 中复现；当前各尺寸铺满及 viewport reset 不等于
已证明外部 Chrome 主题或窗口问题的根因。未运行真实 Provider 请求、Safari/移动真机、完整 Python
pytest 或桌面安装包构建；本轮没有新增 API、权限或后端执行语义。
