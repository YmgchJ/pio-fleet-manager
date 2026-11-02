import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import dual_sensor_recovery_plan as rec
import test_dual_sensor_plan as pilot_tests
from experiment_sequence import ExperimentSequenceController,SequenceValidationError

class RecoveryPlanTests(unittest.TestCase):
 def setUp(self):
  self.helper=pilot_tests.DualPilotTests('test_four_conditions');self.helper.setUp();self.addCleanup(self.helper.doCleanups)
  h=self.helper;self.root=h.root;self.seq=h.seq;self.params=h.params;self.exp=h.exp
  self.ctrl=ExperimentSequenceController(self.seq,self.exp,Mock(),lambda ids:[])
 def choose(self,pid,sid=None):return rec.prepare(pid,self.seq,self.params,self.root/'new_backups',sid,self.exp)
 def fixture(self):
  d,sid=self.helper.fixture('b')
  self.choose('calibration');plan=self.ctrl._load_plan(None)
  q=json.loads((d/'sequence_plan.json').read_text());q['plan']=plan;(d/'sequence_plan.json').write_text(json.dumps(q))
  status=json.loads((d/'sequence_status.json').read_text())
  for e in status['event_log']:e['recovery']='0';e['build']=rec.build_id()
  (d/'sequence_status.json').write_text(json.dumps(status));return d,sid
 def robots(self,p):
  robots=self.helper.robots(p)
  for r in robots:r['build']=rec.build_id();r['dual_profile']=p['research_plan']['dual_profile']
  return robots
 def test_fresh_calibration_and_blocks(self):
  d,sid=self.fixture();self.choose('block1',sid);p=self.ctrl._load_plan(None)
  self.assertEqual(p['research_plan']['recovery_order'],'BFFB')
  self.assertEqual(len(p['phase_labels']),4);self.assertEqual(p['trial_phase_offsets_rad']['12'],[p['trial_phase_offsets_rad']['12'][0]]*4)
  self.assertEqual(p['research_plan']['kick_times_ms'],[60000,90000]);self.assertEqual(p['research_plan']['kick_degrees'],30)
  for aid in [12,13]:
   cmd=self.ctrl.build_arm_command('ps000000_000000',1,'B1',p['phase_differences_rad'][0],4,aid,p)
   self.assertIn('recovery:BFFB',cmd)
  self.ctrl._validate_arm_command_lengths(p);rec.validate_targets(self.robots(p),p)
  with self.assertRaises(ValueError):self.choose('block2',sid)
  b=self.exp/'sequence_block1';b.mkdir();(b/'sequence_plan.json').write_text(json.dumps({'plan':p}))
  (b/'sequence_status.json').write_text(json.dumps(dict(status='completed',final_upload=dict(session_id='block1',log_end={str(a):dict(ok=True) for a in [12,13]}))))
  cal1=p['sync_params_snapshot']['dual_controller']['calibration']
  self.choose('block2',sid);p=self.ctrl._load_plan(None)
  self.assertEqual(p['research_plan']['recovery_order'],'FBBF');self.assertEqual(p['sync_params_snapshot']['dual_controller']['calibration'],cal1)
  rec.validate_targets(self.robots(p),p)
 def test_wrong_build_profile_kick_and_changed_calibration_rejected(self):
  d,sid=self.fixture();self.choose('block1',sid);p=self.ctrl._load_plan(None)
  for field,value in [('build','old'),('dual_profile','noise_fixed_return_joint'),('dual_cal','old')]:
   robots=self.robots(p);robots[0][field]=value
   with self.subTest(field=field),self.assertRaises(ValueError):rec.validate_targets(robots,p)
  for field,value in [('kick_degrees',20),('recovery_order','BBBB'),('kick_times_ms',[30000,60000])]:
   q=copy.deepcopy(p);q['research_plan'][field]=value
   with self.subTest(field=field),self.assertRaises(ValueError):rec.validate(q)
 def test_only_recovery_calibration_accepted(self):
  d,sid=self.helper.fixture('b')
  with self.assertRaises(ValueError):rec.estimate(d)
  self.choose('calibration');p=self.ctrl._load_plan(None);self.assertEqual(p['kappa'],0)
 def test_upload_environments(self):
  import server
  with patch.object(server,'_recovery_sequence_active',return_value=True):
   self.assertEqual(server._pio_ota_env_name(),'ota_two_sensor_recovery')
   self.assertEqual(server._pio_usb_env_name(),'pico_w_two_sensor_recovery')
if __name__=='__main__':unittest.main()
