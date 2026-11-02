// Preset selection only. Hardware sync and sequence START remain explicit.
let convergencePending=false, convergenceSignature='';
async function loadCurrentConvergencePlan(){
 const panel=document.getElementById('current-convergence-presets');if(!panel)return;
 try{
  const r=await fetch('/api/experiment/current-convergence-plan',{cache:'no-store'});if(!r.ok)throw new Error(await readApiError(r));
  const data=await r.json(),sig=JSON.stringify(data);
  if(sig!==convergenceSignature){
   const selected=new Map(Array.from(panel.querySelectorAll('select')).map((s,i)=>[i+1,s.value]));panel.replaceChildren();
   for(let block=1;block<=5;block++){
    const line=document.createElement('p'),sel=document.createElement('select');line.className='convergence-preset-row';sel.className='convergence-calibration-select';sel.setAttribute('aria-label',`セット${block}の補正ログ`);
    const option=document.createElement('option');option.value='';option.textContent=`セット${block}の補正ログを選択`;sel.append(option);
    const candidates=data.calibrations.filter(c=>c.valid&&c.block===block);sel.disabled=data.busy||data.preview_only||!candidates.length;
    for(const c of candidates){const o=document.createElement('option');o.value=c.session_id;o.textContent=`${c.completed_at} / ${c.session_id}`;sel.append(o);}
    const active=data.active?.offset_calibration_session;
    if(candidates.some(c=>c.session_id===active))sel.value=active;
    else if(candidates.some(c=>c.session_id===selected.get(block)))sel.value=selected.get(block);
    else if(candidates.length)sel.value=candidates[0].session_id;
    function button(label,pid,cal){const b=document.createElement('button');b.className='btn btn-outline';b.textContent=label;b.disabled=convergencePending||data.busy||data.preview_only||(cal&&!candidates.length);b.onclick=()=>selectCurrentConvergencePreset(pid,cal?sel.value:null);line.append(b);}
    button(`セット${block}：①補正60秒`,`60mm_mean_initial_set${block}_calibration`,false);line.append(sel);button('②初期位相差6試行',`60mm_mean_initial_set${block}`,true);
    const note=document.createElement('span');const update=()=>{const c=candidates.find(c=>c.session_id===sel.value);note.textContent=c?` 中心補正 ID12 ${c.agents['12'].offset_mA.toFixed(2)} / ID13 ${c.agents['13'].offset_mA.toFixed(2)} mA`:'';};sel.onchange=update;update();line.append(note);panel.append(line);
    for(const c of data.calibrations.filter(c=>!c.valid&&c.block===block)){const error=document.createElement('p');error.className='convergence-calibration-error';error.textContent=`補正ログを使用できません：${c.completed_at} / ${c.session_id}：${c.error}`;panel.append(error);}
   }
   const done=document.createElement('p');done.textContent='両機回収済み：'+(data.completed.map(c=>`${c.preset_id} (${c.session_id})`).join('、')||'なし');panel.append(done);convergenceSignature=sig;
  }
  document.getElementById('current-convergence-active').textContent=data.active?.id?.startsWith('current_convergence_')?`選択中：${data.active.preset_id}`:'初期位相差実験は未選択です';
  document.getElementById('current-convergence-ready').textContent=data.preview_only?'表示確認専用':(data.ready?'✓ ':'')+data.readiness;
 }catch(e){document.getElementById('current-convergence-ready').textContent=e.message;}
}
async function selectCurrentConvergencePreset(presetId,sessionId){
 if(convergencePending)return;convergencePending=true;
 try{const r=await fetch('/api/experiment/current-convergence-plan/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preset_id:presetId,calibration_session:sessionId})});if(!r.ok)throw new Error(await readApiError(r));
 for(const fn of [typeof refreshFirmwareUploadProfile==='function'?refreshFirmwareUploadProfile:null,typeof loadExperimentConfig==='function'?loadExperimentConfig:null,typeof loadPhaseSequenceStatus==='function'?loadPhaseSequenceStatus:null])if(fn)await fn();
 }catch(e){alert(e.message);}finally{convergencePending=false;convergenceSignature='';await loadCurrentConvergencePlan();}
}
document.addEventListener('DOMContentLoaded',()=>{loadCurrentConvergencePlan();setInterval(loadCurrentConvergencePlan,5000);});
