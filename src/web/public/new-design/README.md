# 九尾狐素材来源

五张中文文件名 PNG 为用户提供的原始设计，保留作输入参考。
当前运行时使用 image_gen 重绘的 v2 五种九尾形态；原生透明 PNG 为 1254×1254，
来源与完整提示词保存在 [`docs/brand/mascot/`](../../../../docs/brand/mascot/README.md)。

运行 `node src/web/scripts/convert-fox-assets.mjs` 从 `docs/brand/mascot/sources-v2/` 转换：

- `fox-stage-0N-v2.webp` 保留 1254×1254 原尺寸，供下载；不声称 2K，也不插值放大。
- `-960`、`-480` 和 `-160` 为缩小的 lossless WebP，保留透明通道。
- `fox-stage-01-v2-blink.webp` 为 960px 闭眼帧，运行时只显示眼部补丁；其余阶段没有眨眼帧。

共 21 张 WebP，约 12.16 MiB。首页使用匹配实际宽度的 480w/960w 响应式候选，
CSS 尺寸为桌面 240px、手机 168px、短屏 144px。左上、右上及 favicon 使用 Fold L 标志。
旧版 WebP 已归档至被 Git 忽略的本地备份；两轮备份、外观映射与动效范围见上述形象文档。
当前 UI 用樱粉、杏白、薄荷、雾蓝、浅紫五档浅色背景联动五种形态，与思考强度无关。
真实外观从首页输入框下方的「外观」五档滑杆直接调整，自动保存在当前浏览器；系统设置已移除原外观菜单。
素材文件名与原始生成提示词保留原样。
