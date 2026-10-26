'use strict';

// Fixed editorial examples; these cards never claim to have fetched private or live sources.
const exampleUpdates = [
  {id:'brief',topic:'work',kind:'工作简报',title:'今天，有两件事值得你关注',text:'Lumen v2 的接口设计等待确认，一封合作邮件需要回复。已读邮箱和 GitHub；日历没有读到，未计入这份简报。',time:'今天 08:32',sources:'邮箱 · GitHub',action:'查看完整简报'},
  {id:'travel',topic:'travel',kind:'目标进展',title:'东京旅行的第一版路线，整理好了',text:'六天的城市漫游，留出了半天自由探索。机票检查会继续跟进，找到预算内的选择再提醒你。',time:'昨天 18:20',sources:'东京旅行目标 · 你确认的偏好',action:'打开行程'},
  {id:'reading',topic:'work',kind:'为你整理',title:'把自动化做可靠，比让它一直忙更重要',text:'来源读不到就明确说明，结果送达后再记账，有值得行动的变化再提醒。这三个检查点可以收藏到资源库，供设计评审时参考。',time:'昨天 16:40',sources:'Claude 自动化文章 · 示例摘记',action:'阅读笔记'},
];
const exampleIdeas = [
  {id:'weekly',category:'work',categoryName:'工作',icon:'GithubLogo',title:'周五下班前，收到一份项目回顾',text:'把本周的变更、未决问题和下一步整理在一起，周末不用再翻聊天记录。',reason:'结合你正在推进的 Lumen 项目 · 示例建议',intent:'每周五 17:00 帮我整理 Lumen 项目进展、待决问题和下周优先事项，附上原始来源。'},
  {id:'travel',category:'life',categoryName:'生活',icon:'CalendarBlank',title:'让东京旅行计划，跟着新发现慢慢完善',text:'从已经整理好的路线出发，把想去的地方收在一起。需要预订的时候，再由你确认。',reason:'关联目标：十一月东京旅行',intent:'帮我继续完善东京六天行程，优先直飞、留出自由探索时间；需要预订时先给我确认。'},
  {id:'reading',category:'learning',categoryName:'学习',icon:'FileText',title:'读过的技术文章，周末再消化一次',text:'把收藏的内容变成一页笔记，留下值得实践的想法。先从 Claude 的自动化文章开始。',reason:'参考 Claude 自动化文章 · 示例建议',intent:'帮我梳理 Claude 自动化文章的关键实践，并对照 Lumen 项目列出可以尝试的改进。'},
];
function resourceRows() {
  return [
    {id:'brief',title:state.savedTitle,type:'document',typeName:'文档',sub:'每天早上的工作简报',date:'今天 08:32',preview:'需要你确认的设计决策、待回复邮件，以及本次来源覆盖情况。'},
    {id:'travel',title:'东京秋日旅行 · 第一版行程',type:'plan',typeName:'行程',sub:'东京旅行目标',date:'昨天 18:20',preview:'六天城市漫游：浅草、上野、表参道与镰仓。航班和住宿尚未预订。'},
    {id:'mail',title:'Lumen v2 设计评审 · 回复草稿',type:'draft',typeName:'草稿',sub:state.approval==='pending'?'等待你确认':state.approval==='approved'?'已模拟发送':'本次不发送',date:'今天 08:31',preview:'收件人、主题和正文已拟好。发送前需要你本次确认。'},
    ...(state.savedFeed.includes('reading')?[{id:'reading',title:'可靠自动化 · 设计笔记',type:'note',typeName:'笔记',sub:'从信息流收藏',date:'昨天 16:40',preview:'读取覆盖、送达回执、偏好与运行状态的边界，以及如何少打扰用户。'}]:[]),
  ];
}
function feedCards() {
  const cards=exampleUpdates.filter(item=>state.feedTopics.includes(item.topic));
  if(state.approval==='pending') cards.splice(1,0,{id:'mail',topic:'work',kind:'需要你确认',title:'给合作伙伴的回复，已经拟好了',text:'关于 Lumen v2 的设计评审时间。请核对收件人和完整正文，确认后再发送。',time:'今天 08:31',sources:'邮箱 · 发送前逐次确认',action:'查看草稿并确认'});
  return cards.filter(item=>feedFilter==='all'||feedFilter==='attention'&&item.id==='mail'||feedFilter==='saved'&&state.savedFeed.includes(item.id));
}
const assistantViews = {
  chat() {
    const latest=state.messages.findLast(m=>m.role==='user')?.text;
    if(!state.messages.length) return `<section class="chat-welcome"><div class="welcome-entry"><div class="welcome-intro"><span></span><h1>有什么需要处理？</h1><div class="welcome-mascot" data-mascot></div></div>${composer()}<div class="prompt-suggestions">${[['brief','整理一份工作简报'],['travel','继续规划东京旅行'],['weekly','每周回顾项目进展']].map(([id,title])=>`<button data-action="quick-prompt" data-id="${id}">${title} ${icon('ArrowUpRight')}</button>`).join('')}</div><p class="welcome-note">聊一件眼前的事，也可以把反复要做的事交给 Lumen。</p></div></section>`;
    return `<section class="chat-layout"><h1 class="sr-only">与 Lumen 对话</h1>${state.messages.map(m=>`<div class="chat-message ${m.role}"><small>${m.role==='user'?'你':'Lumen · 模拟回复'}</small><p>${esc(m.text)}</p></div>`).join('')}<article class="chat-callout"><strong>把这件事，变成持续的委托</strong><p>${esc(latest||'每个工作日早上 8:30，整理需要我决策的事。')}</p><div class="actions">${button('设置为自动化','create-from-chat','','')}${button('查看信息流','go-feed')}</div></article>${composer()}<p class="hint">对话内容为本机模拟；持续执行前会确认来源、时间和权限。</p></section>`;
  },
  feed() {
    const active=state.tasks.filter(t=>t.status==='active');
    return `<section>${heading('信息流','你关注的事，有了新的进展。',button(icon('SlidersHorizontal')+' 定制信息流','feed-settings'))}<div class="feed-layout"><div><div class="tabs" role="group" aria-label="信息流筛选">${[['all','为你整理'],['attention','需要你确认'],['saved','已收藏']].map(([value,text])=>`<button aria-pressed="${feedFilter===value}" data-action="feed-filter" data-id="${value}">${text}${value==='attention'&&state.approval==='pending'?' 1':''}</button>`).join('')}</div>
      <div class="feed-stream">${feedCards().map(item=>`<article class="feed-card"><div class="feed-card-meta"><span class="feed-avatar">${icon(item.id==='mail'?'EnvelopeSimple':item.id==='travel'?'Target':'FileText')}</span><span><b>${item.kind}</b><small>${item.time} · 示例内容</small></span><button class="icon-button bookmark ${state.savedFeed.includes(item.id)?'is-saved':''}" data-action="bookmark" data-id="${item.id}" aria-label="${state.savedFeed.includes(item.id)?'取消收藏':'收藏'}${esc(item.title)}" aria-pressed="${state.savedFeed.includes(item.id)}">${icon('BookmarkSimple')}</button></div><h2>${item.id==='brief'?esc(state.savedTitle):item.title}</h2><p>${item.id==='brief'?esc(state.savedBody):item.text}</p>${item.id==='brief'?'<div class="source-warning">'+icon('WarningCircle')+' 生成时日历未读取，历史结果仍保留该说明。</div>':''}<div class="feed-source">${icon('Link')} ${item.sources}</div><div class="feed-card-footer"><button class="text-button strong" data-action="artifact" data-id="${item.id}">${item.action} ${icon('ArrowUpRight')}</button><button class="text-button" data-action="discuss-update" data-id="${item.id}">${icon('ChatCircle')} 继续聊聊</button></div></article>`).join('')||'<div class="empty"><h2>这里暂时没有更新</h2><p>可以切换筛选、收藏一条内容，或调整你关注的主题。</p></div>'}</div></div>
      <aside class="feed-aside"><h2>正在为你跟进</h2>${active.map(t=>`<div class="task-line"><div class="task-copy"><button class="task-title" data-action="task" data-id="${esc(t.id)}">${esc(t.name)}</button><small>${esc(t.schedule)} · ${esc(t.zone)}</small><span class="task-status">${t.sources.some(s=>!state[s])?icon('WarningCircle')+' 来源不可用':'等待下次触发'}</span></div></div>`).join('')||'<p class="hint">目前没有启用的自动化。</p>'}<a class="text-button aside-link" href="#automations">管理自动化 ${icon('ArrowRight')}</a><div class="aside-section"><h2>你正在靠近的目标</h2><button class="aside-goal" data-action="go-goals">东京秋日旅行<span>${state.milestones.filter(Boolean).length} / 4 个里程碑 ${icon('CaretRight')}</span></button></div><div class="quiet-note">${icon('Bell')} ${state.quiet?'有重要变化，再轻轻提醒你。':'每次完成后提醒。'}<br>免打扰：22:00—08:00（示例）</div><p class="hint">当前为本机演示，没有抓取真实邮件或动态。<br><button class="text-button" data-action="refresh-feed">${icon('ArrowClockwise')} 重新整理示例</button></p></aside></div></section>`;
  },
  ideas() {
    const visible=exampleIdeas.filter(item=>!state.dismissedIdeas.includes(item.id)&&(ideaFilter==='all'||item.category===ideaFilter));
    return `<section>${heading('灵感','从你在意的事情里，找到下一步。')}<div class="tabs" role="group" aria-label="灵感分类">${[['all','为你推荐'],['work','工作'],['life','生活'],['learning','学习']].map(([id,text])=>`<button aria-pressed="${ideaFilter===id}" data-action="idea-filter" data-id="${id}">${text}</button>`).join('')}</div><div class="ideas-grid">${visible.map(item=>`<article class="idea-card"><div class="idea-type">${icon(item.icon)} ${item.categoryName}</div><h2>${item.title}</h2><p>${item.text}</p><small>${item.reason}</small><div class="actions">${button(state.startedIdeas.includes(item.id)?'继续讨论':'开始这件事','start-idea',item.id,'')}${button('不感兴趣','dismiss-idea',item.id)}</div></article>`).join('')||'<div class="empty"><h2>这一类建议暂时看完了</h2><p>换个分类，或恢复你隐藏的示例建议。</p>'+button('恢复隐藏的建议','restore-ideas')+'</div>'}</div><div class="idea-note">${icon('Lightbulb')} 灵感是建议。点击开始后先进入对话，自动化需要你另外确认。${state.dismissedIdeas.length?'<button class="text-button" data-action="restore-ideas">恢复隐藏的建议</button>':''}</div></section>`;
  },
  library() {
    const rows=resourceRows().filter(row=>(libraryType==='all'||libraryType==='saved'&&state.savedFeed.includes(row.id)||row.type===libraryType)&&row.title.toLowerCase().includes(libraryQuery.toLowerCase()));
    return `<section>${heading('资源库','做好的文档、计划和收藏，随时找得到。')}<div class="library-filter"><label class="sr-only" for="library-search">搜索资源</label><div class="resource-search">${icon('MagnifyingGlass')}<input id="library-search" class="field-input" value="${esc(libraryQuery)}" placeholder="搜索资源"></div><label class="sr-only" for="library-type">资源类型</label><select id="library-type" class="field-input">${[['all','全部资源'],['saved','已收藏'],['document','文档'],['plan','行程'],['draft','草稿'],['note','笔记']].map(([id,text])=>`<option value="${id}" ${libraryType===id?'selected':''}>${text}</option>`).join('')}</select></div><div class="library-grid">${rows.map(row=>`<article class="library-card"><button class="resource-open" data-action="artifact" data-id="${row.id}"><div class="resource-meta">${icon(row.type==='plan'?'CalendarBlank':'FileText')}<span>${row.typeName}</span></div><h2>${esc(row.title)}</h2><p>${row.preview}</p></button><div class="resource-footer"><small>${row.date} · ${row.sub}</small><button class="icon-button bookmark ${state.savedFeed.includes(row.id)?'is-saved':''}" data-action="bookmark" data-id="${row.id}" aria-label="${state.savedFeed.includes(row.id)?'取消收藏':'收藏'}${esc(row.title)}" aria-pressed="${state.savedFeed.includes(row.id)}">${icon('BookmarkSimple')}</button></div></article>`).join('')||'<div class="empty"><h2>没有找到对应资源</h2><p>试试其他关键词或资源类型，也可以从信息流收藏一份笔记。</p></div>'}</div></section>`;
  },
};
