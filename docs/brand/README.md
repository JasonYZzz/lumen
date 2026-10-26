# Lumen Brand System

Lumen Web 的左上品牌标志、右上活动入口及 favicon 使用 **Lumen Fold** 的 L 标志。
2026-10-22 的九尾狐升级用于对话首页、设置预览及空状态；思考图标恢复 `lumen-claude-amber.svg` 及原有呼吸摆动动画。角色基于用户参考重新生成为
五种九尾形态，原生源为 1254×1254 透明 PNG。五档浅色背景对应独立的形象成长，与思考强度无关；角落 L 标志不随形态变化。规则与资产见
[`mascot/README.md`](mascot/README.md)，Web 色彩、布局和组件见
[`src/web/DESIGN.md`](../../src/web/DESIGN.md)。本轮仅升级 Web，未替换其他客户端的既有身份。

Architecture Atlas、TUI、macOS 原生启动页、原生应用图标及 README 继续使用 **Lumen Fold**。
以下几何、色彩与 SVG 规范适用于 Fold：粗体字母 L 是品牌名称与本地执行路径的共同骨架，转角处的透明
折叠光隙代表 Open Agent Harness 把模型推理转化为工具行动。标志只保留一个主体和一个负空间切口，
在延续琥珀色视觉识别的同时，移除了旧标志的多层入口、复杂透视和装饰性轨迹。

## Fold 资产

| 资产 | 用途 |
|---|---|
| [`lumen-mark.svg`](lumen-mark.svg) | Fold 彩色透明标志，用于保留 Fold 身份的界面与文档 |
| [`lumen-mark-mono.svg`](lumen-mark-mono.svg) | 单色印刷、终端或受限色彩环境；通过 `currentColor` 着色 |
| [`lumen-lockup-dark.svg`](lumen-lockup-dark.svg) | 深色背景横向组合 |
| [`lumen-lockup-light.svg`](lumen-lockup-light.svg) | 浅色背景横向组合 |

实际产品中的字标使用可访问的 DOM 文本 `lumen`，而不是把文字烘焙成图片；这样可以保持清晰、可缩放、
可选择，并复用 Web 的 Geist 字体栈。macOS 原生启动页复用标志和可访问字标，采用系统字体栈；
应用图标在 `src/desktop/icons/`。静态 lockup 是导出预览，保留相同的字体 fallback。

## 几何与安全区

- 基础画板：`64 × 64`。
- 标志主体外接范围：`x=8..60`、`y=5..60`。
- 最小安全区：四周至少 `6` 个画板单位；与字标组合时，mark 与 wordmark 间距不少于 mark 宽度的 `0.28`。
- 最小显示：彩色标志 `18 px`；小于 `18 px` 使用 favicon 的深色底版本。
- 不封闭或填平折叠光隙，不改变 L 的纵横比例，不增加 glow/filter。Web 九尾狐资产遵循独立的形象规范。

## 色彩

| Token | Hex | 用途 |
|---|---|---|
| Lumen Amber | `#F39A1D` | Fold 标志主体 |
| Fold Gold | `#FFD16A` | 折叠上沿与琥珀高光 |
| Copper Edge | `#B85A0A` | 转角深部与浅色背景对比 |
| Carbon | `#090A09` | 深色组合背景 |
| Warm Paper | `#F7F3EA` | 深色背景字标 |

## Fold 保留位置

- Web 左上品牌、右上活动入口及 `/favicon.svg`；
- Architecture Atlas favicon 与顶部品牌标志；
- macOS 原生启动页与 `src/desktop/icons/` 原生应用图标；
- TUI 欢迎页的 Lumen Fold 像素变体与小写 wordmark；
- README 深色/浅色自适应首屏 lockup。
