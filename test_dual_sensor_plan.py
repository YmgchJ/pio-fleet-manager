"""Offline tests only: no robot commands."""
import asyncio,copy,json,math,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import pandas as pd
import dual_sensor_plan as m
from experiment_sequence import ExperimentSequenceController

class DualPilotTests(unittest.TestCase):
 def setUp(self):
  td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup);self.root=Path(td.name)
  self.seq=self.root/'seq.yaml';self.params=self.root/'params.yaml';self.exp=self.root/'exp';self.exp.mkdir()
  self.ctrl=ExperimentSequenceController(self.seq,self.exp,Mock(),lambda ids:[])
 def choose(self,pid,sid=None):return m.prepare(pid,self.seq,self.params,self.root/'backups',sid,self.exp)
 def fixture(self,kind='b'):
  self.choose(kind+'_calibration');plan=self.ctrl._load_plan(None);d=self.exp/('sequence_'+kind);d.mkdir();sid='upload_'+kind
  (d/'sequence_plan.json').write_text(json.dumps(dict(plan=plan,sequence_id='cal_'+kind)))
  rows=[]
  for aid in [12,13]:
   for k in range(3000):
    t=k*.02;rows.append(dict(agent_id=aid,micros32=int(t/256e-6),a0=int((t*(3 if aid==12 else 2.9)*math.pi%(2*math.pi))*255/(2*math.pi)),a1=147,a2=100))
  csv=d/'data.csv';pd.DataFrame(rows).to_csv(csv,index=False)
  events=[dict(agent_id=a,event='UPLOAD_DONE',build=m.build_id(),interaction_path='dual60_precalibrated_flex_current_ma5',kappa=0.,ina_ready=1,dual_ready=1,dual_fault=0,flex_unit='adc12',duration_ms=60000,mode='precalibrated_two_sensor',feedback_tau=0.,dual_cal='cal_'+kind,servo_center=110.,servo_amplitude=75.,omega=(3 if a==12 else 2.9)*math.pi,records=3000,trial_start_micros24_all='0',flex_c=1600.,flex_d=500.,flex_cal_n=2001) for a in [12,13]]
  (d/'sequence_status.json').write_text(json.dumps(dict(status='completed',event_log=events,final_upload=dict(session_id=sid,merged_csv=str(csv),log_end={str(a):dict(ok=True,records=3000) for a in [12,13]}))))
  return d,sid
 def robots(self,p,applied=True):
  ref=p['sync_params_snapshot']['dual_controller']['calibration'];out=[]
  for a in [12,13]:
   rr=ref['agents'][str(a)]
   out.append(dict(id=a,ip='127.0.0.1',paused=1,params=1,mode='precalibrated_two_sensor',flex_used=1,interaction_path='dual60_precalibrated_flex_current_ma5',build=m.build_id(),p_tau=0.,s_center=110.,s_amplitude=75.,dual_cal=ref['calibration_id'],dual_cal_ready=1,dual_flex_unit='adc12',dual_flex_c=rr['flex']['center'],dual_flex_d=rr['flex']['width'],dual_cur_c=rr['current']['corrected_center_mA'],dual_cur_d=rr['current']['width_mA'],kappa=1. if applied else 0.,omega=3*math.pi,dual_configured=int(applied),dual_fault=0,dual_profile=p['research_plan']['dual_profile']))
  return out
 def test_four_conditions(self):
  self.assertEqual(m.documents('b_once')[0]['trial_phase_offsets_rad'],m.documents('c_once')[0]['trial_phase_offsets_rad'])
  for kind in ['b','c']:
   self.choose(kind+'_calibration');p=self.ctrl._load_plan(None)
   self.assertEqual((p['kappa'],p['trial_duration_ms'],p['sequence_log_hz']),(0,60000,50))
   self.assertAlmostEqual(p['partner_omegas_rad'][0],2.9*math.pi)
  with self.assertRaises(ValueError):self.choose('b_once','old')
 def test_calibrate_then_one_run(self):
  for kind in ['b','c']:
   d,sid=self.fixture(kind);self.assertEqual(m.estimate(d)['schema'],2)
   self.choose(kind+'_once',sid);p=self.ctrl._load_plan(None)
   self.assertEqual((p['kappa'],p['trial_duration_ms'],p['sequence_log_hz']),(1,120000,25))
   self.assertEqual(p['trial_phase_offsets_rad'],m.initial_offsets());self.assertEqual(len(p['phase_labels']),1)
   self.assertAlmostEqual(p['phase_differences_rad'][0],m.initial_offsets()['13'][0]-m.initial_offsets()['12'][0])
   m.validate_targets(self.robots(p),p);m.validate_targets(self.robots(p,False),p,require_applied=False)
   with self.assertRaises(ValueError):m.validate_targets(self.robots(p,False),p)
 def test_stale_or_wrong_readbacks(self):
  d,sid=self.fixture();self.choose('b_once',sid);p=self.ctrl._load_plan(None)
  for key,val in [('dual_cal','rebooted'),('build','old'),('dual_profile','local_flex'),('dual_cur_c',0.),('dual_configured',0)]:
   robots=self.robots(p);robots[0][key]=val
   with self.subTest(key=key),self.assertRaises(ValueError):m.validate_targets(robots,p)
  robots=self.robots(p);robots[0]['dual_cal']='rebooted'
  with self.assertRaises(ValueError):m.validate_targets(robots,p,require_applied=False)
 def test_mutated_settings_and_wrong_calibration_kind(self):
  d,sid=self.fixture()
  with self.assertRaises(ValueError):self.choose('c_once',sid)
  self.choose('b_once',sid);p=self.ctrl._load_plan(None)
  for key in ['width','center','cycles','profile']:
   bad=copy.deepcopy(p);params=bad['sync_params_snapshot']
   if key=='width':params['current_observer']['by_agent']['12']['width_mA']+=1
   if key=='center':params['current_observer']['by_agent']['12']['center_mA']+=1
   if key=='cycles':params['current_observer']['phase_cycles']=2
   if key=='profile':params['dual_controller']['profile']='local_flex'
   with self.subTest(key=key),self.assertRaises(ValueError):m.validate(bad)
 def test_explicit_apply_and_reboot_rejection(self):
  import server
  d,sid=self.fixture();self.choose('b_once',sid);p=self.ctrl._load_plan(None)
  self.ctrl.target_provider=lambda ids:self.robots(p,False)
  with patch.object(server,'sequence_controller',self.ctrl),patch.object(server.socket,'socket'),patch.object(server,'_send_udp',return_value=True) as send:
   asyncio.run(server.apply_dual_sensor_profile());self.assertEqual(send.call_count,2)
   for call in send.call_args_list:self.assertIn(b'DUAL_CONFIG,profile:local_joint,cal:cal_b,',call.args[1])
   robots=self.robots(p,False);robots[0]['dual_cal']='after_reboot';self.ctrl.target_provider=lambda ids:robots
   send.reset_mock()
   with self.assertRaises(server.HTTPException):asyncio.run(server.apply_dual_sensor_profile())
   send.assert_not_called()
 def test_live_build_id_and_dual_readback(self):
  import server
  protocol=server.UdpDiscoveryProtocol()
  with patch.dict(server.agent_live_state,{},clear=True),patch.object(server.log_receiver,'record_status'),patch.object(server,'broadcast_telemetry'),patch.object(server,'update_agent_ip'):
   status=b'STATUS,id:12,paused:1,params:1,mode:precalibrated_two_sensor,build:test'
   protocol.datagram_received(status,('127.0.0.1',5012))
   protocol.datagram_received(b'DUAL_STATUS,id:12,configured:1,profile:local_joint,build:test',('127.0.0.1',5012))
   protocol.datagram_received(status,('127.0.0.1',5012))
   self.assertEqual(server.agent_live_state[12]['build'],'test')
   self.assertEqual(server.agent_live_state[12]['dual_profile'],'local_joint')
 def test_seven_conditions_common_phase_and_one_calibration(self):
  for kind in ['b','c']:
   d,sid=self.fixture(kind);ref=m.estimate(d)
   self.choose(kind+'_base7',sid);p=self.ctrl._load_plan(None)
   self.assertEqual(len(p['phase_labels']),7)
   self.assertEqual(p['research_plan']['display_delta_omega_rad_s'],m.GRID)
   self.assertEqual(p['trial_duration_ms']*p['sequence_log_hz']*7/1000,21000)
   self.assertEqual(p['sync_params_snapshot']['dual_controller']['calibration'],ref)
   for aid,phases in p['trial_phase_offsets_rad'].items():self.assertEqual(phases,m.initial_offsets()[aid]*7)
   self.assertEqual(p['phase_differences_rad'],[m.initial_offsets()['13'][0]-m.initial_offsets()['12'][0]]*7)
   for omega,delta in zip(p['partner_omegas_rad'],m.GRID):self.assertAlmostEqual(omega-3*math.pi,delta)
   self.assertEqual(20+7*120+6*20,980)
   m.validate_targets(self.robots(p),p)
   self.ctrl._validate_arm_command_lengths(p)
   bad=copy.deepcopy(p);bad['partner_omegas_rad'][-1]+=.05
   with self.assertRaises(ValueError):m.validate(bad)
   bad=copy.deepcopy(p);bad['trial_phase_offsets_rad']['12'][-1]+=.1
   with self.assertRaises(ValueError):m.validate(bad)
 def test_calibration_event_without_build_uses_live_verification(self):
  d,sid=self.fixture();file=d/'sequence_status.json';status=json.loads(file.read_text())
  for e in status['event_log']:e.pop('build',None)
  file.write_text(json.dumps(status))
  self.assertEqual(m.estimate(d)['session_id'],sid)
  self.choose('b_base7',sid);p=self.ctrl._load_plan(None)
  m.validate_targets(self.robots(p),p)
  robots=self.robots(p);robots[0]['build']='wrong'
  with self.assertRaises(ValueError):m.validate_targets(robots,p,require_applied=False)
  status['event_log'][0]['build']='explicit_wrong_build';file.write_text(json.dumps(status))
  with self.assertRaises(ValueError):m.estimate(d)
  self.assertEqual(m.calibrations(self.exp),[])
  diagnostics=m.calibrations(self.exp,include_invalid=True)
  self.assertEqual(len(diagnostics),1);self.assertFalse(diagnostics[0]['valid'])
  self.assertIn('build ID',diagnostics[0]['error'])
 def test_stale_rows_outside_calibration_are_isolated_but_missing_rows_rejected(self):
  d,sid=self.fixture('c');file=d/'data.csv';df=pd.read_csv(file)
  old=df[df.agent_id==13].copy();old['micros32']+=int(400/.000256)
  pd.concat([df,old],ignore_index=True).to_csv(file,index=False)
  ref=m.estimate(d)
  self.assertEqual(ref['record_selection']['13']['excluded_records'],3000)
  self.assertEqual(ref['record_selection']['13']['selected_records'],3000)
  self.choose('c_base7',sid);self.ctrl._load_plan(None)
  pd.concat([df.iloc[1:],old],ignore_index=True).to_csv(file,index=False)
  with self.assertRaises(ValueError):m.estimate(d)
 def test_environment(self):
  import server
  with patch.object(server,'_dual_sequence_active',return_value=True):
   self.assertEqual(server._pio_usb_env_name(),'pico_w_precalibrated_two_sensor')
   self.assertEqual(server._pio_ota_env_name(),'ota_precalibrated_two_sensor')
if __name__=='__main__':unittest.main()
