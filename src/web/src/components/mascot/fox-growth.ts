export const FOX_APPEARANCES = [
  { id: 'pink', label: '樱粉' },
  { id: 'apricot', label: '杏白' },
  { id: 'mint', label: '薄荷' },
  { id: 'blue', label: '雾蓝' },
  { id: 'lilac', label: '浅紫' },
] as const

export function foxStageForAppearance(value: string | null): number {
  return Math.max(0, FOX_APPEARANCES.findIndex(appearance => appearance.id === value))
}

export function foxImage(stage = 0, size: 'full' | 'compact' | 'retina' | 'icon' = 'compact') {
  const safe = Number.isInteger(stage) && stage >= 0 && stage < FOX_APPEARANCES.length ? stage : 0
  return `/new-design/fox-stage-0${safe + 1}-v2${size === 'full' ? '' : size === 'icon' ? '-160' : size === 'retina' ? '-960' : '-480'}.webp`
}
