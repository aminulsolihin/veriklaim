// Generates the synthetic sample in the E-Klaim TXT layout, plus the labels of the injected patterns.
// Run: node tools/make_sample.js   (writes app/data/ and app/static/assets/sample/)
const fs = require('fs');
const path = require('path');

function mulberry32(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}

/* Column layout of the E-Klaim TXT export, as listed for pull_claim in Petunjuk Teknis E-Klaim 5.1.1 */
const COLS=['KODE_RS','KELAS_RS','KELAS_RAWAT','KODE_TARIF','PTD','ADMISSION_DATE','DISCHARGE_DATE','BIRTH_DATE','BIRTH_WEIGHT','SEX','DISCHARGE_STATUS','DIAGLIST','PROCLIST','ADL1','ADL2','IN_SP','IN_SR','IN_SI','IN_SD','INACBG','SUBACUTE','CHRONIC','SP','SR','SI','SD','DESKRIPSI_INACBG','TARIF_INACBG','TARIF_SUBACUTE','TARIF_CHRONIC','DESKRIPSI_SP','TARIF_SP','DESKRIPSI_SR','TARIF_SR','DESKRIPSI_SI','TARIF_SI','DESKRIPSI_SD','TARIF_SD','TOTAL_TARIF','TARIF_RS','LOS','ICU_INDIKATOR','ICU_LOS','VENT_HOUR','NAMA_PASIEN','MRN','UMUR_TAHUN','UMUR_HARI','DPJP','SEP','NOKARTU','PAYOR_ID','CODER_ID','VERSI_INACBG','VERSI_GROUPER','C1','C2','C3','C4'];

/* Placeholder reference prices, not catalogue data. Must match the seed values in app/seed.py. */
const REF={RR04:{ref:25000000},DD01:{ref:4000000}};

function makeSample(seed){
  const r=mulberry32(seed), U=(a,b)=>a+r()*(b-a), I=(a,b)=>Math.floor(U(a,b+1)), pick=a=>a[Math.floor(r()*a.length)];
  const HOSP=[['9990001','A','AP'],['9990002','B','BP'],['9990003','B','BS'],['9990004','C','CP'],['9990005','C','CS'],['9990006','C','CP'],['9990007','B','BP'],['9990008','D','DP']];
  const HF={AP:1,BP:.86,BS:.89,CP:.74,CS:.77,DP:.64}, SEVF={I:.82,II:1,III:1.27}, SEVN={I:'RINGAN',II:'SEDANG',III:'BERAT'};
  const pad=(n,w)=>String(n).padStart(w,'0');
  const dmy=day=>{const d=new Date(Date.UTC(2026,3,1+day));return pad(d.getUTCDate(),2)+'/'+pad(d.getUTCMonth()+1,2)+'/'+d.getUTCFullYear()};
  let pseq=0;
  const patient=()=>({kartu:'999'+pad(Math.floor(r()*1e10),10),mrn:pad(I(100000,999999),6),sex:I(1,2),n:++pseq});
  const out=[];
  function claim(h,item,o){
    const H=HOSP[h], sev=o.sev||pick(['I','II','II','III']), kr=pick([1,2,3,3]), p=o.patient||patient(), hip=item==='RR04';
    const age=hip?I(55,82):I(45,78), los=o.los||(hip?I(5,9):I(4,7)), day=o.day!=null?o.day:I(0,84);
    const base=(hip?{1:47119400,2:40388100,3:33656700}:{1:9800000,2:8400000,3:7000000})[kr];
    const tcbg=Math.round(base*SEVF[sev]*HF[H[2]]/100)*100;
    const tsp=hip?13099000:0, tsr=hip?26197900:0, tsd=hip?0:4596000;
    const cover=o.cover||(hip?U(1.9,3.0):U(2.2,4.6));
    const trs=Math.round(REF[item].ref*cover/1000)*1000;
    out.push({inj:o.inj||null,day,row:{
      KODE_RS:H[0],KELAS_RS:H[1],KELAS_RAWAT:kr,KODE_TARIF:H[2],PTD:1,ADMISSION_DATE:dmy(day),DISCHARGE_DATE:dmy(day+los),BIRTH_DATE:'01/01/'+(2026-age),BIRTH_WEIGHT:0,SEX:p.sex,DISCHARGE_STATUS:1,
      DIAGLIST:hip?pick(['M16.1','S72.0','M17.1']):pick(['I21.0','I21.1','I21.9']),PROCLIST:hip?pick(['81.51','81.52','81.54']):'99.10',ADL1:'-',ADL2:'-',
      IN_SP:hip?'YY01':'None',IN_SR:hip?'RR04':'None',IN_SI:'None',IN_SD:hip?'None':'DD01',
      INACBG:(hip?'M-1-04-':'I-4-10-')+sev,SUBACUTE:'None',CHRONIC:'None',SP:hip?'YY-01-II':'None',SR:hip?'RR-04-III':'None',SI:'None',SD:hip?'None':'DD-01-II',
      DESKRIPSI_INACBG:(hip?'PROSEDUR PADA SENDI TUNGKAI BAWAH':'INFARK MIOKARD AKUT')+' ('+SEVN[sev]+')',TARIF_INACBG:tcbg,TARIF_SUBACUTE:0,TARIF_CHRONIC:0,
      DESKRIPSI_SP:hip?'HIP REPLACEMENT / KNEE REPLACEMENT':'-',TARIF_SP:tsp,DESKRIPSI_SR:hip?'HIP IMPLANT / KNEE IMPLANT':'-',TARIF_SR:tsr,DESKRIPSI_SI:'-',TARIF_SI:0,DESKRIPSI_SD:hip?'-':'STREPTOKINASE',TARIF_SD:tsd,
      TOTAL_TARIF:tcbg+tsp+tsr+tsd,TARIF_RS:trs,LOS:los,ICU_INDIKATOR:0,ICU_LOS:0,VENT_HOUR:0,
      NAMA_PASIEN:'PASIEN SINTETIS '+pad(p.n,3),MRN:p.mrn,UMUR_TAHUN:age,UMUR_HARI:age*365+I(0,364),DPJP:'DPJP SINTETIS '+pad(I(1,24),2),SEP:'',NOKARTU:p.kartu,PAYOR_ID:'3;JKN',CODER_ID:'000000000',VERSI_INACBG:'5.1.1',VERSI_GROUPER:4,C1:1,C2:0,C3:23,C4:'-'
    }});
  }
  // 64 ordinary claims + one legitimate bilateral pair (same card, two implants about ten weeks apart)
  for(let i=0;i<64;i++){const h=I(0,7); claim(h,h===7||r()<0.55?'DD01':'RR04',{})}
  const bil=patient(); claim(0,'RR04',{patient:bil,day:6}); claim(0,'RR04',{patient:bil,day:76});
  // injected: hospital bill below the reference price of the item it claims a top-up for
  [[1,'RR04',.52],[4,'RR04',.68],[6,'RR04',.84],[7,'DD01',.60],[2,'DD01',.80],[0,'RR04',.96]].forEach(([h,it,c])=>claim(h,it,{cover:c,inj:'bill'}));
  // injected: fictitious-claim pattern with an ordinary-looking bill: same card, same top-up, short stays, one hospital
  const pa=patient(), pb=patient();
  [[10,1],[24,2],[41,1]].forEach(([d,l])=>claim(3,'RR04',{patient:pa,day:d,los:l,inj:'fiktif'}));
  [[15,1],[27,1],[38,2]].forEach(([d,l])=>claim(5,'DD01',{patient:pb,day:d,los:l,inj:'fiktif'}));
  claim(5,'DD01',{cover:.7,los:1,inj:'both'});
  claim(2,'RR04',{cover:2.2,los:6,inj:'fiktif-quiet'});
  out.sort((a,b)=>(a.day+a.row.LOS)-(b.day+b.row.LOS)||a.row.NOKARTU.localeCompare(b.row.NOKARTU));
  const labels={};
  out.forEach((c,i)=>{const mm=c.row.DISCHARGE_DATE.slice(3,5);c.row.SEP=c.row.KODE_RS.slice(0,4)+'R'+c.row.KODE_RS.slice(4)+mm+'26V'+pad(i+1,6); if(c.inj)labels[c.row.SEP]=c.inj});
  const txt=COLS.join('\t')+'\n'+out.map(c=>COLS.map(k=>c.row[k]).join('\t')).join('\n')+'\n';
  return {txt,labels};
}

const s = makeSample(20261005);
const root = path.join(__dirname, '..', 'app');
fs.writeFileSync(path.join(root, 'data', 'sample-eklaim.txt'), s.txt);
fs.writeFileSync(path.join(root, 'data', 'sample-labels.json'), JSON.stringify(s.labels, null, 1) + '\n');
fs.writeFileSync(path.join(root, 'static', 'assets', 'sample', 'veriklaim-sample-eklaim.txt'), s.txt);
console.log('sample bytes', s.txt.length, 'claims', s.txt.trim().split('\n').length - 1, 'labels', Object.keys(s.labels).length);
