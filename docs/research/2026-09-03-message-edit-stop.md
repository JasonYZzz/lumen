# Web 消息编辑、停止与模型文本标签

日期：2026-09-03。范围：用户提供的 ChatGPT 停止按钮、消息复制/编辑截图，以及 Lumen `</thinking>` 泄漏截图。

## 设计依据

| 观察 | 根因与设计决策 |
| --- | --- |
| 运行中同时显示红色停止和禁用发送 | 停止是当前主要动作。空草稿时以圆形方块占据发送位置；有补充内容时显示停止、队列发送及输入方式 |
| 点击停止缺少请求反馈 | 请求处理中禁用重复提交，显示正在停止；网络失败保留运行与重试入口，不能把取消请求失败等同于模型运行失败 |
| 普通正文里出现 `</thinking>` | 部分模型把标签放在 TextPart 中，未产生原生 ThinkingPart。Web 在分组前扫描文本，识别完整/分段标签和孤立关闭标签；原始事件、provider 消息和撤回字符偏移不变 |
| 用户气泡只有正文 | 气泡下增加复制/编辑动作。编辑在原处展开，取消恢复焦点，提交时锁定，失败保留草稿；窄屏自动滚动使整个编辑区可见 |
| 编辑会影响后续模型上下文 | 复用 SessionRepository.fork，在目标 turn 之前创建分支，再通过既有 StartRun / InvokeSkill / InvokePrompt 执行；前文保留，目标消息及其后续不进入新分支的对话上下文 |

过程和答案使用同一 Web 投影，流式接收、历史恢复、复制回答和 `/copy` 一致。标签仅在非代码文本中解释，代码块、行内代码和转义示例保留。扫描逐字符推进，不对每个字符重新扫描整段前缀。

## 编辑契约

1. Timeline 的 user 行投影持久 turn_index、interaction_id 和附件引用；不能用显示序号或相同文本猜目标。刚结束的流式行通过 interaction_id 对应到持久记录。
2. `ForkSessionAtTurn.include_turn` 默认 true，现有检查点分支行为保留。Web 编辑传 false，第一条消息也可生成空前文分支。
3. fork 和 start 使用各自的 client request ID。相同编辑重试复用分支与启动请求，失败时原页及编辑草稿保留。Host 在进程生命周期内缓存 fork 请求，参数冲突拒绝。此幂等性不声称跨服务重启持久。
4. 新分支以新问题命名；原对话保留在任务列表，本次编辑后有返回入口。活动运行期间禁止修改历史输入，先停止或等待结束。页面主输入框草稿不因重新生成而清空。
5. 保留原审批模式及 collaboration mode；替换消息清除旧方案的 review/execution 授权。已发生的文件、外部副作用以及既有 work-state 恢复信息继续保留，编辑不回滚工作区。
6. 普通消息保留附件引用。Skill/Prompt 继续由对应 Host Interface 展开；将含图片消息改为暂不支持图片的 Skill/Prompt 命令时明确报错，避免静默丢失附件。
7. 恢复活动 Session 后重放同一 run 的首事件，先替换该 run 的恢复投影，再接收流，不重复追加用户消息。后续排队输入不会被误当成历史重放而删除。

Session schema 仍为 v9；没有历史迁移、原地覆写或第二套运行/分支调度。TUI 与 headless 使用同一 Host 和 Repository；本轮消息气泡及标签投影的新增交互范围为 Web。

## 验收

验收图与逐项记录见根目录 `design-qa.md` 的“消息交互第二轮”。生产前端连接独立本地 Host，使用确定性的 FunctionModel，未调用外部模型或操作用户工作区。

最小 Python 回归 86 项通过；全量 915 项中沙箱内 912 项通过，3 项因端口绑定 / 嵌套 sandbox-exec 受限，经授权在沙箱外重跑通过。60 张 TUI snapshot 通过。Web 91 项通过，TypeScript、Next build、Ruff、Pyright、OpenAPI/契约生成及 Architecture Atlas 同步通过。
