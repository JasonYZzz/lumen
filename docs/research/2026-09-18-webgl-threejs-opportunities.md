# Lumen WebUI 的 WebGL / Three.js 优化建议

调研日期：2026-09-18。范围：当前 WebUI、角色、思考反馈、实时语音、运行时检查和未来 3D 内容预览。
本报告是设计与技术选型建议，没有新增依赖或修改运行行为。

## 1. 判断

可以增强，而且已有不错的基础。建议把预算集中在首页品牌区域、独立语音体验和按需打开的
3D 内容预览。日常对话、工具记录与审批仍以可读、可操作的 DOM 为主。
先改善素材与状态反馈，再根据真实几何、相机与灯光需求引入 Three.js。

优先组合：已有原生 WebGL + GSAP；预设着色器可选 Paper Shaders；需要真实 3D 时选择
Three.js + React Three Fiber（R3F）+ Drei。粒子、后处理、图可视化和高斯泼溅按真实功能分别添加。
以下优先级和效果参数属于 Lumen 的建议，不是已完成性能验收。

## 2. 当前实现：哪些事实影响选择

| 表面 | 当前事实与入口 | 对选型的影响 |
| --- | --- | --- |
| 新任务入口 | `landing-entry.tsx` / `landing-entry.module.css`：最大 760px，桌面角色列 168px，移动端单列 | 特效适合局部品牌区域，应保持输入区稳定 |
| 九尾狐 | `mascot-scene.tsx` 默认调用 `VideoMascotRenderer`；视频是 RGB 与 alpha 并排编码，WebGL 用两次纹理采样合成透明度 | 已有 WebGL，不必为当前效果重建 3D 模型 |
| 生命周期 | `mascot-visibility.ts` 处理页面与元素可见性；视频渲染用 `requestVideoFrameCallback`，不可用时用 RAF；资源有清理 | 新效果应复用这些行为，不能悄悄新增永远运行的循环 |
| 降级 | full / reduced / static 模式；非 full 不下载、解码视频；WebGL 失败显示 poster；DPR 上限 2 | 保留静态先显示和低动态路径 |
| 思考状态 | `thinking-orb.tsx`：18px SVG + GSAP；对话使用状态摘要、进展与工具详情 | 小图标缺少展示复杂材质的面积，GPU 场景收益很小 |
| 实时语音 | `live-voice-controls.tsx`：状态驱动的四条 CSS 波形；`realtime-client.ts` 已有 AudioContext / AudioWorklet | 大语音视觉可增加真实音量响应；现有波形不能称为实际音频频谱 |
| 运行时 | `RuntimeInspector` 是 Agent、工作对象和副作用列表；PlanStep 有 `depends_on` | 小型有向依赖优先二维，空间探索是可选扩展 |
| 技术栈 | Next.js 15、React 19、静态 export；直接依赖有 GSAP、Rive，没有 Three.js / R3F | 新 GPU 模块需客户端懒加载，不能影响无 GPU 的基础交互 |

旧文档 `web-3d-character-benchmark.md` 中的“完整 PNG 网格形变”描述不代表当前默认渲染。
当前 public 资源未发现供九尾狐运行的 glTF/GLB 模型。
视频文件实际大小：idle 836,653 bytes、react 888,372 bytes、poster 243,921 bytes，
合计约 1.88 MiB；这是本地文件大小，不等于每次打开页面的实测网络流量。

lockfile 中有 `@splinetool/runtime`，链路来自 `@lobehub/icons` 的 peer 环境中的
`@lobehub/ui`。目标 UI 源码未见 Spline 接入；包存在不能证明效果已经使用或已进入页面 bundle。

## 3. 项目筛选

读取官方仓库、文档，并通过 GitHub API 检查 archived / pushed_at，通过 npm registry
检查发布版本和 peerDependencies。所有下表仓库在查询时均未归档。
最后 push 可来自任意分支或自动化，不能单独证明稳定性、响应速度或发布质量。

| 项目 | 能力与 Lumen 落点 | 授权 / 最后 push（UTC 日期） | 建议 |
| --- | --- | --- | --- |
| [Three.js](https://github.com/mrdoob/three.js) | 几何、材质、灯光、模型加载；真正的 3D 预览或角色 | MIT / 2026-09-18 | 真实 3D 的基础，按需加载 |
| [React Three Fiber](https://github.com/pmndrs/react-three-fiber) | React 组件与 Three 场景衔接 | MIT / 2026-09-16 | 与当前 React 架构最匹配 |
| [Drei](https://github.com/pmndrs/drei) | 模型加载、相机控制、环境、实例、性能工具与材质辅助 | MIT / 2026-09-07 | 和 R3F 配套，按需采用具体能力 |
| [Paper Shaders](https://github.com/paper-design/shaders) | 独立 canvas 着色器与 React 组件；首页局部材质、轻动态背景 | Apache-2.0 / 2026-09-17 | 首轮原型候选；它不是 Three.js 插件 |
| [react-postprocessing](https://github.com/pmndrs/react-postprocessing) | R3F 后处理；受控的场景辉光、色调效果 | MIT / 2026-09-02 | 有明确画质收益再加，首轮不默认开启 Bloom / DOF |
| [three.quarks](https://github.com/Alchemist0823/three.quarks) | 批处理粒子、轨迹、VFX；已存在的 3D 场景中短时状态反馈 | MIT / 2026-05-21 | 第二阶段候选；普通 UI 的少量粒子用 CSS/SVG 即可 |
| [react-force-graph](https://github.com/vasturiano/react-force-graph) | 2D/3D 力导向图；Agent 或知识网络探索 | MIT / 2026-02-04 | 有网络探索需求再加；小型计划 DAG 不适合持续漂动布局 |
| [Cosmos.gl](https://github.com/cosmosgl/graph) | GPU 图布局与绘制；大量节点的二维网络 | MIT / 2026-09-18 | 大规模关系图候选，它不是 Three.js 3D 图框架 |
| [Spark](https://github.com/sparkjsdev/spark) | Three.js 高斯泼溅；扫描、重建场景预览 | MIT / 2026-09-14 | 有真实 splat 文件再用，日常聊天无需场景重建 |
| [curtains.js](https://github.com/martinlaxenaire/curtainsjs) | DOM 图片/视频对应的 WebGL 平面；封面图片位移、过渡 | MIT / 2025-04-03 | 功能合适但更新较慢；不作为整个 UI 的特效基础 |
| [OGL](https://github.com/oframe/ogl) | 少量抽象的自定义 WebGL 着色器 | Unlicense / 2025-04-13 | 自研 shader 的备选；已有小型 WebGL 实现无需强制迁移 |
| [Troika](https://github.com/protectwise/troika) | `troika-three-text` 在 3D 场景中呈现文字 | MIT / 2026-07-24 | 仅供场景标签，聊天正文保留 DOM |
| [three-vrm](https://github.com/pixiv/three-vrm) | VRM 角色加载与运行 | MIT / 2026-09-14 | 将来有人形虚拟助手时考虑，九尾狐优先普通 glTF 骨骼 |
| [Theatre.js](https://github.com/theatre-js/theatre) | 可视化运动编排 | Apache-2.0 / 2024-08-14 | 当前已有 GSAP 且仓库更新间隔长，首轮不引入 |

Paper 的部分旧在线资料仍写 PolyForm Shield；当前仓库 LICENSE、README 和本次查询的
发布包均为 Apache-2.0。应以最终选定版本附带的 LICENSE / NOTICE 为准，
不要把当前许可结论套用于全部历史版本。[当前许可证](https://github.com/paper-design/shaders/blob/main/LICENSE)
OGL 的 GitHub license 元数据为空，但仓库 README 和 package 声明 Unlicense，未将其误记为 MIT。

### 兼容性快照

| npm 包 | 查询到的 latest | 关键 peer 契约 |
| --- | --- | --- |
| `three` | 0.186.0 | 基础渲染库 |
| `@react-three/fiber` | 9.7.0 | React / ReactDOM `>=19 <19.3`；Three `>=0.156` |
| `@react-three/drei` | 10.7.8 | React `^19`；Fiber `^9.0.0`；Three `>=0.159` |
| `@react-three/postprocessing` | 3.1.1 | React `^19.0.0`；Fiber `>=9.7.0`；需要 postprocessing |
| `@paper-design/shaders-react` | 0.0.81 | React `^18 || ^19` |
| `react-force-graph-3d` | 1.29.1 | React `*`；声明宽泛不等于所有交互经过 React 19 验收 |
| `@sparkjsdev/spark` | 2.2.0 | Three `>=0.180.0` |

当前 React 19 与上述核心版本范围匹配，这是候选兼容依据，不能替代 Next export、
StrictMode、Safari 与 Android 真机验证。R3F 官方也明确 v8 对应 React 18，v9 对应 React 19。
[官方说明](https://github.com/pmndrs/react-three-fiber)
registry 查询入口为 `https://registry.npmjs.org/<包名>/latest`，这些值是调研时快照。
没有用 npm unpackedSize 估算页面下载：它不是 tree-shaking 后的压缩 bundle 大小。

## 4. 针对 Lumen 的具体设计

### A. 首页：九尾狐附近的一处品牌效果，优先级 P1

保持暖白背景、输入框与狐狸的布局。在狐狸附近加入非常轻的琥珀色流动轮廓或材质，
输入聚焦时收敛，发送时给一次短反馈，离开首页后结束。
先在已有视频合成 fragment shader 中试验局部背景/轮廓混合，避免额外 canvas 和依赖。
需要更大、更可调的材质区域时再比较 Paper Shaders，限定绘制范围，不覆盖整页阅读区域。

验收重点是品牌是否更有识别度、输入是否更稳定；单纯增加持续运动不算收益。
装饰层不拦截指针，不承载文本和按钮，静态 poster 在着色器就绪前先显示。

### B. 九尾狐：素材优先，真正 3D 属于后续资产项目

当前视频路线已经解决了插画角色完整动作的呈现。优先打磨 idle/react 的动作质量、
循环接缝、光影一致性和反应节奏。两个视频目前都创建并 `preload='auto'`：可试验先加载
idle，首帧就绪后空闲预取 react，而不是等用户点击才首次下载。
针对旧设备，在没有 requestVideoFrameCallback 的 RAF 路径评估按素材帧率节流。

若要真实侧转、视线跟随、尾巴独立摆动与灯光变化，才选择 glTF/GLB + Three/R3F。
必须准备有质量的网格、毛发风格、骨骼、九条尾巴动作、表情、循环和授权资产。
这项工作的主要成本是角色资产制作，不是 npm 安装。
延续 `MascotScene` 的 renderer 选择与回退模式即可；没有第二个真实需求时不新建通用渲染框架。

模型制作阶段可使用 [glTF Transform](https://github.com/donmccurdy/glTF-Transform)
和 [meshoptimizer](https://github.com/zeux/meshoptimizer) 处理资产，贴图尺寸与压缩也一起评估。
不为当前视频合成引入模型优化工具。

### C. 语音：最有功能意义的 GPU 视觉，优先级 P1/P2

现有小弹层保持四条 CSS 波形。若增加用户主动打开的大语音视图，可以用原生 shader
或 R3F/Drei 实现一个克制的流动球体，真实输入/输出音量控制振幅，连接状态控制外观。
聆听、用户说话、助手说话、工具执行、审批与重连必须有不同的文本状态。

复用 `RealtimeVoiceClient` 已拥有的 AudioContext 和音频节点，接入本地 AnalyserNode
或轻量音量回调；不要额外请求麦克风或创建第二套音频连接。
RMS/频谱只用于本地视觉，避免写入 Session 或增加 Provider 请求。
音量值用 ref/uniform 更新，别每帧 React setState。
审批时视觉降到静止，审批按钮成为主要对象；不把音量或球体转速表现为模型推理进度。

### D. 计划与 Agent：清晰关系优先，优先级 P2

PlanStep 已有真实 `depends_on`，小型计划用稳定分层的二维图可直接表达依赖。
成功、运行、阻塞状态与选中步骤对应；列表与图共享现有权威数据。
不把 force simulation 的节点距离解释为进度、相似度或任务优先级。

大量 Agent / 知识节点才考虑 react-force-graph 的空间探索视图，固定布局或终止布局
运动，选中节点显示 DOM 详情。AgentRecord 目前没有强契约的 edges 列表，不根据自由文本
编造关系；新增关系契约需另行设计。大量二维图比较 Cosmos.gl，它近期已迁移至 luma.gl
WebGL2，不套用旧 regl 接口。[官方发布说明](https://github.com/cosmosgl/graph/releases)

### E. 3D 工具结果：产品能力扩展，优先级 P2

当用户真的产出或上传 GLB/glTF 时，按点击加载 R3F + Drei 预览，保留文件名称、
下载、加载进度、失败重试和文字说明。需要扫描场景再提供 Spark 支持。
这是未来功能，当前 document-preview 的能力不能直接当作已支持 3D 文件。
3D 文件及外部贴图读取必须沿用工作区文件和文档安全边界；图像资产的许可独立于库许可。

### F. 思考与工具记录：继续保持轻量，优先级 P0

18px 思考标记保留 SVG/GSAP。若想更有 Lumen 特征，应优先调整自有图形与动作节奏。
处理过程、工具记录、来源与审批保持 DOM，可复制、可检索、可键盘操作。
完成反馈是一次简短的状态转变，不给每个工具行创建 WebGL context，也不把正文变成纹理。

## 5. 工程策略和预算

每个首次实现的表面直接管理自己的 canvas、渲染循环与清理，复用现有可见性和运动偏好。
没有第二个真实 GPU 表面前，不建设共享 renderer / 特效注册中心。
若首页同时保留视频 canvas 和额外材质 canvas，要比较与同一 canvas 合成的成本；
共享 context 不是两行配置，不为节省一个小 context 提前重构全部生命周期。

真实 3D 组件用 Next 客户端动态加载，提供静态 fallback；GPU 模块不进入所有对话的必需路径。
静态预览用 `frameloop='demand'`，活动动画期间显式推动更新，离屏/后台停止。
持续动画不是设了 demand 就自动免费，uniform 连续变化仍需要绘制。
复用几何与材质，多同类对象采用实例，卸载时清理拥有的 GPU 资源。
[R3F 性能指导](https://github.com/pmndrs/react-three-fiber/blob/master/docs/advanced/scaling-performance.mdx)
[帧循环陷阱](https://github.com/pmndrs/react-three-fiber/blob/master/docs/advanced/pitfalls.mdx)

建议作为首轮试验的预算，全部需要真机测量：

- GPU 模块懒加载，基础页面不等待模型、HDR、粒子贴图或 shader 编译。
- 首页装饰以 30 FPS 为起始目标；互动预览根据设备质量档位调整；DPR 初始上限 1.5，
  与当前视频上限 2 比较画质，移动端先用 1，不机械套用统一数值。
- reduced/static 模式不启动装饰动画；完全隐藏时不继续请求渲染帧。
- 第一阶段不使用实时阴影、景深、多重辉光、全屏多层透明粒子。
- 原型增加的压缩 JS、素材下载、活跃 GPU 时间、长任务和输入延迟分别测量，不能只看 FPS。
- 用相同设备比较开启/关闭效果，覆盖输入、长回答流式更新、工具展开、语音打断、切换任务、
  后台恢复和 context loss；桌面与 Android/iOS 分开记录。

WebGPU 可作为后续实验。Three 官方 WebGPURenderer 优先 WebGPU，不支持时回退 WebGL2；
这不意味着现有 GLSL 材质、Quarks、后处理和全部 Drei 组件天然兼容它。
自定义 shader 与插件需分别验证，首轮以目标设备能稳定运行的路径交付。
[官方 renderer 文档](https://threejs.org/docs/pages/WebGPURenderer.html)
Quarks 的 WebGPU nodes 当前仍有实验性说明，不能将其概括为完整 WebGPU 支持。

## 6. 实施顺序

| 阶段 | 交付 | 继续投资的依据 |
| --- | --- | --- |
| P0：现有体验 | 素材加载与循环、静态降级、轻量思考反馈；建立性能基线 | 输入/流式阅读与视频反馈稳定 |
| P1：首页原型 | 同 canvas 局部 shader 与 Paper 预设两种方案比较，最终只选一条 | 品牌识别提升，性能代价可接受 |
| P1/P2：语音原型 | 真实音量响应的大语音视觉，保留轻量控件 | 状态更容易理解，打断/审批不受影响 |
| P2：按需 3D | 有真实 GLB 产物时做预览；大量关系数据时做图探索 | 功能需求和资产明确 |
| P3：高级表现 | 真 3D 九尾狐、Quarks VFX、选择性后处理、WebGPU | 角色资产质量与真机指标足以支撑 |

第一轮建议实际只做“九尾狐附近的一处轻 shader + 更好的素材加载”。
语音与真 3D 各做独立原型后决定，不同时安装所有候选项目。

## 7. 验证边界

本次读取源码、资源实际字节数、官方文档、14 个仓库元数据和 7 个 npm 包发布契约，
并参考本会话先前检查的桌面 WebUI 图像。未运行这些库在 Lumen 中的原型，
未进行新增效果的 bundle、GPU、功耗或真机性能验收；建议参数不属于实测结论。
这是纯研究文档新增，保留了此前 UI 工作的未提交修改；无需重跑全量应用测试。

## 8. 首轮实施记录

研究完成后按用户指示实施首轮：首页九尾狐附近的局部 shader 与素材加载优化。
选用现有 WebGL canvas 的 GLSL 合成，没有新增渲染库、canvas 或素材依赖。
轮廓来自视频的真实 alpha，在边缘加入轻微琥珀色扫光；输入聚焦时减弱，回应动画时增强。
角色姿态与动作继续由原视频和 MascotPlayback 拥有，不将扫光解释为推理进度。

idle 在角色可见且允许运动时加载，react 在 idle 首帧显示后的空闲时机预取；
聚焦或点击直接触发 react 加载。离屏或暂停会取消尚未执行的预取，不中止已经开始的下载。
保留解码帧回调；旧浏览器的 RAF 路径限制到 30 FPS。DPR 桌面上限 1.5，窄屏上限 1，
uniform 位置在初始化时缓存，帧循环不进行 React 更新或像素回读。
reduced/static 不下载视频，context loss、WebGL 不可用或视频错误仍回退静态图片。

新增回归覆盖延迟预取、交互提前加载、离屏/暂停取消、RAF 限速、原生视频帧回调和
context loss 资源释放。完整 Web 测试 191 项、TypeScript 检查、生产构建和设计检测均通过；
Chrome 桌面与 390px 移动视口已检查，透明角色、输入框及任务设置无溢出或渲染报错。
尚未完成 Android/iOS 真机 GPU、功耗或
输入延迟基准，不将降低 DPR 和延迟预取等实现策略表述为已测得的性能收益。
语音视觉、真实 3D 角色及 GLB/图谱预览保持后续独立阶段。

## 9. 后续实施状态

用户随后授权实施升级计划（M4 暂缓），项目已直接接入 Three.js、React Three Fiber 和 Drei，
用于语音专注视图、按需 GLB 预览及可选正式角色加载接口。
第 2 节的依赖现状和第 8 节的原生 WebGL 记录属于本次后续实施之前的阶段，
不能继续据此判断当前仓库“没有 Three.js”。九尾狐正式模型待提供，默认视频保持不变。
实现、静态包体与验证边界见 [实施记录](2026-09-18-threejs-implementation.md)。
