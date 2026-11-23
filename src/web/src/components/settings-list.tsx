import { CaretDown, CaretUp } from '@phosphor-icons/react'
import { useId, useState, type ReactNode } from 'react'

const PAGE_SIZE = 5

export function SettingsList<T>({ title, items, empty, action, beforeList, children }: {
  title: string
  items: readonly T[]
  empty: string
  action?: ReactNode
  beforeList?: ReactNode
  children: (item: T) => ReactNode
}) {
  const [limit, setLimit] = useState(PAGE_SIZE)
  const id = useId()
  const visible = Math.min(limit, items.length)
  return <section className="extension-list" aria-labelledby={id}>
    <header className="extension-list-heading">
      <h3 id={id}>{title} <small>{items.length}</small></h3>
      {action}
    </header>
    {beforeList}
    <div id={id + '-rows'}>
      {items.length ? items.slice(0, visible).map(children) : <p className="extension-empty">{empty}</p>}
    </div>
    {items.length > PAGE_SIZE && <footer className="extension-pagination">
      <span aria-live="polite">已显示 {visible} / {items.length}</span>
      {visible < items.length && <button type="button" aria-controls={id + '-rows'}
        aria-label={`显示更多${title}`} onClick={() => setLimit(visible + PAGE_SIZE)}>
        显示更多 <CaretDown aria-hidden="true" />
      </button>}
      {visible > PAGE_SIZE && <button type="button" aria-controls={id + '-rows'}
        aria-label={`收起${title}`} onClick={() => setLimit(PAGE_SIZE)}>
        收起 <CaretUp aria-hidden="true" />
      </button>}
    </footer>}
  </section>
}
