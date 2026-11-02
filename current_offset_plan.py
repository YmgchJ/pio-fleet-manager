"""Session-fixed current offset experiment, separate from unchanged fixed-center plan.
Calibration is an explicit recovered60s open-loop run; correction is frozen for7+6.
No hardware I/O in this module.
"""
from __future__ import annotations
import copy, hashlib, json, math, tempfile
from pathlib import Path
import numpy as np
import pandas as pd
import yaml
from current_zopt_plan import ROOT, read_preset, activate_preset
CAL_ID='current_offset_calibration_v1'
LOCK_ID='current_offset_lock_13point_v1'
PLAN_IDS=(CAL_ID,LOCK_ID)
CATALOG=ROOT/'fleet/current_offset_lock_compare'
RAW=ROOT/'reports/impulsive_interaction/20261022_current_order2/verified_raw_real_coefficients.npz'
EXPERIMENTS=ROOT/'fleet_manager/data/experiments'
CAL_PRESETS=tuple(f'{mm}mm_{kind}_calibration' for mm in (40,60,80) for kind in ('single','mean'))
LOCK_PRESETS=tuple(f'{mm}mm_{kind}_zopt_{batch}' for mm in (40,60,80) for kind in ('single','mean') for batch in ('base7','extra6'))

def basis(p):
 return np.stack([np.ones_like(p),np.cos(p),np.sin(p),np.cos(2*p),np.sin(2*p)],axis=-1)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def parts(pid):
 if pid not in CAL_PRESETS+LOCK_PRESETS:raise ValueError('未登録の補正実験条件です')
 a=pid.split('_');return int(a[0][:-2]),a[1]

def documents(pid):
 mm,kind=parts(pid);cal=pid in CAL_PRESETS;base=f'{mm}mm_{kind}_zopt_base7' if cal else pid
 s,p=read_preset(base);s,p=copy.deepcopy(s),copy.deepcopy(p)
 s['research_plan'].update(id=CAL_ID if cal else LOCK_ID,preset_id=pid,offset_mode='session_fixed_mean_residual',calibration_scope='one_per_13point_controller_session')
 s['name']='current_offset_'+pid
 if cal:
  s.update(experiment_mode='open_loop_identification',kappa=0.,trial_duration_ms=60000,analysis_window_start_s=10.,analysis_window_end_s=50.,sequence_log_hz=50,delta_omega_rad=[math.pi*.1],delta_omega_labels=['offset_calibration'],trial_phase_offsets_rad={'12':[0.0],'13':[0.0]})
  s['research_plan'].update(batch='calibration',display_delta_omega_rad_s=[-math.pi*.1],offset_reference='per-agent10-trial raw-mA MA5 Fourier mean')
  p['kappa']=0.;p['omega_by_agent']={12:3*math.pi,13:3*math.pi}  # Idle readback; SEQ_ARM applies2.9pi to partner.
 return s,p

def calibration_candidates(experiments_dir=EXPERIMENTS):
 out=[]
 for path in experiments_dir.glob('sequence_*/sequence_plan.json'):
  try:
   plan=json.loads(path.read_text())['plan'];m=plan.get('research_plan',{})
   if m.get('id')!=CAL_ID:continue
   st=json.loads(path.with_name('sequence_status.json').read_text());up=st.get('final_upload',{})
   if st.get('status')!='completed' or any(not up.get('log_end',{}).get(str(i),{}).get('ok') for i in (12,13)):continue
   if not Path(up['merged_csv']).is_file():continue
   out.append(dict(preset_id=m['preset_id'],session_id=up['session_id'],sequence_dir=str(path.parent),completed_at=st['completed_at']))
  except (KeyError,ValueError,OSError,TypeError):continue
 return sorted(out,key=lambda x:x['completed_at'],reverse=True)

def estimate(sequence_dir, *, plan_id=CAL_ID, base_preset=None):
 path=Path(sequence_dir);plan=json.loads((path/'sequence_plan.json').read_text())['plan'];st=json.loads((path/'sequence_status.json').read_text());meta=plan['research_plan'];pid=meta['preset_id']
 if meta.get('id')!=plan_id or st.get('status')!='completed':raise ValueError('補正測定が完了していません')
 if base_preset is not None:pid=base_preset
 mm,kind=parts(pid);expected,_=documents(pid)
 for k,val in [('kappa',0),('trial_duration_ms',60000),('sequence_log_hz',50)]:
  if plan[k]!=val:raise ValueError('補正測定の設定が不一致です: '+k)
 up=st['final_upload'];csv=Path(up['merged_csv']);data=pd.read_csv(csv);raw=np.load(RAW);series={};quality={}
 for aid in (12,13):
  end=up['log_end'][str(aid)]
  if not end['ok']:raise ValueError('2台分のログ回収が必要です')
  ack=[e for e in st['event_log'] if e.get('agent_id')==aid and e.get('event')=='UPLOAD_DONE'][-1]
  if ack.get('ina_ready')!=1 or int(ack.get('ina_ok',0))<=0:raise ValueError('補正測定の電流センサが未確認です')
  if ack.get('interaction_path')!='current_50hz_ma5_fixed_affine' or ack.get('kappa')!=0:raise ValueError('補正測定は固定尺度・κ0が必要です')
  if (ack.get('servo_center'),ack.get('servo_amplitude'),ack.get('sample_period_us'))!=(110.,75.,20000):raise ValueError('補正測定の動作条件が不一致です')
  a=data[data.agent_id==aid];starts=list(map(int,ack['trial_start_micros24_all'].split(';')))
  if len(starts)!=1 or len(a)!=end['records'] or len(a)!=ack['records']:raise ValueError('補正ログの件数が一致しません')
  t=(a.micros32.to_numpy()-starts[0])*256e-6
  if len(t)<2 or not np.all(np.diff(t)>0) or t[0]>.1 or t[-1]<59.9 or np.max(np.diff(t))>.1:raise ValueError('補正ログの時間範囲・連続性を確認してください')
  ph=np.unwrap(a.a0.to_numpy(float)*2*np.pi/255);current=(a.a1.to_numpy(float)-128)*1000/127
  # Native50Hz causalMA5, matching identification processing. No25Hz substitution.
  ma=np.convolve(current,np.ones(5)/5,mode='full')[:len(current)]
  series[aid]=(t,ph,ma)
  quality[str(aid)]=dict(records=len(a),log_rate_hz=float(1/np.median(np.diff(t))),max_gap_s=float(np.max(np.diff(t))),clipped_current_samples=int(np.sum((a.a1==0)|(a.a1==255))))
 result={}
 for aid in (12,13):
  t,ph,ma=series[aid];pt,pp,_=series[25-aid];mask=(t>=10)&(t<=50);tt=t[mask];own=ph[mask];partner=np.interp(tt,pt,pp);c=np.mean([raw[f'raw_mA_{mm}_{aid}_{h}'] for h in range(1,11)],axis=0)
  pred=np.einsum('ni,ij,nj->n',basis(own),c,basis(partner));res=ma[mask]-pred
  if not np.all(np.isfinite(res)):raise ValueError('補正ログ・参照曲面に非有限値があります')
  delta=float(np.mean(res));laps=[float(np.mean(res[(tt>=lo)&(tt<hi)])) for lo,hi in [(10,30),(30,50)]]
  _,params=documents(pid);base=params['current_observer']['by_agent'][str(aid)];result[str(aid)]=dict(offset_mA=delta,base_center_mA=base['center_mA'],corrected_center_mA=base['center_mA']+delta,width_mA=base['width_mA'],lap_offsets_mA=laps,lap_difference_mA=laps[1]-laps[0],mean_measured_mA=float(ma[mask].mean()),mean_predicted_mA=float(pred.mean()),relative_phase_span_rad=float(np.ptp(partner-own)),**quality[str(aid)])
 return dict(schema=1,calibration_preset=pid,paddle_mm=mm,z_kind=kind,session_id=up['session_id'],sequence_dir=str(path.resolve()),csv=str(csv),csv_sha256=sha(csv),raw_coefficients_sha256=sha(RAW),analysis_window_s=[10,50],method='delta_i=mean(MA5(I_i)-Ihat10_i(phi_i,phi_j)); c_new=c_base+delta; width unchanged; no rolling/clip/A removal',agents=result)

def completed_batches(experiments_dir=EXPERIMENTS):
 out=[]
 for path in experiments_dir.glob('sequence_*/sequence_plan.json'):
  try:
   plan=json.loads(path.read_text())['plan'];m=plan.get('research_plan',{});st=json.loads(path.with_name('sequence_status.json').read_text());up=st.get('final_upload',{})
   if m.get('id')!=LOCK_ID or st.get('status')!='completed':continue
   if any(not up.get('log_end',{}).get(str(i),{}).get('ok') or up['log_end'][str(i)].get('records',0)<=0 for i in (12,13)):continue
   if not Path(up['merged_csv']).is_file():continue
   out.append(dict(preset_id=m['preset_id'],calibration_session=m['offset_calibration_session'],session_id=up['session_id']))
  except (OSError,ValueError,KeyError,TypeError):continue
 return out

def prepare(pid,sequence_path,params_path,backup_root,session_id=None,experiments_dir=EXPERIMENTS):
 s,p=documents(pid);mm,kind=parts(pid);cal=pid in CAL_PRESETS;record=None
 if not cal:
  if not session_id:raise ValueError('今回の13点用の補正測定ログを選択してください')
  choices=[c for c in calibration_candidates(experiments_dir) if c['session_id']==session_id and c['preset_id']==f'{mm}mm_{kind}_calibration']
  if len(choices)!=1:raise ValueError('パドル・Zに対応する完了済み補正測定がありません')
  if pid.endswith('_extra6') and not any(x['preset_id']==pid.replace('_extra6','_base7') and x['calibration_session']==session_id for x in completed_batches(experiments_dir)):
   raise ValueError('先に同じ補正値で7点を完了・回収してください')
  record=estimate(choices[0]['sequence_dir'])
  # A correction is immutable for both batches: derive both from this same record.
  for aid,a in record['agents'].items():p['current_observer']['by_agent'][aid]['center_mA']=a['corrected_center_mA']
  p['current_observer']['session_offset_calibration']=record
  s['research_plan']['offset_calibration_session']=session_id
 with tempfile.TemporaryDirectory() as tmp:
  cat=Path(tmp);internal=f'{mm}mm_{kind}_zopt_base7'
  (cat/f'{internal}.yaml').write_text(yaml.safe_dump(s,allow_unicode=True,sort_keys=False));(cat/f'params_{internal}.yaml').write_text(yaml.safe_dump(p,allow_unicode=True,sort_keys=False))
  backup=activate_preset(internal,sequence_path,params_path,backup_root,catalog=cat)
 return backup,record

def validate(plan):
 """Fail closed even if selected YAML is subsequently edited manually."""
 meta=plan.get('research_plan',{});id=meta.get('id')
 if id not in PLAN_IDS:return
 pid=meta.get('preset_id');s,p=documents(pid);actual=plan['sync_params_snapshot'];cal=id==CAL_ID
 if cal!=(pid in CAL_PRESETS):raise ValueError('補正測定／閉ループの条件が不一致です')
 if cal:
  if (plan['trial_duration_ms'],plan['sequence_log_hz'],plan['kappa'],len(plan['phase_labels']))!=(60000,50,0,1) or abs(plan['partner_omegas_rad'][0]-2.9*math.pi)>1e-6:raise ValueError('補正測定は60s・50Hz・κ0・ω2=2.9πです')
  if (plan['analysis_window_start_s'],plan['analysis_window_end_s'])!=(10,50):raise ValueError('補正窓は10–50sです')
 else:
  ref=actual.get('current_observer',{}).get('session_offset_calibration')
  if not ref or ref.get('session_id')!=meta.get('offset_calibration_session'):raise ValueError('固定補正値の出典がありません')
  if pid.endswith('_extra6') and not any(x['preset_id']==pid.replace('_extra6','_base7') and x['calibration_session']==ref['session_id'] for x in completed_batches()):
   raise ValueError('追加6点には同じ補正値で完了した7点が必要です')
  rebuilt=estimate(ref['sequence_dir'])
  if rebuilt!=ref:raise ValueError('補正記録・元ログが変更されています。再選択してください')
  if (ref['paddle_mm'],ref['z_kind'])!=parts(pid):raise ValueError('補正測定のパドル・Zが不一致です')
  for aid,a in ref['agents'].items():p['current_observer']['by_agent'][aid]['center_mA']=a['corrected_center_mA']
 for key in ['feedback_tau','center','amplitude','z_mode','prc_n','kappa','kappa_init']+[f'prc_{v}{n}' for n in range(5) for v in ('a','b')]:
  if actual.get(key,0)!=p.get(key,0):raise ValueError('補正条件のZ・動作設定が不一致です: '+key)
 obs=actual.get('current_observer',{})
 if any(obs.get(k)!=p['current_observer'].get(k) for k in ('mode','ma_samples','sample_hz','clip','by_agent')):raise ValueError('補正条件の中心・幅・前処理が不一致です')
 if plan['agent_ids']!=[12,13]:raise ValueError('補正条件はID12/13です')
