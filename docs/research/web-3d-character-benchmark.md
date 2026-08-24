# Web 端动态角色技术与体验基准研究

研究日期：2026-08-02
研究范围：Lumen 当前 composer mascot、Meoo 首页角色、主流 Web 2D/2.5D/3D 角色实现路线。
目的：解释当前动画“不自然、偏大”的原因，建立可落地的优化优先级与验收指标。

## 结论摘要

1. Meoo 首页角色并不是实时 3D。公开页面使用两段预渲染 GIF：idle 与 hover，通过鼠标进入/离开切换透明度。其自然感主要来自逐帧动画质量、动作节奏和克制的状态数量，而不是复杂渲染技术。
2. Lumen 当前也不是真 3D：四张完整角色 PNG 被绘制到 2D canvas，再用 10×10 网格做形变，外层叠加 CSS 3D transform 与 GSAP。它同时承担“姿态切换、呼吸、视差、弹性”，容易出现橡皮片式变形。
3. 当前 Lumen 桌面端角色盒为 218×202px，按 canvas 中最大 500/560 的轮廓归一化计算，视觉轮廓约 195px。Meoo 默认 130px 组件的可见轮廓约 99px，160px 变体约 121px；Lumen 约为 Meoo 可见尺寸的 1.6–2.0 倍，用户感知“略大”有明确量化依据。
4. 自然度的首要问题不是缺少更多动画，而是动作通道没有分层：全身呼吸缩放、硬切整张姿势图、整张平面跟随鼠标、持续光环和粒子同时存在，运动信息彼此竞争。
5. 代码中存在一个具体冲突：姿势切换调用 gsap.killTweensOf(character)，会在第一次切姿后杀掉绑定到同一元素的常驻 breathTween。之后“呼吸层”消失，动作节奏会前后不一致。
6. 对 Lumen 当前偏插画化的品牌形象，推荐路线是 Rive 状态机，而不是直接重建 Three.js 真 3D。Rive 更适合把呼吸、眨眼、视线、姿势一次性动作拆成独立层并平滑混合，且静止状态可停止推进。
7. 若产品明确需要真实视角、材质和灯光，第二阶段再升级为 glTF 骨骼 + morph targets + Three.js / React Three Fiber；不要把 WebGPU 当作迁移动机，一只角色的核心问题是资产和运动设计，而非渲染 API。
8. 第一轮无需更换技术栈也能显著改善：缩小 18–25%、取消常驻粒子、拆分 transform wrapper、把大动作间隔改为随机 8–18 秒、眨眼和视线独立、姿势切换交叉淡化、暂停离屏动画。

## 1. 证据边界与方法

### 已验证事实

- 阅读 Lumen 当前 mascot 组件、全局样式和公开角色资源。
- 下载并分析 Meoo 首页公开 HTML、CSS、JavaScript bundle 与两个角色 GIF。
- 对 GIF 读取尺寸、帧数、时长和首帧 alpha 轮廓边界。
- 对 Three.js、pmndrs、GSAP、Rive、Live2D、MDN 与 W3C 的官方资料进行方案对照。

### 研究限制

- Meoo 的交互式页面在当前浏览器自动化环境中超时，因此没有把浏览器录屏或交互帧时序作为证据。
- Meoo 的尺寸、状态机和素材结论来自其 2026-08-02 公开生产 HTML/bundle 与公开 CDN 素材，站点后续部署可能变化。
- 文中标注为“项目目标”或“建议值”的数字是面向 Lumen 的设计假设，不是行业标准，需要通过真机 A/B 和视觉 QA 校准。

## 2. Meoo 的实现拆解

### 2.1 公开生产实现

Meoo 首页公开 bundle 中的角色组件具有以下行为：

- 组件默认 size 为 130，另一个首页落点使用 size 160。
- 状态只有 idle 与 hover。
- 两个 img 绝对定位叠放，通过 mouse enter / leave 切换 opacity。
- 点击角色打开 assistant/bubble。
- 角色本身没有 canvas、WebGL 或 Three.js 渲染。

对应公开资源：

| 状态 | 源尺寸 | 时长 | 帧数 | 估算帧率 | 文件体积 |
|---|---:|---:|---:|---:|---:|
| idle GIF | 560×560 | 8.08s | 97 | 约 12fps | 约 1.97MB |
| hover GIF | 800×800 | 4.08s | 49 | 约 12fps | 约 1.92MB |

首帧 alpha 边界显示，两套素材的可见角色宽度都约占画布 75.9%，高度约占 66.2%。因此：

- size 130 时，可见轮廓约 99×86px。
- size 160 时，可见轮廓约 121×106px。

公开入口：

- [Meoo 首页](https://meoo.com/)
- [Meoo 生产 JavaScript bundle](https://g.alicdn.com/oneday/onedayAssets/0.2.1006/assets/index-home-perf.js)
- [Meoo 生产 CSS](https://g.alicdn.com/oneday/onedayAssets/0.2.1006/assets/index-home-perf.css)
- [Meoo idle GIF](https://gw.alicdn.com/imgextra/i2/O1CN01eWNW931VtgsOoS3zm_!!6000000002711-1-tps-560-560.gif)
- [Meoo hover GIF](https://gw.alicdn.com/imgextra/i1/O1CN013SeI9O1mKHj2pdTva_!!6000000004935-1-tps-800-800.gif)

### 2.2 为什么简单实现看起来更自然

Meoo 的优势来自“艺术指导优先”：

- 角色轮廓小，动作不会抢占输入区的视觉主导权。
- idle 内部已经包含眨眼、视线偏移和尾巴动作，不需要运行时把整张图扭曲。
- hover 只切换一个明确的情绪状态，没有多个永动层叠加。
- 约 12fps 的帧动画并不追求物理连续，却因每一帧都由作者控制，轮廓、重心和表情始终可信。

这说明“自然”不等于“实时 3D”或“高帧率”。对小尺寸品牌角色，姿势质量、运动层级、停顿和随机性通常比渲染复杂度更重要。

## 3. Lumen 当前实现诊断

主要文件：

- src/web/src/components/mascot-scene.tsx
- src/web/src/app/globals.css
- src/web/public/lumen-fox-complete-*.png

### 3.1 当前技术路径

Lumen 当前为 2.5D 合成：

1. 读取 alert、idle、stretch、wave 四张完整 PNG。
2. 以 560×560 canvas 为输出，源图基准 512。
3. 每帧生成 11×11 顶点，以 10×10 网格绘制 100 个单元、200 个裁剪三角形。
4. 对网格施加正弦位移，形成身体和尾部的软性扭动。
5. 外层 GSAP 继续施加漂浮、呼吸、指针视差、光环、阴影和粒子动画。

优点是资源小：当前四个主要 complete PNG 约 195–245KB/张；body WebP 约 23–29KB/张。缺点是角色的“骨骼”和“表情”没有独立自由度，所有运动最终都落在整张图或规则网格上。

### 3.2 尺寸对比

| 项目 | 桌面端盒尺寸 | 可见角色估算 | 与 Meoo 对比 |
|---|---:|---:|---:|
| Lumen 当前 | 218×202px | 最大边约 195px | 约为 Meoo 160 变体的 1.6 倍 |
| Lumen 移动端 | 190×176px | 最大边约 170px | 约为 Meoo 160 变体的 1.4 倍 |
| Meoo 默认 | 130×130px | 宽约 99px | 基准 |
| Meoo 较大变体 | 160×160px | 宽约 121px | 基准 |

Lumen 的角色靠近 composer，视觉面积还会与 aura、sparkles、speech bubble 叠加，因此主观存在感会比单纯宽度比更强。按当前布局宽度计算，桌面角色 218px 约占 760px composer 的 28.7%；移动端角色 190px 约占 366px composer 的 51.9%。移动端超过一半的横向占比，是应优先修正的构图问题。

### 3.3 不自然的主要来源

#### A. 姿势是硬交换，不是连续运动

当前每次切换姿势先做 0.135 秒 squash，再替换整张 PNG。角色的轮廓、四肢和重心会在一帧内跳到另一姿势。squash 能掩盖切换，却不能建立中间姿态。

建议：至少使用 0.25–0.45 秒双图交叉淡化；更理想的是 Rive blend state，或 3D 骨骼 clip crossfade。该时长为项目建议值。

#### B. 呼吸作用在整张角色上

当前呼吸约为 scaleX 1.006、scaleY 1.014，周期 1.78 秒 yoyo。即使幅度小，脚、尾巴、耳朵和阴影一起扩张，会产生“贴纸缩放”而非胸腔呼吸的感觉。

建议：只驱动胸肩、头部轻微升降和毛发延迟；周期可在 3–5 秒间轻微变化。此范围是项目建议值。

#### C. 基础呼吸会被姿势切换杀掉

switchPose 对 character 调用 killTweensOf，呼吸 tween 也绑定在同一元素。第一次姿势切换后常驻呼吸被清除，导致开场与后续状态的微动不一致。

建议：把运动通道拆为嵌套 wrapper：

```text
character-root
→ pointer-follow
→ idle-breath
→ pose-transition
→ rendered-character
```

每个状态只终止自己的 tween，不应清理其他通道。

#### D. 大动作过于规律和频繁

固定序列 alert → idle → stretch → idle → wave → idle 的总 hold 时长约 9.85 秒，且动作在约 1–2 秒级持续发生。规律重复会快速暴露“循环机器”的感觉。

建议：基础 idle 持续存在；大动作使用带冷却的随机调度，建议 8–18 秒一次，用户触发动作优先级高于随机动作。该范围是项目建议值。

#### E. 规则网格扭曲不理解身体结构

10×10 网格没有骨骼约束，也不知道眼睛、嘴、关节、落地脚和尾巴根部的位置。正弦变形会同时改变面部和身体轮廓，产生橡皮片感。每帧 200 次裁剪 drawImage 也让 CPU 成本与视觉收益不匹配。

建议：停用全身规则 warp。保持 raster 时用预渲染帧或局部蒙版；使用 Rive/Live2D 时绑定参数；使用真 3D 时用骨骼、morph targets 与 additive clips。

#### F. 指针响应驱动整张平面

当前按场景内指针把整张角色最大旋转到约 yaw ±7.5°、pitch ±5°，并叠加位移。一个平面绕轴旋转不等于头和眼睛追踪，边缘透视会放大“纸片”感。

建议：视线层级按 eyes → head → torso 递减，并设置中心死区。项目起始值可为：眼睛 ±8–12°、头部 ±4–6°、身体不超过 ±1–2°；响应 0.25–0.4 秒，回中 0.6–1 秒。需真机校准。

#### G. 装饰动效持续争夺注意力

aura 永久旋转、sparkles 周期约 1.15 秒，同时角色还在浮动、呼吸和切姿势。composer 是任务输入区域，常驻高频粒子会让视觉焦点停留在装饰而不是输入。

建议：移除常驻 sparkles；只在首次出现、发送成功、角色被点击或情绪状态变化时短暂播放。aura 改为静态柔光或极低频亮度呼吸。

#### H. 可访问性与节能控制不完整

当前已有 prefers-reduced-motion、document.hidden 与 GSAP matchMedia，是良好基础；但 canvas rAF 的 reduced-motion 值只在初始化时读取，系统偏好变化时不会同步，且离开视口仍会持续调度 rAF。

建议：

- 监听 MediaQueryList change。
- 使用 IntersectionObserver 在离屏时完全暂停。
- reduced motion 模式使用静态海报或几乎静止的 idle，不播放 warp、自动姿势和 pointer parallax。
- 提供显式“关闭动态角色”入口。

W3C 指出，与其他内容并行且持续超过 5 秒的自动移动内容通常需要暂停、停止或隐藏机制；由交互触发的非必要运动也应可禁用。

## 4. 技术路线对比

| 路线 | 自然度潜力 | 交互性 | 运行成本 | 资产/制作成本 | 适合 Lumen |
|---|---|---|---|---|---|
| 预渲染 GIF / Animated WebP | 高，完全由动画师控制 | 低，适合少量状态 | 低到中，主要是解码与带宽 | 中 | 可作为最快视觉升级或 fallback |
| 当前 PNG + canvas 网格 warp | 中低，局部不受解剖约束 | 中 | CPU 每帧绘制 | 低 | 不建议继续扩展 |
| Rive 状态机 | 高，2D 参数化与状态混合强 | 高 | 低到中，可静止休眠 | 中 | 推荐主路线 |
| Live2D Cubism | 高，眨眼、呼吸、视线、表情成熟 | 高 | 中 | 中高，需专门 rig 与许可评估 | 适合偏角色/IP产品 |
| glTF + Three.js / R3F | 最高，真实视角/光照/空间响应 | 最高 | 中到高 | 高 | 仅在真 3D 是产品需求时采用 |

### 4.1 Rive：推荐的平衡方案

Rive 官方状态机支持 inputs、transitions 与 blend states；Web runtime 默认可按画面变化绘制，状态 settled 后可停止推进，从而避免无意义的持续渲染。适合将现有狐狸拆为：

- Base Idle：基础站姿。
- Breath Layer：胸肩和头部微动。
- Blink Layer：随机间隔，独立于身体。
- Look Target：输入 x/y，驱动眼睛与头部不同权重。
- One-shot：alert、wave、stretch、talk。
- Emotion：neutral、thinking、success、error。

过渡规则：

- 所有 one-shot 从当前基础姿态进入并返回，不硬切。
- blink 不覆盖嘴型；look 不覆盖身体 clip。
- reduced motion、隐藏或离屏时进入 settled 静态状态。

### 4.2 Live2D：角色驱动能力最成熟

Live2D 官方 SDK 分别提供自动眨眼、呼吸、视线追踪、motion fade 与多 motion manager。其设计天然符合“不同参数由不同系统负责”的角色动画架构。

代价是需要以 Cubism 模型方式重新拆层/绑定现有美术，工具链、授权与设计风格约束更强。若 Lumen 长期要做高频表情、口型和陪伴型桌宠，它比通用 Rive 更有深度；若角色只是 composer 的辅助品牌元素，Rive 更轻。

### 4.3 glTF + Three.js / R3F：真正的 3D 路线

若确认需要真实 3D，应采用：

- glTF 模型：身体骨骼 clips、眼睑/嘴型/表情 morph targets。
- AnimationMixer / useAnimations：idle 作为 base action；blink、breath、head follow 作为 additive 或独立权重层；one-shot 使用 crossfade。
- orthographic camera 或窄 FOV：保持插画感，避免近距离透视夸张。
- 根据模型 bounding box 自适应 fit，脚部或尾部锚定 composer，而非固定像素盒。
- 单一柔和环境光/主光，少量 rim light；使用廉价 contact/accumulative shadow。
- Draco 或 Meshopt 压缩几何、KTX2 压缩纹理；按设备动态 DPR。
- 首屏先显示静态 poster，3D 异步增强；加载或能力失败时保留完整体验。

Three.js 官方动画系统支持 bones、morph targets、材质属性、权重、时间缩放、fade、crossfade 和 additive blending，这些才是自然角色运动需要的控制维度。

WebGPU 不应成为本项目的升级理由。MDN 在 2026-05 仍将 WebGPU 标记为 Limited availability；Three.js WebGPURenderer 可在支持时使用 WebGPU并回退 WebGL2，但一只小角色的瓶颈更可能是资产、动画层级和 DPR。

## 5. 推荐方案

### P0：保留现有资产，1 个迭代内修复

1. 尺寸先做 A/B：
   - 桌面盒宽 160–176px，建议先测 168px。
   - 移动端盒宽 146–156px。
   - 这相当于桌面可见轮廓约 143–157px，比当前约 195px 缩小约 19–27%。
   - 若要非常接近 Meoo，则桌面盒宽 145–160px。
2. 拆分 transform wrappers，修复 killTweensOf 误杀呼吸。
3. 停用全身 mesh warp，先以静态/双图 crossfade 验证角色轮廓是否更稳定。
4. 大动作由固定循环改为 8–18 秒随机触发，并设置同动作冷却。
5. 停止常驻 sparkles；aura 改为静态或仅亮度轻微变化。
6. pointer 只驱动局部眼/头；现有完整平面响应至少缩小 60–75%。
7. reduced motion、后台、离屏全部停止自动动作和 rAF。

### P1：Rive 重构，推荐主线

1. 将狐狸拆成头、眼睑、瞳孔、耳、胸肩、身体、前爪、尾巴和阴影等可控层。
2. 建立一个状态机，所有动作通道拥有明确参数责任。
3. 用 blend state 或动画混合替代 PNG 硬切。
4. 将 composer 状态映射到少量明确情绪：
   - idle：安静观察。
   - focused / typing：视线靠近输入，不做大动作。
   - thinking：低幅度思考微动。
   - success：一次性积极反馈。
   - error：一次性克制反馈。
5. 输出静态 poster 作为首屏、低性能与 reduced motion fallback。

### P2：可选真 3D

只有在以下至少两项成立时再投入：

- 角色需要随页面视角真实转身。
- 需要动态灯光、材质或多角度镜头。
- 同一角色要复用到更大的 3D 场景。
- 需要由运行时组合大量动作、表情和道具。

否则，真 3D 会增加模型、骨骼、morph、灯光、压缩、加载与兼容成本，却不必然比优质 2D rig 更自然。

## 6. 动画设计参数建议

以下均为 Lumen 项目起始值，不是标准。

| 通道 | 建议 |
|---|---|
| 基础呼吸 | 3–5 秒，幅度局部、周期轻微随机 |
| 眨眼 | 通常 3–7 秒随机，偶发双眨；不要与大动作锁相 |
| 大动作 | 8–18 秒随机，且用户输入期间降低频率 |
| 姿态过渡 | crossfade/blend 0.25–0.45 秒 |
| 视线响应 | 0.25–0.4 秒；中心死区约容器 8–12% |
| 视线回中 | 0.6–1 秒，缓慢于追踪 |
| 头部旋转 | yaw/pitch 约 ±4–6° |
| 身体跟随 | 不超过 ±1–2° |
| 眼睛范围 | 约 ±8–12°，由美术边界决定 |
| 大动作后停顿 | 1.5–3 秒，避免立即进入下一动作 |
| 粒子 | 只在事件反馈时播放 0.6–1.2 秒 |

随机调度不是每帧噪声。应该在动作结束后选择下一次触发时间，并保持动作内部的稳定节奏；否则会产生抖动而不是生命感。

## 7. 性能、响应式和无障碍验收

### 7.1 性能项目目标

- 首屏立即显示 poster，不等待动画 runtime，避免 mascot 阻塞 LCP。
- mascot 容器固定尺寸，加载增强前后无布局跳动。
- 初始角色关键资源建议控制在 2MB 以内；交互 clip 延迟加载。
- 桌面主流设备 app p75 目标 ≥55fps，中端移动设备 ≥45fps。
- mascot 自身 p95 CPU/GPU frame time 目标低于约 8ms。
- DPR 默认封顶 1.5–2；帧率下降时降到 1，再减少阴影/后处理。
- 使用 Page Visibility + IntersectionObserver 完全暂停不可见角色。

Meoo 的两段 GIF 合计约 3.9MB，证明预渲染方案可以简单，但并不天然节省带宽；Lumen 应按首屏状态拆包，避免为了少量交互一次下载所有状态。

### 7.2 响应式验收

- 不只缩放盒子，要以角色 alpha bounds / model bounds 衡量“可见轮廓”。
- 锚定角色的脚、身体重心或尾部与 composer 的关系，避免不同姿势造成跳位。
- 检查 390×844、768×1024、1207×516、1440×900、2560×1440。
- 检查浏览器 200% zoom、系统字体放大、窄高与宽矮视口。
- 在手机上优先减少动作和装饰，而不是仅等比例缩小。

### 7.3 无障碍验收

- prefers-reduced-motion: reduce 下：无自动换姿、无 mesh warp、无 pointer parallax、无常驻光环/粒子。
- 提供可发现的“暂停/关闭角色动画”控制，设置可持久化。
- 角色若只是装饰继续 aria-hidden；若能点击打开助手，则需要真实 button、可访问名称、键盘焦点和等价操作。
- 动画不能遮挡输入、错误提示或发送状态。

## 8. 验证计划

第一阶段建议只做三个可比较版本：

- A：当前版本。
- B：尺寸 168px + 去常驻粒子 + 降低指针幅度 + 修复 tween 通道。
- C：尺寸 168px + 无 mesh warp + 作者控制的 idle/crossfade。

观察指标：

- 用户是否觉得角色“自然、可信、不打扰”。
- 输入任务完成时间和 composer 点击率是否受影响。
- 首次注意角色的比例、注视时长与主动点击率。
- 桌面/移动帧时间、主线程占用、资源传输量。
- reduced motion 与低性能设备的完整性。

如果 C 明显优于 B，说明主要问题是动画表示方式，应进入 Rive 重构；如果 B 已足够好，先保留轻量实现，避免过度工程化。

## 9. 一手资料

### Three.js

- [Animation system overview](https://threejs.org/manual/en/animation-system.html)
- [AnimationMixer](https://threejs.org/docs/pages/AnimationMixer.html)
- [AnimationAction：crossfade、weight、warp](https://threejs.org/docs/pages/AnimationAction.html)
- [AnimationUtils：additive clip](https://threejs.org/docs/pages/AnimationUtils.html)
- [RobotExpressive：基础状态、one-shot 与 facial morph](https://threejs.org/examples/webgl_animation_skinning_morph.html)
- [Skeletal blending example](https://threejs.org/examples/webgl_animation_skinning_blending.html)
- [Additive blending example](https://threejs.org/examples/webgl_animation_skinning_additive_blending.html)
- [Inverse kinematics example](https://threejs.org/examples/webgl_animation_skinning_ik.html)
- [GLTFLoader](https://threejs.org/docs/pages/GLTFLoader.html)
- [DRACOLoader](https://threejs.org/docs/pages/DRACOLoader.html)
- [PMREMGenerator](https://threejs.org/docs/pages/PMREMGenerator.html)
- [Responsive rendering 与 DPR](https://threejs.org/manual/en/responsive.html)
- [Three.js shadows performance](https://threejs.org/manual/en/shadows.html)
- [Color management](https://threejs.org/manual/en/color-management.html)
- [WebGPURenderer 与 WebGL2 fallback](https://threejs.org/docs/pages/WebGPURenderer.html)

### React Three Fiber / pmndrs

- [React Three Fiber](https://github.com/pmndrs/react-three-fiber)
- [Drei useAnimations](https://drei.docs.pmnd.rs/abstractions/use-animations)
- [AdaptiveDpr](https://drei.docs.pmnd.rs/performances/adaptive-dpr)
- [PerformanceMonitor](https://drei.docs.pmnd.rs/performances/performance-monitor)
- [Center：bounding-box 与 viewport fit](https://drei.docs.pmnd.rs/staging/center)
- [Stage：studio light、centering、contact shadow](https://drei.docs.pmnd.rs/staging/stage)
- [OrthographicCamera](https://drei.docs.pmnd.rs/cameras/orthographic-camera)
- [AccumulativeShadows](https://drei.docs.pmnd.rs/staging/accumulative-shadows)
- [PresentationControls](https://drei.docs.pmnd.rs/controls/presentation-controls)
- [maath damping](https://github.com/pmndrs/maath)

### Rive

- [State machine playback 与 settled state](https://rive.app/docs/runtimes/state-machines)
- [State machine overview](https://rive.app/docs/editor/state-machine/state-machine)
- [Blend states](https://rive.app/docs/editor/state-machine/states)
- [Web runtime](https://rive.app/docs/runtimes/web/web-js)
- [Web runtime parameters 与 DrawOnChanged](https://rive.app/docs/runtimes/web/rive-parameters)

### Live2D

- [Automatic eye blink](https://docs.live2d.com/en/cubism-sdk-manual/autoeyeblink/)
- [Breath](https://docs.live2d.com/en/cubism-sdk-manual/breath/)
- [Motion fade 与 priority](https://docs.live2d.com/en/cubism-sdk-manual/motion/)
- [Eye tracking](https://docs.live2d.com/en/cubism-sdk-tutorials/lookat/)
- [Multiple motion managers](https://docs.live2d.com/en/cubism-sdk-tutorials/multi-motion-management-web/)

### GSAP、浏览器与可访问性

- [GSAP quickTo](https://gsap.com/docs/v3/GSAP/gsap.quickTo%28%29/)
- [GSAP matchMedia](https://gsap.com/docs/v3/GSAP/gsap.matchMedia%28%29/)
- [GSAP easing](https://gsap.com/docs/v3/Eases/)
- [MDN WebGPU availability](https://developer.mozilla.org/en-US/docs/Web/API/WebGPU_API)
- [MDN prefers-reduced-motion](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/%40media/prefers-reduced-motion)
- [MDN Page Visibility](https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API)
- [MDN IntersectionObserver](https://developer.mozilla.org/en-US/docs/Web/API/Intersection_Observer_API)
- [W3C Animation from Interactions](https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions)
- [W3C Pause, Stop, Hide](https://www.w3.org/WAI/WCAG22/Understanding/pause-stop-hide)

## 最终决策

Lumen 的最佳近期方向不是“把现在的 canvas 变得更像 3D”，而是先降低尺寸和运动噪声，再把角色重构为具有独立运动通道的 2D rig。推荐顺序：

P0 现有实现减法与修复 → P1 Rive 状态机 → 只有在真实多角度/灯光需求明确时才进入 P2 glTF 真 3D。

这个顺序既能最快验证用户感知，也避免用更重的渲染栈掩盖动画资产和运动设计本身的问题。
