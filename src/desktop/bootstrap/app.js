import { providers } from './model-presets.js';

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const root = document.querySelector('main');
let currentRequest = null;

function element(tag, text, attributes = {}) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}
function error(message) {
  let node = document.querySelector('#error');
  if (!node) { node = element('p', '', {id: 'error', class: 'error', role: 'alert'}); root.append(node); }
  node.textContent = String(message);
}
function frame(title, description) {
  root.replaceChildren(
    element('h1', title), element('p', description));
}
function button(text, action, primary = false) {
  const node = element('button', text, {type: 'button', ...(primary ? {class: 'primary'} : {})});
  node.addEventListener('click', async () => {
    node.disabled = true;
    try { await action(); } catch (e) { error(e); } finally { node.disabled = false; }
  });
  return node;
}
function actions(...buttons) { const row = element('div', undefined, {class: 'actions'}); row.append(...buttons); root.append(row); }
function field(form, title, name, type = 'text', value = '') {
  const label = element('label', title, {for: `field-${name}`});
  const input = element('input', undefined, {id: `field-${name}`, name, type, autocomplete: 'off'});
  input.value = value; input.required = true;
  if (type === 'password') input.setAttribute('spellcheck', 'false');
  form.append(label, input); return input;
}
function details(lines) {
  const node = element('div', undefined, {class: 'detail'});
  lines.forEach(line => node.append(element('div', line))); root.append(node);
}
function busy() {
  frame('正在打开项目', '正在检查配置和连接项目资源。这一步可能需要一些时间。');
  root.prepend(element('div', undefined, {class: 'busy', role: 'status', 'aria-label': '正在启动'}));
  actions(button('取消启动', () => invoke('cancel_startup')));
}
async function respond(value, remember = false) {
  const id = currentRequest?.request_id;
  await invoke('reply', {requestId: id, value, remember});
  // Only replace the request being answered; the next request may already have arrived.
  if (currentRequest?.request_id === id) { currentRequest = null; busy(); }
}
function request(message) {
  currentRequest = message;
  if (message.type === 'trust_required') {
    frame('信任这个项目？', '项目配置和技能可能包含命令、插件与外部连接。仅在了解项目来源时继续。');
    details([message.workspace, ...message.sources, ...(message.project_skills ? ['项目包含 .lumen/skills'] : [])]);
    actions(button('信任并打开', () => respond({decision: 'trust'}), true), button('取消', () => invoke('cancel_startup')));
  } else if (message.type === 'mcp_required') {
    frame('允许连接项目 MCP？', '此连接来自项目配置。授权仅作用于当前定义；工具调用仍遵循 Lumen 的审批规则。');
    details([message.name, message.transport, message.source, `定义指纹 ${message.fingerprint}`]);
    actions(button('仅本次允许', () => respond({decision: 'once'}), true),
      button('始终允许', () => respond({decision: 'always'})), button('拒绝连接', () => respond({decision: 'deny'})));
    actions(button('取消启动', () => invoke('cancel_startup')));
  } else if (message.type === 'credentials_required') {
    frame('连接你的模型', '输入自己的 API Key。连接将在发送第一条消息时进行；模型服务可能按用量收费。');
    const form = element('form', undefined, {id: 'credential-form'});
    for (const name of message.names) {
      const provider = providers.find(provider => provider.key === name);
      const input = field(form, provider ? `${provider.name} API Key` : name, name, 'password');
      input.setAttribute('aria-describedby', `help-${name}`);
      form.append(element('p', `凭据变量：${name}`, {id: `help-${name}`, class: 'hint'}));
    }
    const label = element('label', undefined, {class: 'check'});
    const remember = element('input', undefined, {type: 'checkbox', id: 'remember'});
    label.append(remember, document.createTextNode('保存到 macOS 钥匙串')); form.append(label);
    root.append(form);
    const submit = element('button', '继续打开项目', {type: 'submit', form: 'credential-form', class: 'primary'});
    form.addEventListener('submit', async event => {
      event.preventDefault(); submit.disabled = true;
      const values = Object.fromEntries(message.names.map(name => [name, form.elements.namedItem(name).value]));
      try { await respond({values}, remember.checked); form.reset(); }
      catch (e) { error(e); } finally { submit.disabled = false; }
    });
    actions(submit, button('取消', () => { form.reset(); return invoke('cancel_startup'); }));
    const forget = button('删除这些已保存的凭据', async () => {
      for (const name of message.names) await invoke('forget_credential', {name});
      forget.textContent = '已从钥匙串删除';
    }); root.append(element('p', '不勾选时仅供本次项目使用；凭据不会写入项目配置或日志。', {class: 'footnote'}), forget);
  } else if (message.type === 'configuration_required') {
    frame('选择你的第一个模型', '先选服务商和模型，下一步再填写 API Key。之后可在应用内添加或切换模型。');
    const form = element('form', undefined, {id: 'configuration-form'});
    const provider = element('select', undefined, {id: 'provider', 'aria-describedby': 'provider-help'});
    form.append(element('label', '模型服务商', {for: 'provider'}), provider);
    providers.forEach((item, index) => provider.append(element('option', item.name, {value: index})));
    provider.append(element('option', '自定义 / 本地模型', {value: 'custom'}));
    const model = element('select', undefined, {id: 'model'});
    const modelLabel = element('label', '模型', {for: 'model'});
    const help = element('p', '', {id: 'provider-help', class: 'hint', 'aria-live': 'polite'});
    form.append(modelLabel, model, help);
    const advanced = element('details', undefined, {class: 'advanced'});
    advanced.append(element('summary', '高级设置 · 自定义地址与模型 ID'));
    const id = field(advanced, '模型 ID', 'model_id', 'text');
    const label = element('label', '接口', {for: 'api'});
    const api = element('select', undefined, {id: 'api', name: 'api'});
    for (const [value, text] of [['responses', 'Responses'], ['chat', 'Chat Completions']]) {
      api.append(element('option', text, {value}));
    } advanced.append(label, api);
    const url = field(advanced, '服务地址（可选）', 'base_url'); url.required = false;
    const key = field(advanced, '凭据变量名（本地模型可留空）', 'api_key_env'); key.required = false;
    form.append(advanced);
    function selectProvider() {
      const selected = providers[Number(provider.value)];
      model.hidden = modelLabel.hidden = !selected;
      advanced.open = !selected;
      model.replaceChildren();
      if (selected) {
        for (const [value, label] of selected.models) model.append(element('option', label, {value}));
        id.value = `openai:${model.value}`; api.value = selected.api;
        url.value = selected.url; key.value = selected.key;
        help.textContent = selected.description;
      } else {
        id.value = ''; api.value = 'chat'; url.value = ''; key.value = '';
        help.textContent = '填写完整模型 ID，例如 ollama:qwen3.8。OpenAI 兼容服务使用 openai: 前缀。';
      }
    }
    provider.addEventListener('change', selectProvider);
    model.addEventListener('change', () => { id.value = `openai:${model.value}`; });
    id.addEventListener('invalid', () => { advanced.open = true; });
    selectProvider();
    root.append(form);
    const submit = element('button', '下一步：填写 API Key', {type: 'submit', form: 'configuration-form', class: 'primary'});
    provider.addEventListener('change', () => { submit.textContent = provider.value === 'custom' ? '保存配置并继续' : '下一步：填写 API Key'; });
    form.addEventListener('submit', async event => {
      event.preventDefault(); submit.disabled = true;
      const data = new FormData(form);
      try { await respond({model_id: data.get('model_id').trim(), api: data.get('api'),
        base_url: data.get('base_url').trim() || null, api_key_env: data.get('api_key_env').trim() || null}); }
      catch (e) { error(e); } finally { submit.disabled = false; }
    });
    actions(submit, button('取消', () => invoke('cancel_startup')));
    root.append(element('p', '仅保存所选模型到 ~/.lumen/agent.yaml，不包含 API Key。', {class: 'footnote'}));
  }
  root.querySelector('select, input, button')?.focus({preventScroll: true});
  window.scrollTo(0, 0);
}
async function choose(path = null) {
  if (await invoke('choose_project', {path}) && !currentRequest) busy();
}
async function start() {
  const state = await invoke('bootstrap');
  if (state.pending) { request(state.pending); return; }
  if (state.running) { busy(); return; }
  frame('从一个项目开始', '打开工作文件夹，让 Lumen 帮你理解项目、完成任务。');
  actions(button('选择项目文件夹', () => choose(), true));
  if (state.error) error(state.error);
  if (state.recent.length) {
    const recent = element('section', undefined, {class: 'recent', 'aria-label': '最近项目'});
    recent.append(element('h2', '最近项目'));
    for (const path of state.recent) {
      const node = button('', () => choose(path)); node.className = 'project';
      node.append(element('strong', path.split('/').filter(Boolean).at(-1) || path), element('span', path)); recent.append(node);
    } root.append(recent);
  }
  root.append(element('p', '首次使用会引导你选择模型并填写 API Key。已有 Lumen 配置与项目记录可直接沿用。', {class: 'footnote'}));
  const settings = element('details', undefined, {class: 'recent'});
  settings.append(element('summary', '管理已保存的凭据'));
  const form = element('form');
  const name = field(form, '凭据变量名', 'saved_credential', 'text', 'OPENAI_API_KEY');
  const remove = element('button', '从钥匙串删除', {type: 'submit'});
  form.append(element('p', '删除后重新打开项目可输入新的凭据。', {class: 'footnote'}), remove);
  form.addEventListener('submit', async event => {
    event.preventDefault(); remove.disabled = true;
    try { await invoke('forget_credential', {name: name.value.trim()}); remove.textContent = '已删除'; }
    catch (e) { error(e); } finally { remove.disabled = false; }
  }); settings.append(form); root.append(settings);
}
const header = element('header');
header.append(element('img', undefined, {src: 'mark.svg', alt: ''}), element('strong', 'Lumen')); document.body.prepend(header);
await listen('desktop-event', event => {
  if (event.payload.type === 'starting') { currentRequest = null; busy(); }
  else if (event.payload.type === 'startup_failed') error(event.payload.message);
  else request(event.payload);
});
try { await start(); } catch (e) { frame('暂时无法打开', '重新启动应用后再试。'); error(e); }
