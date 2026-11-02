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
import current_convergence_plan as m
import current_offset_plan as offset
from experiment_sequence import ExperimentSequenceController, SequenceValidationError
from sync_params import build_param_payload
import test_current_zopt_13point as baseline_tests


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
            tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
            self.root=Path(tmp.name);self.seq=self.root/'seq.yaml';self.params=self.root/'params.yaml'
            self.exp=self.root/'experiments';self.exp.mkdir()
            self.ctrl=ExperimentSequenceController(self.seq,self.exp,Mock(),lambda ids:[])

    def choose(self,pid,sid=None):
            return m.prepare(pid,self.seq,self.params,self.root/'backups',sid,self.exp)

    def fixture(self):
            self.choose(m.CAL_PRESETS[0]);plan=self.ctrl._load_plan(None)
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

    def test_grid_capacity_and_same_controls(self):
        d,raw=self.fixture()
        with patch.object(offset,'RAW',raw):
            ref=m.estimate(d)
            self.choose(m.LOCK_PRESETS[0],'newcal')
            p=self.ctrl._load_plan(None)
            self.assertEqual(p['kappa'],1)
            self.assertEqual(len(p['phase_labels']),6)
            self.assertEqual(p['sequence_log_hz']*p['trial_duration_ms']/1000*6,18000)
            self.assertEqual(p['sync_params_snapshot']['current_observer']['phase_cycles'],1)
            self.assertEqual(sorted(p['research_plan']['initial_phase_degrees']),[-150,-90,-30,30,90,150])
            for a,b,label in zip(p['trial_phase_offsets_rad']['12'],p['trial_phase_offsets_rad']['13'],p['research_plan']['initial_phase_degrees']):
                self.assertAlmostEqual(math.atan2(math.sin(b-a),math.cos(b-a)),math.radians(label))
            self.assertEqual(p['first_start_delay_ms'],20000)
            self.assertEqual(p['inter_trial_pause_ms'],20000)
            base=m.documents(m.LOCK_PRESETS[0])[1]
            for aid in ('12','13'):
                self.assertEqual(p['sync_params_snapshot']['current_observer']['by_agent'][aid]['width_mA'],base['current_observer']['by_agent'][aid]['width_mA'])
                self.assertEqual(p['sync_params_snapshot']['current_observer']['by_agent'][aid]['center_mA'],ref['agents'][aid]['corrected_center_mA'])
            with self.assertRaisesRegex(ValueError,'セット用'):self.choose(m.LOCK_PRESETS[1],'newcal')
            original=yaml.safe_load(self.params.read_text())
            for field in ['phase_cycles','width','Z','omega']:
                bad=copy.deepcopy(original)
                if field=='phase_cycles':bad['current_observer']['phase_cycles']=2
                if field=='width':bad['current_observer']['by_agent']['12']['width_mA']+=1
                if field=='Z':bad['zopt_profile']='cur_zopt_60mm_single01'
                if field=='omega':bad['omega_by_agent'][13]+=.1
                self.params.write_text(yaml.safe_dump(bad))
                with self.subTest(field=field),self.assertRaises(SequenceValidationError):self.ctrl._load_plan(None)
            self.params.write_text(yaml.safe_dump(original))
            robots=baseline_tests.Current13Tests.statuses(self,p)
            self.ctrl.target_provider=lambda ids:robots
            self.assertEqual(len(self.ctrl._validate_targets([12,13],p)),2)
            robots[0]['interaction_path']='current_50hz_ma5_rolling_p10_p90'
            with self.assertRaises(SequenceValidationError):self.ctrl._validate_targets([12,13],p)

    def test_each_calibration_and_missing_log(self):
        orders=[]
        for pid in m.CAL_PRESETS:
            self.choose(pid);p=self.ctrl._load_plan(None)
            self.assertEqual((p['trial_duration_ms'],p['sequence_log_hz'],p['kappa']),(60000,50,0))
            self.assertAlmostEqual(p['partner_omegas_rad'][0],2.9*math.pi)
        for pid in m.LOCK_PRESETS:
            with self.assertRaises(ValueError):self.choose(pid,'old')
            orders.append(tuple(m.documents(pid)[0]['research_plan']['initial_phase_degrees']))
        self.assertEqual(len(set(orders)),5)

    def test_incomplete_calibration_cannot_select(self):
        d,raw=self.fixture();path=d/'sequence_status.json';st=json.loads(path.read_text())
        del st['final_upload']['log_end']['13'];path.write_text(json.dumps(st))
        self.assertEqual(m.calibration_candidates(self.exp),[])

class ConvergenceApiTests(unittest.TestCase):
    def test_routes_are_registered_and_busy_selection_is_rejected(self):
        import server
        from fastapi import HTTPException
        paths={route.path for route in server.app.routes}
        self.assertIn('/api/experiment/current-convergence-plan',paths)
        self.assertIn('/api/experiment/current-convergence-plan/select',paths)
        ctrl=Mock();ctrl.ACTIVE_STATES={'running'};ctrl.get_status.return_value={'status':'awaiting_upload'}
        with patch.object(server,'sequence_controller',ctrl),patch.object(m,'prepare') as prepare:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(server.select_current_convergence_preset(server.CurrentOffsetPresetRequest(preset_id=m.CAL_PRESETS[0])))
            self.assertEqual(caught.exception.status_code,409)
            prepare.assert_not_called()

    def test_api_lists_valid_timestamped_calibration(self):
        import server
        candidate=dict(preset_id=m.CAL_PRESETS[0],block=1,session_id='newcal',sequence_dir='/unused',completed_at='2026-11-02T00:00:00Z')
        with patch.object(server,'sequence_controller',None),patch.object(server,'_read_yaml_path',return_value={}),patch.object(m,'calibration_candidates',return_value=[candidate]),patch.object(m,'estimate',return_value={'agents':{}}),patch.object(m,'completed_batches',return_value=[]):
            data=server.get_current_convergence_plan()
        self.assertTrue(data['calibrations'][0]['valid'])
        self.assertEqual(data['calibrations'][0]['completed_at'],candidate['completed_at'])
        self.assertFalse(data['ready'])

if __name__=='__main__':unittest.main()
