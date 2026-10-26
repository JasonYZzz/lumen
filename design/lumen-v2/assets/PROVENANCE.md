# Lumen v2 资产来源

## r2 当前使用

- `lumen-mark.svg`：逐字节复制自 `src/web/public/lumen-mark.svg`，保留当前 Lumen Logo。
- `mascot/fox-idle.alpha.mp4`、`mascot/fox-react.alpha.mp4`、`mascot/fox-poster.png`：逐字节复制自 `src/web/public/mascot/`；没有重新生成或编辑角色。
- `mascot-player.js` / `.css`：由 `../build-mascot.cjs` 自动构建，入口 `../mascot-entry.tsx`。使用现有 React 及 `VideoMascotRenderer`、motion / visibility hooks；仅在打包时适配素材 URL，不更改生产源码。bundler 的许可注释及 `React-LICENSE.txt` 随文件保留。
- `Geist-*.woff2` 和 Phosphor 图标沿用下方许可；新增导航图标也从同一已安装图标库导出。
- `../evidence/r2-*.jpg`：本轮内置浏览器实际截图，含桌面四页及 390 × 844 对话与信息流；文件为 JPEG。

## r1 历史提案（当前页面不使用）

- `lumen-fox-v2.png`：内置 ImageGen 生成，2026-10-20。透明 PNG，1254 × 1254。像素与 alpha 保持原样，仅写入原始生成提示词元数据。
- 身份参考：`src/web/public/lumen-fox-complete-idle-9tail.png`，Lumen 仓库原有九尾狐。保留暖白毛色、额间火纹、琥珀眼睛与淡彩尾尖；生成新 Q 版提案，不覆盖原文件。
- 原始生成文件：`/Users/admin/.codex/generated_images/01a15832-232d-72a2-a1fa-3acb8ab7026d/exec-03362768-6069-4a86-af7e-011c9b7ef9c3.png`。项目内副本为交付权威路径。
- `Geist-*.woff2`：从现有 `src/web/node_modules/geist/dist/fonts/geist-sans/` 复制，许可证见 `Geist-LICENSE.txt`。
- `icons/*.svg`：从现有 `@phosphor-icons/react` 2.1.10 的 `dist/defs/*.es.js` 导出 regular 图标路径，以已安装 ReactDOM `renderToStaticMarkup` 生成 SVG；无手工改画，许可证见 `Phosphor-LICENSE.txt`。
- `../evidence/01—14`：第一轮内置浏览器实际截图。01—05 是 Muse 本地参考 / 当时 Lumen 的结构证据；06—14 的暖色设计与形象页已被 r2 替代。截图不是产品行为或性能承诺。

## 完整生成提示词

```text
Create a NEW V2 mascot design for Lumen based on the attached ORIGINAL Lumen fox reference (reference identity, not copy). One single full-body adorable CHIBI nine-tailed baby fox, polished premium 3D clay illustration with matte porcelain softness, transparent alpha background, no ground/background. Preserve warm ivory fur, subtle apricot inner ears, gentle amber/honey eyes, small flame-shaped forehead marking and very soft pastel lavender and ice-blue tips. Strong redesign toward cuteness: oversized round head 45% body height, tiny short legs, small plump bean-shaped torso, short rounded muzzle, small affectionate smile, round expressive eyes with understated highlights, ears less tall/sharp than source. Exactly NINE individually countable fluffy tails forming a beautiful rounded fan, with clear separation and no cropping. Front three-quarter pose seated, little right paw slightly raised in a friendly hello, nine tails framing the silhouette. Friendly capable personal assistant, calm warm intelligent companion, NOT fierce, NOT seductive, NOT ornate fantasy, no jewelry, no accessories, no text, no UI, no badge, no extra objects. Soft warm studio lighting, subtle ambient occlusion only on character. Clean silhouette legible at small UI size. Entire character comfortably within square composition with clear margin around all nine tails. A single production character cutout, not a character sheet.
```

第一轮使用内置工具，`transparent_background=true`，参考图通过 `referenced_image_paths` 传入。未使用 API key 或 CLI fallback。本轮按用户反馈停止采用这个新形象，直接复用既有动态形象；上述提示词仅保存历史来源。
