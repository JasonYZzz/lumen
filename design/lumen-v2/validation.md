# Lumen v2 r2 原型验证记录

2026-10-20。范围为 `design/lumen-v2/` 本机静态原型与复用的现有动态形象；不是自动化后端、真实 Provider 或个人应用 Connector 验收。

## 实际执行

| 检查 | 结果 |
| --- | --- |
| JavaScript 语法 | app.js、surfaces.js、构建与自检脚本通过 |
| `node design/lumen-v2/build-mascot.cjs` | 通过；使用已安装 esbuild 打包现有播放器，未新增依赖 |
| `node design/lumen-v2/selfcheck.cjs` | 23 项通过，包含最后三项行为修正的回归 |
| 原播放器相关测试 | video-mascot-renderer / mascot-playback / mascot-motion-preference 三文件、22 tests passed |
| 现有形象与标志复用 | 三份 MP4 / poster 及 Logo 与生产资源逐字节一致 |
| 实际浏览器动态形象 | 欢迎页为 `data-renderer="video"`，canvas 可见且有有效尺寸；非仅静态图片 |
| 生产配色对照 | body 背景 rgb(253,253,252)、正文 rgb(36,36,34)，与当前源码一致 |
| 桌面捕获 | 对话、信息流、灵感、资源库，六张 r2 证据中的四张 |
| 390 × 844 捕获 | 对话、信息流；document scrollWidth 383，无横向溢出 |
| scoped detector | 本轮运行一次；保留用户指定现有字体与中性样式；r1 设计文档已同步为 r2 |
| 独立结束审查 | 视觉符合修订 brief；三项交互问题修正后结论 Ship，仅限原型 |

原播放器测试命令：

```bash
pnpm --dir src/web test src/components/mascot/video-mascot-renderer.test.tsx src/components/mascot/mascot-playback.test.ts src/components/mascot/mascot-motion-preference.test.ts
```

浏览器动态观察由主执行者完成；独立审查以源码、截图和自检复核，没有单独验证视频播放或完整辅助技术行为。复用原 motion / visibility hooks 并通过相关单测，不等于真实设备上的全部动效验收。

## 23 项自检覆盖

1. 五个主导航、辅助自动化与设置子页正常渲染；没有独立记忆或形象入口。
2. 取消创建不提交。
3. 对话意图带入可编辑草稿。
4. 信息流任务随启停变化，健康提示检查全部来源。
5. 恢复连接不改写历史来源覆盖。
6. 草稿成功试运行后才可启用。
7. 空来源与未连接来源阻止创建。
8. 送达不确定暂停，回查不重发。
9. 预算用尽与来源不完整分别展示。
10. 审批为一次性决定。
11. 拒绝保留草稿，不模拟发送。
12. 撤销连接使相关试运行失效。
13. 设置内偏好转义、编辑和遗忘。
14. 资源编辑持久化，搜索支持空态。
15. 重置只清本原型命名空间。
16. 信息流收藏进入资源库，可取消。
17. 主题筛选不隐藏待审批项。
18. 灵感可忽略、恢复、进入对话，不直接启用任务。
19. 新对话或其他灵感之后重开建议，恢复正确意图。
20. 从搜索开始灵感会关闭搜索面板。
21. 收藏和资源筛选更新后保持键盘焦点。
22. 损坏 JSON 恢复示例初态。
23. 不完整持久状态恢复示例初态。

## 本轮审查修正

| 发现 | 最终修正 |
| --- | --- |
| 开始过的灵感在新对话后不再恢复原意图 | 每次开始恢复对应 intent，带入自动化草稿；只有完全相同的末条意图避免重复 |
| 从全局搜索开始灵感后，搜索层仍遮挡对话 | 动作先关闭 dialog，再打开对话 |
| 收藏或筛选重绘后焦点丢失 | render 保存并恢复触发控件；目标消失时回退 main |

三项均有回归断言，独立复核均 resolved；无需为未改变视觉的行为修正重新制作截图。

## 当前证据

| 截图 | 内容 |
| --- | --- |
| [r2-chat-desktop](evidence/r2-chat-desktop.jpg) | 当前 Lumen 风格对话、原动态九尾狐的一帧 |
| [r2-feed-desktop](evidence/r2-feed-desktop.jpg) | 信息流、待确认、来源与安排 |
| [r2-ideas-desktop](evidence/r2-ideas-desktop.jpg) | 灵感分类、推荐理由与开始动作 |
| [r2-library-desktop](evidence/r2-library-desktop.jpg) | 资源搜索、类型与收藏 |
| [r2-chat-mobile](evidence/r2-chat-mobile.jpg) | 390 × 844 对话 |
| [r2-feed-mobile](evidence/r2-feed-mobile.jpg) | 390 × 844 信息流 |

旧 01—05 是前轮捕获的 Muse 本地参考与当前产品结构证据；旧 06—14 为被本轮取代的暖色视觉和新形象提案，不能当作 r2 当前界面。

## 范围与限制

- 未运行整套生产 Web test / typecheck / build、Python Ruff / Pyright / pytest：此次未修改生产代码、schema、配置或依赖。相关播放器测试已单独运行。
- 未执行真实模型、付费调用、邮件、OAuth、日历、外发消息或定时任务。演示人物与账号均虚构，状态仅保存到本机 `lumen.v2.prototype.1`。
- 未登录真实 Muse；本地参考原型不等于生产 Muse 审计。主要技术依据为用户指定 Claude 自动化文章。
- 未做真实手机、屏幕阅读器、完整键盘遍历、所有主题 WCAG 或性能压测。深色为额外预览，本轮主验收为当前 Lumen 浅色。
- 第一轮生成形象仅保留历史；当前页面复用已有动态形象，无新角色制作。

保留 loopback 静态预览，仅服务 design/；不发布、不创建实际自动化、不提交或推送代码。
