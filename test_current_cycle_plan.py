"""Cycle2 presets, source freezing, FW readback and native interpolation tests."""
import asyncio
import copy
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import numpy as np
import pandas as pd
import yaml
import current_cycle_plan as m
import current_offset_plan as offset
from experiment_sequence import ExperimentSequenceController, SequenceValidationError
from sync_params import build_param_payload
import test_current_zopt_13point as baseline_tests


class CyclePlanTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.seq=self.root/'seq.yaml';self.params=self.root/'params.yaml'
        self.exp=self.root/'experiments';self.exp.mkdir()
        self.ctrl=ExperimentSequenceController(self.seq,self.exp,Mock(),lambda ids:[])

    def choose(self,pid,sid=None):
        return m.prepare(pid,self.seq,self.params,self.root/'backups',sid,self.exp)

    def fixture(self):
        self.choose(m.CAL_PRESET);plan=self.ctrl._load_plan(None)
        d=self.exp/'sequence_newcal';d.mkdir()
        (d/'sequence_plan.json').write_text(json.dumps({'plan':plan}))
        rows=[]
        for aid,code in ((12,147),(13,144)):
            for k in range(3000):
                w=(3 if aid==12 else 2.9)*math.pi
                rows.append(dict(agent_id=aid,micros32=int(k*.02/256e-6),
                                 a0=int((k*.02*w%(2*math.pi))*255/(2*math.pi)),a1=code))
        csv=d/'data.csv';pd.DataFrame(rows).to_csv(csv,index=False)
        ack=[dict(agent_id=a,event='UPLOAD_DONE',trial_start_micros24_all='0',records=3000,
                  ina_ready=1,ina_ok=3000,interaction_path='current_50hz_ma5_fixed_affine',
                  kappa=0,servo_center=110.,servo_amplitude=75.,sample_period_us=20000,
                  cur_cycles=1,cycle_faults=0) for a in (12,13)]
        st=dict(status='completed',completed_at='2026-10-29T00:00:00Z',event_log=ack,
                final_upload=dict(session_id='newcal',merged_csv=str(csv),
                                  log_end={str(a):dict(ok=True,records=3000) for a in (12,13)}))
        (d/'sequence_status.json').write_text(json.dumps(st))
        raw=self.root/'raw.npz';values={}
        for aid in (12,13):
            for h in range(1,11):
                c=np.zeros((5,5));c[0,0]=100;values[f'raw_mA_60_{aid}_{h}']=c
        np.savez(raw,**values)
        return d,raw

    def statuses(self,plan):
        robots=baseline_tests.Current13Tests.statuses(self,plan)
        cycles=plan['sync_params_snapshot']['current_observer']['phase_cycles']
        for r in robots:
            r['cur_cycles']=cycles;r['kappa']=plan['kappa'];r['cycle_faults']=0
            if cycles==2:r['interaction_path']='current_50hz_ma5_cycle2_fixed_affine'
        return robots

    def test_catalog_and_no_old_calibration(self):
        self.choose(m.CAL_PRESET);p=self.ctrl._load_plan(None)
        self.assertEqual(p['sync_params_snapshot']['current_observer']['phase_cycles'],1)
        self.assertEqual((p['trial_duration_ms'],p['sequence_log_hz']),(60000,50))
        self.assertAlmostEqual(p['partner_omegas_rad'][0],2.9*math.pi)
        for pid in m.LOCK_PRESETS:
            with self.assertRaisesRegex(ValueError,'新規補正'):self.choose(pid,'20261027_170120')

    def test_calibration_and_frozen_base7(self):
        d,raw=self.fixture()
        with patch.object(offset,'RAW',raw):
            ref=m.estimate(d)
            self.assertEqual(ref['calibration_preset'],m.CAL_PRESET)
            self.assertAlmostEqual(ref['agents']['12']['offset_mA'],19*1000/127-100)
            self.choose(m.LOCK_PRESETS[0],'newcal');p=self.ctrl._load_plan(None)
            self.assertEqual((p['kappa'],p['trial_duration_ms'],p['sequence_log_hz']),(1,120000,25))
            self.assertEqual((p['analysis_window_start_s'],p['analysis_window_end_s']),(60,120))
            self.assertEqual(p['research_plan']['display_delta_omega_rad_s'],[-.3,-.2,-.1,0,.1,.2,.3])
            self.assertAlmostEqual(p['research_plan']['lock_criterion_rad'],.2*math.pi)
            self.assertEqual(p['sync_params_snapshot']['current_observer']['phase_cycles'],2)
            self.assertIn('cur_cycles:2',build_param_payload(12,p['sync_params_snapshot']))
            with self.assertRaisesRegex(ValueError,'7点'):self.choose(m.LOCK_PRESETS[1],'newcal')
            original=yaml.safe_load(self.params.read_text())
            for field in ('phase_cycles','width','Z'):
                bad=copy.deepcopy(original)
                if field=='phase_cycles':bad['current_observer']['phase_cycles']=1
                if field=='width':bad['current_observer']['by_agent']['12']['width_mA']+=1
                if field=='Z':bad['zopt_profile']='cur_zopt_60mm_single1'
                self.params.write_text(yaml.safe_dump(bad))
                with self.subTest(field=field),self.assertRaises((SequenceValidationError,FileNotFoundError)):
                    self.ctrl._load_plan(None)
            self.params.write_text(yaml.safe_dump(original))
            # Same source/center/width must serve additional six points.
            completed=dict(preset_id=m.LOCK_PRESETS[0],calibration_session='newcal')
            with patch.object(m,'completed_batches',return_value=[completed]):
                self.choose(m.LOCK_PRESETS[1],'newcal');extra=self.ctrl._load_plan(None)
            self.assertEqual(extra['sync_params_snapshot']['current_observer'],original['current_observer'])
            self.assertEqual(extra['research_plan']['display_delta_omega_rad_s'],[-.25,-.15,-.05,.05,.15,.25])

    def test_old_or_wrong_fw_cannot_start(self):
        self.choose(m.CAL_PRESET);p=self.ctrl._load_plan(None);robots=self.statuses(p)
        self.ctrl.target_provider=lambda ids:robots
        self.assertEqual(len(self.ctrl._validate_targets([12,13],p)),2)
        del robots[0]['cur_cycles']
        with self.assertRaisesRegex(SequenceValidationError,'周期'):self.ctrl._validate_targets([12,13],p)
        d,raw=self.fixture()
        with patch.object(offset,'RAW',raw):
            self.choose(m.LOCK_PRESETS[0],'newcal');p=self.ctrl._load_plan(None);robots=self.statuses(p)
            self.ctrl.target_provider=lambda ids:robots
            self.assertEqual(len(self.ctrl._validate_targets([12,13],p)),2)
            robots[0]['interaction_path']='current_50hz_ma5_fixed_affine'
            with self.assertRaises(SequenceValidationError):self.ctrl._validate_targets([12,13],p)

    def test_status_and_api_guard(self):
        import server
        from fastapi import HTTPException
        row=server.parse_status_message('STATUS,id:12,cur_cycles:2,cycle_ready:1,cycle_faults:0',12,'127.0.0.1')
        self.assertEqual((row['cur_cycles'],row['cycle_ready'],row['cycle_faults']),(2,1,0))
        self.choose(m.CAL_PRESET)
        with patch.object(server,'sequence_controller',self.ctrl),patch.object(server,'_sequence_config_path',return_value=self.seq),patch.object(server,'SYNC_PARAMS_PATH',self.params),patch.object(server,'EXPERIMENTS_DIR',self.exp),patch.object(server.socket,'socket') as sock:
            result=server.get_current_cycle_plan();self.assertFalse(result['ready'])
            with self.assertRaises(HTTPException):asyncio.run(server.send_global_command(server.GlobalCommandRequest(command='START')))
            sock.return_value.sendto.assert_not_called()
            old=dict(status='completed',final_upload={'session_id':'keep'},event_log=[])
            self.ctrl._state=copy.deepcopy(old)
            asyncio.run(server.select_current_cycle_preset(server.CurrentOffsetPresetRequest(preset_id=m.CAL_PRESET)))
            self.assertEqual(self.ctrl._state,old)


class NativeCycleTests(unittest.TestCase):
    def test_variable_speed_interpolation_reset_and_faults(self):
        compiler=shutil.which('clang++') or shutil.which('g++')
        self.assertIsNotNone(compiler,'A native C++ compiler is needed for the FW observer check')
        code=r'''
#include "phase_cycle_mean.h"
#include <cassert>
#include <cmath>
int main() {
 current_cycle::TwoCycleMean m; float y=123;
 for(int k=0;k<700;++k) {
  float p=.06f*k+.00002f*k*k;
  bool ok=m.push(p,2*p+5,y);
  if(p>=m.TwoPi) {assert(ok);assert(std::fabs(y-(2*p+5-m.TwoPi))<.0001f);}
  else assert(!ok);
 }
 assert(m.ready()&&!m.fault());
 assert(!m.push(1,0,y));assert(m.fault());
 m.reset();assert(!m.ready()&&!m.fault());
 assert(!m.push(0,2,y));
 for(int k=1;k<256;++k)m.push(.001f*k,2,y);
 assert(m.fault()); // Fixed capacity exhaustion cannot select a partial average.
 m.reset();
 for(int k=0;k<1600;++k) {
  float p=.04f*k;float x=std::cos(p)+.3f*std::sin(2*p);
  if(m.push(p,x,y))assert(std::fabs(y-x)<.0003f);
 }
 assert(!m.fault());
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'check.cpp';binary=Path(tmp)/'check';source.write_text(code)
            subprocess.run([compiler,'-std=c++17','-O2','-I',str(m.ROOT/'firmware/pico_robot/include'),str(source),'-o',str(binary)],check=True,capture_output=True,text=True)
            result=subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
