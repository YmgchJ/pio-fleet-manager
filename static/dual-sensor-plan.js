// Explicit operator actions only: select writes files; apply configures stopped robots.
let dualPending=false, dualSignature='';
function dualCalibrationDate(cal){
 const date=cal.motion_start_epoch?new Date(cal.motion_start_epoch*1000):cal.sequence_created_at?new Date(cal.sequence_created_at):null;
 if(!date||!Number.isFinite(date.getTime()))return '日時不明';
 return new Intl.DateTimeFormat('ja-JP',{timeZone:'Asia/Tokyo',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(date)+' JST';
}
async function loadDualSensorPlan(){
 const panel=document.getElementById('dual-sensor-presets');if(!panel)return;
 try{
  const res=await fetch('/api/experiment/dual-sensor-plan',{cache:'no-store'});
  if(!res.ok)throw new Error(await readApiError(res));
  const data=await res.json(),sig=JSON.stringify(data);
  if(sig!==dualSignature){
   const previous={};for(const e of panel.querySelectorAll('select'))previous[e.dataset.kind]=e.value;
   panel.replaceChildren();
   for(const kind of ['b','c']){
    const label=kind.toUpperCase(),row=document.createElement('p');
    const select=document.createElement('select');select.dataset.kind=kind;select.setAttribute('aria-label',label+'用の校正ログ');
    const empty=document.createElement('option');empty.value='';empty.textContent=label+'用校正ログを選択';select.append(empty);
    const candidates=data.calibrations.filter(x=>x.kind===kind&&x.valid!==false);
    candidates.forEach((cal,index)=>{const o=document.createElement('option');o.value=cal.session_id;
     o.textContent=`${index===0?'【最新】 ':''}${dualCalibrationDate(cal)} ｜ ${cal.session_id}`;select.append(o);});
    select.value=previous[kind]||((data.active?.kind===kind)?data.active.calibration_session:'')||'';
    const button=(text,pid,needsCal)=>{const b=document.createElement('button');b.className='btn btn-outline';b.textContent=text;b.style.marginRight='.4rem';
     const update=()=>{b.disabled=dualPending||data.busy||data.preview_only||(needsCal&&!select.value);};select.addEventListener('change',update);update();
     b.onclick=()=>dualAction('select',{preset_id:pid,calibration_session:needsCal?select.value:null});return b;};
    row.append(button(label+'校正60秒',kind+'_calibration',false),select,button(label+'本試行7条件・各120秒',kind+'_base7',true));panel.append(row);
   }
   for(const c of data.calibrations.filter(x=>x.valid===false)){const note=document.createElement('p');note.textContent=`校正ログ ${c.session_id}：${c.error}`;panel.append(note);}
   const apply=document.createElement('button');apply.className='btn btn-primary';apply.textContent='校正値・制御則を実機へ適用（再起動なし）';
   apply.disabled=dualPending||data.busy||data.preview_only||data.active?.id!=='dual60_pilot_v1';
   apply.onclick=()=>dualAction('apply');panel.append(apply);dualSignature=sig;
  }
  document.getElementById('dual-sensor-active').textContent=data.active?.id?.startsWith('dual60_')?'選択中：'+data.active.preset_id:'併用条件は未選択です';
  document.getElementById('dual-sensor-ready').textContent=data.preview_only?'表示専用：実機通信は無効です':(data.ready?'✓ ':'')+data.readiness;
 }catch(e){document.getElementById('dual-sensor-ready').textContent=e.message;}
}
async function dualAction(action,body){
 if(dualPending)return;dualPending=true;
 try{
  const res=await fetch('/api/experiment/dual-sensor-plan/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  if(!res.ok)throw new Error(await readApiError(res));
  for(const fn of [typeof refreshFirmwareUploadProfile==='function'?refreshFirmwareUploadProfile:null,typeof loadExperimentConfig==='function'?loadExperimentConfig:null,typeof loadPhaseSequenceStatus==='function'?loadPhaseSequenceStatus:null])if(fn)await fn();
 }catch(e){alert(e.message);}finally{dualPending=false;dualSignature='';await loadDualSensorPlan();}
}
document.addEventListener('DOMContentLoaded',()=>{loadDualSensorPlan();setInterval(loadDualSensorPlan,5000);});
