# Lumen Web UI 与动态角色完整实施计划

> 状态：M1–M3 与 M5 runtime adapter 已实施并通过构建；M4 正式 `.riv` 资产待编辑器可用后导出
> 日期：2026-08-02
> 依据：`docs/research/web-3d-character-benchmark.md` 与 `design-audit/2026-08-02-web3d/audit.md`
> 主决策：先完成可独立上线的 P0 减法优化，再以 Rive 状态机替换当前 Canvas 2.5D 实现；真 3D 只保留为条件式后续路线。

### 实施记录（2026-08-02）

- 已落地独立 `LandingEntry`、desktop 168px / mobile 120px 安全布局。
- 已删除 legacy Canvas 网格扭曲、常驻星粒、动态光环与 GSAP 依赖。
- 已落地 Raster V2：静态柔光、独立尾部轻动、随机 8–18 秒 one-shot、聚焦 alert、提交 wave、320ms 姿态交叉淡化。
- 已落地 system reduced motion、用户静态开关、页面后台与离屏暂停。
- 已增加局部通道：4.2 秒胸肩/头部遮罩呼吸、独立眨眼、独立尾巴、focus 头部轻响应；脚部和尾根保持静止。
- 角色画面不再显示播放/暂停图标；静态偏好入口移入 Composer 工具栏。
- 已接入 `@rive-app/react-canvas` 渐进增强 adapter；通过 `NEXT_PUBLIC_MASCOT_RENDERER=rive` 启用，只有 `lumen-fox.riv` 加载且包含 `Mascot` 状态机时才 280ms 淡入。
- `.riv` 404、WASM/runtime 或状态机合同失败时自动回退已验收的 layered renderer；已在真实浏览器中验证无空白、无 console error/warning。
- 图像生成的拆层候选因改变眼型、头部比例且无法像素级配准而被拒绝，未进入项目；正式 `.riv` 继续以现有透明 body/tail/pose 原画作为唯一身份源。

---

## 1. 目标与非目标

### 1.1 用户目标

1. 角色看起来更自然、安静、有生命感，不像一张被拉伸和旋转的贴纸。
2. 角色不能抢过标题和输入框的主视觉层级。
3. 桌面、移动端、字体放大和窄屏下都不遮挡核心内容。
4. 角色对聚焦、输入和提交有明确但克制的反馈。
5. 动效不能明显增加首屏等待、主线程负担或低性能设备耗电。
6. 系统 reduced motion、页面后台、角色离屏和用户主动暂停时必须停止非必要动画。

### 1.2 产品范围

本轮角色仍然是“新任务入口的品牌辅助元素”，而不是贯穿整个对话的常驻桌宠。

默认支持三个业务状态：

- `idle`：新任务页安静观察。
- `focused`：输入框获得焦点或用户正在输入。
- `submitting`：用户提交任务时播放一次短反馈，随后角色随页面进入会话而离场。

`thinking`、`success`、`error` 暂不默认启用。只有后续确认角色需要在会话中常驻时，才通过单独的 UX 验证引入。

### 1.3 非目标

- 不在本轮把整个 Lumen Web 重做成强品牌化落地页。
- 不复制 Meoo 的 GIF 技术栈或视觉形象。
- 不为追求“技术先进”直接引入 WebGPU。
- 不在没有真实多角度需求时制作 glTF 真 3D 模型。
- 不让角色承担运行状态、错误提示或审批状态等关键语义；这些仍由现有 UI 文本和控件表达。

---

## 2. 成功标准

### 2.1 视觉与布局

- 桌面角色容器基准宽度：`168px`，允许 A/B 范围 `160–176px`。
- 移动角色容器基准宽度：`120px`，允许范围 `116–128px`。
- 角色容器与标题 DOM 盒不相交；不依赖透明像素“看起来没挡住”。
- 角色、标题、composer 在 390×844、768×1024、1207×516、1440×900、2560×1440 下均无裁切和遮挡。
- 200% zoom、系统字体放大和 320px 窄屏下仍能完成任务输入。
- 角色加载前后 `CLS = 0`；必须预留固定容器尺寸。

### 2.2 动画体验

- 基础呼吸周期 `3–5s`，只作用于胸肩/头部层，不缩放脚和整张角色。
- 眨眼间隔 `3–7s` 随机，偶发双眨，不与呼吸或大动作锁相。
- 自动大动作间隔 `8–18s`，同一动作连续触发有冷却。
- 姿势进入/退出使用 `250–450ms` blend/crossfade，不允许一帧硬切。
- focus 只触发视线、耳朵或轻微头部响应；不自动完整挥手。
- submit 一次性反馈控制在 `600–1200ms`，结束后不继续表演。
- 无常驻 sparkle；aura 静态或仅极低频亮度变化。

### 2.3 性能

- poster 立即显示，动画 runtime 不阻塞首屏主要内容。
- 首屏角色增量关键资源总传输目标 `< 2MB`；非当前状态 clip 按需加载。
- 桌面主流设备 p75 `≥55fps`，中端移动设备 `≥45fps`。
- 角色自身 p95 CPU/GPU frame time 目标 `<8ms`。
- 页面后台、角色离屏、静态模式时无持续 rAF/状态机推进。
- 动画增强失败时自动保留静态 poster，不影响输入和提交。

### 2.4 可访问性

- `prefers-reduced-motion: reduce` 下无自动换姿、指针视差、mesh warp 和常驻粒子。
- 提供“静态角色/完整动效”控制，用户选择写入 `localStorage`。
- 视觉角色保持 `aria-hidden="true"`；动效控制按钮单独具有可访问名称和键盘焦点。
- 动画不承担唯一的状态信息。
- 键盘用户可以在不触发大动作的情况下聚焦和使用 composer。

---

## 3. 目标架构

### 3.1 外部 seam

页面业务只使用一个深 module：

```ts
export type MascotActivity = 'idle' | 'focused' | 'submitting'

export type MascotMotionMode = 'full' | 'reduced' | 'static'

export interface MascotSceneProps {
  activity: MascotActivity
  motionMode?: MascotMotionMode
  className?: string
}
```

调用者只需要知道业务状态，不需要知道姿势图片、计时器、GSAP、Rive inputs、随机间隔或暂停策略。

### 3.2 module 内部职责

`MascotScene` 隐藏以下 implementation：

- renderer 选择与失败回退。
- idle、blink、focus、submit 的动作优先级。
- 随机动作调度和冷却。
- system reduced-motion 与用户偏好合并。
- Page Visibility 与 IntersectionObserver 暂停。
- poster → runtime 渐进增强。
- renderer 加载失败诊断。

### 3.3 两个 renderer adapter

计划中存在两个真实 adapter，因此 renderer seam 有实际价值：

1. `RasterMascotRenderer`：P0 上线版本与 Rive 失败 fallback。
2. `RiveMascotRenderer`：P1 主版本。

旧的 Canvas 网格实现只作为短期对照，不进入长期 interface；Rive 稳定后删除旧代码和 complete PNG。

### 3.4 目录建议

```text
src/web/src/components/
  landing-entry.tsx
  mascot/
    mascot-scene.tsx
    mascot-types.ts
    mascot-director.ts
    mascot-motion-preference.ts
    mascot-visibility.ts
    raster-mascot-renderer.tsx
    rive-mascot-renderer.tsx
    mascot.module.css
    mascot-director.test.ts
    mascot-motion-preference.test.ts

src/web/public/mascot/
  lumen-fox-poster.webp
  lumen-fox.riv
  raster/
    body-idle.webp
    body-idle-breath.webp
    body-blink.webp
    body-alert.webp
    body-wave.webp
    body-stretch.webp
    tail-fan.webp
```

### 3.5 动作优先级

从高到低：

1. `static` / 页面隐藏 / 离屏。
2. 用户触发的 `submitting` one-shot。
3. `focused`。
4. 正在运行的 idle one-shot。
5. base idle、breath、blink。

高优先级状态进入时暂停低优先级 one-shot，但 blink、breath、look 等独立通道不应通过全局 kill 被误杀。

---

## 4. 页面结构调整

### 4.1 从 Composer 移出角色

当前 `Composer` 通过 `showMascot` 把角色绝对定位在 composer 上方，这使标题、角色和输入框没有共同布局约束。

计划改为新增 `LandingEntry`：

```text
LandingEntry
├── LandingIntro
│   ├── MascotStage
│   └── 标题“有什么需要处理？”
└── Composer
```

职责调整：

- `Composer` 只负责输入、命令、文件选择与提交。
- `LandingEntry` 负责 landing 构图和本地 focus 状态。
- `MascotScene` 只负责角色，不再通过 `closest('.composer')` 查询外部 DOM。
- `LumenApp` 只决定当前是否为 landing，并传递 submit 行为。

### 4.2 桌面布局

- `LandingEntry` 宽度继续使用 `var(--conversation-rail)`。
- 标题居中，角色放在标题右侧上方的明确 grid 区域。
- 角色脚部可以轻触 composer 上边缘，但角色 DOM 盒不覆盖标题和 textarea。
- 默认气泡关闭；首次会话提示由标题和 placeholder 承担。

### 4.3 移动布局

- 角色宽 `120px`，置于标题上方或标题右侧的独立 grid cell。
- 角色、标题、composer 形成正常文档流，不使用跨区域绝对定位。
- 低于 360px 时允许降至静态 poster，并缩小到 `108–116px`。
- 移除移动端 aura 和 shadow blur；保留轻微静态接触阴影。

---

## 5. 分阶段实施

### M0 · 基线、测量与保护网（0.5–1 天）

#### 任务

1. 固化当前 4 个视觉基线：desktop idle、focus、hover、390×844 mobile。
2. 新增布局测量脚本或 Playwright helper，记录标题、角色、composer 的盒尺寸和交集。
3. 记录当前关键资源体积、首屏请求数、空闲 10 秒主线程占用和帧率。
4. 为 animation state 增加可测试的固定随机种子或 visual-test 模式。
5. 建立 renderer 选择开关，仅开发/测试环境支持 `legacy | raster | rive`。

#### 预计文件

- `src/web/package.json`
- `src/web/e2e/mascot.spec.ts`（若引入 Playwright）
- `src/web/playwright.config.ts`
- `design-audit/2026-08-02-web3d/`

#### Definition of Done

- CI 或本地脚本能稳定复现固定状态截图。
- mobile 基线测试能够明确失败于当前标题/角色相交。
- 测试不会依赖动画运行到某个不确定帧。

---

### M1 · 布局与尺寸 P0（1–1.5 天）

#### 任务

1. 新增 `LandingEntry`，把角色从 `Composer` 移出。
2. 删除 `Composer.showMascot` 与 `scene.closest('.composer')` 依赖。
3. 桌面角色改为 168px，移动角色改为 120px。
4. 用 CSS grid / normal flow 建立标题、角色、composer 安全区。
5. 删除默认气泡自动循环；仅保留后续显式触发能力。
6. 桌面端与移动端全部删除常驻 sparkles，不保留周期性粒子循环。
7. 桌面端 aura 改为不旋转的静态柔光；移动端完全移除 aura 和大 blur shadow，只保留轻微静态接触阴影。

#### 预计文件

- 修改 `src/web/src/components/lumen-app.tsx`
- 修改 `src/web/src/components/composer.tsx`
- 新增 `src/web/src/components/landing-entry.tsx`
- 修改 `src/web/src/app/globals.css`
- 新增/迁移 `src/web/src/components/mascot/mascot.module.css`

#### Definition of Done

- 所有目标 viewport 无标题遮挡。
- composer 输入、slash command、@ 文件、queue、stop、send 行为无回归。
- 新任务页视觉主次顺序为标题 → composer → 角色。
- mobile 角色宽不超过 composer 的 35%。
- desktop 与 mobile 均不存在常驻 sparkle 动画；aura 不包含旋转或周期性位移。

---

### M2 · Raster V2 与动作 director（2–3 天）

#### 任务

1. 新建 `MascotScene`、类型和动作 director。
2. 使用现有分层 WebP：`tail-fan` 在后，body 状态在前。
3. 为所有 body/tail 素材测量 alpha bounds、脚部基线和尾根锚点，集中写入 `mascot-assets.ts`。
4. 删除全身 10×10 mesh warp 和每帧 200 次 triangle draw。
5. base idle 使用静态 body、独立 tail，以及作者控制的 `body-idle-breath.webp` 局部呼吸循环。
   - 呼吸素材周期必须为 3–5 秒。
   - 只允许胸肩、鬃毛和头部产生低幅运动；脚部、身体落地点和尾根逐帧保持固定。
   - 禁止通过 CSS/GSAP 对完整 body 图片执行 `scaleX`、`scaleY` 或整体呼吸位移。
   - 如果局部呼吸素材尚未通过美术验收，Raster V2 先使用静态 body，不得以整图缩放作为临时替代。
6. blink 使用 idle/blink body 的短 crossfade。
7. focus 使用小幅 body/head 位移或 alert 的克制 crossfade，不使用整平面 3D rotation。
8. submit 使用一次性 wave，结束后返回 idle；如果页面立即切换，则允许自然淡出。
9. 自动大动作使用 8–18 秒随机调度，并加入同动作冷却。
10. 每种运动使用独立 wrapper/通道，不再调用全局 `killTweensOf(character)`。

#### 动作 director interface

```ts
type MascotFrameState = {
  base: 'idle' | 'focused'
  oneShot: 'wave' | 'stretch' | null
  blink: boolean
  paused: boolean
}

function useMascotDirector(
  activity: MascotActivity,
  motionMode: MascotMotionMode,
): MascotFrameState
```

随机数与时间依赖由 implementation 注入到纯调度逻辑，单元测试使用确定性 clock/random；不把这些依赖暴露给页面调用者。

#### 预计文件

- 新增 `src/web/src/components/mascot/mascot-scene.tsx`
- 新增 `src/web/src/components/mascot/mascot-director.ts`
- 新增 `src/web/src/components/mascot/mascot-types.ts`
- 新增 `src/web/src/components/mascot/mascot-assets.ts`
- 新增 `src/web/src/components/mascot/raster-mascot-renderer.tsx`
- 删除或替换 `src/web/src/components/mascot-scene.tsx`
- 整理 `src/web/public/mascot/raster/`

#### 单元测试

- idle 大动作间隔始终落在配置范围。
- focused 时不会触发自动 wave/stretch。
- submitting 只触发一次 one-shot。
- one-shot 完成后回到正确 base。
- blink 与 one-shot 可并行，不会被全局 kill。
- static/paused 不调度新 timer。
- 相同随机种子产生相同事件序列。
- unmount 后所有 timer 与 listener 清理。

#### Definition of Done

- 不再存在 Canvas mesh warp。
- 空闲 20 秒中，脚部基线无跳跃，姿势变化无一帧硬切。
- 呼吸逐帧检查确认运动仅发生在胸肩/鬃毛/头部，脚部、落地点和尾根保持稳定。
- M0 性能基线不退化，主线程占用应明显下降。
- Raster V2 可独立上线，并成为 Rive 加载失败 fallback。

---

### M3 · 动效偏好与生命周期（1–1.5 天）

#### 任务

1. 新建 motion preference module，合并系统与用户偏好。
2. 监听 `MediaQueryList.change`，系统设置变化无需刷新页面即可生效。
3. 使用 Page Visibility 暂停后台动画。
4. 使用 IntersectionObserver 暂停离屏动画。
5. 新增“完整动效 / 静态角色”控制，并持久化用户选择。
6. static 模式只渲染 poster，不创建 GSAP timeline、timer 或 renderer loop。

#### 偏好优先级

```text
用户选择 static
  > 系统 prefers-reduced-motion
  > 默认 full
```

系统 reduced motion 默认映射为 `reduced`；用户可进一步选择 `static`，但页面不主动覆盖用户明确选择。

#### 预计文件

- 新增 `mascot-motion-preference.ts`
- 新增 `mascot-visibility.ts`
- 修改 `landing-entry.tsx`
- 修改 `mascot.module.css`

#### Definition of Done

- 后台、离屏、static 三种情况均无持续动画推进。
- 系统 reduced-motion 动态切换后 1 秒内进入 reduced 状态。
- 控制按钮可键盘访问，有清楚的 `aria-label` 和 focus 样式。
- localStorage 损坏或不可用时安全回退，不影响页面。

---

### M4 · Rive 美术拆层与状态机（3–5 个动效设计日）

此里程碑依赖动效/视觉设计，不与 M1/M2 阻塞；可并行制作。

#### 美术拆层

- 头部
- 左/右耳
- 左/右眼睑
- 左/右瞳孔
- 嘴与可选口型
- 胸肩/鬃毛
- 躯干
- 左/右前爪
- 九尾分组与尾根
- 接触阴影
- 可选静态 aura

#### 状态机 inputs

建议保持小 interface：

```text
activity: number        // idle, focused, submitting
motionLevel: number     // full, reduced, static
lookX: number           // -1..1
lookY: number           // -1..1
submit: trigger
```

blink、breath 和自动 idle one-shot 在 Rive 内部或 director 内部管理，但只能由一个位置负责，不能两边同时调度。

#### 动画层

- `Base Idle`
- `Breath`
- `Blink`
- `Look Target`
- `Ear Follow`
- `Wave One-shot`
- `Stretch One-shot`
- `Submit/Nod One-shot`

#### 动画规则

- 眼睛先于头部响应，头部先于身体响应。
- 脚部和尾根保持稳定锚点。
- blink 不覆盖嘴型和 one-shot。
- reduced 模式只保留偶发眨眼或完全静止，由最终可用性测试决定。
- 所有 one-shot 可从任意 base 平滑进入并返回。

#### 输出物

- `lumen-fox.riv`
- `lumen-fox-poster.webp`
- 状态机 input 文档
- 每个状态的录屏/逐帧验收稿
- 动作时长与 easing 表

#### Definition of Done

- 同一角色在所有状态下比例、眼距、耳位、脚位和尾根一致。
- 8–18 秒 idle 录屏中无明显机械循环、滑脚或轮廓跳变。
- 动画设计评审通过 desktop 168px 与 mobile 120px 两个真实展示尺寸，而不是只看大画布。

---

### M5 · Rive runtime adapter 与渐进增强（2–3 天）

#### 任务

1. 在独立 spike 中确认 Rive Web/React runtime 包体、WASM 加载、Safari 与当前 Next 15 构建兼容性。
2. 新建 `RiveMascotRenderer`，通过 dynamic import 仅在客户端加载。
3. 首帧先显示 poster；runtime ready 后在固定容器内短 crossfade 增强。
4. `MascotActivity` 映射到 Rive inputs，不把 Rive input 名称泄露给 `LandingEntry`。
5. Rive 加载、初始化或资源失败时自动回退 `RasterMascotRenderer`。
6. full 模式才加载 Rive；reduced/static 默认留在轻量 renderer/poster。
7. 后台和离屏时停止 Rive playback；恢复时从稳定 base 恢复，不补播错过的大动作。

#### 预计文件

- 修改 `src/web/package.json`
- 新增 `rive-mascot-renderer.tsx`
- 修改 `mascot-scene.tsx`
- 新增 `public/mascot/lumen-fox.riv`
- 新增 `public/mascot/lumen-fox-poster.webp`

#### Definition of Done

- poster → Rive 无布局跳动、无白闪和重复入场动作。
- 模拟 `.riv` 404、WASM 加载失败和 runtime exception 时，输入功能仍完整且 Raster fallback 可见。
- Rive inputs 只在值变化时更新，不在每次 React render 重建实例。
- 资源与性能达到第 2 节预算。

---

### M6 · 视觉回归、无障碍、性能与上线（1.5–2 天）

#### 自动化测试矩阵

| 类型 | 场景 |
|---|---|
| Unit | director 调度、优先级、清理、motion preference |
| Integration | focus → focused；submit → one-shot；离开 landing → 卸载 |
| Visual | desktop idle/focused、mobile、reduced/static、Rive fallback |
| Layout | mascot/title/composer 盒不相交 |
| Accessibility | 键盘、focus、aria-hidden、动效控制名称 |
| Failure | poster/Rive/raster 单项加载失败 |
| Performance | 首屏传输、10s idle frame time、后台/离屏零推进 |

#### Viewport

- 320×568
- 390×844
- 768×1024
- 1207×516
- 1280×720
- 1440×900
- 2560×1440

#### 人工体验检查

1. 连续观察 idle 30 秒，记录是否出现重复感、抢注意力或滑脚。
2. 连续 focus/blur 10 次，确认不会重复完整挥手或积累 timer。
3. 输入、slash command、@ 文件、submit、stop 全流程回归。
4. 系统 reduced motion 在页面打开期间动态切换。
5. 页面切后台 30 秒后恢复，不补播动画、不跳姿态。
6. 200% zoom 与系统字体放大。
7. Safari、Chrome、Firefox 至少各一次真机检查。

#### A/B 版本

- A：当前 legacy Canvas。
- B：Raster V2，168/120px，减法动效。
- C：Rive，保持与 B 完全一致的布局尺寸。

主观指标：

- “自然”评分。
- “不打扰输入”评分。
- “与 Lumen 品牌一致”评分。
- “愿意长期保留”评分。

行为指标仅在项目已有合规埋点能力时采集；本计划不为此单独引入第三方分析服务。

#### 上线策略

1. 先上线 M1–M3 Raster V2。
2. 内部/开发环境开启 Rive。
3. Rive 达标后成为默认，Raster 保留失败 fallback。
4. 稳定一个发布周期后删除 legacy Canvas、complete PNG 与调试开关。

#### Definition of Done

- `pnpm test`、`pnpm typecheck`、`pnpm build` 全部通过。
- 浏览器控制台无新增 error/warning。
- 所有目标 viewport 截图通过。
- reduced/static/失败 fallback 均已验证。
- 产品、设计和前端三方接受同一份最终截图与动效录屏。

---

## 6. 具体文件变更清单

### 修改

- `src/web/src/components/lumen-app.tsx`
  - landing 与 conversation 布局分流。
  - 使用 `LandingEntry`。
- `src/web/src/components/composer.tsx`
  - 删除 `showMascot`。
  - 通过明确的 focus/blur props 与 landing 协作。
- `src/web/src/app/globals.css`
  - 删除旧 mascot 全局样式。
  - 保留 conversation/composer 共享布局 token。
- `src/web/package.json`
  - M0 可选增加 Playwright。
  - M5 增加经 spike 确认的 Rive runtime。

### 新增

- `src/web/src/components/landing-entry.tsx`
- `src/web/src/components/mascot/*`
- `src/web/public/mascot/*`
- `src/web/e2e/mascot.spec.ts`
- 必要的 visual baseline。

### 最终删除

- `src/web/src/components/mascot-scene.tsx` 旧 Canvas implementation。
- `src/web/public/lumen-fox-complete-*.png`，确认没有其他调用后删除。
- 旧 aura/sparkle/bubble 自动循环 CSS。
- 临时 renderer 调试 flag。

删除前必须用 `rg` 确认无引用；资产删除放在 Rive 默认稳定后的独立提交中，便于回滚。

---

## 7. 测试设计细节

### 7.1 Director 测试通过外部 interface

测试应断言可观察的 `MascotFrameState` 和调度结果，不断言内部 timer 数量、GSAP 对象或 Rive实例。

需要注入的本地依赖：

- clock
- random

生产使用真实 adapter，测试使用 fake clock 与确定性 random。这是本地可替代依赖，不需要把 adapter 暴露到页面 interface。

### 7.2 Renderer contract tests

Raster 与 Rive 必须共享以下行为：

- 接收相同 `activity` / `motionMode`。
- 保持固定容器尺寸。
- static 状态不推进。
- submitting one-shot 最多触发一次。
- 初始化失败返回 fallback，而不是抛到页面。

### 7.3 视觉测试稳定性

- visual-test 模式使用固定 random seed。
- 冻结在明确状态，不依赖 sleep 猜测动画帧。
- Rive 截图使用确定时间输入或测试状态机 input。
- 动态效果另用短录屏人工验收，不把完整时序塞进像素快照。

---

## 8. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---:|---:|---|
| Rive 美术拆层耗时高 | 中 | 高 | M1–M3 Raster V2 独立可上线，不阻塞体验改进 |
| Rive runtime 包体或 WASM 过大 | 中 | 中 | spike 先测；poster 首屏；full 模式延迟加载；Raster fallback |
| 角色缩小后品牌感过弱 | 中 | 低 | 160/168/176 三档截图 A/B，控制轮廓而非仅 CSS box |
| crossfade 产生双影 | 中 | 中 | 统一脚位/眼位锚点；过渡只用于短时；最终由 rig blend 替代 |
| timer/状态竞争导致重复动作 | 中 | 中 | 单一 director、明确优先级、确定性测试、unmount 清理 |
| 移动端仍因语言/字体变化遮挡 | 低 | 高 | normal flow/grid；DOM box 不相交测试；200% zoom |
| reduced motion 只在首屏读取 | 中 | 中 | 监听 media query change；集中 motion preference module |
| Rive 加载失败导致空白 | 低 | 高 | poster 常驻直到 ready；异常自动 Raster fallback |
| 旧资产删除影响回滚 | 低 | 中 | 稳定一个发布周期后独立删除提交 |

---

## 9. 工作量与角色分工

| 里程碑 | 前端工程 | 动效/视觉 | 合计预估 |
|---|---:|---:|---:|
| M0 基线 | 0.5–1 天 | 0 | 0.5–1 天 |
| M1 布局尺寸 | 1–1.5 天 | 0.5 天评审 | 1.5–2 天 |
| M2 Raster V2 | 2–3 天 | 0.5–1 天校准 | 2.5–4 天 |
| M3 偏好/暂停 | 1–1.5 天 | 0 | 1–1.5 天 |
| M4 Rive 资产 | 0.5 天协作 | 3–5 天 | 3.5–5.5 天 |
| M5 Rive 接入 | 2–3 天 | 0.5 天校准 | 2.5–3.5 天 |
| M6 QA/上线 | 1.5–2 天 | 0.5 天验收 | 2–2.5 天 |

串行总计约 13–20 个工作日；M4 与 M1–M3 并行时，日历周期约 10–14 个工作日。若只有前端工程师且需要同时学习/制作 Rive，建议预留 3–4 周。

---

## 10. 提交与回滚建议

建议拆成可独立回滚的提交：

1. `web: add mascot visual baselines`
2. `web: move mascot into landing layout`
3. `web: add raster mascot director`
4. `web: respect mascot motion preferences`
5. `web: add rive mascot adapter`
6. `web: make rive mascot the default`
7. `web: remove legacy canvas mascot`

每个提交都必须保持 `pnpm test`、`pnpm typecheck` 和 `pnpm build` 可通过。旧 Canvas 删除必须是最后一个独立提交，确保上线异常时可以单独 revert。

---

## 11. 真 3D 决策门

Rive 上线后再评估真 3D。只有以下至少两项成立才创建 glTF + Three/R3F 项目：

- 角色需要真实转身或自由视角。
- 需要动态灯光、材质变化或镜头运动。
- 角色会进入更大的 3D 场景。
- 需要大量运行时组合的动作、表情、道具或服装。
- 角色会在远大于 168px 的展示区域长期出现。

若满足条件，单独进行 2–3 天技术 spike，比较模型体积、骨骼/morph、Safari/WebGL2、低端设备帧率与制作成本。该 spike 不与当前 P0/P1 路线混在同一实施周期。

真 3D spike 的最低技术方案必须明确包含：

- glTF 骨骼与独立 animation clips，用于 base idle、breath 和一次性动作。
- 眼睑、瞳孔、嘴型和表情 morph targets。
- Three.js `AnimationMixer` 作为统一动画调度器。
- `AnimationAction.crossFadeFrom` / `crossFadeTo` 处理姿势连续过渡。
- additive blending 管理 blink、breath、head follow 等可叠加通道，避免 one-shot 覆盖全部基础动作。
- 窄 FOV 或正交相机、模型 bounds 自适应 fit、固定脚部/尾根锚点。
- KTX2、Draco 或 Meshopt 压缩，以及按设备能力调整 DPR。

若 spike 无法在目标设备上达到第 2 节性能预算，或自然度没有显著超过 Rive，则终止真 3D 路线并保留 Rive。

---

## 12. 最终交付定义

完成本计划意味着：

1. Lumen 新任务页在桌面与移动端拥有稳定、清晰的布局层级。
2. 角色不再依赖整图 Canvas 网格扭曲和全平面 3D 旋转。
3. Raster V2 是可靠 fallback，Rive 是默认增强。
4. 动画状态由单一 director 管理，页面只传业务 activity。
5. full、reduced、static、后台、离屏和失败回退都有测试。
6. 角色更自然，但不会降低 coding-agent 的输入效率和专业感。

---

## 13. 决策依据

以下官方资料是本计划的实现与验收依据：

- [W3C WCAG 2.2：Pause, Stop, Hide](https://www.w3.org/WAI/WCAG22/Understanding/pause-stop-hide)：持续自动运动需要可暂停、停止或隐藏；对应 M3 的静态角色入口、后台和离屏暂停。
- [Rive State Machine Playback](https://rive.app/docs/runtimes/state-machines)：状态机 inputs、transitions 与 settled 行为；对应 M4–M5 的独立动作通道和停止推进策略。
- [Three.js AnimationMixer](https://threejs.org/docs/pages/AnimationMixer.html)：真 3D 动画调度；对应第 11 节的条件式 glTF 路线。
- [Three.js AnimationAction](https://threejs.org/docs/pages/AnimationAction.html)：fade、crossfade、weight 与 warp；对应姿势过渡和 clip 权重管理。
- [Three.js AnimationUtils](https://threejs.org/docs/pages/AnimationUtils.html)：additive animation clip；对应 blink、breath 和 head-follow 的叠加层。
