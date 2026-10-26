import { expect, it } from 'vitest'
import { FOX_APPEARANCES, foxImage, foxStageForAppearance } from './fox-growth'

it('restores five appearance choices and safely defaults missing, outdated and reasoning values', () => {
  expect(FOX_APPEARANCES.map(item => foxStageForAppearance(item.id))).toEqual([0, 1, 2, 3, 4])
  for (const value of [null, '', 'future', 'high', '4']) expect(foxStageForAppearance(value)).toBe(0)
})
it('uses local responsive WebP assets and safely bounds invalid stage indices', () => {
  expect(foxImage(4, 'full')).toBe('/new-design/fox-stage-05-v2.webp')
  expect(foxImage(1, 'icon')).toBe('/new-design/fox-stage-02-v2-160.webp')
  expect(foxImage(0, 'retina')).toBe('/new-design/fox-stage-01-v2-960.webp')
  for (const stage of [-1, 5, 1.5, NaN]) expect(foxImage(stage)).toBe('/new-design/fox-stage-01-v2-480.webp')
})
