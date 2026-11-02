import importlib.util,json,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import test_dual_sensor_recovery_plan as setup_tests
import dual_sensor_recovery_plan as rec
spec=importlib.util.spec_from_file_location('recovery_analysis',Path(__file__).resolve().parents[1]/'analysis/python/analyze_two_sensor_recovery_60mm_20261030.py')
analysis=importlib.util.module_from_spec(spec);spec.loader.exec_module(analysis)

class RecoveryAnalysisTests(unittest.TestCase):
 def test_complete_synthetic_blocks(self):
  h=setup_tests.RecoveryPlanTests('test_fresh_calibration_and_blocks');h.setUp();self.addCleanup(h.doCleanups)
  calpath,sid=h.fixture();cal=rec.estimate(calpath)
  for block in ['block1','block2']:
   h.choose(block,sid);plan=h.ctrl._load_plan(None);folder=h.exp/('sequence_'+block);folder.mkdir();order=rec.ORDERS[block]
   (folder/'sequence_plan.json').write_text(json.dumps(dict(sequence_id=block,plan=plan)))
   rows=[];events=[];starts=[k*546875 for k in range(4)];join=lambda x:';'.join(map(str,x))
   for aid in [12,13]:
    for k in range(4):
     t=np.arange(0,120,.04);phase=3*np.pi*t+4.9049095
     if aid==13:
      phase+=.05
      for at,sign in [(60,1 if k%2==0 else -1),(90,-1 if k%2==0 else 1)]:
       mask=t>=at;phase[mask]+=sign*np.pi/6*np.exp(-(t[mask]-at)/2)
     for j in range(len(t)):
      micros=starts[k]+int(round(t[j]/.000256))
      rows.append(dict(agent_id=aid,micros32=micros,time_local_sec=micros*.000256,a0=int((phase[j]%(2*np.pi))*255/(2*np.pi))))
    rr=cal['agents'][str(aid)]
    event=dict(event='UPLOAD_DONE',agent_id=aid,recovery='1',r_profiles=order,dual_fault=0,ina_fail=0,dual_cal=cal['calibration_id'],
     flex_c=rr['flex']['center'],flex_d=rr['flex']['width'],dual_cur_c=rr['current']['corrected_center_mA'],dual_cur_d=rr['current']['width_mA'],
     records=12000,trial_start_micros24_all=join(starts),r_n='2;2;2;2',r_a1=join([v+234375 for v in starts]),r_a2=join([v+351562 for v in starts]),
     r_d1=join([(1 if k%2==0 else -1)*np.pi/6 if aid==13 else 0 for k in range(4)]),r_d2=join([(-1 if k%2==0 else 1)*np.pi/6 if aid==13 else 0 for k in range(4)]),
     r_base_rms='0.1;0.1;0.1;0.1',r_return_rms='0.2;0.2;0.2;0.2',r_return_peak='0.3;0.3;0.3;0.3')
    events.append(event)
   csv=folder/'data.csv';pd.DataFrame(rows).to_csv(csv,index=False)
   (folder/'sequence_status.json').write_text(json.dumps(dict(status='completed',event_log=events,final_upload=dict(session_id=block,merged_csv=str(csv),log_end={str(a):dict(ok=True,records=12000) for a in [12,13]}))))
  blocks=rec.completed_blocks(h.exp)
  with patch.object(rec.dual.offset,'EXPERIMENTS',h.exp),patch.object(analysis,'ROOT',h.root),patch.object(rec,'completed_blocks',return_value=blocks):
   result=analysis.analyze(sid)
  self.assertEqual(len(result),16)
  self.assertTrue(result.half_return_s.between(1,2).all())
  self.assertEqual(result.groupby('controller').size().to_dict(),{'B':8,'Flex_fixed':8})

if __name__=='__main__':unittest.main()
