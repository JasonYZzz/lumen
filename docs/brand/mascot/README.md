# 首页狐狸连续动画

2026-09-04：按 meoo.com 的“预渲染角色动画 + 页面交互”方向制作；没有复制其角色素材。
当前默认不使用实时 3D，也不再依靠多张整身 PNG 切换来表现动作。

## 素材与播放

- 原角色：`src/web/public/lumen-fox-complete-idle-9tail.png`，保持未改动。
- `fox-video-reference.png`：使用 imagegen 准备的同角色绿幕首尾帧；生成过程会存在细节差异，不承诺像素级复刻。
- 极客智坊 `MiniMax-H3`：分别生成待机、回应两条视频。请求 6 秒、768P、首尾同图、无水印、无自动重试；输出约 6.58 秒、768×768、24fps。
- 发布素材：`src/web/public/mascot/fox-{idle,react}.alpha.mp4` 和 `fox-poster.png`。视频去掉音轨，512×512 RGB 与灰度 alpha 横向拼接为 1024×512 H.264，合计约 1.65 MiB。海报约 238 KiB。
- 浏览器用原生视频解码 + WebGL 两次纹理采样恢复透明度，没有逐帧 React 更新或 CPU 抠图。
- `MascotPlayback` 是动作切换的唯一权威：待机循环；点击或输入焦点变化排队回应；完成动作后回待机；重复触发合并，不打断动作。响应可能等待剩余循环，最多约 6.6 秒。这不是即时自由操控的骨骼角色。
- 页面隐藏/离屏暂停、回来继续；减少动态/静态模式使用海报；视频加载、自动播放或 WebGL 失败安全降级为海报。无音频。
- 保留旧实现作为显式回退：构建时设置 `NEXT_PUBLIC_MASCOT_RENDERER=layered` 或 `rive`。旧导演与视频播放不会同时运行。此次未删除旧素材或旧实现。

## 本地验收

`/mascot-preview` 提供 168px 原尺寸、336px 放大、半速、暂停、深色背景和静态模式。
通过 Lumen 的静态文件入口访问构建产物时使用 `/mascot-preview.html`。
重点看脸型是否漂移、尾巴是否粘连、循环接缝是否明显；不要用静态截图代替动态验收。
首页 `MascotScene` 已默认使用同一播放器，原有输入焦点与提交活动仍通过原 Interface 接入。

## 密钥与制作脚本

只在离线制作阶段调用商业 API，前端、浏览器和线上首页不接触密钥，也不会按访问次数调用生成接口。
从环境变量 `GEEKAI_API_KEY` 或 macOS 钥匙串读取（service `lumen.geekai.video`，account `lumen`）。
不得把真实密钥写到本文件、`.env`、命令历史或仓库。已在聊天中暴露过的密钥建议轮换。

在 `src/web` 运行，`$PRODUCTION_DIR` 应为仓库之外的私有绝对路径：

```sh
node scripts/generate-mascot-video.mjs --action model
node scripts/generate-mascot-video.mjs --action submit --clip idle --image ../../docs/brand/mascot/fox-video-reference.png --out "$PRODUCTION_DIR"
node scripts/generate-mascot-video.mjs --action status --clip idle --out "$PRODUCTION_DIR"
node scripts/generate-mascot-video.mjs --action download --clip idle --out "$PRODUCTION_DIR"
node scripts/pack-mascot-video.mjs --input "$PRODUCTION_DIR/idle.original.mp4" --output public/mascot/fox-idle.alpha.mp4 --poster public/mascot/fox-poster.png
```

互动素材将 `idle` 改为 `react`，且不再生成海报。打包需本机 `ffmpeg`，可用 `FFMPEG_PATH` 指定，不新增前端依赖。
脚本拒绝覆盖目标输出。提交前独占写入回执，避免超时后重复付费；结果不明确时必须人工核对平台账单，不删除回执重试。
本轮共提交两次付费生成。模型查询单价 ¥0.5/请求秒，按 2×6 秒预估 ¥6；实际以平台结算为准。
响应中的 `video_result` 实测为数组，脚本兼容数组/单对象。输出 CDN 下载不发送 API Authorization。

参考接口：[极客智坊 MiniMax-H3](https://docs.geekai.co/cn/docs/video/minimax/MiniMax-H3)。
未来若需即时跟随、任意转身或丰富情绪组合，再评估专用 2D 骨骼/3D 模型；框架升级本身不能改善素材动作质量。

## 本轮验证记录

- Web 全量 Vitest：19 文件、140 测试通过，其中宠物相关 22 测试。
- TypeScript typecheck、Next production build、两个制作脚本的 `node --check` 通过。
- Chrome 宽屏实际首页：确认 idle 时间连续推进，点击后切换 react，并返回 idle。
- 预览页：168px/336px、浅色/深色背景、暂停位置保持、静态降级已检查；没有明显绿底残留。
- 两段视频抽帧检查了脸、尾巴与首尾姿态，编码产物为 H.264、24fps、无音轨。AI 生成的小幅细节变化仍应由用户动态验收。
- 尚未实机测试 Safari/iOS；采用 H.264 + WebGL 是兼容方案选择，不代表已完成跨浏览器验收。
