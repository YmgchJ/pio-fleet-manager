// Separate fixed-session center calibration. No automatic START/OTA/parameter send.
let offsetSelectionPending = false;
let offsetSignature = '';
async function loadCurrentOffsetPlan() {
 const panel=document.getElementById('current-offset-presets');if(!panel)return;
 try {
  const response=await fetch('/api/experiment/current-offset-plan',{cache:'no-store'});
  if(!response.ok)throw new Error(await readApiError(response));
  const data=await response.json();
  const signature=JSON.stringify(data);
  if(signature!==offsetSignature){
   panel.replaceChildren();
   const done=document.createElement('details');const summary=document.createElement('summary');summary.textContent='回収済みの補正実験';done.append(summary);let count=0;
   for(const mm of [40,60,80])for(const kind of ['single','mean']){
    const row=document.createElement('div');row.style.marginBottom='.65rem';
    const label=document.createElement('strong');label.textContent=`${mm} mm・Zopt,${kind==='single'?'1':'10'} `;row.append(label);
    const calibrationId=`${mm}mm_${kind}_calibration`;
    const cs=data.calibrations.filter(c=>c.preset_id===calibrationId);
    const valid=cs.filter(c=>c.valid);
    const select=document.createElement('select');select.setAttribute('aria-label',`${mm} mm ${kind} 補正測定ログ`);
    if(!valid.length){const o=document.createElement('option');o.textContent='補正測定ログ未回収';o.value='';select.append(o);}
    for(const c of valid){const o=document.createElement('option');o.value=c.session_id;o.textContent=c.session_id;select.append(o);}
    const activeRef=data.active?.offset_calibration_session;
    if(valid.some(c=>c.session_id===activeRef))select.value=activeRef;
    const makeButton=(text,pid,disabled=false)=>{
     const b=document.createElement('button');b.className='btn btn-outline';b.style.marginLeft='.35rem';b.textContent=text;b.disabled=disabled||data.busy||data.preview_only||offsetSelectionPending;
     b.addEventListener('click',()=>selectCurrentOffsetPreset(pid,select.value||null));return b;
    };
    row.append(makeButton(valid.length?'①補正測定・回収済':'①補正測定60秒',calibrationId));row.append(select);
    const note=document.createElement('span');note.style.fontSize='.85rem';note.style.marginLeft='.4rem';
    const update=()=>{const c=valid.find(c=>c.session_id===select.value);note.textContent=c?`中心補正: ID12 ${c.agents['12'].offset_mA.toFixed(1)} mA / ID13 ${c.agents['13'].offset_mA.toFixed(1)} mA（2周の差 ${c.agents['12'].lap_difference_mA.toFixed(1)} / ${c.agents['13'].lap_difference_mA.toFixed(1)} mA）`:cs.find(c=>!c.valid)?.error||'';};select.addEventListener('change',update);update();
    let completedCount=0;
    for(const [batch,text] of [['base7','②閉ループ7点'],['extra6','③追加6点']]){
     const pid=`${mm}mm_${kind}_zopt_${batch}`;const recovered=data.completed.filter(c=>c.preset_id===pid);
     const b=makeButton(text,pid,!valid.length);
     if(recovered.length){b.textContent=`${mm} mm・Zopt,${kind==='single'?'1':'10'}・${text}（回収済）`;done.append(b);count++;completedCount++;}else row.append(b);
    }
    row.append(document.createElement('br'),note);
    if(completedCount===2){const details=document.createElement('details');const title=document.createElement('summary');title.textContent=`${mm} mm・Zopt,${kind==='single'?'1':'10'} 補正測定・再実行`;details.append(title,row);done.append(details);}else panel.append(row);
   }
   if(count)panel.append(done);offsetSignature=signature;
  }
  document.getElementById('current-offset-active').textContent=data.active?.id?.startsWith('current_offset_')?`選択中: ${data.active.preset_id}${data.active.offset_calibration_session?' / 補正ログ '+data.active.offset_calibration_session:''}`:'補正実験は未選択です';
  document.getElementById('current-offset-ready').textContent=data.preview_only?'表示確認専用：実機通信は無効です':data.ready?'✓ '+data.readiness:data.readiness;
 }catch(e){document.getElementById('current-offset-active').textContent=`補正実験読込失敗: ${e.message}`;}
}
async function selectCurrentOffsetPreset(presetId,sessionId){
 if(offsetSelectionPending)return;offsetSelectionPending=true;
 try{
  const r=await fetch('/api/experiment/current-offset-plan/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preset_id:presetId,calibration_session:sessionId})});
  if(!r.ok)throw new Error(await readApiError(r));
  for(const fn of [typeof refreshFirmwareUploadProfile==='function'?refreshFirmwareUploadProfile:null,typeof loadExperimentConfig==='function'?loadExperimentConfig:null,typeof loadPhaseSequenceStatus==='function'?loadPhaseSequenceStatus:null])if(fn)await fn();
 }catch(e){alert(e.message);}finally{offsetSelectionPending=false;offsetSignature='';await loadCurrentOffsetPlan();if(typeof loadCurrentZoptPlan==='function')await loadCurrentZoptPlan();}
}
document.addEventListener('DOMContentLoaded',()=>{loadCurrentOffsetPlan();setInterval(loadCurrentOffsetPlan,5000);});
