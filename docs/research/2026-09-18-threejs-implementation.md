# WebUI Three.js 升级实施记录

日期：2026-09-18。依据：[实施计划](../plans/2026-09-18-webui-threejs-upgrade-plan.md)。
用户要求暂不执行 M4；九尾狐最初选择“只完成加载接口，等待正式模型”。
后续获准尝试本地建模，但第一版因与参考图差距明显被用户否决：实验资产已移出 public，
预览恢复视频，首页 3D 入口锁定。启用前必须通过[角色质量门禁](../brand/mascot/three/README.md)。

## 1. 已交付的范围

| 阶段 | 实现与入口 | 交付边界 |
| --- | --- | --- |
| M0 | 静态构建基线、依赖与许可核对、`report-scene-bundles.mjs` | 没有进行设备 GPU 或输入延迟基准 |
| M1 | Three.js 0.186.0、R3F 9.7.0、Drei 10.7.8；`scene-canvas.tsx` | 按需加载，透明 WebGL，无阴影或后处理；DOM 操作独立于 GPU 模块 |
| M2 | `voice-focus.tsx`、`voice-scene.tsx`、Live 音频分析接口 | 大语音视图复用当前通话；输入/输出 RMS 分离，静音、审批、重连、异常有明确状态 |
| M3 | `document-preview.tsx`、`model-preview.tsx`、`glb.ts`、`three-model.ts` | 工作区自包含静态 GLB 预览、旋转/缩放/复位和原文件下载 |
| M5 | `three-mascot-renderer.tsx` 与现有 `mascot-scene.tsx` 装配 | 仅正式 GLB 加载与动画接口；没有生成或引入九尾狐模型，默认视频不变 |

三个运行时依赖均为 MIT，实际安装的 React / React DOM 19.2.8 满足 Fiber 与 Drei 的 peer 契约。
没有安装其他候选渲染库，没有增加首页环境背景、粒子或对话 3D 效果。
保留本会话已有的执行过程内联、成功后收起和活动步骤文字流光交互。

## 2. 交互与资源生命周期

语音专注视图桌面使用居中浮层，移动端占满视口；收起只关闭视图，结束按钮才停止通话。
静音、打断、按住说话、设备切换、字幕和审批继续使用原 Live 权威，GPU 不拥有通话状态。
输入 RMS 仅用于聆听状态，输出 RMS 仅用于助手说话；TTS 无可分析输出时不伪造音量。
审批、异常、重连使用静态中性形体；reduced/static 模式与 GPU 失败保留 DOM 静态反馈。

音频分析只在活动且可见的大视图内启动，不额外申请麦克风。
Host 路径复用既有 AudioContext，分析分支经过零增益节点，不改变可听播放路由；
Direct 路径拥有独立分析 context，关闭视图释放它，原媒体连接继续。
取消后迟到的麦克风流或 Host Live 会话会被释放；切换设备保留静音状态，
结束与打断清空 PCM 播放队列，迟到的 TTS 完成事件不恢复已结束的播放状态。

Canvas 桌面 DPR 上限 1.5，窄屏 1；静态预览 demand，动画最多以 30Hz 请求帧。
离屏/后台停止请求，卸载移除事件和定时器；几何、材质、纹理、骨骼及 ImageBitmap
按拥有关系去重释放。模型解析途中关闭时丢弃并释放迟到的解析结果。
context loss、渲染失败以及动态模块加载失败由各 DOM 所有者降级，操作控件和下载保留。
动态模块加载失败提示“刷新页面后重试”；正常模型解析与 WebGL 错误有局部重试。

## 3. GLB 初始约束

工作区文件读取仍通过现有 API 和路径权限，不改变后端边界。二进制不转成文本。
解析前检查 GLB 2.0 结构、JSON/BIN 边界、节点拓扑、accessor、材质与嵌入图片头，
拒绝任何 URI（包括 data/blob）、外部资源、稀疏 accessor、morph 和不支持的扩展。
工作区预览拒绝骨骼与动画；角色路径单独允许经过检查的骨骼和有限动画。
不加载 CDN 解码器，仅接受基础 PNG/JPEG 嵌入纹理与 `KHR_materials_unlit`。

初始上限：文件 20MiB、JSON 1MiB、解码几何 32MiB、512 节点/32 层、128 mesh、
256 draw batch、20 万顶点/40 万实例顶点；16 张纹理，单张最大 2048²，
总计不超过 8,388,608 像素。角色额外限制骨骼、关节与动画数量。
这些是解析前的保守限制，尚未经过 M4 真机测试冻结；不宣称覆盖所有合法 GLB。

## 4. 角色接口与本地验证

以下是加载接口的配置方式，目前受 MODEL_APPROVED=false 门禁限制，不能启用角色。
仅在角色质量验收通过后，才可解除门禁、将正式模型放入 `src/web/public/mascot/` 并配置：

```sh
NEXT_PUBLIC_MASCOT_RENDERER=three \
NEXT_PUBLIC_MASCOT_MODEL_URL=/mascot/fox.glb \
pnpm --dir src/web build
```

URL 仅接受 `/mascot/` 内 GLB 路径，拒绝父目录、远程地址和路径逃逸。
接口选择 `idle`（或首个 clip）与 `react` / `reaction` / `wave` 动画；
正式模型的比例、姿态、骨骼和转场等待真实资产验收。
未配置正式模型、不可见、静态偏好或加载失败时保留既有视频/图片路径。

`/scene-preview` 是本地功能验证页，静态导出对应 `/scene-preview.html`。
模拟状态、RMS 和审批均有显式说明，不连接 Provider、不申请麦克风、不执行文件工具。
立方体是页内生成的自包含 GLB 验证样本，不是角色资产；本地文件选择不上传文件。

## 5. 静态包体记录

[实施前基线](2026-09-18-threejs-baseline.json) 的首页脚本 gzip 合计 322,967 字节，8 个脚本。
最终数值与逐文件记录见 [构建包体报告](2026-09-18-threejs-bundles.json)，通过命令生成：

```sh
node src/web/scripts/report-scene-bundles.mjs docs/research/2026-09-18-threejs-bundles.json
```

导出的首页与验证页启动脚本未包含 `THREE.WebGLRenderer:` / `THREE.GLTFLoader:` 实现标记。
Three 场景在打开相应功能后按需加载；共享异步 chunk 不能按场景重复计入总包体。
这里统计 gzip 文件字节数和构建 manifest，不是实际网络请求、解析耗时、GPU 或输入延迟。

| 静态导出 / manifest 分组 | gzip 字节数 | 说明 |
| --- | --- | --- |
| 首页启动脚本 | 326,898 | 9 个 script，相对基线增加 3,931 字节（约 1.22%） |
| 本地场景验证页启动脚本 | 154,229 | 不含按需 Three 主体 |
| VoiceScene 异步文件集合 | 243,460 | manifest 列出 5 个文件，包含共享依赖 |
| ModelPreview 异步文件集合 | 265,003 | manifest 列出 7 个文件，包含共享依赖 |
| ThreeMascotRenderer 异步文件集合 | 260,217 | manifest 列出 6 个文件，包含共享依赖 |

后三行从本次 `.next/react-loadable-manifest.json` 的对应组件映射，
对其 `files` 去重后求导出文件 gzip 总和；它们彼此共享文件，不能相加为新增下载总量。
版本升级与 chunk 拆分会改变这些值，需重新构建再比较。

## 6. 实际验证与未验收项

- `pnpm --dir src/web test`：217 项测试、29 个文件通过，覆盖真实 GLTFLoader
  几何解析/零外部 fetch、GLB 边界、资源释放、音频分析分支、迟到取消、PCM 清空、
  语音复用/审批/焦点及实际 React.lazy 拒绝后的 DOM 操作保留。
- `pnpm --dir src/web typecheck` 与 `pnpm --dir src/web build` 通过。
- pnpm 10.34.5 在隔离 package/lock 副本上 frozen lockfile-only 验证通过；
  本地完整安装与构建使用 Node 24.19.0 / pnpm 12.3.4，不冒充 Node 22 CI 环境验证。
- Chrome 最终导出检查桌面语音、审批、模型以及 390px 移动视图，截图位于
  `.impeccable/review/`。该页使用模拟音量/审批，不能代表真实 Provider 通话。
- 浏览器存在 R3F 使用 `THREE.Clock` 的第三方弃用提示；测试有 Three CJS 弃用提示，
  均未导致验证失败。Canvas 卸载时的 Context Lost 日志不作性能异常证据。

M4 未执行：没有 Android/iOS 真机、FPS、GPU、功耗、长任务或 p95 输入延迟验收。
未进行真实 Provider 端到端语音验证，正式九尾狐资产仍待提供。
代码与文档未提交、推送或发布。

## 7. 设计复核

按 Impeccable 现有界面扩展流程保留主 WebUI 的暖白底、Geist、低对比辅助文字与少量琥珀强调。
独立 finish reviewer 的最终 disposition 为 `ship`，范围仅为其列出的动态模块失败隔离与恢复
修复：三处 DOM 所有者降级、操作/下载保留及真实页面刷新恢复均评分为 resolved。
五张最终截图通过其声明范围，包括 390×844 CSS 的移动审批视图；不扩大为真机性能通过。
documenter 对照现有系统与截图，保留 root `PRODUCT.md` / `DESIGN.md`，
没有将 Architecture Atlas 的设计权威覆盖到主 WebUI，也没有创建新的全局设计体系。
