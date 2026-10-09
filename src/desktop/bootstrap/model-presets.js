// Reviewed 2026-10-09; official sources and endpoint notes live in docs/desktop.md.
export const providers = [
  {name: 'OpenAI', api: 'responses', url: 'https://api.openai.com/v1', key: 'OPENAI_API_KEY',
    description: 'GPT‑6 系列 · Responses 接口支持 Agent 工具调用。',
    models: [['gpt-6.1-sol', 'GPT‑6.1 Sol'], ['gpt-6-astra', 'GPT‑6 Astra'], ['gpt-6-luna', 'GPT‑6 Luna']]},
  {name: 'DeepSeek', api: 'responses', url: 'https://api.deepseek.com', key: 'DEEPSEEK_API_KEY',
    description: 'DeepSeek 官方 API · 使用你的开放平台 API Key。',
    models: [['deepseek-flash', 'DeepSeek Flash'], ['deepseek-v4-pro', 'DeepSeek V4 Pro']]},
  {name: 'Qwen / 阿里云百炼', api: 'chat', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', key: 'DASHSCOPE_API_KEY',
    description: '默认中国内地接口；其他地域或业务空间专属地址请在高级设置中修改。',
    models: [['qwen3.8-max', 'Qwen 3.8 Max'], ['qwen3.8-flash', 'Qwen 3.8 Flash']]},
  {name: 'Kimi / Moonshot', api: 'chat', url: 'https://api.moonshot.cn/v1', key: 'MOONSHOT_API_KEY',
    description: 'Moonshot 开放平台 API · Coding 订阅请使用其专属地址与凭据。',
    models: [['kimi-k3', 'Kimi K3']]},
  {name: 'GLM / 智谱', api: 'chat', url: 'https://open.bigmodel.cn/api/paas/v4', key: 'ZHIPU_API_KEY',
    description: '智谱开放平台 API · Coding Plan 请在高级设置中填写专属地址。',
    models: [['glm-5.3', 'GLM 5.3']]},
];
