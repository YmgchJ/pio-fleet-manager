"""Explicit, shared-calibration B vs fixed-flex recovery experiment; no robot I/O."""
from pathlib import Path
import copy,json,hashlib,tempfile,sys,math
from datetime import datetime
import yaml
import dual_sensor_plan as dual
from current_zopt_plan import ROOT,activate_preset
CAL_ID='dual60_recovery_calibration_v1'
LOCK_ID='dual60_recovery_blocks_v1'
PLAN_IDS=(CAL_ID,LOCK_ID)
PRESETS=('calibration','block1','block2')
ORDERS={'block1':'BFFB','block2':'FBBF'}
CATALOG=ROOT/'fleet/dual_sensor_recovery_60mm'
RECORD=dual.MODEL/'recovery_firmware_integration.json'
def build_id():return json.loads(RECORD.read_text())['build_id']
def documents(pid):
 if pid not in PRESETS:raise ValueError('未登録の復帰実験条件です')
 cal=pid=='calibration';s,p=dual.documents('b_calibration' if cal else 'b_once');s,p=copy.deepcopy(s),copy.deepcopy(p)
 order=ORDERS.get(pid,'');n=1 if cal else 4
 s.update(name='dual60_recovery_'+pid,delta_omega_rad=[.1*math.pi] if cal else [0.]*n,
  delta_omega_labels=['recovery_cal'] if cal else [f'{c}{j+1}' for j,c in enumerate(order)],
  trial_phase_offsets_rad={'12':[0.],'13':[0.]} if cal else {k:v*n for k,v in dual.initial_offsets().items()})
 s['research_plan'].update(id=CAL_ID if cal else LOCK_ID,preset_id=pid,kind='recovery',dual_profile='local_joint' if cal or order[0]=='B' else 'local_flex',
  expected_fw_build=build_id(),display_delta_omega_rad_s=[-.1*math.pi] if cal else [0.]*n,
  external_perturbation=not cal,recovery_order=order,kick_agent=13,kick_degrees=30,kick_times_ms=[60000,90000],kick_first_order='PNPN',
  shared_calibration=True)
 p['dual_controller'].update(profile=s['research_plan']['dual_profile'],calibration=None)
 return s,p

def estimate(path):
 sys.path.insert(0,str(ROOT/'analysis/python'))
 from calibrate_precalibrated_two_sensor_60mm_20261030 import estimate as recover
 path=Path(path);snap=json.loads((path/'sequence_plan.json').read_text());meta=snap['plan']['research_plan']
 if meta.get('id')!=CAL_ID or meta.get('expected_fw_build')!=build_id():raise ValueError('復帰実験専用FWで新しい共通校正が必要です')
 result=recover(path)
 if result.get('schema')!=2:raise ValueError('生ADCの両センサ校正が必要です')
 status=json.loads((path/'sequence_status.json').read_text())
 for aid in [12,13]:
  ack=[e for e in status['event_log'] if e.get('agent_id')==aid and e.get('event')=='UPLOAD_DONE'][-1]
  if ack.get('recovery') not in (0,'0'):raise ValueError('新しい復帰実験FWの読戻しが必要です')
 result.update(kind='recovery',sequence_dir=str(path.resolve()),session_id=status['final_upload']['session_id'])
 return result

def calibrations(experiments_dir=dual.offset.EXPERIMENTS):
 rows=[]
 for f in experiments_dir.glob('sequence_*/sequence_plan.json'):
  try:
   q=json.loads(f.read_text())
   if q['plan']['research_plan'].get('id')!=CAL_ID:continue
   r=estimate(f.parent);st=json.loads(f.with_name('sequence_status.json').read_text())
   rows.append(dict(session_id=r['session_id'],sequence_dir=str(f.parent),completed_at=st.get('completed_at'),motion_start_epoch=st.get('motion_start_epoch'),valid=True))
  except (ValueError,KeyError,OSError,TypeError,IndexError) as exc:
   try:
    snap=json.loads(f.read_text())
    if snap['plan']['research_plan'].get('id')!=CAL_ID:continue
    st=json.loads(f.with_name('sequence_status.json').read_text())
    rows.append(dict(session_id=(st.get('final_upload') or {}).get('session_id',f.parent.name),valid=False,error=str(exc)))
   except (ValueError,KeyError,OSError,TypeError):pass
 return sorted(rows,key=lambda r:r['session_id'],reverse=True)

def completed_blocks(experiments_dir=dual.offset.EXPERIMENTS):
 out=[]
 for f in experiments_dir.glob('sequence_*/sequence_plan.json'):
  try:
   snap=json.loads(f.read_text());meta=snap['plan']['research_plan'];st=json.loads(f.with_name('sequence_status.json').read_text())
   if meta.get('id')!=LOCK_ID or st.get('status')!='completed':continue
   up=st['final_upload']
   if not all(up['log_end'][str(i)]['ok'] for i in [12,13]):continue
   out.append(dict(preset_id=meta['preset_id'],calibration_session=meta['calibration_session'],session_id=up['session_id']))
  except (ValueError,KeyError,OSError,TypeError):continue
 return out

def prepare(pid,sequence_path,params_path,backup_root,session_id=None,experiments_dir=dual.offset.EXPERIMENTS):
 s,p=documents(pid);r=None
 if pid!='calibration':
  rows=[v for v in calibrations(experiments_dir) if v['session_id']==session_id and v.get('valid')]
  if len(rows)!=1:raise ValueError('新しい共通校正を回収・選択してください')
  r=estimate(rows[0]['sequence_dir']);p['dual_controller']['calibration']=r;s['research_plan']['calibration_session']=session_id
  if pid=='block2' and not any(b['preset_id']=='block1' and b['calibration_session']==session_id for b in completed_blocks(experiments_dir)):
   raise ValueError('同じ校正値でブロック1を回収してからブロック2を選択してください')
  for aid,entry in r['agents'].items():p['current_observer']['by_agent'][aid]['center_mA']=entry['current']['corrected_center_mA']
 with tempfile.TemporaryDirectory() as td:
  cat=Path(td);alias='60mm_mean_zopt_base7'
  (cat/f'{alias}.yaml').write_text(yaml.safe_dump(s,allow_unicode=True,sort_keys=False));(cat/f'params_{alias}.yaml').write_text(yaml.safe_dump(p,allow_unicode=True,sort_keys=False))
  backup=activate_preset(alias,sequence_path,params_path,backup_root,catalog=cat)
 return backup,r

def validate(plan):
 meta=plan['research_plan'];pid=meta['preset_id'];s,p=documents(pid);cal=pid=='calibration';actual=plan['sync_params_snapshot']
 if plan['agent_ids']!=[12,13] or plan['paddle']!='mid' or plan['feedback_source']!='dual' or plan['current_only'] is not False:raise ValueError('ID12/13・60mm・併用FWが必要です')
 for key in ['kappa','trial_duration_ms','sequence_log_hz','feedback_start_ms','analysis_window_start_s','analysis_window_end_s','trial_phase_offsets_rad']:
  if plan[key]!=s[key]:raise ValueError('復帰実験の設定不一致: '+key)
 if len(plan['phase_labels'])!=(1 if cal else 4) or any(abs(w-(2.9*math.pi if cal else 3*math.pi))>1e-8 for w in plan['partner_omegas_rad']):raise ValueError('試行数・Δωが不一致です')
 for key in ['id','recovery_order','kick_agent','kick_degrees','kick_times_ms','kick_first_order','expected_fw_build','design_sha256','external_perturbation']:
  if meta.get(key)!=s['research_plan'][key]:raise ValueError('外乱/設計条件の不一致: '+key)
 if actual['dual_controller']['profile']!=s['research_plan']['dual_profile'] or actual['current_observer']['phase_cycles']!=1:raise ValueError('制御則・前処理不一致')
 if actual.get('feedback_tau')!=0 or actual.get('center')!=110 or actual.get('amplitude')!=75 or actual.get('kappa')!=s['kappa']:raise ValueError('駆動パラメータ不一致')
 if not cal:
  r=actual['dual_controller']['calibration']
  if not r or estimate(r['sequence_dir'])!=r:raise ValueError('共通校正の出典・値が変更されています')
 for aid in ['12','13']:
  obs=actual['current_observer']['by_agent'][aid]
  if obs['width_mA']!=p['current_observer']['by_agent'][aid]['width_mA']:raise ValueError('電流幅不一致')
  if not cal and obs['center_mA']!=r['agents'][aid]['current']['corrected_center_mA']:raise ValueError('電流中心不一致')

def validate_targets(robots,plan,*,require_applied=True):
 # Same stopped-state/raw-ADC/calibration readbacks, different dedicated build.
 # Avoid mutating the legacy module's build selector while requests are concurrent.
 q=copy.deepcopy(plan);cal=plan['research_plan']['id']==CAL_ID
 q['research_plan']['id']=dual.CAL_ID if cal else dual.LOCK_ID
 q['research_plan']['kind']='b'  # Override profile check below; all numeric checks remain.
 want=plan['research_plan']['dual_profile']
 expected_build=build_id()
 for row in robots:
  if row.get('build')!=expected_build:raise ValueError('復帰実験専用FWのbuild IDが未確認です')
 # Legacy validator expects its own build and a B profile. Translate copies only.
 mapped=copy.deepcopy(robots)
 for row in mapped:
  row['build']=dual.build_id()
  if require_applied and row.get('dual_profile')==want:row['dual_profile']='local_joint'
  elif require_applied and not cal:raise ValueError('先頭の制御則が未適用です')
 return dual.validate_targets(mapped,q,require_applied=require_applied)

def export():
 CATALOG.mkdir(exist_ok=True)
 for pid in PRESETS:
  s,p=documents(pid);s['parameter_file']=str((CATALOG/f'params_{pid}.yaml').relative_to(ROOT))
  (CATALOG/f'{pid}.yaml').write_text(yaml.safe_dump(s,allow_unicode=True,sort_keys=False));(CATALOG/f'params_{pid}.yaml').write_text(yaml.safe_dump(p,allow_unicode=True,sort_keys=False))
if __name__=='__main__':export()
