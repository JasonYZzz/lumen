import Anthropic from '@lobehub/icons/es/Anthropic/components/Mono'
import DeepSeek from '@lobehub/icons/es/DeepSeek/components/Mono'
import Gemini from '@lobehub/icons/es/Gemini/components/Mono'
import Kimi from '@lobehub/icons/es/Kimi/components/Mono'
import OpenAI from '@lobehub/icons/es/OpenAI/components/Mono'
import Qwen from '@lobehub/icons/es/Qwen/components/Mono'
import Zhipu from '@lobehub/icons/es/Zhipu/components/Mono'
import { Robot } from '@phosphor-icons/react'

/** Display-only brand matching; provider capabilities remain owned by the Host. */
export function ModelIcon({ model }: { model: string }) {
  const name = model.toLowerCase()
  const Icon = /deepseek/.test(name) ? DeepSeek
    : /qwen/.test(name) ? Qwen
      : /kimi|moonshot/.test(name) ? Kimi
        : /glm|zhipu/.test(name) ? Zhipu
          : /claude|anthropic/.test(name) ? Anthropic
            : /gemini|google/.test(name) ? Gemini
              : /gpt|openai|^o[134](?:-|$)/.test(name) ? OpenAI : null
  return Icon ? <Icon size={18} aria-hidden="true" /> : <Robot size={18} aria-hidden="true" />
}
