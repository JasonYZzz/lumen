// Offline green-screen -> side-by-side RGB/alpha H.264. No runtime/API dependency.
// FFMPEG_PATH may point at a local ffmpeg binary; otherwise use PATH.
import { spawnSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { parseArgs } from 'node:util'

const { values } = parseArgs({ options: { input: {type:'string'}, output: {type:'string'}, poster: {type:'string'} } })
if (!values.input || !values.output) throw new Error('Required: --input original.mp4 --output clip.alpha.mp4 [--poster poster.png]')
const binary = process.env.FFMPEG_PATH || 'ffmpeg'
const matte = 'scale=512:512:flags=lanczos,format=rgba,colorkey=0x00A63D:0.24:0.12,despill=type=green:green=-1'
const run = args => {
  const result = spawnSync(binary, ['-hide_banner','-loglevel','error','-n','-i',resolve(values.input),...args], {stdio:'inherit'})
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`ffmpeg failed (${result.status})`)
}
mkdirSync(resolve(values.output, '..'), {recursive:true})
run(['-filter_complex',`[0:v]${matte},split[color][mask];[color]format=rgb24[rgb];[mask]alphaextract,format=rgb24[alpha];[rgb][alpha]hstack=inputs=2[packed]`,
  '-map','[packed]','-an','-c:v','libx264','-preset','slow','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',
  '-metadata','comment=AI-generated Lumen mascot; MiniMax-H3; RGB-left alpha-right; no audio',resolve(values.output)])
if (values.poster) run(['-vf',matte,'-frames:v','1',resolve(values.poster)])
