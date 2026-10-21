/* Halaman Koding: indikator upcoding. Penskoran berjalan di server; skrip ini hanya menampilkan. */
(function(){
'use strict';
const {S,$,api,esc,dots,dec,rp,rpShort,one,times,sc,when,toast,fail,icon,openSheet,closeSheet,stripHtml,cardHtml,detailHtml,syncResp,bindTabs,listKeys,nextAfter,setPanel,focusDetail}=window.VK;

const CREV={confirmed:'Upcoding dikonfirmasi',cleared:'Dinyatakan wajar',docs_requested:'Rekam medis diminta'};
const STATUS={both:'Kedua pemeriksaan',rule:'Aturan koding',model:'Lapisan anomali',clear:'Bersih'};
const FEAT=['RS: tingkat berat melebihi RS lain','Lama rawat lebih pendek dari lazim','Diagnosis sekunder melebihi perawatan','Kenaikan tarif karena tingkat'];
const DRIVER=['Pola rumah sakit','Lama rawat','Diagnosis sekunder','Kenaikan tarif'];
const VERDICT={tinjau:['Perlu ditinjau','flag'],didukung:['Didukung perawatan','ok'],wajar:['Dalam kisaran',''],sedikit:['Klaim terlalu sedikit','']};
const HLAB={strong:'upcoding kuat disisipkan',mild:'upcoding ringan disisipkan',referral:'rumah sakit rujukan, tingkat tinggi memang wajar',tiny:'terlalu sedikit klaim untuk dibandingkan',normal:'tanpa pola disisipkan'};
const roman=n=>['','I','II','III'][n]||String(n);
const pct=v=>Math.round(v*100)+'%';

const PAGE=20, HOSP_PAGE=8, MQ=window.matchMedia('(max-width:980px)');
const K={batches:[],batch:null,claims:[],byId:{},hospitals:[],hmap:{},k:12,view:'queue',rs:null,sel:null,draft:null,lo:0,hi:1,limit:PAGE,allHosp:false,want:null,tab:'queue',q:'',cnt:null};

function reset(){
  K.batches=[]; K.batch=null; K.claims=[]; K.byId={}; K.hospitals=[]; K.hmap={}; K.sel=null; K.draft=null; K.rs=null;
}

/* ---------- batch ---------- */
async function loadList(){
  K.batches=await api('GET','/api/coding/batches');
  $('kdSel').innerHTML=K.batches.map(b=>`<option value="${b.id}">${esc(b.name)}</option>`).join('');
  if(K.batch&&K.batches.some(b=>b.id===K.batch.id)) $('kdSel').value=K.batch.id;
}
async function open(){
  await loadList();
  const none=!K.batches.length;
  $('kdEmpty').hidden=!none; $('kdBody').hidden=none; $('kdSel').closest('.field').hidden=K.batches.length<2;
  $('kdSave').hidden=!(S.user.role==='supervisor'||!!S.user.guest);
  if(none){$('kdTitle').textContent='Pemeriksaan koding'; $('kdBar').textContent=''; return}
  const want=K.want; K.want=null;
  if(want&&K.batches.some(b=>b.id===want)) await loadBatch(want);
  else if(!K.batch||!K.batches.some(b=>b.id===K.batch.id)) await loadBatch(K.batches[0].id);
  else update();
}
async function loadBatch(id){
  const d=await api('GET','/api/coding/batches/'+id);
  K.batch=d.batch; K.claims=d.claims; K.hospitals=d.hospitals; K.byId={}; K.hmap={}; K.rs=null;
  K.claims.forEach(c=>{K.byId[c.id]=c}); K.hospitals.forEach(h=>{K.hmap[h.rs]=h});
  K.k=K.batch.topK; K.draft=null; K.view='queue'; K.limit=PAGE; K.allHosp=false; K.q=''; K.rs=null; $('kdSearch').value='';
  $('kdSel').value=id; $('kdK').max=K.batch.maxTopK; $('kdK').value=K.k;
  const sc2=K.claims.filter(c=>c.live).map(c=>c.score);
  K.hi=Math.max(...sc2,0.5); K.lo=Math.min(...sc2,K.hi-0.05);
  classify();
  K.sel=K.claims.find(c=>c.st!=='clear')||null;
  update();
  if(K.sel) loadDetail(K.sel);
}
$('kdSel').addEventListener('change',e=>loadBatch(+e.target.value).catch(fail));

function classify(){
  K.claims.forEach(c=>{c.rf=!!c.rule; c.mf=c.live&&c.rank<=K.k; c.st=c.rf&&c.mf?'both':c.rf?'rule':c.mf?'model':'clear'});
}
function counts(){
  const n={rule:0,modelOnly:0,flag:0,reviewed:0,uplift:0,caught:0,ruleCaught:0,extra:0,perRs:{}};
  K.claims.forEach(c=>{
    if(c.st==='clear') return;
    n.flag++; n.uplift+=c.uplift||0;
    n.perRs[c.rs]=(n.perRs[c.rs]||0)+1;
    if(c.rf) n.rule++; else n.modelOnly++;
    if(c.review) n.reviewed++;
    if(c.label){n.caught++; if(c.rf) n.ruleCaught++} else n.extra++;
  });
  return n;
}

/* ---------- halaman ---------- */
function renderHead(){
  const b=K.batch;
  $('kdTitle').textContent=b.name;
  const parts=[`<span><b>${dots(b.rowsRead)}</b> klaim dari ${b.hospitalsN} rumah sakit</span>`,`<span>diunggah oleh ${esc(b.uploadedBy)}, ${esc(when(b.uploadedAt))}</span>`];
  if(b.bad) parts.push(`<span>${dots(b.bad)} baris tidak terbaca atau ganda, dilewati</span>`);
  if(b.truncated) parts.push(`<span>hanya ${dots(b.rowsRead)} klaim pertama yang diperiksa</span>`);
  $('kdBar').innerHTML=parts.join('');
}
function renderPipe(n){
  const b=K.batch;
  $('kdPipe').innerHTML=[
    ['Tahap 1 · Baca TXT E-Klaim',`<span class="num">${dots(b.rowsRead)}</span> klaim`,`dari ${b.hospitalsN} rumah sakit`],
    ['Tahap 2 · Bandingkan dengan kasus sejenis',`<span class="num">${dots(b.compared)}</span> klaim II/III`,`pada ${b.groups} kelompok kasus yang cukup besar`],
    ['Tahap 3 · Aturan dan lapisan anomali',`<span class="num">${n.flag}</span> di antrean`,`${n.rule} lolos aturan koding, ${n.modelOnly} hanya lapisan anomali`],
    ['Tahap 4 · Alasan dan keputusan',`<span class="num">${n.reviewed}</span> dari ${n.flag}`,'sudah diperiksa']
  ].map(s=>`<li><span class="step">${s[0]}</span><span class="big">${s[1]}</span><span class="cap">${s[2]}</span></li>`).join('');
}
function renderKpis(n){
  const b=K.batch, share=b.compared?Math.round(n.flag/b.compared*100):0;
  $('kdKpis').innerHTML=stripHtml([
    [`${n.flag}<small> dari ${dots(b.compared)}</small>`,'klaim tingkat II/III perlu diperiksa'],
    [`${n.rule}`,'ditandai aturan koding'],
    [`+${n.modelOnly}`,'hanya ditandai lapisan anomali'],
    [`${n.reviewed}<small> dari ${n.flag}</small>`,'sudah diperiksa',n.flag?n.reviewed/n.flag:0]
  ]);
  $('kdValNote').innerHTML=`<b>Perkiraan selisih tarif.</b> Bila klaim di antrean diturunkan ke tingkat terendah, tarif INA-CBG-nya turun sekitar ${rpShort(n.uplift)}. Ini perkiraan, bukan kerugian terbukti. Antrean memuat ${share}% dari ${dots(b.compared)} klaim II/III yang dibandingkan. Aturan koding: diagnosis sekunder berlebih, tanpa ICU atau ventilator, rawat singkat.`;
  $('kdBanner').innerHTML=`Ini indikator upcoding, bukan penilaian kebutuhan medis.${b.labelled?' Data sintetis: hanya menunjukkan cara kerja, bukan kinerja pada klaim nyata.':''} <button type="button" class="go" data-goto-tab="about" aria-label="Rincian cara kerja dan batasan, di tab Gambaran">Rincian</button>`;

  const note=$('kdLabelNote'); note.hidden=!b.labelled;
  if(b.labelled){
    const rand=b.planted*n.flag/Math.max(1,b.compared);
    const strong=K.hospitals.filter(h=>h.label==='strong'), hit=strong.filter(h=>h.verdict==='tinjau').length;
    const ref=K.hospitals.filter(h=>h.label==='referral'), refHit=ref.filter(h=>h.verdict==='tinjau').length;
    note.innerHTML=`<b>Data sintetis.</b> ${b.planted} klaim disisipi upcoding: tingkatnya dinaikkan padahal perawatannya setara tingkat I. Dengan pengaturan ini, <b>${n.caught} dari ${b.planted}</b> masuk antrean (aturan koding saja: ${n.ruleCaught}), bersama ${n.extra} klaim lain. Sampel acak dengan beban yang sama (${n.flag} klaim) rata-rata hanya memuat <b>${dec(rand,1)}</b>. Pada tingkat rumah sakit, ${hit} dari ${strong.length} rumah sakit dengan upcoding kuat ditandai "Perlu ditinjau" dan ${refHit} dari ${ref.length} rumah sakit rujukan yang wajar ikut ditandai. Pola disisipkan oleh aturan yang sama dengan yang dipakai pemeriksaan, jadi ini menunjukkan kode bekerja sesuai rancangan, bukan perkiraan kinerja pada klaim nyata.`;
  }
  const small=$('kdSmallNote'); small.hidden=!b.smallBase;
  if(b.smallBase) small.innerHTML=`<b>Pembanding tipis.</b> Berkas ini hanya memuat ${b.hospitalsN} rumah sakit. Perbandingan dengan rumah sakit lain tidak diandalkan; pemeriksaan per klaim tetap berjalan. Unggah berkas dari lebih banyak rumah sakit untuk pola per rumah sakit yang berarti.`;
  const th=$('kdTabHosp'); th.hidden=!!b.smallBase||!K.hospitals.length;
  if(th.hidden&&K.tab==='hosp') tabs.set('queue');
}
function renderUnsaved(){
  const b=K.batch, dirty=K.k!==b.topK;
  $('kdUnsaved').hidden=!dirty;
  if(dirty) $('kdUnsavedText').textContent=`Anda sedang mencoba kapasitas lain. Kapasitas tersimpan untuk batch ini: ${b.topK} klaim.`+(S.user.role==='supervisor'||S.user.guest?'':' Hanya supervisor yang dapat menyimpan perubahan.');
  $('kdSum').textContent=`${K.k} klaim anomali`+(dirty?' · belum disimpan':'');
}
function renderHosp(n){
  const list=K.allHosp?K.hospitals:K.hospitals.slice(0,HOSP_PAGE);
  const more=$('kdHospMoreRow'); more.hidden=K.hospitals.length<=HOSP_PAGE;
  $('kdHospMore').textContent=K.allHosp?'Tampilkan lebih sedikit':`Tampilkan semua ${K.hospitals.length} rumah sakit`;
  const rows=list.map(h=>{
    const [vt,vc]=VERDICT[h.verdict]||['',''], q=n.perRs[h.rs]||0;
    const lab=K.batch.labelled&&h.label?`<span class="sm">Label sintetis: ${esc(HLAB[h.label]||h.label)}</span>`:'';
    const sup=h.support==null?'<span class="sm">terlalu sedikit klaim II/III untuk dinilai</span>':`<span class="num">${pct(h.support)}</span> klaim II/III didukung perawatan<span class="sm">median rumah sakit lain ${h.support_ref==null?'-':pct(h.support_ref)}</span>`;
    const ratio=h.n>=15&&h.ratio!=null?`${times(h.ratio)} harapan · z = ${dec(h.z,1)}`:'terlalu sedikit klaim untuk uji';
    return `<tr><td>RS ${esc(h.rs)}<span class="sm">Kelas ${esc(h.kelas||'?')} · ${h.n} klaim dibandingkan</span>${lab}</td>
      <td><span class="num">${h.high}</span> dikodekan II/III, harapan <span class="num">${dec(h.expected,1)}</span><span class="sm">${ratio}</span></td>
      <td>${sup}</td>
      <td><span class="tag${vc?' '+vc:''}">${vt}</span></td>
      <td class="r act">${q?`<span class="num">${q}</span> di antrean <button type="button" class="btn small ghost" data-rs="${esc(h.rs)}" aria-label="Lihat ${q} klaim RS ${esc(h.rs)} di antrean">Lihat</button>`:'<span class="num">0</span> di antrean'}</td></tr>`;
  }).join('');
  $('kdHosp').innerHTML=rows||'<tr><td colspan="5">Tidak ada rumah sakit dengan pembanding yang cukup di berkas ini.</td></tr>';
}
$('kdHospMore').addEventListener('click',()=>{K.allHosp=!K.allHosp; renderHosp(counts())});
$('kdHosp').addEventListener('click',e=>{
  const b=e.target.closest('[data-rs]'); if(!b) return;
  K.rs=b.dataset.rs; K.view='queue'; K.limit=PAGE; tabs.set('queue'); renderRows(); window.scrollTo({top:0,behavior:'smooth'});
});

function queueList(){
  const q=K.q.trim().toLowerCase();
  let list=K.claims.filter(c=>c.st!=='clear'&&(K.view==='queue'||!c.review)&&(!K.rs||c.rs===K.rs));
  if(q) list=list.filter(c=>`${c.sep} ${c.rs} ${c.grp} ${c.cbg} ${roman(c.sev)}`.toLowerCase().includes(q));
  return list.sort((a,b)=>a.rank-b.rank);
}
function renderSeg(){
  const n=K.cnt; if(!n) return;
  const t={queue:['Di antrean',n.flag],todo:['Belum diperiksa',n.flag-n.reviewed]};
  document.querySelectorAll('#kdSeg button[data-kview]').forEach(b=>{const x=t[b.dataset.kview]; b.innerHTML=`${x[0]}<span class="cnt">${dots(x[1])}</span>`; b.setAttribute('aria-pressed',String(b.dataset.kview===K.view))});
}
function renderRows(){
  let list=queueList();
  const total=list.length; if(total>K.limit) list=list.slice(0,K.limit);
  renderSeg();
  const q=K.q.trim();
  $('kdMoreRow').hidden=total<=K.limit; $('kdMore').textContent=`Tampilkan ${Math.min(PAGE,total-K.limit)} lagi (sisa ${dots(total-K.limit)})`;
  $('kdLede').innerHTML=total?`${dots(total)} klaim${q?` cocok dengan “${esc(q)}”`:''}${K.rs?`, hanya RS ${esc(K.rs)}`:''}, diurutkan menurut skor anomali. Pilih satu untuk melihat alasannya.`+(K.rs?` <button type="button" class="go" id="kdClearRs">Tampilkan semua rumah sakit</button>`:''):(K.rs?`<button type="button" class="go" id="kdClearRs">Tampilkan semua rumah sakit</button>`:'');
  $('kdRows').innerHTML=list.map(c=>{
    const d=c.mf&&c.phi?topDriver(c):-1;
    const reason=d>=0?`${DRIVER[d]}: ${d===0&&c.hr!=null?pct(c.hr)+' didukung perawatan':featText(c,d)}`:c.rf?`${c.nSec} diagnosis sekunder, lazim ${one(c.careDx)}`:'';
    return cardHtml({id:c.id,pfx:'k',sep:c.sep,st:c.st,current:K.sel===c,rv:c.review?CREV[c.review.status]:'',
      title:`Kelompok ${esc(c.grp)}, tingkat ${roman(c.sev)}`,
      meta:`<span class="num sepn">${esc(c.sep)}</span><span class="msub">RS ${esc(c.rs)} · rawat ${c.los} hari · pulang ${esc(c.disTxt||c.admTxt)}</span>`,
      why:`<b>${STATUS[c.st]}</b>${reason?' · '+esc(reason):''}`});
  }).join('')||`<li class="noclaims">${q?`Tidak ada klaim yang cocok dengan “${esc(q)}”. Hapus pencarian untuk melihat semua.`:K.rs?`Tidak ada klaim RS ${esc(K.rs)} di daftar ini.`:K.view==='todo'?'Semua klaim di antrean sudah diperiksa. Ekspor CSV untuk melaporkan hasilnya.':'Tidak ada klaim di antrean dengan pengaturan ini. Naikkan kapasitas pemeriksaan.'}</li>`;
  $('kdExport').href=`/api/coding/batches/${K.batch.id}/export.csv?k=${K.k}`;
}
listKeys($('kdRows'));
$('kdSearch').addEventListener('input',e=>{K.q=e.target.value; K.limit=PAGE; renderRows()});
$('kdRows').addEventListener('click',e=>{const el=e.target.closest('.claim[data-id]'); if(el&&K.byId[el.dataset.id]) select(K.byId[el.dataset.id],true,e.detail===0)});
$('kdLede').addEventListener('click',e=>{if(e.target.id==='kdClearRs'){K.rs=null; K.limit=PAGE; renderRows()}});
$('kdSeg').addEventListener('click',e=>{const b=e.target.closest('button[data-kview]'); if(b){K.view=b.dataset.kview; K.limit=PAGE; renderRows()}});
$('kdMore').addEventListener('click',()=>{K.limit+=PAGE; renderRows()});
const tabs=bindTabs($('kdTabs'),$('kdBody'),name=>{K.tab=name});

function topDriver(c){if(!c.phi)return -1;let b=0;for(let i=1;i<c.phi.length;i++)if(c.phi[i]>c.phi[b])b=i;return b}
function featText(c,i){
  if(i===0){const h=K.hmap[c.rs]; return h&&c.hr!=null?`${h.high} dari ${h.n} dikodekan II/III, ${pct(c.hr)} didukung`:'pola RS'}
  if(i===1) return `${c.los} hari, lazim ${one(c.typLos)}`;
  if(i===2) return `${c.nSec} sekunder, lazim ${one(c.careDx)}`;
  return `+${rpShort(c.uplift)}`;
}

/* ---------- klaim terpilih ---------- */
async function select(c,user,focus){
  if(!c) return;
  K.sel=c; K.draft=null;
  if(focus){const i=queueList().indexOf(c); if(i>=K.limit) K.limit=Math.ceil((i+1)/PAGE)*PAGE}
  renderRows(); renderWhy();
  if(user) openSheet($('kdWhy'));
  if(focus&&!(user&&MQ.matches)){
    const card=document.querySelector(`#kdRows .claim[data-id="${c.id}"]`); if(card) card.scrollIntoView({block:'nearest'});
    requestAnimationFrame(()=>focusDetail($('kdWhy')));
  }
  await loadDetail(c);
}
async function loadDetail(c){
  if(c.full) return;
  try{
    const d=await api('GET','/api/coding/claims/'+c.id);
    Object.assign(c,d,{full:true});
    if(K.sel===c){renderWhy(); renderRows()}
  }catch(e){fail(e)}
}
function reasonsFor(c){
  const phi=c.phi, h=K.hmap[c.rs], reasons=[];
  if(c.rf) reasons.push(`Diagnosis sekunder ${c.nSec}, jauh di atas ${one(c.careDx)} yang lazim untuk perawatan seperti ini, padahal tidak ada ICU atau ventilator dan lama rawat (${c.los} hari) tidak melebihi tingkat terendah (${one(c.baseLos)} hari).`);
  if(phi){
    [0,1,2,3].sort((a,b)=>phi[b]-phi[a]).forEach(i=>{
      if(phi[i]<0.03) return;
      if(i===0&&h) reasons.push(`Rumah sakit ini mengodekan tingkat II atau III pada ${h.high} dari ${h.n} klaim yang dibandingkan; dengan campuran kasus yang sama, rumah sakit lain akan menghasilkan sekitar ${one(h.expected)}.`+(c.hr!=null&&h.support_ref!=null?` Klaim II/III-nya didukung perawatan pada ${pct(c.hr)}, median rumah sakit lain ${pct(h.support_ref)}.`:''));
      if(i===1) reasons.push(`Lama rawat ${c.los} hari, padahal klaim tingkat ${roman(c.sev)} pada kelompok ini lazimnya ${one(c.typLos)} hari.`);
      if(i===2) reasons.push(`Diagnosis sekunder ${c.nSec}, sedangkan klaim dengan perawatan seperti ini lazimnya ${one(c.careDx)}. Diagnosis tambahan tanpa perawatan yang sepadan dapat menaikkan tingkat.`);
      if(i===3&&c.uplift>0) reasons.push(`Tingkat ini menaikkan tarif INA-CBG sekitar ${rpShort(c.uplift)} dibanding klaim tingkat terendah pada kelas rumah sakit yang sama.`);
    });
  }
  if(c.supported) reasons.push('Perawatan yang tercatat (ICU, ventilator, kematian, atau lama rawat setara tingkat ini) mendukung tingkat yang dikodekan. Klaim ini bukan indikasi pada dirinya sendiri; ia masuk karena pola rumah sakit atau karena sinyal lain.');
  if(c.died) reasons.push('Pasien meninggal. Rawat singkat pada kematian tidak dihitung sebagai sinyal.');
  if(!reasons.length) reasons.push(phi?'Tidak ada yang menonjol pada pola rumah sakit, lama rawat, diagnosis sekunder, atau tarif.':'Memuat penjelasan.');
  const next=c.st==='clear'?'':'Minta resume medis, hasil laboratorium dan penunjang, catatan ICU bila ada, dan dasar klinis untuk tiap diagnosis sekunder yang dikodekan, lalu bandingkan dengan diagnosis di klaim. Kebutuhan medis dinilai verifikator medis dari dokumen itu; halaman ini tidak menilainya.';
  return {reasons,next};
}
function renderWhy(){
  const c=K.sel, box=$('kdWhy'); if(!c){box.innerHTML='<div class="empty-detail"><p>Pilih klaim di daftar untuk melihat alasannya dan mencatat keputusan.</p></div>';return}
  const phi=c.phi, b=K.batch, h=K.hmap[c.rs], N=b.compared;
  const verdict={both:'Ditandai aturan koding dan lapisan anomali',rule:'Ditandai aturan koding saja',model:'Ditandai lapisan anomali saja',clear:'Tidak masuk antrean dengan pengaturan ini'}[c.st];
  const implied=c.implied?`menyerupai tingkat ${roman(c.implied)}`:'tidak dapat dinilai';
  const s1=c.pPeer==null?'Tidak ada cukup klaim di rumah sakit lain pada kelompok ini untuk pembanding.'
    :`Kelompok <span class="num">${esc(c.grp)}</span>: rumah sakit lain mengodekan tingkat II atau III pada <b>${pct(c.pPeer)}</b> klaim. Klaim ini dikodekan tingkat <b>${roman(c.sev)}</b>.`;
  const s2=`Lama rawat <b>${c.los} hari</b> (lazim tingkat ${roman(c.sev)}: ${one(c.typLos)}; tingkat terendah: ${one(c.baseLos)}). ICU ${c.icu?'ya':'tidak'}, ventilator ${c.vent?'ya':'tidak'}${c.died?', pasien meninggal':''}. Perawatan ${implied}. Diagnosis sekunder <b>${c.nSec}</b>; klaim dengan perawatan seperti ini lazimnya ${one(c.careDx)}.${c.rf?' <b>Lolos aturan koding.</b>':''}`;
  const s3=h?`RS ${esc(c.rs)} mengodekan II/III pada <b>${h.high}</b> dari ${h.n} klaim yang dibandingkan, harapan ${dec(h.expected,1)}${h.n>=15?` (z = ${dec(h.z,1)})`:''}. Dukungan perawatan ${c.hr==null?'tidak dinilai':pct(c.hr)}${h.support_ref!=null?`, median rumah sakit lain ${pct(h.support_ref)}`:''}. Status: <b>${(VERDICT[h.verdict]||[''])[0]}</b>.`:'Tidak ada pola rumah sakit yang dapat dihitung untuk klaim ini.';
  const s4=`Skor <b class="num">${sc(c.score)}</b>, peringkat ${c.rank} dari ${dots(N)} klaim II/III yang dibandingkan. Rata-rata klaim <span class="num">${sc(b.baseScore)}</span>.`;
  let bars='<p class="note">'+(c.live?'Menghitung penjelasan.':'Semua sinyal nol: tidak ada yang perlu dijelaskan.')+'</p>';
  if(phi){
    const D0=-0.06,D1=0.2, px=v=>((Math.max(D0,Math.min(D1,v))-D0)/(D1-D0)*100);
    bars='<div class="contrib">'+[0,1,2,3].sort((a,b2)=>phi[b2]-phi[a]).map(i=>{
      const v=phi[i], z=px(0), e=px(v), l=Math.min(z,e), w=Math.abs(e-z);
      return `<div class="crow"><span class="cl">${esc(FEAT[i])} <span>· ${esc(featText(c,i))}</span></span><span class="cv">${v>=0?'+':'−'}${dec(Math.abs(v),3)}</span>
        <div class="track"><i class="bar${v<0?' neg':''}" style="left:${l.toFixed(2)}%;width:${w.toFixed(2)}%"></i><i class="zero" style="left:${z.toFixed(2)}%"></i></div></div>`;
    }).join('')+`</div><p class="note">Batang ke kanan menaikkan skor, ke kiri menurunkannya. Jumlahnya sama dengan selisih antara rata-rata klaim (${sc(b.baseScore)}) dan klaim ini (${sc(c.score)}).</p>`;
  }
  const {reasons,next}=reasonsFor(c);
  const rv=c.review, draft=K.draft&&K.draft.id===c.id?K.draft:{status:rv?rv.status:'',note:rv?rv.note:'',resp:rv?rv.rsResponse||'':''};
  setPanel(box,detailHtml({
    panelId:'kdWhy',
    ids:{form:'kdReviewForm',note:'kdNote',resp:'kdResp',reopen:'kdReopen',radio:'kdStatus'},
    tag:b.labelled?`<span class="tag">Label sintetis · ${c.label?'upcoding disisipkan':'klaim biasa'}</span>`:'',
    st:c.st, status:STATUS[c.st], sep:c.sep, verdict,
    hero:`Kelompok ${esc(c.grp)} · dikodekan tingkat ${roman(c.sev)}${c.implied&&c.implied!==c.sev?`, perawatan ${implied.replace('menyerupai ','')}`:''}`,
    reasonsTitle:c.st==='clear'?'Penjelasan':'Mengapa ditandai', reasons, next,
    rv, draft, labels:CREV,
    opts:[['confirmed','Upcoding dikonfirmasi','tingkat tidak didukung rekam medis'],['cleared','Dinyatakan wajar','tingkat sesuai rekam medis'],['docs_requested','Rekam medis diminta','menunggu jawaban rumah sakit']],
    notePh:'Apa yang diperiksa pada rekam medis, dan apa yang ditemukan', respPh:'Dokumen yang dikirim RS dan penjelasannya',
    facts:[
      ['Rumah sakit',`${esc(c.rs)} · Kelas ${esc(c.kelasRs||'?')}`],
      ['Masuk – pulang',`${esc(c.admTxt)} – ${esc(c.disTxt)} · LOS ${c.los}`],
      ['INA-CBG',`<span class="num">${esc(c.cbg)}</span> ${esc(c.cbgDesc||'')}`],
      ['Diagnosis · prosedur',`${esc(c.diag||'-')} · ${esc(c.proc||'-')}`,'num'],
      ['Tarif INA-CBG',rp(c.tarif)+(c.uplift>0?` · selisih perkiraan ${rp(c.uplift)}`:''),'num']
    ],
    stages:[['Tahap 2 · Kasus sejenis',`<p>${s1}</p>`],['Tahap 2 · Dukungan perawatan',`<p>${s2}</p>`],['Tahap 3 · Pola rumah sakit',`<p>${s3}</p>`],['Tahap 3 · Lapisan anomali',`<p>${s4}</p>`],['Tahap 4 · Apa yang menaikkan skor',bars]],
    open:S.foldOpen}));
}
$('kdWhy').addEventListener('input',e=>{
  const f=e.target.closest('#kdReviewForm'); if(!f||!K.sel) return;
  const st=f.querySelector('input[name=kdStatus]:checked');
  K.draft={id:K.sel.id,status:st?st.value:'',note:$('kdNote').value,resp:$('kdResp').value};
  syncResp(f);
});
$('kdWhy').addEventListener('toggle',e=>{if(e.target.classList&&e.target.classList.contains('fold')) S.foldOpen=e.target.open},true);
async function saveReview(status,note,resp,go){
  const c=K.sel;
  try{
    const d=await api('PUT','/api/coding/claims/'+c.id+'/review',{status,note,rsResponse:resp||''});
    Object.assign(c,d,{full:true}); K.draft=null;
    update();
    if(!status){toast('Klaim dibuka ulang.'); return}
    const next=go?nextAfter(queueList(),c):null;
    if(next){toast(`Keputusan tersimpan: ${CREV[status]}. Klaim berikutnya dibuka.`); select(next,false,true)}
    else{toast(go?`Keputusan tersimpan: ${CREV[status]}. Tidak ada klaim lain yang menunggu.`:`Keputusan tersimpan: ${CREV[status]}.`); closeSheet(); if(!MQ.matches) focusDetail($('kdWhy'))}
  }catch(e){fail(e)}
}
$('kdWhy').addEventListener('submit',e=>{
  if(e.target.id!=='kdReviewForm') return;
  e.preventDefault();
  const st=e.target.querySelector('input[name=kdStatus]:checked');
  if(!st){toast('Pilih keputusan terlebih dahulu.',true);return}
  saveReview(st.value,$('kdNote').value,$('kdResp').value,!!(e.submitter&&e.submitter.value==='next'));
});
$('kdWhy').addEventListener('click',e=>{if(e.target.id==='kdReopen') saveReview(null,'',''); if(e.target.closest('[data-act=close]')) closeSheet()});

/* ---------- pengaturan, unggah, hapus ---------- */
function update(){
  if(!K.batch) return;
  classify(); const n=counts(); K.cnt=n;
  $('kdKOut').textContent=K.k;
  renderHead(); renderPipe(n); renderKpis(n); renderUnsaved(); renderHosp(n); renderRows(); renderWhy();
}
$('kdK').addEventListener('input',e=>{K.k=+e.target.value; update()});
$('kdReset').addEventListener('click',()=>{K.k=K.batch.topK; $('kdK').value=K.k; update()});
$('kdSave').addEventListener('click',async()=>{
  try{
    const saved=await api('PATCH','/api/coding/batches/'+K.batch.id,{topK:K.k});
    K.batch=Object.assign(K.batch,saved); K.k=K.batch.topK; update(); toast('Kapasitas batch ini tersimpan.');
  }catch(e){fail(e)}
});

/* unggah dan hapus ada di halaman Berkas; di sini hanya menandai batch yang akan dibuka */
function focus(id){K.want=id}
function forget(id){
  if(K.want===id) K.want=null;
  if(K.batch&&K.batch.id===id){K.batch=null; K.claims=[]; K.sel=null}
}

window.VKCoding={open,reset,focus,forget};
})();
