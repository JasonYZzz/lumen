export type LibraryBook = {
  id: string
  title: string
  type: string
  revision: number
  date: string
  source: string
}

export type LibraryTheme = { page: string; surface: string; accent: string; ink: string; line: string }

// Keep the registered HTML intact; adapt its catalogue and copy, never its geometry or motion.
export function librarySource(source: string, books: LibraryBook[], theme: LibraryTheme) {
  const json = JSON.stringify({ books, theme }).replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029')
  const replace = (pattern: string | RegExp, value: string) => {
    if (!(typeof pattern === 'string' ? source.includes(pattern) : pattern.test(source)))
      throw new Error('Ashen Press source no longer matches the registered integration')
    source = source.replace(pattern, () => value)
  }
  replace("const STUDIO = 'FromSoftware';", `const LUMEN = ${json};\nconst STUDIO = 'Lumen';`)
  replace(/const BOOKS = \[[\s\S]*?\n\];/, `const BOOKS = LUMEN.books.map((b,i)=>({
    ...b, t1:Array.from(b.title).slice(0,10).join(''), t2:Array.from(b.title).slice(10,20).join(''),
    sub:b.type, year:b.date, vol:String(b.revision), shelf:Math.floor(i/5),
    slot:i%5+2-(Math.min(5,LUMEN.books.length-Math.floor(i/5)*5)-1)/2,
    spine:LUMEN.theme.accent, cloth:'#302b33', edge:LUMEN.theme.surface,
    blurb:b.type+' · 版本 '+b.revision+' · '+b.source
  }));`)
  replace(/Object.assign\(MARK_SRC,\{[\s\S]*?\n\}\);/, '')
  replace(/Object.assign\(PLATE_SRC,\{[\s\S]*?\n\}\);/, '')
  replace(/function artBack\(b,W,H\)\{[\s\S]*?\n\}\nfunction artSpine/, `function artBack(b,W,H){
    const c=C(W,H),g=c.getContext('2d');
    g.fillStyle=LUMEN.theme.surface;g.fillRect(0,0,W,H);
    g.strokeStyle=LUMEN.theme.line;g.lineWidth=2;g.strokeRect(W*.07,H*.05,W*.86,H*.90);
    g.fillStyle=LUMEN.theme.accent;g.font='500 '+W*.042+'px '+SANS;
    g.fillText('LUMEN · 资料空间',W*.12,H*.14);
    g.fillStyle=LUMEN.theme.ink;g.font='500 '+W*.055+'px '+SANS;
    let line='',y=H*.28;
    for(const ch of Array.from(b.title)){
      if(g.measureText(line+ch).width>W*.76){g.fillText(line,W*.12,y);line='';y+=W*.08;}
      line+=ch;if(y>H*.55)break;
    }
    g.fillText(line,W*.12,y);
    g.font='400 '+W*.032+'px '+SANS;
    [b.type,'版本 '+b.revision,b.date,b.source].forEach((text,i)=>g.fillText(text,W*.12,H*.68+i*W*.065));
    grain(g,W,H,6,5);return c;
  }
  function artSpine`)
  source = source.replaceAll('ASHEN PRESS', 'LUMEN').replaceAll("'AP'", "'L'")
    .replaceAll('Volume ${d.vol}', '版本 ${d.vol}').replaceAll('Volume ${b.vol}', '版本 ${b.vol}')
    .replaceAll("'VOLUME '", "'版本 '")
    .replaceAll('`${STUDIO} · ${d.year}`', '`${d.type} · ${d.year}`')
  replace('tint:[1.0,0.876,0.726]', `tint:[${theme.page.slice(1).match(/../g)!.map(v => parseInt(v,16)/255).join(',')}]`)
  replace('renderer.setClearColor(0xc6ae8e,1);', `renderer.setClearColor(LUMEN.theme.page,1);`)
  replace('const paper=tintBand(a,TW,h,219,202,175,true);', `const paper=tintBand(a,TW,h,parseInt(LUMEN.theme.surface.slice(1,3),16),parseInt(LUMEN.theme.surface.slice(3,5),16),parseInt(LUMEN.theme.surface.slice(5,7),16),true);`)
  replace("pg.addColorStop(0,'rgb(222,206,180)');\n  pg.addColorStop(0.72,'rgb(219,202,175)');\n  pg.addColorStop(1,'rgb(213,195,167)');", `pg.addColorStop(0,LUMEN.theme.surface);pg.addColorStop(1,LUMEN.theme.page);`)
  replace('<span class="brandname rise" data-d="70">Ashen Press</span>', '<span class="brandname rise" data-d="70">资料书架</span>')
  replace('<title>Ashen Press — The Art Book Shelf</title>', '<title>Lumen 资料书架</title>')
  replace('<a class="pill rise" data-d="285" href="#" onclick="return false">Join the press</a>', '<span class="scene-help rise" data-d="285">选择资料 · 翻转 · 拖拽旋转</span>')
  replace('<button id="detail-look">Look inside</button>', '<button id="detail-open">打开资料</button><button id="detail-look">查看封底</button>')
  replace('<button id="detail-close">Close</button>', '<button id="detail-close">放回书架</button>')
  replace('>Drag to turn</div>', '>拖拽旋转 · 轻点翻转</div>')
  replace('<button id="mbar-details">Details</button>', '<button id="mbar-details">取出</button>')
  replace('<button class="solid" id="mbar-look">Look inside</button>', '<button id="mbar-look">翻转</button><button class="solid" id="mbar-open">打开资料</button>')
  replace('aria-label="Catalogue"', 'aria-label="资料目录"')
  replace("mDetails.textContent='Details';", "mDetails.textContent='取出';")
  replace("mDetails.textContent='Close';", "mDetails.textContent='放回';")
  replace('B.flipped=!B.flipped; B.loadBack();', `B.flipped=!B.flipped; B.loadBack();
    dLook.textContent=B.flipped?'查看封面':'查看封底';dLook.setAttribute('aria-pressed',String(B.flipped));
    document.body.dataset.face=B.flipped?'back':'front';`)
  replace('function showDetail(B){', `function showDetail(B){
    detailEl.inert=false;
    document.body.dataset.face='front';dLook.textContent='查看封底';dLook.setAttribute('aria-pressed','false');`)
  replace("const detailEl=document.getElementById('detail');", "const detailEl=document.getElementById('detail');detailEl.inert=true;detailEl.setAttribute('aria-label','资料详情');")
  replace('const B=S.active;S.active=null;', `const B=S.active;S.active=null;detailEl.inert=true;
    document.querySelectorAll('.tli')[B.index]?.focus({preventScroll:true});`)
  replace("if(S.active){toInactive(S.active);S.active=null;detailEl.classList.remove('on');", "if(S.active){toInactive(S.active);S.active=null;detailEl.inert=true;detailEl.classList.remove('on');")
  replace('`${STUDIO} · ${d.year} · 版本 ${d.vol}`', '`${d.type} · ${d.year} · 版本 ${d.vol}`')
  replace('b.setAttribute(\'aria-label\',`${d.title}, ${STUDIO}, ${d.year}. Opens the volume.`);', `b.setAttribute('aria-label','取出 '+d.title);`)
  replace(/  b.innerHTML=`\$\{mark\}<span class="tltxt">[\s\S]*?<\/span>`;/, `  const txt=document.createElement('span');txt.className='tltxt';
    for(const [tag,text] of [['p',d.type],['p','版本 '+d.vol],['h2',d.title]]){
      const el=document.createElement(tag);el.textContent=text;txt.appendChild(el);
    }b.appendChild(txt);`)
  replace('b.innerHTML=`<span>${d.year} · 版本 ${d.vol}</span>${d.title}`;', `const meta=document.createElement('span');meta.textContent=d.type+' · 版本 '+d.vol;
    b.append(meta,document.createTextNode(d.title));`)
  replace("mLook.addEventListener('click',e=>{e.stopPropagation();lookInside(books[M.index]);});", `mLook.addEventListener('click',e=>{e.stopPropagation();lookInside(books[M.index]);});
    const openBook=()=>{const b=S.active||books[M.index];if(b)parent.postMessage({type:'lumen:open-resource',id:b.data.id},'*');};
    document.getElementById('detail-open').addEventListener('click',openBook);
    document.getElementById('mbar-open').addEventListener('click',openBook);`)
  // The upstream loop skips pointer dragging on mobile; enable rotation for a selected volume there too.
  replace("if(S.anim==='intro'||MOBILE)return;", "if(S.anim==='intro'||(MOBILE&&!S.active))return;")
  replace('  startIntro();', `  startIntro();parent.postMessage({type:'lumen:shelf-ready'},'*');`)
  const styles = `<style>
    :root{--bg:${theme.page};--ink:${theme.ink};--ink-soft:${theme.accent}}
    body{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    header{height:56px;padding:0 24px}header .mark{display:none}
    .brandname{font:500 15px/1.5 Inter,sans-serif;letter-spacing:0;text-transform:none}
    .scene-help{font-size:12px;color:${theme.accent}}.detail{padding:80px 64px 80px 32px}
    .detail h2{font:600 clamp(22px,3vw,36px)/1.4 Inter,sans-serif;overflow-wrap:anywhere}
    .detail p{font:400 14px/1.8 Inter,sans-serif;color:${theme.ink}}
    .detail .meta{font-size:12px;letter-spacing:0}.detail .acts{flex-wrap:wrap;gap:8px}
    .detail .acts button,.mbar-acts button{padding:12px 14px;min-height:44px;border-radius:10px;box-shadow:none;background:${theme.surface};color:${theme.accent};font-size:13px}
    .detail .acts button:hover{box-shadow:none;background:${theme.page}}
    #detail-open,.mbar-acts button.solid{background:${theme.accent};color:white}
    button:focus-visible{outline:2px solid ${theme.accent};outline-offset:3px}
    footer{background:linear-gradient(to top,${theme.page} 46%,transparent);border-color:${theme.line}}
    .tl{justify-content:flex-start;gap:24px}.tli{min-height:44px;max-width:180px}
    .tli h2{max-width:180px;color:${theme.ink}}.tli p{font-size:11px}.hint{background:${theme.surface};color:${theme.accent}}
    .menu{background:${theme.page}}.menu button{font:500 16px/1.5 Inter,sans-serif;overflow-wrap:anywhere;min-height:44px}
    .mbar{align-items:stretch;flex-direction:column}.mbar-txt h2{font:500 16px/1.5 Inter,sans-serif;overflow-wrap:anywhere}
    .mbar-txt p{font-size:11px;letter-spacing:0}.mbar-acts{justify-content:flex-end}
    body.mobile .mbar{background:linear-gradient(to top,${theme.page} 72%,transparent)}
    @media(max-width:767px){.scene-help,body.mobile .hint{display:none}.mbar-txt h2{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}}
  </style>`
  replace('</style>', '</style>'+styles)
  replace('<script type="importmap">', `<script>
    const fail=()=>parent.postMessage({type:'lumen:shelf-error'},'*');
    addEventListener('error',fail);addEventListener('unhandledrejection',fail);
  </script><script type="importmap">`)
  return source
}
