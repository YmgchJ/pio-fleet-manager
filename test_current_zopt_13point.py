"""Offline tests: no robot sockets, OTA, START or live configuration writes."""
import math,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import yaml
from current_zopt_plan import PRESET_IDS,activate_preset
from experiment_sequence import ExperimentSequenceController,SequenceValidationError
from sync_params import build_param_payload
class Current13Tests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.seq=self.root/'seq.yaml';self.params=self.root/'params.yaml';self.ctrl=ExperimentSequenceController(self.seq,self.root,Mock(),lambda ids:[])
 def choose(self,pid):
  activate_preset(pid,self.seq,self.params,self.root/'backups');return self.ctrl._load_plan(None)
 def statuses(self,plan):
  p=plan['sync_params_snapshot'];out=[]
  for a in (12,13):
   obs=p['current_observer']['by_agent'][str(a)];r=dict(id=a,ip='127.0.0.1',paused=1,params=1,src=1,zmode=1,mode='current_only',interaction_path='current_50hz_ma5_fixed_affine',flex_used=0,kappa=1,omega=3*math.pi,cur_center=obs['center_mA'],cur_width=obs['width_mA'],p_n=p['prc_n'],p_tau=p['feedback_tau'],s_center=p['center'],s_amplitude=p['amplitude'],p_a0=p['prc_a0'])
   for n in range(1,5):r[f'p_a{n}']=p.get(f'prc_a{n}',0);r[f'p_b{n}']=p.get(f'prc_b{n}',0)
   out.append(r)
  return out
 def test_all_twelve_presets(self):
  self.assertEqual(len(PRESET_IDS),12)
  for mm in (40,60,80):
   pair=[]
   for kind in ('single','mean'):
    xs=[];obs=[];ph=[]
    for batch in ('base7','extra6'):
     p=self.choose(f'{mm}mm_{kind}_zopt_{batch}');self.assertEqual((p['kappa'],p['trial_duration_ms'],p['analysis_window_start_s'],p['analysis_window_end_s'],p['sequence_log_hz']),(1,120000,60,120,25));self.assertLessEqual(len(p['phase_labels'])*120*25,21000);xs+=p['research_plan']['display_delta_omega_rad_s'];obs.append(p['sync_params_snapshot']['current_observer']);ph.append(p['trial_phase_offsets_rad']);self.assertEqual(p['phase_difference_definition'],'phi_13 - phi_12 = phi2 - phi1')
     for x,w in zip(p['research_plan']['display_delta_omega_rad_s'],p['partner_omegas_rad']):self.assertAlmostEqual(w-3*math.pi,x)
     for j,psi in enumerate(p['phase_differences_rad']):self.assertAlmostEqual(psi,p['trial_phase_offsets_rad']['13'][j]-p['trial_phase_offsets_rad']['12'][j])
     self.ctrl._plan=p;self.ctrl._targets=[{'id':12},{'id':13}];self.ctrl._send_one=Mock();self.ctrl._send_arm_commands('offline',1,p['phase_labels'][0],p['phase_differences_rad'][0],len(p['phase_labels']));self.assertLessEqual(len(self.ctrl._send_one.call_args.args[1].encode()),1024)
     for a in (12,13):self.assertIn('cur_width:',build_param_payload(a,p['sync_params_snapshot']))
    self.assertEqual(sorted(round(x,2) for x in xs),[round(i*.05,2) for i in range(-6,7)]);self.assertEqual(obs[0],obs[1]);pair.append((obs,ph))
   self.assertEqual(pair[0],pair[1])
 def test_readback_guard(self):
  p=self.choose('40mm_single_zopt_base7');r=self.statuses(p);self.ctrl.target_provider=lambda ids:r;self.assertEqual(len(self.ctrl._validate_targets([12,13],p)),2)
  for field,value in [('interaction_path','current_50hz_ma5_rolling_p10_p90'),('cur_width',1),('cur_center',None),('p_a3',99),('s_amplitude',70)]:
   old=r[0][field];r[0][field]=value
   with self.subTest(field=field),self.assertRaises(SequenceValidationError):self.ctrl._validate_targets([12,13],p)
   r[0][field]=old
 def test_wrong_conditions_rejected(self):
  for key,value in [('trial_duration_ms',60000),('sequence_log_hz',50),('kappa',5)]:
   self.choose('60mm_mean_zopt_base7');s=yaml.safe_load(self.seq.read_text());s[key]=value;self.seq.write_text(yaml.safe_dump(s))
   with self.subTest(key=key),self.assertRaises(SequenceValidationError):self.ctrl._load_plan(None)
 def test_zsin_cannot_replace_the_comparison(self):
  self.choose('40mm_single_zopt_base7');p=yaml.safe_load(self.params.read_text());p['z_mode']='zsin';self.params.write_text(yaml.safe_dump(p))
  with self.assertRaisesRegex(SequenceValidationError,'Zopt,1／Zopt,10のみ'):self.ctrl._load_plan(None)
 def test_ready_api_and_upload_profile(self):
  import server
  p=self.choose('80mm_mean_zopt_extra6');robots=self.statuses(p);self.ctrl.target_provider=lambda ids:robots
  with patch.object(server,'sequence_controller',self.ctrl),patch.object(server,'_sequence_config_path',return_value=self.seq),patch.object(server,'SYNC_PARAMS_PATH',self.params):
   result=server.get_current_zopt_plan();self.assertEqual(len(result['presets']),12);self.assertEqual(result['active'],'80mm_mean_zopt_extra6');self.assertTrue(result['ready']);self.assertEqual(result['upload_profile']['pio_ota_env'],'ota')
 def test_global_start_cannot_bypass_validation(self):
  import server,asyncio
  from fastapi import HTTPException
  self.choose('40mm_single_zopt_base7')
  with patch.object(server,'_sequence_config_path',return_value=self.seq),patch.object(server.socket,'socket') as sock:
   with self.assertRaises(HTTPException) as raised:asyncio.run(server.send_global_command(server.GlobalCommandRequest(command='START')))
   self.assertEqual(raised.exception.status_code,409);sock.return_value.sendto.assert_not_called()
if __name__=='__main__':unittest.main()
