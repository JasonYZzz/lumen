# WebUI 升级实施计划：真正接入 Three.js 生态

日期：2026-09-18。状态：M0 静态基线、M1–M3 和 M5 加载接口已实施。
用户要求先不做 M4；M5 最初按确认“只完成加载接口，等待正式模型”。
后续授权建模的首版未通过用户视觉验收，已退出发布和预览路径；继续默认视频。
真实模型启用必须满足[角色质量门禁](../brand/mascot/three/README.md)，不能将加载成功视为画质达标。
范围：`src/web` 主应用；Architecture Atlas 的 DESIGN.md 不作为主应用视觉规范。
依据：当前源码、用户提供的接入审计，以及当天复核的官方文档。

## 1. 目标与现状

目标是交付用户能实际使用的 Three.js 场景，并证明接入、交互与性能；同时保留现有对话阅读体验。
首个场景建议选择可展开的大语音视图，第二个场景是工作区 GLB 预览。
首页真实 3D 九尾狐在合格模型资产准备后单独推进，18px 思考标记继续使用 SVG。

| 当前事实 | 计划中的处理 |
| --- | --- |
| package.json 无 Three / R3F / Drei 直接依赖，源码未接入它们 | 第一阶段引入并实际挂载 Three + R3F；有具体调用再引入 Drei |
| 九尾狐是 alpha 视频 + 原生 WebGL，已经实现轮廓光与加载优化 | 保持可用；不把视频平面或 GLSL 效果称为真实 3D 角色 |
| 思考和工具过程已连续展示、活动文字流光、完成自动收起 | 作为升级后的固定行为，纳入后续回归 |
| 语音入口依赖 bootstrap.liveEnabled；当前为紧凑控件及状态驱动 CSS 波形 | 加入用户主动展开的大视图，紧凑控件仍可使用 |
| AudioContext 只在 host_websocket 路径创建；direct_webrtc 使用远端流和 Audio 元素 | 分媒体路径接入本地音量采样；不可假定已有统一音频图 |
| live.response.approved 还可能使用 speechSynthesis 播放 | 浏览器 TTS 无可直接读取的标准 PCM 节点；只显示播放状态，不伪造输出音量 |
| 前端文档后缀不含 GLB/glTF；后端按路径与 20 MiB 边界读取普通文件，无格式白名单 | GLB 先沿用已有安全读取，首版不扩大后端限制或默认支持外部资源 |

`outputs/threejs-ui-enhancement-research-2026-09-18.md` 中“无 WebGL”与该文自身的
视频 WebGL 描述冲突，不能当作当前事实或已证明的历史状态；其 Spline 传递依赖链也需以
实际 lockfile 复核。`docs/research/2026-09-18-webgl-threejs-opportunities.md` 的第 2 节是
首轮实施前快照，第 8 节记录后续变化。本计划以源码为基线，候选清单不算已实施。

## 2. 选型与依赖边界

| 包/能力 | 首次引入阶段 | 实际职责 |
| --- | --- | --- |
| `three` | M1 | 真正的几何、相机、灯光、材质和 WebGLRenderer |
| `@react-three/fiber` v9 | M1 | React 场景生命周期、Canvas、帧更新与资源管理 |
| `@types/three` | M1，开发依赖 | 匹配选定 Three 版本的 TypeScript 类型 |
| `@react-three/drei` | M3；M2 仅在确定使用具体材质时提前 | OrbitControls、Bounds 或其他有实际调用的模型预览能力 |
| 后处理 / Quarks / Spark / 图可视化 / Paper | 条件式后续 | 首轮不安装；由独立的场景需求和测量决定 |

R3F v9 对应 React 19，选定安装版本时重新检查 React/ReactDOM、Three、Drei 的 peer 范围，
以 pnpm-lock.yaml 固定实际解析结果，不照抄旧报告的 latest。
[R3F 官方兼容说明](https://r3f.docs.pmnd.rs/)

首轮使用 Three WebGLRenderer，不同时迁移 WebGPU。通过客户端组件中的动态 import
加载场景，兼容 Next 静态 export；普通对话和关闭的语音入口不请求 3D 模块。
第一次原型由自身组件管理 canvas 和清理；只有第二个场景确实重复了同样能力才提取公共部分。

## 3. 用户体验设计

### 对话与执行过程：保持已完成的升级

进展和工具按发生顺序直接展示，当前步骤与运行工具有文字流光；成功完成自动收起，
正文与简洁回看入口保留。工具参数、结果、来源、失败和审批保持 DOM。
18px 思考标记不创建 Three context；可复制、键盘操作和减少动态效果的行为不能退化。
完成后的“思考了”使用已记录整轮耗时，提示中说明包含模型、工具与等待。

### 大语音视图：第一个真实 3D 场景

用户开始语音后，可主动展开专注视图；打开视觉本身不再次申请麦克风或建立媒体连接。
中央为程序化网格构成的低幅度立体形体，有真实透视、材质和灯光；下方保留文字状态、
麦克风开关、打断、设备选择及结束入口。文字和按钮在 canvas 外。
沿用主应用的暖白、中性色与少量琥珀色；透明背景，不使用全屏粒子、景深或默认辉光。

| 状态 | 视觉与操作 |
| --- | --- |
| 请求权限 / 连接 / 重连 | 明确文字状态；形体静止或短时反馈，不显示假音量 |
| 聆听 / 用户说话 | 有真实输入 RMS 时轻微改变体积或表面形态；静音后归零 |
| 模型处理 / 工具执行 | 使用已有 Live 状态给稳定视觉区分；形体不代表完成百分比 |
| 助手说话 | 有真实输出音频数据才驱动音量；浏览器 TTS 仅显示说话状态 |
| 审批等待 | 视觉静止，审批信息和按钮优先，不能被大视图遮挡 |
| 打断 / 结束 / 错误 | 立即收敛并保留恢复入口；视觉失败不结束媒体会话 |
| 减少动态效果 / GPU 不可用 | 保留相同功能的静态 SVG/DOM 视图，不强制加载 3D |

桌面使用居中专注浮层；手机使用完整专注视图，结束、静音与打断保持可见。
关闭专注视图只退出视觉，结束语音是独立操作；焦点返回展开入口。
若 Live 未启用，不新增不能使用的入口，M1 在本地开发验收场景验证。

### GLB 预览：第二个真实 3D 场景

点击实际工作区 `.glb` 文件后按需加载；复用已有预览壳与下载入口。
支持旋转、缩放、重置视角、居中适配和加载失败重试。
键盘用户可使用 DOM 视角按钮，不以拖动作为唯一操作；关闭后返回文件入口。
首版支持内嵌 buffer 与纹理的静态 GLB，不承诺 glTF 外链文件、复杂骨骼、压缩扩展或 splat。
动画模型播放、Draco/KTX2 等作为后续明确的格式扩展，解码器需本地提供。

Three GLTFLoader 可以接受单独的 LoadingManager；资源 URL 可在请求前重映射或拒绝。
首版默认拒绝模型中的外部网络及外部工作区资源，不能让 loader 绕过安全读取直接请求。
已有 blob/data 资源也必须确认由本次模型读取拥有，释放时撤销；只修改 URL 不等于完成安全验收。
[Three GLTFLoader](https://threejs.org/docs/pages/GLTFLoader.html)、
[LoadingManager](https://threejs.org/docs/pages/LoadingManager.html)

## 4. 分阶段工作包与退出标准

| 阶段 | 实施任务 | 可交付结果与退出标准 |
| --- | --- | --- |
| M0：基线 | 整理报告时序，记录当前路由 JS、首次下载、输入延迟和设备；确认 Live 是否可测试 | 一份可复测基线，明确已实施/候选/未验收，不把包大小当页面成本 |
| M1：Three/R3F spike | 安装最少依赖并更新 lock；动态加载一处几何+材质+灯光场景；落实静态与 context-loss 回退 | 源码真实 import 并挂载场景，交互会改变 3D 对象；静态 export 成功；关闭后资源与循环清理 |
| M2：语音产品化 | 扩展现有 RealtimeVoiceClient 的本地音量回调；按传输模式采样；接入专注视图和全部状态 | 大视图可从真实入口打开，音量与状态来自真实数据，打断/静音/切换设备/重连/审批保持正确 |
| M3：GLB 预览 | 加后缀与二进制读取分支；引入有实际用途的 Drei；校验模型资源；接入视角/重试/下载 | 实际 GLB 在文件入口可预览；外部资源被拒绝；坏文件/超限/context loss/关闭切换均可恢复 |
| M4：验证与默认启用 | 同设备对比效果开关；测量动态 chunk、GPU/帧、输入延迟；覆盖桌面/Android/iOS | 指标和行为达到约定预算才启用；保留直接回退，文档写实际结果及未运行项 |
| M5：真实九尾狐，条件式 | 获取授权、建模、材质与骨骼动画；在原 mascot seam 接真实 GLB；对比视频路线 | 有真实几何、相机、灯光和动画资产，画质及设备成本足够才替换默认视频；资产不合格则继续视频 |

建议拆成 M0–M1、M2、M3、M4 四个可独立评审的改动。M5 是单独的资产与产品项目。
M1 失败或成本不可接受时，交付失败证据并移除未使用依赖，不只留下“已安装”的成果。
M2 依赖可用 Live 测试环境；M3 可以在 M1 通过后独立推进，不必等待语音全链路。

## 5. 代码定位和关键工程任务

| 入口 | 计划中的改动 |
| --- | --- |
| `src/web/package.json` / `pnpm-lock.yaml` | 按阶段加入直接依赖，检查许可证、peer 与真实 chunk |
| `src/web/src/components/live-voice-controls.tsx` | 保留紧凑入口；新增专注视图开关、焦点恢复，复用现有操作 |
| `src/web/src/lib/live/realtime-client.ts` | 本地音量回调、分析节点拥有权、设备替换、停止与重连清理 |
| `src/web/src/lib/live/live-reducer.ts` | 保持业务状态来源；不把每帧音量写入 reducer 或持久化事件 |
| 拟新增 `src/web/src/components/voice-scene.tsx` | 一处客户端 3D 场景与状态反馈；名称为计划项，不代表当前文件存在 |
| `src/web/src/components/document-preview.tsx` | GLB 识别、二进制读取、按需渲染、模型错误与下载保留 |
| 拟新增 `src/web/src/components/model-preview.tsx` | 场景、视角、格式验证、受控资源读取与关闭清理 |
| `src/lumen/files/documents.py` / `application/host.py` / `api/app.py` | 优先沿用当前读取 Interface；仅有真实需求才改契约和安全边界 |
| `src/web/src/components/mascot/*` | 第一轮保持默认视频路径；M5 才增加真实 3D 实现 |

语音实现的具体边界：

- host_websocket：复用现有 AudioContext，在 captureSource 和 playbackNode 旁路采样；
  只监听，不能把麦克风连成可听回放或把输出重复送到 destination。
- direct_webrtc：复用已取得的输入流及远端流；确实需要分析时由同一个 Client 创建并拥有
  一个 AudioContext，不额外 getUserMedia；Audio 元素继续拥有可听输出，分析旁路不重复播放。
- speechSynthesis：明确只有状态反馈，不将预定文字或随机值称为真实输出 RMS。
- 换设备重接输入分析源；静音、结束、取消连接、重连和卸载清理旧节点、计时器及回调。
- 音量通过 ref 更新，采样上限约 30 Hz，不按帧 React setState，不写日志/Session/Provider。
- 视觉不可见时停止采样与绘制，但不能停止用户正在使用的媒体连接；分析失败只降级视觉。

GLB 实现的具体边界：

- 沿用 workspace-relative 路径、descriptor/no-follow、普通文件和 20 MiB 限制；Windows
  保持已有不支持提示，不能用普通 open 绕过边界。
- 检查 GLB magic/version/声明长度及 JSON/BIN 结构；外部 URI 与未支持压缩扩展给出明确错误。
- 字节上限不能限制解码成本：还需限制几何数量、顶点/纹理解码规模及递归深度，阈值由代表样本
  测量后冻结并测试；在限额明确前，不启用任意工作区模型的默认预览。
- 简单受控静态资产可用 Drei useGLTF；任意工作区内容优先自有 GLTFLoader/LoadingManager
  进行验证与实例拥有权管理，不默认开启公共 CDN 解码或全局缓存预载。
- 切换文件和关闭会取消尚未完成的读取；废弃的解析结果不能挂载，已创建资源仍需释放。
  geometry/material/texture/ImageBitmap、blob URL 和缓存分别确认拥有权，不 dispose 其他场景共享对象。

## 6. 性能预算与测量

以下为初始验收预算，属于待测目标，不是已有收益；M0 记录设备与样本后再冻结。

| 项目 | 初始预算/检查方式 |
| --- | --- |
| 普通对话 | 3D 关闭时无 Three 场景 chunk 下载、无新增 GPU context；关键首次 JS 不混入 3D 主体 |
| 绘制 | 静态模型优先 demand；拖动/缩放触发更新；语音活动限量更新约 30 FPS；不能用 demand 掩盖持续 invalidate 的成本 |
| 分辨率 | DPR 移动端 1、桌面上限 1.5；首轮不使用实时阴影、景深、全屏后处理或高层透明叠加 |
| 输入延迟 | 同设备、相同脚本比较，p95 较关闭效果的基线增加不超过 10ms；测量方法和样本数随记录交付 |
| 场景绘制 | 约定代表设备上 p95 帧间隔不超过 33.3ms；GPU timing 可用才记录，不能把 CPU 帧间隔称为 GPU 时间 |
| 离屏/后台 | 无视觉绘制和分析采样循环；语音媒体按用户会话需求继续工作 |
| 资源稳定 | 连续打开/关闭 20 次，无持有旧场景/分析节点/回调的增长；记录堆与 WebGL 资源趋势 |
| 动态资源 | 分别记录语音/模型 chunk 的实际 gzip/brotli 字节、模型与纹理大小、首次可交互时间；不拿 npm unpackedSize 替代 |

R3F 的 demand/invalidate、资源复用与实例策略可按官方说明采用；动态场景必须显式控制触发。
[R3F 性能指导](https://r3f.docs.pmnd.rs/advanced/scaling-performance)
低端设备不达预算时先降分辨率与材质复杂度，再回退 DOM，不优先叠加更多特效。

## 7. 测试与完成证据

Web 每阶段运行 `pnpm --dir src/web test`、`typecheck`、`build`；Node 22 / pnpm 10 与 CI 一致。
生命周期测试覆盖动态模块未加载、错误回退、关闭/切换清理、reduced motion、context loss、焦点恢复。
语音用 Fake AudioContext/节点检查旁路拓扑与清理，再用真实媒体验证静音、打断、换设备和重连。
GLB 用有效、截断、长度冲突、外部资源、超限与不支持扩展样本检查行为及网络请求。
对话连续进展、成功自动收起、全部并发工具、审批和原始结果回看保留回归。

若改 Python，执行 Ruff、Pyright 与相关 pytest；涉及 Host/API 加跑 host/web 契约测试。
仅改前端识别与渲染时不扩展 Python 契约。若改变 schema，按仓库命令更新 OpenAPI 与类型生成物。
真实 Provider 与 Android/iOS 测试未运行时写明缺口，不以 mock、桌面视口模拟或 build 替代。

每个已完成 3D 阶段必须交付四类证据：

1. **依赖**：直接依赖与 lock 的实际版本及许可证。
2. **使用**：生产入口可触发的 import、挂载组件和真实三维对象；仅包存在不算接入。
3. **体验**：桌面/移动状态检查、键盘操作、失败与静态路径的记录。
4. **成本**：模块、下载、绘制、输入延迟和资源清理的可复测记录；未测项单列。

## 8. 后续开始实施时的默认顺序

先执行 M0–M1：基线和一处 Three/R3F 场景，证明 Next export、动态加载与降级有效。
通过后执行 M2 大语音视图；若 Live 测试环境不可用，保留 M2 的真实媒体门禁并推进 M3 GLB。
首页环境层不额外创建持续 canvas；正式九尾狐模型需另行提供或制作授权资产。
postprocessing、Quarks、Spark、图谱和 WebGPU 暂不进入安装清单。
本计划已进入实施，实际结果见下节。

## 9. 本轮实施结果与剩余门禁

| 阶段 | 实际结果 |
| --- | --- |
| M0 | 保存实施前导出首页的 8 个 script src，合计 gzip 322,967 字节；新增可复测脚本。不是实际网络、延迟或 GPU 测量 |
| M1 | 真正安装、import 并挂载 Three 0.186.0、R3F 9.7.0；场景按需加载，有真实网格、灯光、材质和静态/渲染失败回退 |
| M2 | 专注视图接既有媒体会话和全部操作；真实输入/输出 RMS 通过 ref 更新，采样约 30 Hz；TTS 不伪造音量，离屏停止视觉和分析，取消/重连释放旧资源 |
| M3 | GLB 文件链接/卡片识别与二进制读取；有界格式校验后 GLTFLoader 解析，Drei 10.7.8 OrbitControls 提供拖动与 DOM 旋转/缩放/重置，失败可重试、下载保留 |
| M4 | **按用户要求不执行**；真机帧/GPU、p95 输入延迟、20 次资源趋势、Android/iOS 和性能默认启用没有验收结论 |
| M5 | 完成本地正式 GLB 加载接口、idle/react 动画连接、交互与取消/释放/失败回退；正式资产待提供，视频保持默认，没有制作替代角色 |

模型解码限额是初始保守边界，尚未经 M4 代表设备测量冻结；仅用户主动预览，不后台预载。
复杂扩展、Draco/KTX2、外部资源、文档蒙皮与动态模型播放不在首版支持范围内。
真实 Live Provider 全链路和正式角色画质仍需相应环境/资产，mock、模拟视口与 build 不替代这些验收。

复测：`pnpm --dir src/web build` 后执行
`node src/web/scripts/report-scene-bundles.mjs /tmp/lumen-scene-bundles.json`。
历史基线见 [静态 script 快照](../research/2026-09-18-threejs-baseline.json)；当前结果与验证限制见
[实施验收记录](../research/2026-09-18-threejs-implementation.md)。
`/scene-preview` 是明确标注模拟状态/RMS 的本地开发验收页；导出后访问 `/scene-preview.html`。
示例立方体是真实内嵌 GLB，不是九尾狐资产，也不申请麦克风或访问 Provider。
