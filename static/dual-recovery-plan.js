let recoveryPending=false,recoverySignature='';
async function loadDualRecoveryPlan(){
 const panel=document.getElementById('dual-recovery-presets');if(!panel)return;
 try{
  const response=await fetch('/api/experiment/dual-recovery-plan',{cache:'no-store'});
  if(!response.ok)throw new Error(await readApiError(response));
  const data=await response.json(),sig=JSON.stringify(data);
  if(sig!==recoverySignature){
   const old=panel.querySelector('select')?.value;panel.replaceChildren();
   const select=document.createElement('select');select.setAttribute('aria-label','復帰実験の共通校正ログ');
   const empty=document.createElement('option');empty.value='';empty.textContent='新しい共通校正ログを選択';select.append(empty);
   data.calibrations.filter(x=>x.valid).forEach((c,i)=>{const o=document.createElement('option');o.value=c.session_id;
    const date=c.motion_start_epoch?new Date(c.motion_start_epoch*1000):new Date(c.completed_at);
    o.textContent=(i===0?'【最新】 ':'')+new Intl.DateTimeFormat('ja-JP',{timeZone:'Asia/Tokyo',dateStyle:'short',timeStyle:'medium'}).format(date)+' JST ｜ '+c.session_id;select.append(o);});
   select.value=old||data.active?.calibration_session||'';
   const button=(label,pid,cal)=>{const b=document.createElement('button');b.className='btn btn-outline';b.textContent=label;b.style.marginRight='.4rem';
    const update=()=>{b.disabled=recoveryPending||data.busy||data.preview_only||(cal&&!select.value);};select.addEventListener('change',update);update();
    b.onclick=()=>recoveryAction('select',{preset_id:pid,calibration_session:cal?select.value:null});return b;};
   panel.append(button('① 共通校正60秒','calibration',false),select,document.createElement('br'),button('② ブロック1：B・曲げ・曲げ・B','block1',true),button('③ ブロック2：曲げ・B・B・曲げ','block2',true));
   const apply=document.createElement('button');apply.className='btn btn-primary';apply.textContent='共通校正値と先頭の制御則を実機へ適用';
   apply.disabled=recoveryPending||data.busy||data.preview_only||data.active?.id!=='dual60_recovery_blocks_v1';apply.onclick=()=>recoveryAction('apply');panel.append(document.createElement('br'),apply);
   for(const c of data.calibrations.filter(x=>!x.valid)){const p=document.createElement('p');p.textContent=`校正候補 ${c.session_id}：${c.error}`;panel.append(p);}
   const done=document.createElement('p');done.textContent='回収済み：'+(data.completed.map(x=>x.preset_id+' / '+x.session_id).join('、')||'なし');panel.append(done);recoverySignature=sig;
  }
  document.getElementById('dual-recovery-active').textContent=data.active?.id?.startsWith('dual60_recovery_')?'選択中：'+data.active.preset_id:'復帰実験は未選択です';
  document.getElementById('dual-recovery-ready').textContent=data.preview_only?'表示専用：実機通信は無効です':(data.ready?'✓ ':'')+data.readiness;
 }catch(e){document.getElementById('dual-recovery-ready').textContent=e.message;}
}
async function recoveryAction(action,body){
 if(recoveryPending)return;recoveryPending=true;
 try{
  const response=await fetch('/api/experiment/dual-recovery-plan/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  if(!response.ok)throw new Error(await readApiError(response));
  for(const f of [typeof refreshFirmwareUploadProfile==='function'?refreshFirmwareUploadProfile:null,typeof loadExperimentConfig==='function'?loadExperimentConfig:null,typeof loadPhaseSequenceStatus==='function'?loadPhaseSequenceStatus:null])if(f)await f();
 }catch(e){alert(e.message);}finally{recoveryPending=false;recoverySignature='';await loadDualRecoveryPlan();}
}
document.addEventListener('DOMContentLoaded',()=>{loadDualRecoveryPlan();setInterval(loadDualRecoveryPlan,5000);});
