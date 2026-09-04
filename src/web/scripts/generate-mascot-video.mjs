// Offline asset-production CLI. Never import this into the browser bundle.
// Credentials: GEEKAI_API_KEY, or macOS Keychain service lumen.geekai.video/account lumen.
// A submission intent is persisted BEFORE POST; ambiguous failures never auto-retry.
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { parseArgs } from 'node:util'

const { values } = parseArgs({ options: {
  action: {type:'string'}, clip: {type:'string'}, image: {type:'string'}, out: {type:'string'},
} })
const common = '固定机位，完整单镜头，保持首帧中的九尾狐身份、脸型、眼睛、细腻奶油白毛发、粉紫蓝尾尖花纹和坐姿。恰好九条尾巴始终存在、相互分离。角色完整可见，构图和大小保持不变，脚掌始终落在原处。背景全程为完全均匀的纯绿色 #00B140，背景没有阴影、物体、纹理、反光或变化。动作是有生命的角色表演，身体体积稳定，头部、耳朵、胸口和尾巴各有自然的延迟，运动平滑、有缓入缓出。动画最后自然回到提供的尾帧，与首帧同一个姿势、位置、光照。保持最后0.4秒安静稳定。没有镜头移动、缩放、切镜、字幕或水印。角色不说话。'
const prompts = {
  idle: common + '6秒温柔待机循环：0-1秒安静呼吸；1-2秒自然眨眼一次，耳尖轻轻动一下；2-4秒胸口有极轻呼吸起伏，九条蓬松尾巴以不同相位轻柔摆动，根部稳定、尾梢稍有拖延，头部轻微好奇倾斜约3度；4-6秒轻柔回正，尾巴自然稳定。保持温柔、安静的陪伴感，不跳跃，不挥手，不夸张扭曲。'
  ,react: common + '6秒被轻轻唤起的互动：0-1秒保持安静坐姿；1-2秒眼睛明亮地看向观众，耳朵轻轻竖起；2-3.5秒头部小幅向左歪约6度，做一个温柔的小点头，嘴角维持原来的轻微笑意；尾巴比待机稍活泼但仍然克制；3.5-5秒自然回正并眨眼一次；5-6秒恢复初始坐姿和尾巴形状。动作包括预备、响应、缓冲、回落，不走动，不变脸，不张嘴说话。',
}
const api = 'https://geekai.co/api/v1'
function credential() {
  if (process.env.GEEKAI_API_KEY) return process.env.GEEKAI_API_KEY.trim()
  try { return execFileSync('security',['find-generic-password','-a','lumen','-s','lumen.geekai.video','-w'],{stdio:['ignore','pipe','ignore'],encoding:'utf8'}).trim() }
  catch { throw new Error('Configure GEEKAI_API_KEY or the documented macOS Keychain item. Never put a key in source.') }
}
const key = credential()
const safe = value => String(value ?? '').replaceAll(key,'[REDACTED]').replace(/sk-[A-Za-z0-9_-]+/g,'[REDACTED]').slice(0,600)
async function request(path, body) {
  const response = await fetch(api + path, {method:body?'POST':'GET',headers:{Authorization:`Bearer ${key}`,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,redirect:'error',signal:AbortSignal.timeout(120_000)})
  let data
  try { data = await response.json() } catch { throw new Error(`Provider returned non-JSON HTTP ${response.status}`) }
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${safe(data.error?.message ?? data.message ?? data.code)}`)
  return data
}
function compact(data) {
  const video = Array.isArray(data.video_result) ? data.video_result[0] : data.video_result
  return {model:data.model,task_id:data.task_id,task_status:data.task_status,
    video_result:video?{url:video.url,duration:video.duration}:undefined,
    error:data.error?{code:safe(data.error.code),message:safe(data.error.message)}:undefined}
}
async function run() {
  if (values.action === 'model') {
    const model=await request('/models/MiniMax-H3')
    console.log(JSON.stringify({id:model.id,type:model.type,price:model.price}))
    return
  }
  if (!values.out || !Object.hasOwn(prompts,values.clip ?? '')) throw new Error('Required: --out <private production directory> --clip idle|react')
  const out=resolve(values.out), receipt=resolve(out,`${values.clip}.receipt.json`)
  await mkdir(out,{recursive:true,mode:0o700})
  if(values.action === 'submit') {
    if(!values.image) throw new Error('Required: --image <PNG first/last frame>')
    const image=await readFile(resolve(values.image))
    if(image.length>8*1024*1024) throw new Error('Reference exceeds this pipeline’s 8 MB limit')
    const description={model:'MiniMax-H3',duration:6,resolution:'768P',aspect_ratio:'adaptive',watermark:false,async:true,retries:0,prompt:prompts[values.clip]}
    const intent={submittedAt:new Date().toISOString(),state:'submission-intent',imageSha256:createHash('sha256').update(image).digest('hex'),request:description}
    await writeFile(receipt,JSON.stringify(intent,null,2),{flag:'wx',mode:0o600})
    const frame=`data:image/png;base64,${image.toString('base64')}`
    // Never automatically repeat this POST after a timeout or process interruption.
    const result=compact(await request('/videos/generations',{...description,image:frame,image_tail:frame}))
    await writeFile(receipt,JSON.stringify({...intent,state:'submitted',result},null,2),{mode:0o600})
    if(!result.task_id) throw new Error('No task ID returned. Inspect receipt/provider dashboard before any new submission.')
    console.log(JSON.stringify({clip:values.clip,task_id:result.task_id,status:result.task_status}))
    return
  }
  const saved=JSON.parse(await readFile(receipt,'utf8'))
  const id=saved.result?.task_id
  if(!/^[a-zA-Z0-9-]+$/.test(id??'')) throw new Error('Missing task ID. Do not resubmit an ambiguous paid request.')
  const result=compact(await request(`/videos/${encodeURIComponent(id)}`))
  await writeFile(receipt,JSON.stringify({...saved,result},null,2),{mode:0o600})
  if(values.action==='status') {console.log(JSON.stringify({clip:values.clip,status:result.task_status,error:result.error,duration:result.video_result?.duration}));return}
  if(values.action!=='download') throw new Error('Expected --action model|submit|status|download')
  if(result.task_status!=='succeed'||!result.video_result?.url) throw new Error('Video is not ready')
  const url=new URL(result.video_result.url)
  if(url.protocol!=='https:') throw new Error('Refusing a non-HTTPS output URL')
  // No authorization header is ever sent to the output CDN.
  const response=await fetch(url,{signal:AbortSignal.timeout(120_000)})
  if(!response.ok) throw new Error(`Download HTTP ${response.status}`)
  const bytes=new Uint8Array(await response.arrayBuffer())
  const path=resolve(out,`${values.clip}.original.mp4`)
  await writeFile(path,bytes,{flag:'wx'})
  console.log(JSON.stringify({path,bytes:bytes.length}))
}
run().catch(error=>{console.error(safe(error.message));process.exitCode=1})
