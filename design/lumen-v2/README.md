# Lumen v2 设计交付

本目录是 2026-10-20 的 r2 设计提案，已按最新反馈改为 Muse 的信息组织、当前 Lumen 的视觉与动态形象；不是已上线的 v2 实现。保留现有 `design/` 参考文件与产品源码。

后续方向更新（2026-10-21）：[真实产品研究与联动审计](../../docs/research/2026-10-21-muse-chatgpt-space-product-research.md)发现目标／对话／成果没有形成工作链。[新优化实施计划](../../docs/plans/2026-10-21-lumen-v2-product-flow-upgrade.md)接替 r2 的文件自动化入口方向；本目录交互保留为历史提案，没有据新方案改写原型。Muse 官方设计原文以 Meta 发布文直接链接的 `introducing.muse.ai` 为准，原型中补全的交互不作为官方事实。

## 打开

当前生产源码已按新计划接通目标／资料工作流；见[实施验证](../../docs/research/2026-10-21-v2-product-flow-validation.md)。下面仍是旧 r2 静态演示的启动方式，不代表新版生产界面的 API 行为。

在仓库根目录运行：

```bash
python3 -m http.server 8876 --bind 127.0.0.1 --directory design
```

打开 [Lumen v2 原型](http://127.0.0.1:8876/lumen-v2/#chat)。若端口已被使用，选择一个空闲端口。静态运行文件全部在本目录，无 CDN。请使用 HTTP 预览，确保动态视频与 localStorage 一致加载。

## 交付内容

| 交付 | 入口 |
| --- | --- |
| 可点击 UI 原型 | [index.html](index.html)，对话、信息流、灵感、目标、资源库，辅助自动化及设置 |
| 深度分析与当前 UI 截图 | [research.md](research.md) |
| 技术升级实施计划 | [implementation-plan.md](implementation-plan.md) |
| 原动态九尾狐的原型桥接 | [mascot-entry.tsx](mascot-entry.tsx)，复用生产播放器和 idle / react 视频 |
| 当前资产来源与历史提案 | [PROVENANCE.md](assets/PROVENANCE.md) |
| 该提案的设计系统 | [DESIGN.md](DESIGN.md) |
| 验证记录与限制 | [validation.md](validation.md) |

## 推荐体验路径

1. 在默认对话页与原动态九尾狐打招呼，使用快捷建议填入输入框。
2. 在“信息流”收藏阅读笔记，切换到“资源库”查找；打开简报、编辑，或回到信息流继续讨论。
3. 在“灵感”查看推荐理由，开始一条建议；从对话把原始意图带入自动化草稿。忽略或开始建议都不会直接启用任务。
4. 保存草稿、模拟试运行、确认后启用；在辅助“自动化”入口暂停，再回信息流检查安排。
5. 从信息流查看邮件草稿，核对收件人、主题、正文与授权范围，再本次确认或拒绝模拟发送。
6. 打开自动化详情，选择来源失败、预算用尽或送达不确定；不确定时回查而不重发。
7. 在“设置”中查看个性化偏好及连接权限；撤销示例邮箱后检查来源提示。导航不设独立记忆入口。
8. 缩至 390 px 检查窄屏布局；设置内另有深色预览，默认仍是当前 Lumen 浅色。

## 自检

使用仓库已安装的 jsdom，无新增依赖：

```bash
node design/lumen-v2/selfcheck.cjs
```

23 项行为检查通过。动态播放器桥接的重建命令为 `node design/lumen-v2/build-mascot.cjs`；它依赖现有 Web node_modules，自动复制生产视频并打包原 renderer，不手工编辑生成的播放器文件。

自检不调用真实 API。原型只在 `lumen.v2.prototype.1` 保存演示状态；重置只影响此 key。个人姓名、邮箱、计划与活动均为虚构示例，任何运行、连接、授权或发送都不产生真实外部动作。

主要技术参考为用户指定的 [Claude 自动化文章](https://claude.dev/blog/building-effective-agent-automations/)。第一轮生成的 Q 版 PNG 和旧编号截图保留为历史，当前页面没有使用新形象或暖色主题。
