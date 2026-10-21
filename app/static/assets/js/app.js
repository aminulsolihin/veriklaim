/* Antarmuka VeriKlaim. Semua data berasal dari API; penskoran berjalan di server. */
(function(){
'use strict';

const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const dots=n=>Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g,'.');
const dec=(v,d)=>v.toFixed(d).replace('.',',');
const rp=n=>'Rp '+dots(n);
const rpShort=n=>n>=1e9?'Rp '+dec(n/1e9,2)+' miliar':n>=1e6?'Rp '+dec(n/1e6,1)+' juta':rp(n);
const times=v=>dec(Math.round(v*100)/100,2).replace(/,?0+$/,'')+'×';
const sc=v=>dec(v,3);
const one=v=>dec(Math.round(v*10)/10,1).replace(/,0$/,'');
const when=s=>{if(!s)return '';const d=new Date(s);return isNaN(d)?'':d.toLocaleString('id-ID',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'})};

const STATUS={both:'Kedua pemeriksaan',rule:'Aturan tagihan',model:'Lapisan anomali',clear:'Bersih'};
const REVIEW={confirmed:'Temuan dikonfirmasi',cleared:'Dinyatakan wajar',docs_requested:'Dokumen diminta'};
const KIND={SR:'Special Prosthesis',SD:'Special Drug'};
const CREV={confirmed:'Upcoding dikonfirmasi',cleared:'Dinyatakan wajar',docs_requested:'Rekam medis diminta'};
const LABELS={bill:'sisipan: top-up tanpa dukungan tagihan',fiktif:'sisipan: pola klaim fiktif',both:'sisipan: kedua pola','fiktif-quiet':'sisipan: klaim fiktif yang dibuat tampak wajar'};
const DRIVER=['Tagihan RS','Lama rawat','Top-up berulang','Volume top-up'];
const FEAT_LABEL=['Tagihan RS vs harga acuan','Lama rawat vs INA-CBG sama','Top-up sama, kartu sama, 90 hari','Volume top-up RS vs sejawat'];
const ACTIONS={demo_login:'Mulai sesi juri',feedback:'Mengirim umpan balik',login:'Masuk',login_failed:'Gagal masuk',logout:'Keluar',batch_upload:'Mengunggah batch',batch_settings:'Mengubah pengaturan',batch_delete:'Menghapus batch',review_set:'Mencatat keputusan',review_reopened:'Membuka ulang klaim',reference_price_set:'Mengubah harga acuan',reference_price_delete:'Menghapus harga acuan',export:'Ekspor CSV',coding_upload:'Mengunggah batch koding',coding_settings:'Mengubah pengaturan koding',coding_delete:'Menghapus batch koding',coding_review_set:'Mencatat keputusan koding',coding_review_reopened:'Membuka ulang klaim koding',coding_export:'Ekspor CSV koding'};
const ROLE={verifikator:'verifikator',supervisor:'supervisor',juri:'juri · tamu'};
const PERAN={verifikator:'Verifikator / anti-fraud BPJS','koder-rs':'Koder / casemix RS',klinisi:'Dokter / nakes','manajemen-rs':'Manajemen RS',juri:'Juri / penilai',lainnya:'Lainnya'};
const ROUTES=['queue','coding','batches','refs','robust','audit','feedback','about'];

const S={user:null,config:null,batches:[],batch:null,claims:[],byId:{},thr:100,k:12,cut:0,view:'flagged',sel:null,draft:null,route:'queue',refs:[],refPlaceholder:false,rb:null,tab:'queue',limit:20,foldOpen:false,q:'',cnt:null};
const UP_WARN=$('upWarn').textContent;
let X1=5,Y0=0.3,Y1=0.76;

/* ---------- API ---------- */
async function api(method,url,body,opt){
  const o={method,headers:{'X-Requested-With':'veriklaim'},credentials:'same-origin'};
  if(body instanceof FormData) o.body=body;
  else if(body!==undefined){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}
  let res;
  try{res=await fetch(url,o)}
  catch(_){throw new Error('Server tidak dapat dihubungi. Periksa koneksi, lalu coba lagi.')}
  let data=null;
  if((res.headers.get('content-type')||'').includes('application/json')) data=await res.json().catch(()=>null);
  if(res.status===401&&!(opt&&opt.quiet401)){
    showLogin('Sesi Anda berakhir. Silakan masuk lagi.');
    const e=new Error('Silakan masuk untuk melanjutkan.'); e.auth=true; throw e;
  }
  if(!res.ok){
    let msg=data&&data.detail;
    if(Array.isArray(msg)) msg=msg.map(d=>d.msg).join('. ');
    const e=new Error(msg||('Permintaan gagal ('+res.status+').')); e.status=res.status; throw e;
  }
  return data;
}
let toastTimer;
function toast(msg,isErr){
  const t=$('toast'); t.textContent=msg; t.className='toast'+(isErr?' err':''); t.hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>{t.hidden=true},isErr?6000:3200);
}
const fail=e=>{if(!e.auth) toast(e.message,true)};

/* ---------- masuk / keluar ---------- */
function showOnly(id){
  ['boot','view-login'].concat(ROUTES.map(r=>'view-'+r)).forEach(v=>{$(v).hidden=v!==id});
}
function showLogin(msg){
  S.user=null; $('top').hidden=true; $('foot').hidden=true; showOnly('view-login');
  $('loginMsg').textContent=msg||''; $('password').value='';
  const hint=S.config&&S.config.loginHint; $('loginHint').hidden=!hint; $('loginHint').textContent=hint||'';
  const judge=!!(S.config&&S.config.judgeMode);
  $('judgeCard').hidden=!judge; $('teamLabel').hidden=!judge; $('judgeMsg').textContent='';
  $('teamBox').open=!judge;
  if(judge) $('guestHours').textContent=S.config.guestHours; else $('username').focus();
}
$('loginForm').addEventListener('submit',async e=>{
  e.preventDefault(); const b=$('loginBtn'); b.disabled=true; $('loginMsg').textContent='';
  try{
    S.user=await api('POST','/api/login',{username:$('username').value,password:$('password').value},{quiet401:true});
    $('password').value=''; await enter();
  }catch(err){$('loginMsg').textContent=err.message}
  finally{b.disabled=false}
});
$('judgeBtn').addEventListener('click',async()=>{
  const b=$('judgeBtn'); b.disabled=true; b.textContent='Menyiapkan data juri…'; $('judgeMsg').textContent='';
  try{S.user=await api('POST','/api/demo-login',{},{quiet401:true}); history.replaceState(null,'','#/queue'); await enter()}
  catch(err){$('judgeMsg').textContent=err.message}
  finally{b.disabled=false; b.textContent='Coba sebagai juri'}
});
$('signOut').addEventListener('click',async()=>{
  if(S.user&&S.user.guest&&!window.confirm('Keluar dari sesi juri? Data sesi ini tidak bisa dibuka lagi; Anda bisa memulai sesi baru kapan saja.')) return;
  try{await api('POST','/api/logout',{},{quiet401:true})}catch(_){}
  S.batch=null; S.claims=[]; S.sel=null; if(window.VKCoding) window.VKCoding.reset(); history.replaceState(null,'','#/queue'); showLogin('');
});
async function enter(){
  $('top').hidden=false; $('foot').hidden=false;
  $('whoName').textContent=S.user.name; $('whoRole').textContent=ROLE[S.user.role]||S.user.role;
  $('whoAv').textContent=(S.user.name||'?').trim().charAt(0).toUpperCase();
  const sup=S.user.role==='supervisor', guest=!!S.user.guest;
  document.querySelectorAll('#nav a[data-role]').forEach(a=>{a.hidden=a.dataset.role==='audit'&&!(sup||guest)});
  $('refForm').hidden=!sup; $('saveSettings').hidden=!(sup||guest);
  $('footNote').textContent=guest?`Sesi juri: data Anda terpisah dan dihapus otomatis ${when(S.user.expiresAt)}.`:'Flag adalah permintaan pemeriksaan, bukan tuduhan.';
  setKind(UP_KIND);
  loadTour();
  refreshRefNote();
  await loadBatches();
  route();
}
function route(){
  if(!S.user) return;
  const asked=(location.hash||'').replace(/^#\/?/,'');
  let r=asked||'queue';
  if(!ROUTES.includes(r)||(r==='audit'&&S.user.role!=='supervisor'&&!S.user.guest)) r='queue';
  if(r==='audit') tick('audit');
  if(r==='robust') tick('robust');
  if(asked&&asked!==r) history.replaceState(null,'','#/'+r);
  S.route=r; showOnly('view-'+r);
  document.querySelectorAll('#nav a').forEach(a=>{if(a.dataset.route===r)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')});
  $('moreBtn').toggleAttribute('data-current',!!$('navSec').querySelector('a[aria-current]'));
  Promise.resolve().then(()=>({queue:openQueue,coding:()=>window.VKCoding&&window.VKCoding.open(),batches:openBatches,refs:openRefs,robust:openRobust,audit:openAudit,feedback:openFeedback,about:()=>{}})[r]()).catch(fail);
}
window.addEventListener('hashchange',route);

/* menu "Lainnya" dan lembar rincian klaim: fokus papan ketik dikelola */
const MOBILE=window.matchMedia('(max-width:980px)');
function closeMore(back){
  const was=$('nav').classList.contains('open');
  $('nav').classList.remove('open'); $('moreBtn').setAttribute('aria-expanded','false');
  if(was&&back) $('moreBtn').focus();
}
$('moreBtn').addEventListener('click',()=>{const o=!$('nav').classList.contains('open'); $('nav').classList.toggle('open',o); $('moreBtn').setAttribute('aria-expanded',String(o))});
document.addEventListener('click',e=>{if(!e.target.closest('#nav')) closeMore()});
$('nav').addEventListener('focusout',e=>{if($('nav').classList.contains('open')&&e.relatedTarget&&!$('nav').contains(e.relatedTarget)) closeMore()});
window.addEventListener('hashchange',()=>{closeMore(); closeSheet(false)});
let sheetBack=null;
function openSheet(el){
  if(!MOBILE.matches) return; el=el||$('why');
  const rows=el.id==='kdWhy'?'#kdRows':'#rows';
  sheetBack=()=>{const b=document.querySelector(rows+' .claim.current .cardbtn')||document.querySelector(rows+' .cardbtn'); if(b) b.focus()};
  el.classList.add('sheet'); el.setAttribute('role','dialog'); el.setAttribute('aria-modal','true'); el.setAttribute('aria-labelledby',el.id+'Title');
  el.scrollTop=0; document.body.classList.add('sheet-open');
  requestAnimationFrame(()=>{const b=el.querySelector('.back'); if(b) b.focus({preventScroll:true})});
}
function closeSheet(restore){
  let was=false;
  document.querySelectorAll('.why.sheet').forEach(e=>{was=true; e.classList.remove('sheet'); e.removeAttribute('role'); e.removeAttribute('aria-modal'); e.removeAttribute('aria-labelledby')});
  document.body.classList.remove('sheet-open');
  if(was&&restore!==false&&sheetBack) sheetBack();
  sheetBack=null;
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){closeMore(true); closeSheet(); return}
  if(e.key!=='Tab') return;
  const sh=document.querySelector('.why.sheet'); if(!sh) return;
  const f=[...sh.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select,summary')].filter(x=>x.offsetParent!==null);
  if(!f.length) return;
  const first=f[0], last=f[f.length-1], a=document.activeElement;
  if(e.shiftKey&&(a===first||!sh.contains(a))){e.preventDefault(); last.focus()}
  else if(!e.shiftKey&&(a===last||!sh.contains(a))){e.preventDefault(); first.focus()}
});

/* ---------- batch ---------- */
async function loadBatches(){
  S.batches=await api('GET','/api/batches');
  const sel=$('batchSel');
  sel.innerHTML=S.batches.map(b=>`<option value="${b.id}">${esc(b.name)}</option>`).join('');
  if(S.batch&&S.batches.some(b=>b.id===S.batch.id)) sel.value=S.batch.id;
}
$('batchSel').addEventListener('change',e=>loadBatch(+e.target.value).catch(fail));

async function openQueue(){
  const none=!S.batches.length;
  $('queueEmpty').hidden=!none; $('queueBody').hidden=none; $('batchSel').closest('.field').hidden=S.batches.length<2;
  if(none){$('batchTitle').textContent='Antrean pemeriksaan'; $('databar').textContent=''; return}
  if(!S.batch||!S.batches.some(b=>b.id===S.batch.id)) await loadBatch(S.batches[0].id);
  else update();
}
async function loadBatch(id){
  const d=await api('GET','/api/batches/'+id);
  S.batch=d.batch; S.claims=d.claims; S.byId={}; S.claims.forEach(c=>{S.byId[c.id]=c});
  S.thr=S.batch.thresholdPct; S.k=S.batch.topK; S.draft=null; S.limit=PAGE; S.q=''; $('qSearch').value='';
  $('batchSel').value=id;
  $('kk').max=S.batch.maxTopK; $('kk').value=S.k; $('thr').value=S.thr;
  const N=S.claims.length;
  X1=Math.max(3,Math.min(8,Math.ceil(Math.max(...S.claims.map(c=>c.cover))*1.04)));
  const lo=S.claims[N-1].score, hi=S.claims[0].score; Y0=Math.floor((lo-0.02)*10)/10; Y1=Math.max(Y0+0.3,hi+0.04);
  setView('flagged',true);
  classify();
  update();
  selectClaim(S.claims.find(c=>c.st==='model')||S.claims[0],false);
}
function classify(){
  const C=S.claims,N=C.length;
  S.cut=(C[S.k-1].score+C[Math.min(S.k,N-1)].score)/2;
  C.forEach(c=>{c.pf=c.cover*100<S.thr-1e-9; c.mf=c.rank<=S.k; c.st=c.pf&&c.mf?'both':c.pf?'rule':c.mf?'model':'clear'});
}
function counts(){
  const n={pf:0,mf:0,both:0,ruleOnly:0,modelOnly:0,flag:0,ruleValue:0,modelValue:0,inj:0,caught:0,ruleCaught:0,extra:0,injValue:0,caughtValue:0,confirmed:0,cleared:0,docs:0};
  S.claims.forEach(c=>{
    const flagged=c.st!=='clear';
    if(c.pf){n.pf++;n.ruleValue+=c.topup}
    if(c.mf)n.mf++;
    if(c.st==='both')n.both++; if(c.st==='rule')n.ruleOnly++; if(c.st==='model'){n.modelOnly++;n.modelValue+=c.topup}
    if(flagged){
      n.flag++;
      const s=c.review&&c.review.status;
      if(s==='confirmed')n.confirmed++; else if(s==='cleared')n.cleared++; else if(s==='docs_requested')n.docs++;
    }
    if(c.label){n.inj++; n.injValue+=c.topup; if(flagged){n.caught++;n.caughtValue+=c.topup} if(c.pf)n.ruleCaught++} else if(flagged)n.extra++;
  });
  n.reviewed=n.confirmed+n.cleared+n.docs;
  return n;
}

/* ---------- tanda ---------- */
function mark(st,cx,cy,k){
  k=k||1;
  if(st==='rule') return `<rect x="${cx-4.5*k}" y="${cy-4.5*k}" width="${9*k}" height="${9*k}" rx="1" fill="var(--c-rule)" stroke="var(--mark-line)" stroke-width="1.5"/>`;
  if(st==='model') return `<path d="M${cx},${cy-6.5*k} L${cx+6*k},${cy+4.5*k} L${cx-6*k},${cy+4.5*k} Z" fill="var(--c-model)" stroke="var(--surface)" stroke-width="1.5" stroke-linejoin="round"/>`;
  if(st==='both') return `<path d="M${cx},${cy-6.5*k} L${cx+6.5*k},${cy} L${cx},${cy+6.5*k} L${cx-6.5*k},${cy} Z" fill="var(--c-rule)" stroke="var(--c-model)" stroke-width="2.5" stroke-linejoin="round"/>`;
  return `<circle cx="${cx}" cy="${cy}" r="${4*k}" fill="var(--c-clear)" stroke="var(--surface)" stroke-width="1"/>`;
}
const icon=st=>`<svg class="mk" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">${mark(st,8,8,.92)}</svg>`;

/* ---------- bagian bersama dengan halaman Koding ---------- */
const stripHtml=items=>items.map(k=>`<div class="stat"><b>${k[0]}</b><span>${k[1]}</span>${k[2]!=null?`<i class="meter" aria-hidden="true"><i style="width:${Math.round(k[2]*100)}%"></i></i>`:''}</div>`).join('');
/* satu kartu = satu item daftar; tombol judul adalah pintu papan ketik */
function cardHtml(o){
  const p=o.pfx||'c';
  return `<li class="claim st-${o.st}${o.rv?' done':''}${o.current?' current':''}" data-id="${o.id}">
    <span class="cmark" aria-hidden="true">${icon(o.st)}</span>
    <div class="cbody"><div class="ctop"><h3 class="ctitle"><button type="button" class="cardbtn"${o.why?` aria-describedby="${p}w${o.id}"`:''}${o.current?' aria-current="true"':''}>${o.title}<span class="sr">, SEP ${esc(o.sep)}${o.rv?', '+esc(o.rv):''}</span></button></h3>${o.rv?`<span class="rv" aria-hidden="true">${esc(o.rv)}</span>`:''}</div>
    <p class="cmeta">${o.meta}</p>${o.why?`<p class="cwhy" id="${p}w${o.id}">${o.why}</p>`:''}</div></li>`;
}
/* panah atas/bawah memindahkan fokus antar kartu */
function listKeys(ul){
  ul.addEventListener('keydown',e=>{
    if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key)||!e.target.classList.contains('cardbtn')) return;
    const b=[...ul.querySelectorAll('.cardbtn')], i=b.indexOf(e.target); let n=i;
    if(e.key==='ArrowDown') n=Math.min(b.length-1,i+1); else if(e.key==='ArrowUp') n=Math.max(0,i-1); else if(e.key==='Home') n=0; else n=b.length-1;
    e.preventDefault(); b[n].focus(); b[n].closest('.claim').scrollIntoView({block:'nearest'});
  });
}
/* klaim berikutnya yang belum diperiksa setelah klaim ini, memutar ke awal daftar */
function nextAfter(list,cur){
  const i=list.indexOf(cur), rot=i<0?list:list.slice(i+1).concat(list.slice(0,i));
  return rot.find(c=>c!==cur&&c.st!=='clear'&&!c.review)||null;
}
/* gambar ulang panel tanpa kehilangan fokus (mis. saat rincian selesai dimuat) */
function setPanel(el,html){
  const a=document.activeElement, mine=a&&el.contains(a);
  const id=mine?a.id:'', sel=mine&&a.selectionStart!=null?[a.selectionStart,a.selectionEnd]:null, back=mine&&a.classList&&a.classList.contains('back'), head=mine&&a.classList&&a.classList.contains('vhead');
  el.innerHTML=html;
  const n=id?document.getElementById(id):(back?el.querySelector('.back'):head?el.querySelector('.vhead'):null);
  if(n){n.focus({preventScroll:true}); if(sel&&n.setSelectionRange){try{n.setSelectionRange(sel[0],sel[1])}catch(_){}}}
}
function focusDetail(el){const v=el.querySelector('.vhead'); el.scrollTop=0; if(v) v.focus()}
/* panel rincian: ringkasan putusan, alasan, keputusan; perhitungan dilipat */
function detailHtml(o){
  const f=o.ids, rv=o.rv, d=o.draft;
  const opt=x=>`<label class="opt"><input type="radio" name="${f.radio}" value="${x[0]}"${d.status===x[0]?' checked':''}><span class="t">${x[1]}<span class="s">${x[2]}</span></span></label>`;
  const showResp=d.status==='docs_requested'||!!d.resp;
  return `<header><button type="button" class="back" data-act="close" aria-label="Tutup rincian, kembali ke antrean"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>Antrean</button><h2 id="${o.panelId}Title">Klaim terpilih</h2>${o.tag||''}</header>
  <div class="body">
    <div class="vhead st-${o.st}" tabindex="-1" role="group" aria-label="Klaim ${esc(o.sep)}, ${o.status}">
      <div class="idline"><span class="num">${esc(o.sep)}</span><span class="pill ${o.st}">${icon(o.st)}${o.status}</span></div>
      <p class="verdict">${o.verdict}</p>
      <p class="herometa">${o.hero}</p>
    </div>
    <section class="sect"><h3>${o.reasonsTitle}</h3>
      <ul class="reasons">${o.reasons.map(r=>`<li>${r}</li>`).join('')}</ul>
      ${o.next?`<p class="next"><b>Langkah berikut.</b> ${o.next}</p>`:''}
    </section>
    <form class="review" id="${f.form}">
      <h3>Keputusan verifikator</h3>
      ${rv?`<p class="decided"><b>${esc(o.labels[rv.status])}</b> oleh ${esc(rv.by)}, ${esc(when(rv.at))}</p>`:''}
      <fieldset class="opts"><legend class="sr">Keputusan</legend>${o.opts.map(opt).join('')}</fieldset>
      <label for="${f.note}">Catatan</label>
      <textarea id="${f.note}" maxlength="1000" placeholder="${esc(o.notePh)}">${esc(d.note)}</textarea>
      <div class="resp${showResp?' show':''}"><label for="${f.resp}">Tanggapan rumah sakit</label>
        <textarea id="${f.resp}" maxlength="1000" placeholder="${esc(o.respPh)}">${esc(d.resp)}</textarea></div>
      <div class="btnrow">
        <button type="submit" class="btn" value="next">Simpan dan lanjut</button>
        <button type="submit" class="btn ghost" value="stay">Simpan saja</button>
        ${rv?`<button type="button" class="btn ghost" id="${f.reopen}">Buka ulang</button>`:''}
      </div>
    </form>
    <details class="fold flat"${o.open?' open':''}><summary><span class="t">Perhitungan dan data klaim</span></summary><div class="foldbody">
      <dl class="facts">${o.facts.map(x=>`<div><dt>${x[0]}</dt><dd${x[2]?` class="${x[2]}"`:''}>${x[1]}</dd></div>`).join('')}</dl>
      ${o.stages.map(x=>`<div class="stage"><h3>${x[0]}</h3>${x[1]}</div>`).join('')}
    </div></details>
  </div>`;
}
/* kolom tanggapan RS hanya tampil bila dokumen diminta atau sudah terisi */
function syncResp(form){
  const r=form.querySelector('.resp'); if(!r) return;
  const st=form.querySelector('input[type=radio]:checked'), ta=r.querySelector('textarea');
  r.classList.toggle('show',!!((st&&st.value==='docs_requested')||ta.value));
}
/* tab: satu tampilan per layar, dengan peran dan hubungan ARIA lengkap */
function bindTabs(tabs,body,onChange){
  const btns=()=>[...tabs.querySelectorAll('button[data-tab]')];
  const panels=()=>[...body.querySelectorAll(':scope > [data-tabpanel]')];
  btns().forEach(b=>{if(!b.id) b.id=tabs.id+'-'+b.dataset.tab});
  const idOf=n=>btns().find(b=>b.dataset.tab===n).id;
  btns().forEach(b=>b.setAttribute('aria-controls',tabs.id+'-p-'+b.dataset.tab));
  panels().forEach(p=>{p.id=tabs.id+'-p-'+p.dataset.tabpanel; p.setAttribute('aria-labelledby',idOf(p.dataset.tabpanel))});
  function set(name,focus){
    btns().forEach(b=>{const on=b.dataset.tab===name; b.setAttribute('aria-selected',String(on)); b.tabIndex=on?0:-1; if(on&&focus) b.focus()});
    panels().forEach(p=>{p.hidden=p.dataset.tabpanel!==name});
    if(onChange) onChange(name);
  }
  tabs.addEventListener('click',e=>{const b=e.target.closest('button[data-tab]'); if(b) set(b.dataset.tab)});
  tabs.addEventListener('keydown',e=>{
    const l=btns().filter(b=>!b.hidden), i=l.findIndex(b=>b.getAttribute('aria-selected')==='true'); let n=-1;
    if(e.key==='ArrowRight') n=(i+1)%l.length; else if(e.key==='ArrowLeft') n=(i-1+l.length)%l.length; else if(e.key==='Home') n=0; else if(e.key==='End') n=l.length-1;
    if(n>=0){e.preventDefault(); set(l[n].dataset.tab,true)}
  });
  body.addEventListener('click',e=>{const g=e.target.closest('[data-goto-tab]'); if(g){e.preventDefault(); set(g.dataset.gotoTab); window.scrollTo({top:0,behavior:'smooth'})}});
  set(btns()[0].dataset.tab);
  return {set};
}
/* tabel yang bisa digeser mendapat nama dan fokus papan ketik */
document.querySelectorAll('.tblwrap').forEach((el,i)=>{
  const sec=el.closest('section'), h=sec&&sec.querySelector('h1,h2');
  if(h){if(!h.id) h.id='tw'+i; el.setAttribute('aria-labelledby',h.id)} else el.setAttribute('aria-label','Tabel');
  el.setAttribute('role','group'); el.tabIndex=0;
});

function topDriver(c){if(!c.phi)return -1;let b=0;for(let i=1;i<c.phi.length;i++)if(c.phi[i]>c.phi[b])b=i;return b}
function featText(c,i){
  if(i===0) return times(c.cover)+' acuan';
  if(i===1) return c.los+' hari, median '+one(c.losMed);
  if(i===2) return c.rep+' klaim lain';
  return times(c.vol)+' median sejawat';
}

/* ---------- halaman antrean ---------- */
function renderHead(){
  const b=S.batch,N=S.claims.length;
  $('batchTitle').textContent=b.name;
  $('databar').innerHTML=`<span><b>${dots(N)}</b> klaim ber-top-up disaring dari ${dots(b.rowsTotal)} baris</span><span>diunggah oleh ${esc(b.uploadedBy)}, ${esc(when(b.uploadedAt))}</span>`;
  const skip=[];
  if(b.noTopup) skip.push(`${dots(b.noTopup)} baris tanpa top-up obat atau alkes`);
  if(b.noRef) skip.push(`${dots(b.noRef)} baris dengan top-up tanpa harga acuan`);
  if(b.bad) skip.push(`${dots(b.bad)} baris tidak terbaca atau ganda`);
  if(b.truncated) skip.push(`hanya ${dots(N)} klaim top-up pertama yang disaring`);
  const sn=$('skipNote'); sn.hidden=!skip.length; sn.textContent=skip.length?'Dilewati: '+skip.join('; ')+'.':'';
}
function renderPipe(n){
  $('pipe').innerHTML=[
    ['Tahap 1 · Baca TXT E-Klaim',`<span class="num">${dots(S.claims.length)}</span> klaim`,'dengan top-up obat atau alkes'],
    ['Tahap 2 · Aturan tagihan',`<span class="num">${n.pf}</span> tanpa dukungan`,`tagihan di bawah ${S.thr}% harga acuan`],
    ['Tahap 3 · Lapisan anomali',`<span class="num">${n.mf}</span> tidak biasa`,`${n.modelOnly} di antaranya lolos aturan tagihan`],
    ['Tahap 4 · Alasan dan keputusan',`<span class="num">${n.flag}</span> di antrean`,`${n.reviewed} sudah diperiksa`]
  ].map(s=>`<li><span class="step">${s[0]}</span><span class="big">${s[1]}</span><span class="cap">${s[2]}</span></li>`).join('');
}
function renderBanner(){
  const bn=$('dataBanner'); if(!S.batch){bn.hidden=true;return}
  const bits=[]; if(S.batch.labelled) bits.push('data sintetis'); if(S.refPlaceholder) bits.push('harga acuan contoh');
  bn.hidden=!bits.length;
  if(bits.length){const t=bits.join(' dan '); bn.innerHTML=`${t.charAt(0).toUpperCase()+t.slice(1)}: hanya menunjukkan cara kerja, bukan kinerja pada klaim nyata. <button type="button" class="go" data-goto-tab="about" aria-label="Rincian cara kerja dan batasan, di tab Gambaran">Rincian</button>`}
}
function renderKpis(n){
  const N=S.claims.length, decided=n.confirmed+n.cleared;
  $('kpis').innerHTML=stripHtml([
    [`${n.flag}<small> dari ${dots(N)}</small>`,'klaim perlu diperiksa'],
    [`${n.pf}`,'ditandai aturan tagihan'],
    [`+${n.modelOnly}`,'hanya ditandai lapisan anomali'],
    [`${n.reviewed}<small> dari ${n.flag}</small>`,'sudah diperiksa',n.flag?n.reviewed/n.flag:0]
  ]);
  const precision=decided?` Presisi sejauh ini: <b>${Math.round(n.confirmed/decided*100)}%</b> (${n.confirmed} dikonfirmasi dari ${decided} yang diputuskan).`:'';
  $('valNote').innerHTML=`<b>Nilai top-up yang diklaim.</b> Klaim yang gagal aturan tagihan: ${rpShort(n.ruleValue)}. Klaim yang hanya ditemukan lapisan anomali: ${rpShort(n.modelValue)}. Antrean memuat ${Math.round(n.flag/N*100)}% dari klaim yang disaring. Sudah diperiksa: ${n.confirmed} dikonfirmasi, ${n.cleared} wajar, ${n.docs} menunggu dokumen.${precision}`;
  const note=$('labelNote'); note.hidden=!S.batch.labelled;
  if(S.batch.labelled){
    const rand=n.inj*n.flag/N;
    note.innerHTML=`<b>Data sintetis.</b> ${n.inj} klaim disisipi pola bermasalah. Dengan pengaturan ini, <b>${n.caught} dari ${n.inj}</b> masuk antrean (aturan tagihan saja: ${n.ruleCaught}), bersama ${n.extra} klaim wajar. Sampel acak dengan beban yang sama (${n.flag} klaim) rata-rata hanya memuat <b>${dec(rand,1)} dari ${n.inj}</b>. Nilai top-up klaim bermasalah di antrean: ${rpShort(n.caughtValue)} dari ${rpShort(n.injValue)}. Polanya lebih bersih daripada klaim nyata, jadi ini bukan perkiraan kinerja.`;
  }
  renderBanner();
}
function renderLegend(n){
  $('legend').innerHTML=[['both',n.both],['rule',n.ruleOnly],['model',n.modelOnly],['clear',S.claims.length-n.flag]]
    .map(([st,v])=>`<span>${icon(st)}${st==='both'?'Ditandai kedua pemeriksaan':st==='rule'?'Aturan tagihan saja':st==='model'?'Lapisan anomali saja':'Bersih'} <b>${v}</b></span>`).join('');
}
function groupRates(key,make){
  const by={}, N=S.claims.length; let flagged=0;
  S.claims.forEach(c=>{
    const k=key(c), r=by[k]||(by[k]=make(c));
    r.n++; if(c.st!=='clear'){r.f++; flagged++;
      const s=c.review&&c.review.status; if(s==='confirmed')r.conf++; else if(s==='cleared')r.clr++; else if(s==='docs_requested')r.docs++;}
  });
  const avg=flagged/N;
  return {N,flagged,avg,list:Object.values(by).sort((a,b)=>b.f/b.n-a.f/a.n||b.n-a.n)};
}
function rateCells(r,avg){
  const rate=r.f/r.n, hi=r.n>=4&&rate>=2*avg&&rate>0;
  return `<td class="r num">${r.n}</td><td class="r num">${r.f}</td>
      <td><span class="num">${Math.round(rate*100)}%</span>${hi?' <span class="tag">≥ 2× rata-rata</span>':''}<span class="sbar rate${hi?' on':''}"><i style="width:${Math.round(rate*100)}%"></i></span></td>
      <td class="r num">${r.conf}</td><td class="r num">${r.clr}</td><td class="r num">${r.docs}</td>`;
}
function avgRow(N,flagged,avg){
  return `<tr><td><b>Rata-rata batch</b></td><td class="r num">${N}</td><td class="r num">${flagged}</td><td><span class="num">${Math.round(avg*100)}%</span></td><td></td><td></td><td></td></tr>`;
}
function renderRs(){
  const g=groupRates(c=>c.rs,c=>({rs:c.rs,kelas:c.kelasRs,n:0,f:0,conf:0,clr:0,docs:0}));
  $('rsRows').innerHTML=g.list.map(r=>`<tr><td>RS ${esc(r.rs)}<span class="sm">Kelas ${esc(r.kelas||'?')}</span></td>${rateCells(r,g.avg)}</tr>`).join('')+avgRow(g.N,g.flagged,g.avg);
}
function renderCodes(){
  const g=groupRates(c=>c.code,c=>({code:c.code,item:c.item,n:0,f:0,conf:0,clr:0,docs:0}));
  $('codeRows').innerHTML=g.list.map(r=>`<tr><td><span class="num">${esc(r.code)}</span><span class="sm">${esc(r.item)}</span></td>${rateCells(r,g.avg)}</tr>`).join('')+avgRow(g.N,g.flagged,g.avg);
}
function renderUnsaved(){
  const b=S.batch, dirty=S.thr!==b.thresholdPct||S.k!==b.topK;
  $('unsaved').hidden=!dirty;
  if(dirty) $('unsavedText').textContent=`Anda sedang mencoba pengaturan lain. Pengaturan tersimpan untuk batch ini: ${b.thresholdPct}% dan ${b.topK} klaim.`+(S.user.role==='supervisor'||S.user.guest?'':' Hanya supervisor yang dapat menyimpan perubahan.');
  $('ctlSum').textContent=`tagihan < ${S.thr}% · ${S.k} klaim anomali`+(dirty?' · belum disimpan':'');
}
function drawChart(){
  const box=$('chart');
  if(S.route!=='queue'||S.tab!=='about'||!S.claims.length||!box.clientWidth) return;
  const W=Math.max(280,box.clientWidth-16), H=W<520?300:360, m={l:44,r:16,t:20,b:42};
  const x=v=>m.l+Math.min(v,X1)/X1*(W-m.l-m.r), y=s=>H-m.b-(Math.max(Y0,Math.min(Y1,s))-Y0)/(Y1-Y0)*(H-m.t-m.b);
  let g='';
  for(let t=Math.ceil(Y0*10)/10;t<=Y1+1e-9;t+=0.1){g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--rule)" stroke-width="1"/><text x="${m.l-8}" y="${y(t)+4}" text-anchor="end">${dec(t,1)}</text>`}
  for(let t=1;t<=X1;t++){g+=`<line x1="${x(t)}" x2="${x(t)}" y1="${H-m.b}" y2="${H-m.b+5}" stroke="var(--rule-strong)"/><text x="${x(t)}" y="${H-m.b+18}" text-anchor="middle">${t}×</text>`}
  g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${H-m.b}" y2="${H-m.b}" stroke="var(--rule-strong)"/>`;
  g+=`<text class="axt" x="${(m.l+W-m.r)/2}" y="${H-6}" text-anchor="middle">${W<520?'TARIF_RS ÷ harga acuan item':'Tagihan rumah sakit (TARIF_RS) sebagai kelipatan harga acuan item top-up'}</text>`;
  g+=`<text class="axt" x="${m.l-34}" y="${m.t-8}" text-anchor="start">Skor anomali</text>`;
  const tx=x(S.thr/100), ty=y(S.cut);
  g+=`<line x1="${tx}" x2="${tx}" y1="${m.t}" y2="${H-m.b}" stroke="var(--c-rule-line)" stroke-width="2" stroke-dasharray="5 4"/>`;
  g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${ty}" y2="${ty}" stroke="var(--c-model)" stroke-width="2" stroke-dasharray="5 4"/>`;
  const tl=`tagihan < ${S.thr}%`, right=tx+8+tl.length*7.2<W-m.r-90;
  g+=`<text class="thl" x="${right?tx+7:tx-7}" y="${m.t+10}" text-anchor="${right?'start':'end'}">${tl}</text>`;
  g+=`<text class="thl" x="${W-m.r-2}" y="${ty-6}" text-anchor="end">batas ${dec(S.cut,2)}</text>`;
  const order={clear:0,rule:1,model:2,both:3};
  [...S.claims].sort((a,b)=>order[a.st]-order[b.st]).forEach(c=>{
    c._cx=+x(c.cover).toFixed(1); c._cy=+y(c.score).toFixed(1);
    g+=mark(c.st,c._cx,c._cy);
  });
  const s=S.sel; if(s) g+=`<circle cx="${s._cx}" cy="${s._cy}" r="12.5" fill="none" stroke="var(--ink)" stroke-width="1.75"/>`;
  S.claims.forEach(c=>{g+=`<circle class="hit" data-id="${c.id}" cx="${c._cx}" cy="${c._cy}" r="11" fill="transparent"/>`});
  const old=box.querySelector('svg'); if(old) old.remove();
  box.insertAdjacentHTML('afterbegin',`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Sebaran ${S.claims.length} klaim ber-top-up: tagihan rumah sakit sebagai kelipatan harga acuan item di sumbu mendatar, skor anomali di sumbu tegak. Tabel antrean memuat klaim yang sama.">${g}</svg>`);
}
(function(){
  const box=$('chart'), tip=$('tip');
  box.addEventListener('mousemove',e=>{
    const t=e.target.closest&&e.target.closest('.hit'), c=t&&S.byId[t.dataset.id];
    if(!c){tip.hidden=true;return}
    tip.innerHTML=`<b>${esc(c.sep)}</b>${esc(c.final||c.code)} ${esc(c.item)}<br>tagihan <span class="num">${times(c.cover)}</span> acuan · skor <span class="num">${sc(c.score)}</span><br>${STATUS[c.st]}${c.review?' · '+REVIEW[c.review.status]:''}`;
    tip.hidden=false;
    const bw=box.clientWidth, tw=tip.offsetWidth;
    tip.style.left=(c._cx+22+tw>bw-8?Math.max(8,c._cx-6-tw):c._cx+22)+'px';
    tip.style.top=Math.max(4,c._cy+6-tip.offsetHeight-12)+'px';
  });
  box.addEventListener('mouseleave',()=>{tip.hidden=true});
  box.addEventListener('click',e=>{const t=e.target.closest&&e.target.closest('.hit'); if(t&&S.byId[t.dataset.id]){tabs.set('queue'); window.scrollTo({top:0}); selectClaim(S.byId[t.dataset.id],true)}});
})();

const PAGE=20;
function queueList(){
  const q=S.q.trim().toLowerCase();
  let list=S.claims.filter(c=>S.view==='all'||(c.st!=='clear'&&(S.view==='flagged'||!c.review)));
  if(q) list=list.filter(c=>`${c.sep} ${c.rs} ${c.item} ${c.final||c.code} ${c.cbg}`.toLowerCase().includes(q));
  return list.sort((a,b)=>(a.st==='clear')-(b.st==='clear')||a.rank-b.rank);
}
function renderSeg(){
  const n=S.cnt; if(!n) return;
  const t={flagged:['Di antrean',n.flag],todo:['Belum diperiksa',n.flag-n.reviewed],all:['Semua klaim',S.claims.length]};
  document.querySelectorAll('#view-queue .seg button[data-view]').forEach(b=>{const x=t[b.dataset.view]; b.innerHTML=`${x[0]}<span class="cnt">${dots(x[1])}</span>`; b.setAttribute('aria-pressed',String(b.dataset.view===S.view))});
}
function renderRows(){
  let list=queueList();
  const total=list.length; if(total>S.limit) list=list.slice(0,S.limit);
  renderSeg();
  const q=S.q.trim();
  $('queueLede').textContent=total?`${dots(total)} klaim${q?` cocok dengan “${q}”`:''}, diurutkan menurut skor anomali. Pilih satu untuk melihat alasannya.`:'';
  $('qMoreRow').hidden=total<=S.limit; $('qMore').textContent=`Tampilkan ${Math.min(PAGE,total-S.limit)} lagi (sisa ${dots(total-S.limit)})`;
  $('rows').innerHTML=list.map(c=>{
    const d=c.mf?topDriver(c):-1;
    const reason=d>=0?`${DRIVER[d]}: ${featText(c,d)}`:c.pf?`Tagihan RS: ${times(c.cover)} acuan`:'';
    return cardHtml({id:c.id,pfx:'c',sep:c.sep,st:c.st,current:S.sel===c,rv:c.review?REVIEW[c.review.status]:'',
      title:esc(c.item),
      meta:`<span class="num sepn">${esc(c.sep)}</span><span class="msub">RS ${esc(c.rs)} · pulang ${esc(c.disTxt||c.admTxt)}</span>`,
      why:`<b>${STATUS[c.st]}</b>${reason?' · '+esc(reason):''}`});
  }).join('')||`<li class="noclaims">${q?`Tidak ada klaim yang cocok dengan “${esc(q)}”. Hapus pencarian untuk melihat semua.`:S.view==='todo'?'Semua klaim di antrean sudah diperiksa. Ekspor CSV untuk melaporkan hasilnya.':'Tidak ada klaim di antrean dengan pengaturan ini. Naikkan ambang tagihan atau kapasitas verifikator.'}</li>`;
  $('exportCsv').href=`/api/batches/${S.batch.id}/export.csv?view=${S.view==='all'?'all':'flagged'}&thr=${S.thr}&k=${S.k}`;
}
listKeys($('rows'));
$('qSearch').addEventListener('input',e=>{S.q=e.target.value; S.limit=PAGE; renderRows()});
$('rows').addEventListener('click',e=>{const el=e.target.closest('.claim[data-id]'); if(el&&S.byId[el.dataset.id]) selectClaim(S.byId[el.dataset.id],true,e.detail===0)});
$('qMore').addEventListener('click',()=>{S.limit+=PAGE; renderRows()});
function setView(v,quiet){
  S.view=v; S.limit=PAGE;
  document.querySelectorAll('#view-queue .seg button[data-view]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.view===v));
  if(!quiet) renderRows();
}
document.querySelector('#view-queue .seg').addEventListener('click',e=>{const b=e.target.closest('button[data-view]'); if(b) setView(b.dataset.view)});
const tabs=bindTabs($('tabs'),$('queueBody'),name=>{S.tab=name; if(name==='about') drawChart(); if(name==='bias') tick('rs')});

/* ---------- klaim terpilih ---------- */
async function selectClaim(c,user,focus){
  if(!c) return;
  S.sel=c; S.draft=null;
  if(user){tick('reasons'); if(c.rank===2&&S.batch.labelled) tick('example')}
  if(focus){const i=queueList().indexOf(c); if(i>=S.limit) S.limit=Math.ceil((i+1)/PAGE)*PAGE}
  drawChart(); renderRows(); renderWhy();
  if(user) openSheet();
  if(focus&&!(user&&MOBILE.matches)){
    const card=document.querySelector(`#rows .claim[data-id="${c.id}"]`); if(card) card.scrollIntoView({block:'nearest'});
    requestAnimationFrame(()=>focusDetail($('why')));
  }
  if(c.full) return;
  try{
    const d=await api('GET','/api/claims/'+c.id);
    Object.assign(c,d,{full:true});
    if(S.sel===c){renderWhy(); renderRows()}
  }catch(e){fail(e)}
}
function reasonsFor(c){
  const phi=c.phi, isSR=c.kind==='SR', item=(c.item||'item').toLowerCase(), reasons=[];
  if(c.pf) reasons.push(`Seluruh tagihan rumah sakit lebih rendah dari harga acuan ${esc(item)} yang diklaim top-up-nya. Barang itu mungkin tidak diberikan, atau yang dipakai lebih murah dari yang diklaim.`);
  const short=c.los<c.losMed;
  if(phi){
    [0,1,2,3].sort((a,b)=>phi[b]-phi[a]).forEach(i=>{
      if(phi[i]<0.03) return;
      if(i===0&&!c.pf) reasons.push(`Tagihan ${times(c.cover)} harga acuan, tidak biasa dibanding klaim lain di batch ini.`);
      if(i===1) reasons.push(short?`Lama rawat ${c.los} hari, padahal median INA-CBG yang sama ${one(c.losMed)} hari.`:`Lama rawat ${c.los} hari, lebih panjang dari median ${one(c.losMed)}. Tidak biasa, tetapi rawat panjang bukan sinyal risiko untuk top-up.`);
      if(i===2) reasons.push(`Kartu yang sama punya ${c.rep} klaim lain dengan top-up ini dalam 90 hari.`+(c.rep===1&&isSR?' Satu pengulangan bisa wajar, misalnya sisi kedua pada penggantian sendi bilateral.':''));
      if(i===3) reasons.push(c.vol>1?`Rumah sakit ini punya ${c.volN} klaim dengan top-up ini di berkas, ${times(c.vol)} median rumah sakit lain.`:`Rumah sakit ini punya lebih sedikit klaim dengan top-up ini dibanding sejawatnya (${times(c.vol)}). Tidak biasa, tetapi volume rendah bukan sinyal risiko.`);
    });
  }
  let next='';
  if(c.st!=='clear'){
    const fiktif=phi&&c.mf&&!c.pf&&(phi[2]>=0.03&&c.rep>=2||phi[1]>=0.03&&short&&c.rep>=1);
    if(fiktif) reasons.push('Top-up berulang dan rawat sangat singkat dengan tagihan yang tampak wajar adalah jejak pola klaim fiktif (phantom billing). Ini perlu diverifikasi; model tidak membuktikannya.');
    next=c.pf?(isSR?'Minta faktur pembelian implan dan stiker atau nomor seri implan pada laporan operasi.':'Minta catatan penyerahan obat dari farmasi dan waktu pemberian obat di rekam medis.')
      :c.rep>=1?(isSR?'Cocokkan tiap rawat inap dengan laporan operasi dan catatan implan, dan pastikan sendi mana yang diganti setiap kali.':'Cocokkan tiap rawat inap dengan rekam medis dan catatan pemberian obat.')
      :'Periksa dokumen pendukung klaim ini sebelum dibayar.';
  }
  if(!reasons.length) reasons.push(phi?'Tidak ada yang menonjol pada tagihan, lama rawat, top-up berulang, atau volume.':'Memuat penjelasan.');
  return {reasons,next};
}
function renderWhy(){
  const c=S.sel; if(!c){$('why').innerHTML='<div class="empty-detail"><p>Pilih klaim di daftar untuk melihat alasannya dan mencatat keputusan.</p></div>';return}
  const phi=c.phi, base=S.batch.baseScore, N=S.claims.length;
  const verdict={both:'Ditandai aturan tagihan dan lapisan anomali',rule:'Ditandai aturan tagihan saja',model:'Ditandai lapisan anomali saja',clear:'Tidak masuk antrean dengan pengaturan ini'}[c.st];
  const s2=c.pf
    ?`TARIF_RS <span class="num">${rp(c.tarifRs)}</span>, yaitu <b>${times(c.cover)}</b> harga acuan item (<span class="num">${rp(c.ref)}</span>). Di bawah ambang ${S.thr}%, padahal top-up <span class="num">${rp(c.topup)}</span> diklaim.`
    :`TARIF_RS <span class="num">${rp(c.tarifRs)}</span>, yaitu <b>${times(c.cover)}</b> harga acuan item (<span class="num">${rp(c.ref)}</span>). Lolos pada ambang ${S.thr}%.`;
  const s3=`Skor <b class="num">${sc(c.score)}</b>, peringkat ${c.rank} dari ${dots(N)}. Batas antrean <span class="num">${sc(S.cut)}</span>, rata-rata klaim <span class="num">${sc(base)}</span>. ${c.mf?'Di atas batas.':'Di bawah batas.'}`;
  let bars='<p class="note">Menghitung penjelasan.</p>';
  if(phi){
    const D0=-0.06,D1=0.2, px=v=>((Math.max(D0,Math.min(D1,v))-D0)/(D1-D0)*100);
    bars='<div class="contrib">'+[0,1,2,3].sort((a,b)=>phi[b]-phi[a]).map(i=>{
      const v=phi[i], z=px(0), e=px(v), l=Math.min(z,e), w=Math.abs(e-z);
      return `<div class="crow"><span class="cl">${esc(FEAT_LABEL[i])} <span>· ${esc(featText(c,i))}</span></span><span class="cv">${v>=0?'+':'−'}${dec(Math.abs(v),3)}</span>
        <div class="track"><i class="bar${v<0?' neg':''}" style="left:${l.toFixed(2)}%;width:${w.toFixed(2)}%"></i><i class="zero" style="left:${z.toFixed(2)}%"></i></div></div>`;
    }).join('')+`</div><p class="note">Batang ke kanan menaikkan skor, ke kiri menurunkannya. Jumlahnya sama dengan selisih antara rata-rata klaim (${sc(base)}) dan klaim ini (${sc(c.score)}).</p>`;
  }
  const {reasons,next}=reasonsFor(c);
  const rv=c.review, draft=S.draft&&S.draft.id===c.id?S.draft:{status:rv?rv.status:'',note:rv?rv.note:'',resp:rv?rv.rsResponse||'':''};
  setPanel($('why'),detailHtml({
    panelId:'why',
    ids:{form:'reviewForm',note:'reviewNote',resp:'rsResponse',reopen:'reopen',radio:'status'},
    tag:S.batch.labelled?`<span class="tag">Label sintetis · ${c.label?LABELS[c.label]||esc(c.label):'klaim biasa'}</span>`:'',
    st:c.st, status:STATUS[c.st], sep:c.sep, verdict, hero:`${esc(c.item)} · <span class="num">${esc(c.final||c.code)}</span>`,
    reasonsTitle:c.st==='clear'?'Penjelasan':'Mengapa ditandai', reasons, next,
    rv, draft, labels:REVIEW,
    opts:[['confirmed','Temuan dikonfirmasi','top-up tidak didukung'],['cleared','Dinyatakan wajar','klaim sesuai'],['docs_requested','Dokumen diminta','menunggu jawaban rumah sakit']],
    notePh:'Apa yang diperiksa, dan apa yang ditemukan', respPh:'Dokumen yang dikirim RS dan penjelasannya, misalnya faktur implan no. ...',
    facts:[
      ['Rumah sakit',`${esc(c.rs)} · Kelas ${esc(c.kelasRs||'?')} · ${esc(c.kodeTarif)}`],
      ['Masuk – pulang',`${esc(c.admTxt)} – ${esc(c.disTxt)} · LOS ${c.los}`],
      ['INA-CBG',`<span class="num">${esc(c.cbg)}</span> ${esc(c.cbgDesc||'')}`],
      ['Top-up · '+esc(KIND[c.kind]||c.kind),`<span class="num">${esc(c.final||c.code)}</span> ${esc(c.item)}`],
      ['Diagnosa · prosedur',`${esc(c.diag||'-')} · ${esc(c.proc||'-')}`,'num'],
      ['Token kartu',esc(c.token||'-'),'num'],
      ['TARIF_RS',rp(c.tarifRs),'num'],
      ['Top-up'+(c.total?' · total tarif':''),rp(c.topup)+(c.total?' · '+rp(c.total):''),'num']
    ],
    stages:[['Tahap 2 · Aturan tagihan',`<p>${s2}</p>`],['Tahap 3 · Lapisan anomali',`<p>${s3}</p>`],['Tahap 4 · Apa yang menaikkan skor',bars]],
    open:S.foldOpen}));
}
/* formulir digambar ulang bersama panel, jadi dengarkan pada panel */
$('why').addEventListener('input',e=>{
  const f=e.target.closest('#reviewForm'); if(!f||!S.sel) return;
  const st=f.querySelector('input[name=status]:checked');
  S.draft={id:S.sel.id,status:st?st.value:'',note:$('reviewNote').value,resp:$('rsResponse').value};
  syncResp(f);
});
$('why').addEventListener('toggle',e=>{if(e.target.classList&&e.target.classList.contains('fold')) S.foldOpen=e.target.open},true);
async function saveReview(status,note,resp,go){
  const c=S.sel;
  try{
    const d=await api('PUT','/api/claims/'+c.id+'/review',{status,note,rsResponse:resp||''});
    Object.assign(c,d,{full:true}); S.draft=null;
    const b=S.batches.find(x=>x.id===S.batch.id); if(b) b.stale=true;
    update();
    if(!status){toast('Klaim dibuka ulang.'); return}
    tick('decide');
    const next=go?nextAfter(queueList(),c):null;
    if(next){toast(`Keputusan tersimpan: ${REVIEW[status]}. Klaim berikutnya dibuka.`); selectClaim(next,false,true)}
    else{toast(go?`Keputusan tersimpan: ${REVIEW[status]}. Tidak ada klaim lain yang menunggu.`:`Keputusan tersimpan: ${REVIEW[status]}.`); closeSheet(); if(!MOBILE.matches) focusDetail($('why'))}
  }catch(e){fail(e)}
}
$('why').addEventListener('submit',e=>{
  if(e.target.id!=='reviewForm') return;
  e.preventDefault();
  const st=e.target.querySelector('input[name=status]:checked');
  if(!st){toast('Pilih keputusan terlebih dahulu.',true);return}
  saveReview(st.value,$('reviewNote').value,$('rsResponse').value,!!(e.submitter&&e.submitter.value==='next'));
});
$('why').addEventListener('click',e=>{if(e.target.id==='reopen') saveReview(null,'',''); if(e.target.closest('[data-act=close]')) closeSheet()});

function update(){
  if(!S.batch) return;
  classify(); const n=counts(); S.cnt=n;
  $('thrOut').textContent=S.thr+'%'; $('kOut').textContent=S.k;
  renderHead(); renderPipe(n); renderKpis(n); renderLegend(n); renderUnsaved(); drawChart(); renderRows(); renderWhy(); renderRs(); renderCodes(); renderTour();
}
$('thr').addEventListener('input',e=>{S.thr=+e.target.value;update()});
$('kk').addEventListener('input',e=>{S.k=+e.target.value;tick('capacity');update()});
$('resetSettings').addEventListener('click',()=>{S.thr=S.batch.thresholdPct;S.k=S.batch.topK;$('thr').value=S.thr;$('kk').value=S.k;update()});
$('saveSettings').addEventListener('click',async()=>{
  try{
    S.batch=await api('PATCH','/api/batches/'+S.batch.id,{thresholdPct:S.thr,topK:S.k});
    S.thr=S.batch.thresholdPct; S.k=S.batch.topK; update(); toast('Pengaturan batch ini tersimpan.'); tick('capacity');
  }catch(e){fail(e)}
});
let rz; window.addEventListener('resize',()=>{clearTimeout(rz);rz=setTimeout(()=>{drawChart();drawRobust()},120)});

/* ---------- halaman berkas (top-up dan koding) ---------- */
const SAMPLE={topup:'/assets/sample/veriklaim-sample-eklaim.txt',coding:'/assets/sample/veriklaim-sample-koding.txt'};
let UP_KIND='topup';
function upWarnText(kind){
  if(!(S.user&&S.user.guest)) return UP_WARN;
  return `Unggah hanya berkas uji sintetis; jangan unggah data peserta JKN riil. Satu berkas diperiksa paling banyak ${dots(S.config.maxClaims)} klaim${kind==='topup'?' ber-top-up':''}. Server tidak membaca nama pasien, nomor RM, atau tanggal lahir; ${kind==='topup'?'nomor kartu disimpan hanya sebagai token satu arah':'nomor kartu tidak dibaca'}; berkas mentah tidak disimpan.`;
}
function setKind(k){
  UP_KIND=k;
  document.querySelectorAll('#upKind button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.kind===k)));
  $('upSample').href=SAMPLE[k]; $('upHint').hidden=k!=='coding'; $('upMsg').textContent='';
  $('upBtn').textContent=k==='coding'?'Unggah dan periksa':'Unggah dan saring';
  $('upWarn').textContent=upWarnText(k);
}
$('upKind').addEventListener('click',e=>{const b=e.target.closest('button[data-kind]'); if(b) setKind(b.dataset.kind)});
async function openBatches(){
  await loadBatches();
  const coding=await api('GET','/api/coding/batches');
  setKind(UP_KIND);
  const sup=S.user.role==='supervisor'||!!S.user.guest;
  const tag=t=>`<span class="tag kind">${t}</span>`;
  const rows=S.batches.map(b=>({kind:'topup',b})).concat(coding.map(b=>({kind:'coding',b}))).sort((x,y)=>String(y.b.uploadedAt).localeCompare(String(x.b.uploadedAt)));
  $('batchRows').innerHTML=rows.map(({kind,b})=>{
    const btn=`<button type="button" class="btn small" data-open="${b.id}" data-kind="${kind}" aria-label="Buka berkas ${esc(b.name)}">Buka</button>${sup?` <button type="button" class="btn small danger" data-del="${b.id}" data-kind="${kind}" aria-label="Hapus berkas ${esc(b.name)}">Hapus</button>`:''}`;
    const who=`${esc(when(b.uploadedAt))}<span class="sm">oleh ${esc(b.uploadedBy)}</span>`;
    if(kind==='coding'){
      return `<tr>
      <td><b>${esc(b.name)}</b> ${tag('Koding')}<span class="sm">${esc(b.filename)}${b.labelled?' · sampel sintetis':''}</span></td>
      <td>${who}</td>
      <td><span class="num">${dots(b.rowsRead)}</span> klaim dari ${b.hospitalsN} RS<span class="sm">${dots(b.compared)} klaim II/III dibandingkan${b.bad?`; ${dots(b.bad)} tidak terbaca atau ganda`:''}</span></td>
      <td><span class="num">${b.queued}</span> di antrean<span class="sm">${b.reviewed} diperiksa · ${b.topK} teratas</span></td>
      <td class="act">${btn}</td></tr>`;
    }
    const skipped=[b.noTopup?`${dots(b.noTopup)} tanpa top-up`:'',b.noRef?`${dots(b.noRef)} tanpa harga acuan`:'',b.bad?`${dots(b.bad)} tidak terbaca atau ganda`:''].filter(Boolean).join(', ');
    return `<tr>
      <td><b>${esc(b.name)}</b> ${tag('Top-up')}<span class="sm">${esc(b.filename)}${b.labelled?' · sampel sintetis':''}</span></td>
      <td>${who}</td>
      <td><span class="num">${dots(b.rowsScreened)}</span> disaring dari <span class="num">${dots(b.rowsTotal)}</span>${skipped?`<span class="sm">dilewati: ${skipped}</span>`:''}</td>
      <td><span class="num">${b.flagged}</span> di antrean<span class="sm">${b.reviewed} diperiksa · tagihan &lt; ${b.thresholdPct}%, ${b.topK} teratas</span></td>
      <td class="act">${btn}</td></tr>`;
  }).join('')||'<tr><td colspan="5">Belum ada batch. Unggah berkas TXT E-Klaim untuk membuat yang pertama.</td></tr>';
}
$('batchRows').addEventListener('click',async e=>{
  const o=e.target.closest('[data-open]'), d=e.target.closest('[data-del]');
  if(o){
    if(o.dataset.kind==='coding'){window.VKCoding.focus(+o.dataset.open); location.hash='#/coding'; return}
    S.batch=null; await loadBatch(+o.dataset.open).catch(fail); location.hash='#/queue'; return;
  }
  if(d){
    const id=+d.dataset.del, coding=d.dataset.kind==='coding';
    const b=(coding?await api('GET','/api/coding/batches').catch(()=>[]):S.batches).find(x=>x.id===id);
    if(!b||!window.confirm(`Hapus "${b.name}" beserta ${b.reviewed} keputusan yang tercatat? Tindakan ini tidak bisa dibatalkan.`)) return;
    try{
      await api('DELETE',(coding?'/api/coding/batches/':'/api/batches/')+id);
      if(coding) window.VKCoding.forget(id);
      else if(S.batch&&S.batch.id===id){S.batch=null;S.claims=[];S.sel=null}
      toast('Batch dihapus.'); await openBatches();
    }catch(err){fail(err)}
  }
});
$('uploadForm').addEventListener('submit',async e=>{
  e.preventDefault();
  const f=$('upFile').files[0], msg=$('upMsg'), btn=$('upBtn'), coding=UP_KIND==='coding';
  if(!f){msg.textContent='Pilih berkas terlebih dahulu.';return}
  if(S.config&&f.size>S.config.maxUploadMb*1024*1024){msg.textContent=`Berkas lebih besar dari ${S.config.maxUploadMb} MB, batas untuk prototype ini.`;return}
  const fd=new FormData(); fd.append('file',f); fd.append('name',$('upName').value);
  btn.disabled=true; msg.textContent=coding?'Mengunggah dan memeriksa. Ini bisa memakan beberapa detik.':'Mengunggah dan menyaring. Ini bisa memakan beberapa detik.';
  try{
    const b=await api('POST',coding?'/api/coding/batches':'/api/batches',fd);
    e.target.reset(); tick('upload');
    if(coding){
      msg.textContent=`${dots(b.rowsRead)} klaim dibaca, ${dots(b.compared)} klaim II/III dibandingkan. ${b.queued} masuk antrean.`;
      window.VKCoding.focus(b.id); location.hash='#/coding';
    }else{
      msg.textContent=`${dots(b.rowsScreened)} dari ${dots(b.rowsTotal)} baris disaring. ${b.flagged} klaim masuk antrean.`;
      S.batch=null; await loadBatches(); await loadBatch(b.id); location.hash='#/queue';
    }
  }catch(err){if(!err.auth) msg.textContent=err.message+' Tidak ada yang disimpan.'}
  finally{btn.disabled=false}
});

/* ---------- harga acuan ---------- */
function setRefNote(list){
  S.refPlaceholder=list.some(r=>/^(placeholder|contoh)/i.test(r.source||''));
  $('refNote').hidden=!S.refPlaceholder; renderBanner();
}
async function refreshRefNote(){
  try{setRefNote(await api('GET','/api/reference-prices'))}catch(_){$('refNote').hidden=true; S.refPlaceholder=false; renderBanner()}
}
async function openRefs(){
  S.refs=await api('GET','/api/reference-prices'); setRefNote(S.refs);
  const sup=S.user.role==='supervisor';
  $('refRows').innerHTML=S.refs.map(r=>`<tr>
    <td class="num">${esc(r.code)}</td><td>${esc(r.description)}</td><td>${esc(r.type)}</td>
    <td class="r num">${rp(r.price)}</td><td>${esc(r.source||'-')}</td>
    <td>${esc(when(r.updatedAt))}<span class="sm">oleh ${esc(r.updatedBy)}</span></td>
    <td class="act">${sup?`<button type="button" class="btn small ghost" data-edit="${esc(r.code)}">Ubah</button> <button type="button" class="btn small danger" data-del="${esc(r.code)}">Hapus</button>`:''}</td></tr>`).join('')
    ||'<tr><td colspan="7">Belum ada harga acuan. Tanpa harga acuan, tidak ada klaim yang bisa disaring.</td></tr>';
  try{await openCmg(sup)}catch(_){$('cmgPanel').hidden=true}
}
async function openCmg(sup){
  const d=await api('GET','/api/special-cmg'); S.cmg=d.codes;
  $('cmgSub').textContent=`${d.priced} dari ${d.total} kode punya harga acuan. Harga bawaan prototype masih contoh ilustratif. Klaim dengan kode lain dilewati saat penyaringan sampai supervisor mengisi harganya. Daftar mengikuti ${d.source}.`;
  $('cmgRows').innerHTML=d.codes.map(c=>`<tr>
    <td class="num">${esc(c.code)}</td><td>${esc(c.description)}</td><td>${esc(c.type)}</td>
    <td>${c.priced?`Ada · ${esc(rp(c.price))}`:'<b>Belum ada harga acuan</b>'}</td>
    <td class="act">${sup&&!c.priced?`<button type="button" class="btn small ghost" data-fill="${esc(c.code)}">Isi harga</button>`:''}</td></tr>`).join('');
  $('cmgPanel').hidden=false;
}
$('cmgRows').addEventListener('click',e=>{
  const b=e.target.closest('[data-fill]'); if(!b) return;
  const c=(S.cmg||[]).find(x=>x.code===b.dataset.fill); if(!c) return;
  $('refCode').value=c.code; $('refKind').value=c.kind; $('refDesc').value=c.description; $('refPrice').value=''; $('refSource').value='';
  $('refMsg').textContent='Mengisi '+c.code+'. Masukkan harga dan sumbernya.'; $('refForm').scrollIntoView({behavior:'smooth',block:'center'}); $('refPrice').focus();
});
$('refRows').addEventListener('click',async e=>{
  const ed=e.target.closest('[data-edit]'), del=e.target.closest('[data-del]');
  if(ed){
    const r=S.refs.find(x=>x.code===ed.dataset.edit); if(!r) return;
    $('refCode').value=r.code; $('refKind').value=r.kind; $('refPrice').value=r.price; $('refSource').value=r.source; $('refDesc').value=r.description;
    $('refMsg').textContent='Mengubah '+r.code+'.'; $('refPrice').focus();
  }
  if(del){
    if(!window.confirm(`Hapus harga acuan ${del.dataset.del}? Berkas yang diunggah sesudahnya akan melewati klaim dengan top-up ini.`)) return;
    try{await api('DELETE','/api/reference-prices/'+encodeURIComponent(del.dataset.del)); toast('Harga acuan dihapus.'); await openRefs()}catch(err){fail(err)}
  }
});
$('refForm').addEventListener('submit',async e=>{
  e.preventDefault();
  const code=$('refCode').value.trim().toUpperCase();
  try{
    await api('PUT','/api/reference-prices/'+encodeURIComponent(code),{kind:$('refKind').value,description:$('refDesc').value,price:Math.round(+$('refPrice').value),source:$('refSource').value});
    e.target.reset(); $('refMsg').textContent=code+' tersimpan. Berlaku untuk berkas yang diunggah mulai sekarang.'; await openRefs();
  }catch(err){if(!err.auth) $('refMsg').textContent=err.message}
});
$('refForm').addEventListener('reset',()=>{$('refMsg').textContent=''});

/* ---------- ketahanan ---------- */
const RB_COLOR=['var(--accent)','var(--c-model)','var(--c-rule-line)','var(--ink)'];
const pct=v=>one(v*100)+'%';
function rbMark(i,cx,cy,col){
  if(i===0) return `<circle cx="${cx}" cy="${cy}" r="4.5" fill="${col}" stroke="var(--surface)" stroke-width="1.5"/>`;
  if(i===1) return `<rect x="${cx-4.5}" y="${cy-4.5}" width="9" height="9" fill="${col}" stroke="var(--surface)" stroke-width="1.5"/>`;
  if(i===2) return `<path d="M${cx},${cy-6} L${cx+5.5},${cy+4.5} L${cx-5.5},${cy+4.5} Z" fill="${col}" stroke="var(--surface)" stroke-width="1.5" stroke-linejoin="round"/>`;
  return `<path d="M${cx},${cy-6} L${cx+6},${cy} L${cx},${cy+6} L${cx-6},${cy} Z" fill="${col}" stroke="var(--surface)" stroke-width="1.5" stroke-linejoin="round"/>`;
}
async function openRobust(){
  if(!S.rb) S.rb=await api('GET','/api/robustness');
  const d=S.rb, F=d.fractions, i5=F.indexOf(0.05), b=d.benchmark;
  $('rbN').textContent=dots(d.n); $('rbShare').textContent=Math.round(d.legitRepeaterShare*100)+'%'; $('rbSeeds').textContent=d.seeds;
  $('rbMachine').textContent=b.machine; $('rbMax').textContent=dots(S.config.maxClaims);
  $('rbLegend').innerHTML=d.levels.map((l,i)=>`<span><svg class="mk" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">${rbMark(i,8,8,RB_COLOR[i])}</svg><b>${esc(l.id)}</b> ${esc(l.description)}</span>`).join('')
    +'<span><i class="sw dash" aria-hidden="true"></i>Pemeriksaan acak</span>';
  $('rbRows').innerHTML=d.levels.map(l=>{
    const rec=l.recall[i5], pr=l.precision[i5];
    return `<tr><td><b>${esc(l.id)}</b> · ${esc(l.description)}<span class="sm">pola ${pct(l.baseRate)} dari seluruh klaim</span></td>
      <td class="r num">${pct(rec)}</td><td class="r num">${pct(pr)}</td><td class="r num">${one(pr/l.baseRate)}×</td><td class="r num">${pct(l.legitInQueue[i5])}</td></tr>`;
  }).join('')+`<tr><td><b>Pemeriksaan acak</b><span class="sm">beban yang sama</span></td><td class="r num">5%</td><td class="r num">= proporsi pola</td><td class="r num">1×</td><td class="r num">5%</td></tr>`;
  $('rbBench').innerHTML=b.runs.map(r=>[`${dec(r.seconds,1)} detik`,`${dots(r.rows)} baris berkas${r.topupShare<1?', '+Math.round(r.topupShare*100)+'% ber-top-up':', semuanya top-up'}`,`memori puncak ${dec(r.peakGb,2).replace(/0$/,'')} GB`])
    .concat([[dots(S.config.maxClaims),'klaim per unggahan pada demo ini','batas karena server gratis']])
    .map(k=>`<div class="kpi"><div class="v">${k[0]}</div><div class="l">${k[1]}<br>${k[2]}</div></div>`).join('');
  drawRobust();
}
function drawRobust(){
  if(S.route!=='robust'||!S.rb) return;
  const d=S.rb, F=d.fractions, box=$('rbChart'), W=Math.max(300,box.clientWidth-16), H=W<520?300:380, m={l:46,r:18,t:34,b:46};
  const xmax=Math.max(...F), x=f=>m.l+f/xmax*(W-m.l-m.r), y=r=>H-m.b-r*(H-m.t-m.b);
  let g='';
  [0,.25,.5,.75,1].forEach(t=>{g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--rule)"/><text x="${m.l-8}" y="${y(t)+4}" text-anchor="end">${Math.round(t*100)}%</text>`});
  [0,.05,.1,.15,.2].filter(t=>t<=xmax+1e-9).forEach(t=>{g+=`<line x1="${x(t)}" x2="${x(t)}" y1="${H-m.b}" y2="${H-m.b+5}" stroke="var(--rule-strong)"/><text x="${x(t)}" y="${H-m.b+18}" text-anchor="middle">${Math.round(t*100)}%</text>`});
  g+=`<line x1="${m.l}" x2="${W-m.r}" y1="${H-m.b}" y2="${H-m.b}" stroke="var(--rule-strong)"/>`;
  g+=`<text class="axt" x="${(m.l+W-m.r)/2}" y="${H-6}" text-anchor="middle">${W<520?'Klaim top-up yang diperiksa':'Beban pemeriksaan: porsi klaim top-up yang diperiksa'}</text>`;
  g+=`<text class="axt" x="${m.l-34}" y="${m.t-18}" text-anchor="start">Pola bermasalah yang ikut diperiksa</text>`;
  g+=`<line x1="${x(.05)}" x2="${x(.05)}" y1="${m.t}" y2="${H-m.b}" stroke="var(--c-rule-line)" stroke-width="2" stroke-dasharray="5 4"/><text class="thl" x="${x(.05)+6}" y="${y(.62)}">beban 5%</text>`;
  g+=`<line x1="${x(0)}" y1="${y(0)}" x2="${x(xmax)}" y2="${y(xmax)}" stroke="var(--ink-3)" stroke-width="2" stroke-dasharray="6 5"/>`;
  d.levels.forEach((l,i)=>{
    const pts=[[0,0]].concat(F.map((f,j)=>[f,l.recall[j]]));
    g+=`<polyline fill="none" stroke="${RB_COLOR[i]}" stroke-width="2.5" stroke-linejoin="round" points="${pts.map(p=>x(p[0]).toFixed(1)+','+y(p[1]).toFixed(1)).join(' ')}"/>`;
    pts.slice(1).forEach(p=>{g+=rbMark(i,+x(p[0]).toFixed(1),+y(p[1]).toFixed(1),RB_COLOR[i])});
  });
  const i5=F.indexOf(0.05), sum=d.levels.map(l=>`${l.id} ${pct(l.recall[i5])}`).join(', ');
  const old=box.querySelector('svg'); if(old) old.remove();
  box.insertAdjacentHTML('afterbegin',`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Porsi pola bermasalah yang masuk antrean menurut beban pemeriksaan. Pada beban 5%: ${sum}; pemeriksaan acak 5%. Tabel di bawah memuat angka yang sama.">${g}</svg>`);
}

/* ---------- jejak audit ---------- */
function detailText(a){
  let d; try{d=a.detail?JSON.parse(a.detail):null}catch(_){return a.detail}
  if(!d) return '';
  const kv=o=>Object.keys(o).map(k=>`${k}: ${o[k]===null?'-':typeof o[k]==='object'?JSON.stringify(o[k]):o[k]}`).join(', ');
  if(a.action==='review_set'||a.action==='review_reopened') return `${d.from?REVIEW[d.from]:'belum diperiksa'} → ${d.to?REVIEW[d.to]:'belum diperiksa'}${d.note?' · “'+d.note+'”':''}${d.rsResponse?' · tanggapan RS: “'+d.rsResponse+'”':''}`;
  if(a.action==='coding_review_set'||a.action==='coding_review_reopened') return `${d.from?CREV[d.from]:'belum diperiksa'} → ${d.to?CREV[d.to]:'belum diperiksa'}${d.note?' · “'+d.note+'”':''}${d.rsResponse?' · tanggapan RS: “'+d.rsResponse+'”':''}`;
  if(a.action==='coding_settings') return `${d.from.topK} teratas → ${d.to.topK} teratas`;
  if(a.action==='batch_settings') return `${d.from.thresholdPct}%, ${d.from.topK} teratas → ${d.to.thresholdPct}%, ${d.to.topK} teratas`;
  return kv(d);
}
async function openAudit(){
  const rows=await api('GET','/api/audit?limit=300');
  $('auditRows').innerHTML=rows.map(a=>`<tr>
    <td class="nw">${esc(when(a.at))}</td><td>${esc(a.username)}${a.role?`<span class="sm">${esc(ROLE[a.role]||a.role)}</span>`:''}</td>
    <td>${esc(ACTIONS[a.action]||a.action)}</td><td class="num">${a.batchId==null?'':a.batchId}</td>
    <td class="num">${esc(a.sep||'')}</td><td class="wrap">${esc(detailText(a))}</td></tr>`).join('')
    ||'<tr><td colspan="6">Belum ada catatan.</td></tr>';
}

/* ---------- panduan juri ---------- */
const TOUR=[
  ['example','Buka klaim contoh di slide 11 (peringkat 2) dan bandingkan dengan deck','Buka',openExample],
  ['reasons','Baca alasan, kontribusi skor, dan langkah berikut untuk satu klaim'],
  ['decide','Catat keputusan, misalnya "Dokumen diminta" beserta tanggapan RS'],
  ['capacity','Geser kapasitas verifikator dan lihat isi antrean berubah','Ke pengaturan',()=>{tabs.set('queue'); $('ctlFold').open=true; $('kk').scrollIntoView({behavior:'smooth',block:'center'})}],
  ['rs','Lihat tingkat flag per rumah sakit dan per kode (pemantauan bias)','Lihat',()=>{tabs.set('bias'); $('tabs').scrollIntoView({behavior:'smooth',block:'start'})}],
  ['robust','Buka halaman Ketahanan: kurva pada 2.000 klaim, termasuk pengulang yang sah (slide 13)','Buka',()=>{location.hash='#/robust'}],
  ['audit','Buka jejak audit: setiap tindakan Anda tercatat','Buka',()=>{location.hash='#/audit'}],
  ['upload','Opsional: unggah berkas contoh E-Klaim di halaman Berkas','Ke Berkas',()=>{location.hash='#/batches'}],
  ['feedback','Beri umpan balik, satu menit','Isi',()=>{location.hash='#/feedback'}]
];
S.tour={}; S.tourHidden=false;
async function openExample(){
  if(!S.batch||!S.batch.labelled){
    const sample=S.batches.find(b=>b.labelled);
    if(!sample){toast('Sampel sintetis sudah dihapus dari sesi ini. Mulai sesi juri baru untuk mendapatkannya lagi.',true);return}
    await loadBatch(sample.id).catch(fail);
  }
  const c=S.claims.find(x=>x.rank===2); if(c){tabs.set('queue'); selectClaim(c,true)}
}
const tourKey=()=>'vk-tour-'+(S.user?S.user.username:'');
function loadTour(){
  S.tour={}; S.tourHidden=false;
  let saved=false;
  try{const v=JSON.parse(sessionStorage.getItem(tourKey())||'null'); if(v){saved=true; S.tour=v.t||{}; S.tourHidden=!!v.h}}catch(_){}
  if(!saved) S.tourHidden=true;
  renderTour();
}
function saveTour(){try{sessionStorage.setItem(tourKey(),JSON.stringify({t:S.tour,h:S.tourHidden}))}catch(_){}}
function tick(id){if(!S.user||!S.user.guest||S.tour[id]) return; S.tour[id]=1; saveTour(); renderTour()}
function renderTour(){
  const box=$('tour'), show=!!(S.user&&S.user.guest&&S.batch);
  box.hidden=!show; if(!show) return;
  box.classList.toggle('min',S.tourHidden);
  const done=TOUR.filter(t=>S.tour[t[0]]).length;
  if(S.tourHidden){
    box.innerHTML=`<div class="thead"><h2 id="tourTitle">Panduan juri</h2><span class="prog">${done} dari ${TOUR.length} langkah · <button type="button" class="go" data-tour="show" aria-label="Buka panduan juri">Buka</button></span></div>`;
    return;
  }
  box.innerHTML=`<div class="thead"><h2 id="tourTitle">Panduan juri: ${TOUR.length} langkah, sekitar 5 menit</h2>
    <span class="prog">${done} dari ${TOUR.length} selesai · <button type="button" class="go" data-tour="hide">Sembunyikan</button></span></div>
    <ol>${TOUR.map((t,i)=>`<li class="${S.tour[t[0]]?'done':''}"><span class="ck" aria-hidden="true">${S.tour[t[0]]?'✓':i+1}</span><span class="tx">${esc(t[1])}${t[2]?` <button type="button" class="go" data-step="${t[0]}">${esc(t[2])}</button>`:''}${S.tour[t[0]]?'<span class="sr"> (selesai)</span>':''}</span></li>`).join('')}</ol>
    <p class="meta">Data sintetis berformat E-Klaim. Sesi ini milik Anda sendiri dan dihapus otomatis ${esc(when(S.user.expiresAt))}.</p>`;
}
$('tour').addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return;
  if(b.dataset.tour){S.tourHidden=b.dataset.tour==='hide'; saveTour(); renderTour(); return}
  const t=TOUR.find(x=>x[0]===b.dataset.step); if(t&&t[3]) t[3]();
});


/* ---------- umpan balik ---------- */
async function openFeedback(){
  const sup=S.user.role==='supervisor';
  if(S.user.guest&&!$('fbPeran').value) $('fbPeran').value='juri';
  $('usageBox').hidden=!sup;
  if(!sup) return;
  const [u,rows]=await Promise.all([api('GET','/api/usage'),api('GET','/api/feedback')]);
  $('usageKpis').innerHTML=[
    [`${u.guestSessions}`,'sesi juri / tamu',`${u.guestSessions7d} dalam 7 hari terakhir · ${u.activeGuests} masih aktif`],
    [`${u.decisions}`,'keputusan tercatat',`${u.guestDecisions} oleh tamu · ${u.uploads} unggahan · ${u.exports} ekspor`],
    [`${u.feedback}`,'umpan balik',u.avgUseful==null?'belum ada nilai':`rata-rata kegunaan <b>${dec(u.avgUseful,1)}</b> dari 5`],
    [`${u.distinctAccounts}`,'akun berbeda yang pernah masuk','termasuk sesi juri']
  ].map(k=>`<div class="kpi"><div class="v">${k[0]}</div><div class="l">${k[1]}<br>${k[2]}</div></div>`).join('');
  $('fbRows').innerHTML=rows.map(f=>`<tr><td class="nw">${esc(when(f.at))}<span class="sm">${esc(f.username)}</span></td><td>${esc(PERAN[f.peran]||f.peran)}</td><td>${esc(f.institusi||'-')}</td>
    <td class="r num">${f.useful}</td><td>${esc(f.wouldUse)}</td><td class="wrap">${esc(f.comment||'-')}</td></tr>`).join('')
    ||'<tr><td colspan="6">Belum ada umpan balik.</td></tr>';
}
$('fbForm').addEventListener('submit',async e=>{
  e.preventDefault();
  const useful=e.target.querySelector('input[name=fbUseful]:checked'), use=e.target.querySelector('input[name=fbUse]:checked');
  const btn=$('fbBtn'); btn.disabled=true; $('fbMsg').textContent='';
  try{
    await api('POST','/api/feedback',{peran:$('fbPeran').value,institusi:$('fbInst').value,useful:+useful.value,wouldUse:use.value,comment:$('fbComment').value});
    e.target.reset(); $('fbMsg').textContent='Terima kasih. Umpan balik Anda tersimpan.'; tick('feedback');
    if(S.user.role==='supervisor') await openFeedback();
  }catch(err){if(!err.auth) $('fbMsg').textContent=err.message}
  finally{btn.disabled=false}
});

/* ---------- untuk halaman Koding (coding.js) ---------- */
window.VK={S,$,api,esc,dots,dec,rp,rpShort,one,times,sc,when,toast,fail,icon,openSheet,closeSheet,tourTick:tick,stripHtml,cardHtml,detailHtml,syncResp,bindTabs,listKeys,nextAfter,setPanel,focusDetail,PAGE};

/* ---------- mulai ---------- */
async function boot(){
  const p=$('boot').querySelector('p'), retry=$('bootRetry');
  retry.hidden=true; p.textContent='Memuat VeriKlaim.';
  const slow=setTimeout(()=>{p.textContent='Membangunkan server. Hosting gratis tidur saat sepi dan bisa butuh hingga satu menit untuk menyala.'},2500);
  try{
    S.config=await api('GET','/api/config',undefined,{quiet401:true});
    try{S.user=await api('GET','/api/me',undefined,{quiet401:true})}
    catch(e){if(e.status!==401) throw e; S.user=null}
    clearTimeout(slow);
    if(S.user) await enter(); else showLogin('');
  }catch(e){
    clearTimeout(slow); showOnly('boot'); p.textContent=e.message; retry.hidden=false;
  }
}
$('bootRetry').addEventListener('click',boot);
if(document.fonts&&document.fonts.ready) document.fonts.ready.then(drawChart);
boot();
})();
