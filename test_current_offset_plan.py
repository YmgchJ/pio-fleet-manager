"""Offline tests: no robot I/O, live configuration, or automatic starts."""
import copy,json,math,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import numpy as np,pandas as pd,yaml
import current_offset_plan as m
from experiment_sequence import ExperimentSequenceController,SequenceValidationError
class OffsetTests(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.seq=self.root/'seq.yaml';self.par=self.root/'params.yaml';self.exp=self.root/'experiments';self.exp.mkdir();self.ctrl=ExperimentSequenceController(self.seq,self.exp,Mock(),lambda ids:[])
 def choose(self,pid,sid=None):return m.prepare(pid,self.seq,self.par,self.root/'backups',sid,self.exp)
 def fixture(self):
  self.choose('60mm_single_calibration');p=self.ctrl._load_plan(None);d=self.exp/'sequence_cal';d.mkdir();(d/'sequence_plan.json').write_text(json.dumps({'plan':p}));rows=[]
  for aid,code in [(12,147),(13,144)]:
   for k in range(3000):
    tick=int(k*.02/256e-6);w=3*math.pi if aid==12 else 2.9*math.pi;rows.append(dict(agent_id=aid,micros32=tick,a0=int((k*.02*w%(2*math.pi))*255/(2*math.pi)),a1=code))
  csv=d/'data.csv';pd.DataFrame(rows).to_csv(csv,index=False)
  st=dict(status='completed',completed_at='2026-10-27T01:00:00Z',final_upload=dict(session_id='test_cal',merged_csv=str(csv),log_end={str(a):dict(ok=True,records=3000) for a in (12,13)}),event_log=[dict(agent_id=a,event='UPLOAD_DONE',trial_start_micros24_all='0',records=3000,ina_ready=1,ina_ok=3000,interaction_path='current_50hz_ma5_fixed_affine',kappa=0,servo_center=110.,servo_amplitude=75.,sample_period_us=20000) for a in (12,13)])
  (d/'sequence_status.json').write_text(json.dumps(st));raw=self.root/'raw.npz';cs={}
  for aid in (12,13):
   for h in range(1,11):
    c=np.zeros((5,5));c[0,0]=100;cs[f'raw_mA_60_{aid}_{h}']=c
  np.savez(raw,**cs);return d,raw
 def test_calibration_catalog(self):
  for pid in m.CAL_PRESETS:
   self.choose(pid);p=self.ctrl._load_plan(None);self.assertEqual((p['kappa'],p['trial_duration_ms'],p['sequence_log_hz']),(0,60000,50));self.assertAlmostEqual(p['partner_omegas_rad'][0],2.9*math.pi);self.assertEqual(p['phase_differences_rad'],[0.])
  for pid in m.LOCK_PRESETS:
   with self.assertRaises(ValueError):self.choose(pid)
 def test_math_and_freeze(self):
  d,raw=self.fixture()
  with patch.object(m,'RAW',raw):
   ref=m.estimate(d)
   for aid,code in [('12',147),('13',144)]:self.assertAlmostEqual(ref['agents'][aid]['offset_mA'],(code-128)*1000/127-100,places=9)
   self.choose('60mm_single_zopt_base7','test_cal');p=self.ctrl._load_plan(None);original=yaml.safe_load(self.par.read_text());self.assertEqual(p['sync_params_snapshot']['current_observer']['session_offset_calibration'],ref)
   base=m.documents('60mm_single_zopt_base7')[1]
   for aid in ('12','13'):self.assertEqual(original['current_observer']['by_agent'][aid]['width_mA'],base['current_observer']['by_agent'][aid]['width_mA'])
   with self.assertRaises(ValueError):self.choose('60mm_single_zopt_extra6','test_cal')
   for field in ['center','width','Z','record']:
    changed=copy.deepcopy(original)
    if field=='center':changed['current_observer']['by_agent']['12']['center_mA']+=1
    if field=='width':changed['current_observer']['by_agent']['12']['width_mA']+=1
    if field=='Z':changed['zopt_profile']='cur_zopt_60mm_mean10'
    if field=='record':changed['current_observer']['session_offset_calibration']['csv_sha256']='bad'
    self.par.write_text(yaml.safe_dump(changed))
    with self.subTest(field=field),self.assertRaises(SequenceValidationError):self.ctrl._load_plan(None)
   self.par.write_text(yaml.safe_dump(original));b=self.exp/'sequence_base';b.mkdir();(b/'sequence_plan.json').write_text(json.dumps({'plan':p}));(b/'sequence_status.json').write_text(json.dumps(dict(status='completed',final_upload=dict(merged_csv=str(d/'data.csv'),session_id='base',log_end={str(i):dict(ok=True,records=21000) for i in (12,13)}))))
   self.choose('60mm_single_zopt_extra6','test_cal');self.assertEqual(yaml.safe_load(self.par.read_text())['current_observer'],original['current_observer'])
   with patch.object(m,'completed_batches',return_value=[dict(preset_id='60mm_single_zopt_base7',calibration_session='test_cal')]):self.ctrl._load_plan(None)
 def test_invalid_calibration(self):
  d,raw=self.fixture();sp=d/'sequence_status.json';st=json.loads(sp.read_text())
  for which in ('missing','path','records'):
   bad=copy.deepcopy(st)
   if which=='missing':bad['final_upload']['log_end']['13']['ok']=False
   if which=='path':bad['event_log'][0]['interaction_path']='current_50hz_ma5_rolling_p10_p90'
   if which=='records':bad['final_upload']['log_end']['12']['records']=100
   sp.write_text(json.dumps(bad))
   with patch.object(m,'RAW',raw),self.subTest(which=which),self.assertRaises(ValueError):m.estimate(d)
 def test_live_readback(self):
  from test_current_zopt_13point import Current13Tests
  self.choose('40mm_single_calibration');p=self.ctrl._load_plan(None);r=Current13Tests.statuses(self,p)
  for a in r:a['kappa']=0
  self.ctrl.target_provider=lambda ids:r;self.assertEqual(len(self.ctrl._validate_targets([12,13],p)),2);r[0]['cur_center']+=20
  with self.assertRaises(SequenceValidationError):self.ctrl._validate_targets([12,13],p)
 def test_offset_api_and_start_guard(self):
  import server,asyncio
  from fastapi import HTTPException
  self.choose('40mm_single_calibration')
  with patch.object(server,'sequence_controller',self.ctrl),patch.object(server,'_sequence_config_path',return_value=self.seq),patch.object(server,'SYNC_PARAMS_PATH',self.par),patch.object(server,'EXPERIMENTS_DIR',self.exp):
   result=server.get_current_offset_plan();self.assertEqual(result['active']['preset_id'],'40mm_single_calibration');self.assertFalse(result['ready']);self.assertEqual(result['calibrations'],[])
   with patch.object(server.socket,'socket') as sock:
    with self.assertRaises(HTTPException):asyncio.run(server.send_global_command(server.GlobalCommandRequest(command='START')))
    sock.return_value.sendto.assert_not_called()
   self.ctrl._state={'status':'running_autonomous'}
   with self.assertRaises(HTTPException):asyncio.run(server.select_current_offset_preset(server.CurrentOffsetPresetRequest(preset_id='60mm_single_calibration')))
 def test_selection_preserves_pending_completion_metadata(self):
  import server,asyncio
  self.choose('60mm_single_calibration')
  old=dict(status='completed',sequence_id='previous',final_upload={'session_id':'kept'},event_log=[{'event':'UPLOAD_DONE'}])
  self.ctrl._state=copy.deepcopy(old)
  task=Mock();task.done.return_value=False;self.ctrl._task=task
  with patch.object(server,'sequence_controller',self.ctrl),patch.object(server,'_sequence_config_path',return_value=self.seq),patch.object(server,'SYNC_PARAMS_PATH',self.par),patch.object(server,'EXPERIMENTS_DIR',self.exp):
   asyncio.run(server.select_current_offset_preset(server.CurrentOffsetPresetRequest(preset_id='60mm_single_calibration')))
   self.assertEqual(self.ctrl._state,old)
   with self.assertRaisesRegex(SequenceValidationError,'終了処理中'):asyncio.run(self.ctrl.start())
   self.ctrl._set_status('completed',completed_at='later')
   self.assertEqual(self.ctrl._state['final_upload'],old['final_upload'])
   self.assertEqual(self.ctrl._state['event_log'],old['event_log'])
if __name__=='__main__':unittest.main()
