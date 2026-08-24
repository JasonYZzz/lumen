import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { Composer } from './composer'

(globalThis as typeof globalThis & { React: typeof React }).React = React

describe('Composer image capability', () => {
  it('disables image selection when the active model is text-only', () => {
    const markup = renderToStaticMarkup(
      <Composer
        value=""
        busy={false}
        queueMode="steer"
        slashCommands={[]}
        attachments={[]}
        imageInputEnabled={false}
        onChange={vi.fn()}
        onQueueModeChange={vi.fn()}
        onAttachmentsChange={vi.fn()}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    expect(markup).toMatch(/<button[^>]*disabled=""[^>]*aria-label="添加图片"/)
    expect(markup).toContain('title="当前模型不支持图片输入"')
  })
})
