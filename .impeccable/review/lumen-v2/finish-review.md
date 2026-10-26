disposition: fix

输入范围：本次是 code-led 的现有产品扩展，用户未选择新的 world、seed 或 QUALITY BAR card；以两份 DESIGN.md 和 PRODUCT.md 为方向权威。未提供独立五块 direction contract；r2 三张参考图作为 critique reference，不作像素复刻规范。未读取未提供的其他实现、测试或真实 Provider；未重新运行 detector、浏览器或构建。

## persistence

pass — src/web/PRODUCT.md、src/web/DESIGN.md 和 design/lumen-v2/DESIGN.md 存在，已记录五个主导航、辅助自动化、设置内偏好、原有白灰黑/Geist/九尾狐与真实 Host 数据边界。无需为现有产品扩展补做 comp-led state/spec/diff 或新 seed。全部七张指定截图有效：1280×720 桌面信息流/对话/详情/编辑，390×844 手机信息流/编辑顶部/编辑末尾；最后一张明确为滚动末尾，不误作首屏证据。

## fidelity

faithful — TYPE 保持产品 Geist 与中文回退、27/25px 页面标题和 20px 卡片标题；MATERIAL 保持细线、平面中性阅读表面与原有真实九尾狐画面，没有伪造材质；GROUND 保持 #fdfdfc 页面和浅灰侧栏，与既有产品及参考图的中性观感一致。五主导航和辅助自动化保持可辨，偏好进入设置，无独立记忆按钮。信息流的正文/辅助栏与手机单列、对话欢迎/原狐/宽输入框均保留参考的主要阅读关系。验收示例和 FunctionModel 的限制已明确；原型中的邮件、旅行、成功连接内容没有被包装为生产事实。模态表单在手机可滚动至保存按钮，无横向裁切。可见错误恢复存在下列两处缺口。

## ceiling

reached — 现有产品的克制中性色、局部边界、留白和原狐作为陪伴的设备均已保持；不用新增装饰、视觉世界或吉祥物抬高表面。提供的 detector 结果为 []，未重复运行。截图及代码未见需单独整改的 floor 禁项。静态截图不能证明完整动态、焦点或真实设备可访问性验收；父线程已报告手机导航焦点返回的实际检查。

## material_fixes

1. [Floor / 错误与恢复] src/web/src/components/assistant-workspace.tsx:283–292、950–999：执行详情 Panel 中“查看结果”读取失败或“停止本次执行”失败时，仅将 error 写到底层页面；模态内未渲染该错误。把失败信息与可重试动作呈现在当前详情 Panel 内，使用户无需关闭窗口即可知道结果未读取或停止未确认；用失败响应验证可见反馈。
2. [Floor / 错误与加载状态] src/web/src/components/personal-preferences.tsx:19–25、53–62：首次读取失败后仍显示“正在读取…”，点击“重新读取”成功不清除旧错误，重试失败也被吞掉。让成功清错、失败更新错误、请求结束后停止加载提示；用先失败后成功以及连续失败验证恢复路径。

## keep

保留原有白灰黑、Geist、动态九尾狐、五主导航和辅助自动化，以及显式试跑/启用、来源覆盖、历史结果与验收示例的事实边界。
