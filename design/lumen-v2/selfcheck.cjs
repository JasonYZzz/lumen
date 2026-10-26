// Run from any directory: node /absolute/path/to/design/lumen-v2/selfcheck.cjs
// Uses the repository's installed jsdom; no browser, network, or real Lumen data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('../../src/web/node_modules/jsdom');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
const surfaces = fs.readFileSync(path.join(__dirname, 'surfaces.js'), 'utf8');
let checks = 0;
function create(saved) {
  const dom = new JSDOM(html, { url: 'http://localhost/prototype/', runScripts: 'outside-only' });
  const w = dom.window;
  w.scrollTo = () => {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  if (saved) w.localStorage.setItem('lumen.v2.prototype.1', saved);
  // Separate browser scripts share global lexical bindings; evaluate them together in jsdom.
  w.eval(surfaces + '\n' + script);
  const q = s => w.document.querySelector(s);
  return { dom, w, q,
    click(action, id) { const el = q(`[data-action="${action}"]${id ? `[data-id="${id}"]` : ''}`); assert.ok(el, action); el.click(); },
    go(p) { w.location.hash = p; w.dispatchEvent(new w.Event('hashchange')); },
    submit(id) { q('#' + id).dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); },
    state() { return JSON.parse(w.localStorage.getItem('lumen.v2.prototype.1')); },
  };
}
function check(name, fn) { const a = create(); try { fn(a); checks++; console.log('PASS', name); } finally { a.dom.window.close(); } }
check('Muse-style pages render without a standalone memory or mascot entry', a => {
  for (const p of ['chat','feed','ideas','goals','library','automations','preferences','connections']) { a.go(p); assert.ok(a.q('main h1')); }
  assert.equal(a.q('nav a[href="#memory"]'),null);assert.equal(a.q('nav a[href="#preferences"]'),null);
  assert.equal(a.q('nav a[href="#identity"]'),null);assert.ok(a.q('nav a[href="#feed"]'));assert.ok(a.q('nav a[href="#ideas"]'));
});
check('cancel never submits a new automation', a => {
  a.go('automations');a.click('create'); a.click('close'); assert.equal(a.q('tbody').children.length, 3);
});
check('chat intent survives into an editable delegation draft', a => {
  a.q('#prompt').value = '每周六帮我核查发票'; a.submit('chat-form'); a.click('create-from-chat');
  assert.equal(a.q('[name="intent"]').value, '每周六帮我核查发票');
  assert.equal(a.q('[name="name"]').value, '每周六帮我核查发票');
});
check('the Feed follows active tasks and all source coverage', a => {
  a.go('automations'); a.click('toggle-task', 'travel'); a.go('feed');
  assert.doesNotMatch(a.q('.feed-aside').textContent, /留意十一月东京的机票/);
  a.go('automations'); a.click('toggle-task', 'travel');
  a.go('connections'); a.click('connection', 'mail'); a.click('confirm-connection', 'mail');
  a.go('automations'); a.click('task', 'travel');
  assert.match(a.q('#dialog').textContent, /邮箱 · 不可用/);
  a.click('close'); a.go('feed');
  const travel = [...a.w.document.querySelectorAll('.task-line')].find(row => row.textContent.includes('留意十一月东京的机票'));
  assert.match(travel.textContent, /来源不可用/);
});
check('reconnecting a source does not rewrite historical coverage', a => {
  a.go('connections'); a.click('connection', 'calendar'); a.click('confirm-connection', 'calendar');
  a.go('feed'); assert.match(a.q('.feed-stream').textContent, /日历未读取/);
  a.click('artifact', 'brief'); assert.match(a.q('#dialog').textContent, /不会追改历史覆盖范围/);
});
check('a draft needs a successful trial before activation', a => {
  a.go('automations');a.click('create'); a.submit('task-form'); const t = a.state().tasks.at(-1);
  assert.equal(t.status, 'draft'); a.click('toggle-task', t.id); assert.equal(a.state().tasks.at(-1).status, 'draft');
  a.submit('run-form'); a.click('toggle-task', t.id); assert.equal(a.state().tasks.at(-1).status, 'active');
  a.click('toggle-task', t.id); assert.equal(a.state().tasks.at(-1).status, 'paused');
});
check('missing or disconnected sources block creation', a => {
  a.go('automations');a.click('create'); a.q('form [value="mail"]').checked = false; a.q('form [value="github"]').checked = false;
  a.submit('task-form'); assert.match(a.q('#task-error').textContent, /至少选择/);
  a.q('form [value="calendar"]').checked = true; a.submit('task-form'); assert.match(a.q('#task-error').textContent, /尚未连接/);
});
check('unknown delivery pauses, reconciles, and never repeats a send', a => {
  a.go('automations'); a.click('task', 'travel'); a.q('[name="scenario"]').value = 'unknown'; a.submit('run-form');
  assert.equal(a.state().tasks.find(t => t.id === 'travel').status, 'paused'); assert.equal(a.state().delivery, 'unknown');
  a.click('reconcile', 'travel'); assert.equal(a.state().delivery, 'confirmed'); assert.match(a.state().activities[0].detail, /没有重新发送/);
});
check('budget stops and source failure stays visibly incomplete', a => {
  a.go('automations'); a.click('task', 'travel'); a.q('[name="scenario"]').value = 'budget'; a.submit('run-form');
  assert.equal(a.state().tasks.find(t => t.id === 'travel').status, 'paused'); assert.match(a.q('#dialog').textContent, /预算已用尽/);
  a.q('[name="scenario"]').value = 'partial'; a.submit('run-form'); assert.match(a.q('#dialog').textContent, /部分来源不可用/);
});
check('approval and rejection are explicit one-time decisions', a => {
  a.go('feed');a.click('artifact','mail'); a.click('approve'); assert.equal(a.state().approval, 'approved'); assert.equal(a.state().activities.length, 1);
  a.go('library'); a.click('artifact', 'mail'); assert.equal(a.q('[data-action="approve"]'), null);
});
check('reject keeps draft and does not send', a => { a.go('feed');a.click('artifact','mail');a.click('reject'); assert.equal(a.state().approval, 'rejected'); assert.match(a.state().activities[0].detail, /没有发送/); });
check('revoking a source invalidates its prior trial', a => {
  a.go('connections'); a.click('connection', 'mail'); a.click('confirm-connection', 'mail');
  assert.equal(a.state().mail, false); assert.equal(a.state().tasks.find(t => t.id === 'travel').tested, false);
});
check('memory content is escaped, editable and forgettable', a => {
  a.click('settings');a.click('go-preferences');a.w.dispatchEvent(new a.w.Event('hashchange')); a.click('edit-memory', 'm3'); a.q('[name="content"]').value = '<img src=x onerror=alert(1)> test'; a.submit('memory-form');
  assert.equal(a.q('main img[onerror]'), null); assert.equal(a.state().memories.find(m => m.id === 'm3').kind, 'explicit');
  a.click('edit-memory', 'm3'); a.click('forget-memory', 'm3'); assert.equal(a.state().memories.length, 2);
});
check('artifact edits persist and filters have empty states', a => {
  a.go('library');a.click('artifact', 'brief'); a.click('edit-artifact'); a.q('[name="title"]').value = 'Edited brief'; a.submit('artifact-form');
  assert.equal(a.state().savedTitle, 'Edited brief'); a.click('close'); a.go('library'); a.q('#library-search').value = 'no-such-result';
  a.q('#library-search').dispatchEvent(new a.w.Event('input', { bubbles: true })); assert.match(a.q('main').textContent, /没有找到/);
});
check('only this prototype namespace is reset', a => {
  a.w.localStorage.setItem('other-app', 'keep'); a.click('reset'); a.click('confirm-reset'); assert.equal(a.w.localStorage.getItem('other-app'), 'keep');
});
check('Feed bookmarks appear in the Library and can be removed', a => {
  a.go('feed');a.click('bookmark','reading');assert.ok(a.state().savedFeed.includes('reading'));
  a.go('library');assert.match(a.q('main').textContent,/可靠自动化 · 设计笔记/);
  a.click('artifact','reading');assert.match(a.q('#dialog').textContent,/主要参考 Claude/);a.click('close');
  a.click('bookmark','reading');assert.doesNotMatch(a.q('main').textContent,/可靠自动化 · 设计笔记/);
});
check('Feed preferences filter examples but keep pending approvals', a => {
  a.go('feed');a.click('feed-settings');a.q('[name="topics"][value="work"]').checked=false;a.q('[name="prompt"]').value='关注东京旅行';a.submit('feed-settings-form');
  assert.deepEqual(a.state().feedTopics,['travel']);assert.doesNotMatch(a.q('.feed-stream').textContent,/今日工作简报/);assert.match(a.q('.feed-stream').textContent,/给合作伙伴的回复/);
});
check('Ideas can be dismissed, restored and discussed without enabling a task', a => {
  a.go('ideas');a.click('dismiss-idea','weekly');assert.doesNotMatch(a.q('main').textContent,/周五下班前，收到一份项目回顾/);
  a.click('restore-ideas');assert.match(a.q('main').textContent,/周五下班前，收到一份项目回顾/);
  a.click('start-idea','weekly');a.w.dispatchEvent(new a.w.Event('hashchange'));assert.equal(a.state().tasks.length,3);assert.match(a.q('main').textContent,/每周五 17:00/);assert.equal(a.state().startedIdeas.length,1);
});
check('reopening an Idea always restores its intent after New Chat or another Idea', a => {
  a.go('ideas');a.click('start-idea','weekly');a.w.dispatchEvent(new a.w.Event('hashchange'));
  a.click('new-chat');a.go('ideas');a.click('start-idea','weekly');a.w.dispatchEvent(new a.w.Event('hashchange'));
  assert.match(a.q('main').textContent,/每周五 17:00/);
  a.go('ideas');a.click('start-idea','travel');a.w.dispatchEvent(new a.w.Event('hashchange'));
  a.go('ideas');a.click('start-idea','weekly');a.w.dispatchEvent(new a.w.Event('hashchange'));a.click('create-from-chat');
  assert.match(a.q('[name="intent"]').value,/每周五 17:00/);
});
check('selecting an Idea from Search closes Search', a => {
  a.click('search');a.q('#global-search').value='技术文章';a.q('#global-search').dispatchEvent(new a.w.Event('input',{bubbles:true}));
  a.click('start-idea','reading');a.w.dispatchEvent(new a.w.Event('hashchange'));assert.equal(a.q('#dialog').open,false);assert.match(a.q('main').textContent,/Claude 自动化文章/);
});
check('bookmark and resource filters preserve keyboard focus', a => {
  a.go('feed');a.q('[data-action="bookmark"][data-id="brief"]').focus();a.click('bookmark','brief');assert.equal(a.w.document.activeElement.dataset.action,'bookmark');assert.equal(a.w.document.activeElement.dataset.id,'brief');
  a.go('library');a.q('#library-type').focus();a.q('#library-type').value='plan';a.q('#library-type').dispatchEvent(new a.w.Event('change',{bubbles:true}));assert.equal(a.w.document.activeElement.id,'library-type');
});
for (const saved of ['{bad json', JSON.stringify({version:1,tasks:[{name:'bad'}]})]) {
  const a = create(saved); assert.ok(a.q('main h1')); a.dom.window.close(); checks++;
}
console.log(`${checks} checks passed`);
