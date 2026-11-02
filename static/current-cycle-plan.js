// Selection only; parameter sync, START and OTA remain explicit operator actions.
let cycleSelectionPending = false;
let cyclePlanSignature = '';
async function loadCurrentCyclePlan() {
 const panel=document.getElementById('current-cycle-presets');if(!panel)return;
 try {
  const response=await fetch('/api/experiment/current-cycle-plan',{cache:'no-store'});
  if(!response.ok)throw new Error(await readApiError(response));
  const data=await response.json();const sig=JSON.stringify(data);
  if(sig!==cyclePlanSignature){
   panel.replaceChildren();
   const select=document.createElement('select');select.setAttribute('aria-label','60mm 2周期平均用の新規補正ログ');
   const valid=data.calibrations.filter(x=>x.valid);
   const empty=document.createElement('option');empty.value='';empty.textContent='新規補正ログを選択';select.append(empty);
   for(const c of valid){const o=document.createElement('option');o.value=c.session_id;o.textContent=c.session_id;select.append(o);}
   if(valid.some(x=>x.session_id===data.active?.offset_calibration_session))select.value=data.active.offset_calibration_session;
   const button=(label,pid,needsCal)=>{
    const b=document.createElement('button');b.className='btn btn-outline';b.style.marginRight='.4rem';b.textContent=label;
    b.disabled=cycleSelectionPending||data.busy||data.preview_only||(needsCal&&!valid.length);
    b.addEventListener('click',()=>selectCurrentCyclePreset(pid,needsCal?select.value:null));panel.append(b);
   };
   button('① 新規補正測定60秒','60mm_mean_cycle2_calibration',false);
   panel.append(select,document.createElement('br'));
   button('② 2周期平均・7点','60mm_mean_cycle2_base7',true);
   button('③ 同じ補正値で追加6点','60mm_mean_cycle2_extra6',true);
   const note=document.createElement('p');
   const update=()=>{const c=valid.find(x=>x.session_id===select.value);note.textContent=c?`中心補正：ID12 ${c.agents['12'].offset_mA.toFixed(2)} mA / ID13 ${c.agents['13'].offset_mA.toFixed(2)} mA`:'今回用の補正測定を回収して選択してください。過去の補正なし・補正あり実験のログは使いません。';};
   select.addEventListener('change',update);update();panel.append(note);
   const completed=document.createElement('p');completed.textContent='回収済み：'+(data.completed.map(x=>`${x.preset_id} (${x.session_id})`).join('、')||'なし');panel.append(completed);
   cyclePlanSignature=sig;
  }
  document.getElementById('current-cycle-active').textContent=data.active?.id?.startsWith('current_cycle2_')?`選択中：${data.active.preset_id}`:'2周期平均条件は未選択です';
  document.getElementById('current-cycle-ready').textContent=data.preview_only?'表示確認専用：実機通信は無効です':(data.ready?'✓ ':'')+data.readiness;
 } catch(e){document.getElementById('current-cycle-ready').textContent=e.message;}
}
async function selectCurrentCyclePreset(presetId,sessionId){
 if(cycleSelectionPending)return;cycleSelectionPending=true;
 try{
  const response=await fetch('/api/experiment/current-cycle-plan/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preset_id:presetId,calibration_session:sessionId})});
  if(!response.ok)throw new Error(await readApiError(response));
  for(const fn of [typeof refreshFirmwareUploadProfile==='function'?refreshFirmwareUploadProfile:null,typeof loadExperimentConfig==='function'?loadExperimentConfig:null,typeof loadPhaseSequenceStatus==='function'?loadPhaseSequenceStatus:null])if(fn)await fn();
 }catch(e){alert(e.message);}finally{cycleSelectionPending=false;cyclePlanSignature='';await loadCurrentCyclePlan();}
}
document.addEventListener('DOMContentLoaded',()=>{loadCurrentCyclePlan();setInterval(loadCurrentCyclePlan,5000);});
