"""60 mm current-only initial-phase experiment. Selection performs no hardware I/O."""
from __future__ import annotations
import copy
import json
import math
import random
import tempfile
from pathlib import Path
import yaml
import current_offset_plan as offset
from current_zopt_plan import ROOT, activate_preset

CAL_ID='current_convergence_calibration_v1'
LOCK_ID='current_convergence_60mm_v1'
PLAN_IDS=(CAL_ID,LOCK_ID)
CAL_PRESETS=tuple(f'60mm_mean_initial_set{i}_calibration' for i in range(1,6))
LOCK_PRESETS=tuple(f'60mm_mean_initial_set{i}' for i in range(1,6))
PRESETS=CAL_PRESETS+LOCK_PRESETS
CATALOG=ROOT/'fleet/current_convergence_60mm'
SEED=20261102


def documents(pid):
    if pid not in PRESETS:raise ValueError('未登録の初期位相差条件です')
    cal=pid in CAL_PRESETS;block=int(pid.split('_set')[1].split('_')[0])
    s,p=offset.documents('60mm_mean_calibration' if cal else '60mm_mean_zopt_base7')
    s,p=copy.deepcopy(s),copy.deepcopy(p)
    s['name']='current_convergence_'+pid
    m=s['research_plan'];m.update(id=CAL_ID if cal else LOCK_ID,preset_id=pid,
        block=block,batch='calibration' if cal else 'initial_phase',
        logical_condition='60mm_Zopt10_initial_phase',
        calibration_scope='one_new_calibration_per_six_trial_set',
        current_preprocess='signed_mA_MA5_fixed_affine',phase_cycles=1)
    p['current_observer']['phase_cycles']=1
    if not cal:
        angles=[-150,-90,-30,30,90,150];random.Random(SEED+block).shuffle(angles)
        # Retain ID12's absolute starting phase from the original Δω=0 trial.
        reference=float(s['trial_phase_offsets_rad']['12'][3])
        s.update(sequence_type='phase_sweep',phase_differences_rad=[math.radians(a)%math.tau for a in angles],
            phase_labels=[f'initial_{a:+04d}deg' for a in angles],
            trial_phase_offsets_rad={'12':[reference]*6,'13':[(reference+math.radians(a))%math.tau for a in angles]})
        for k in ('delta_omega_rad','delta_omega_labels'):s.pop(k,None)
        for k in ('full_grid_rad_s','delta_omega_step_rad_s','delta_omega_span_rad_s','delta_omega_display'):m.pop(k,None)
        m.update(initial_phase_degrees=angles,display_delta_omega_rad_s=[0.]*6,
            phase_difference_definition='phi_13 - phi_12 = phi2 - phi1',randomization_seed=SEED+block,
            reference_initial_phase_rad=reference,expected_records_per_agent=18000,
            convergence_reference='per-trial circular mean over 60-120 s',
            convergence_tolerance_deg=36,convergence_origin_s=10,
            convergence_rule='first time after FB onset within final mean +/-36 deg and remaining inside until trial end; retain non-converged trials')
    s['parameter_file']=str((CATALOG/f'params_{pid}.yaml').relative_to(ROOT))
    return s,p


def calibration_candidates(experiments_dir=offset.EXPERIMENTS):
    result=[]
    for path in Path(experiments_dir).glob('sequence_*/sequence_plan.json'):
        try:
            plan=json.loads(path.read_text())['plan'];m=plan.get('research_plan',{})
            if m.get('id')!=CAL_ID or m.get('preset_id') not in CAL_PRESETS:continue
            st=json.loads(path.with_name('sequence_status.json').read_text());up=st.get('final_upload',{})
            if st.get('status')!='completed' or any(not up.get('log_end',{}).get(str(a),{}).get('ok') or up['log_end'][str(a)].get('records',0)<=0 for a in (12,13)):continue
            if not Path(up['merged_csv']).is_file():continue
            result.append(dict(preset_id=m['preset_id'],block=m['block'],session_id=up['session_id'],sequence_dir=str(path.parent),completed_at=st['completed_at']))
        except (OSError,KeyError,ValueError,TypeError):continue
    return sorted(result,key=lambda x:x['completed_at'],reverse=True)


def estimate(path):
    path=Path(path);plan=json.loads((path/'sequence_plan.json').read_text())['plan']
    pid=plan['research_plan'].get('preset_id');s,p=documents(pid)
    if pid not in CAL_PRESETS:raise ValueError('今回のセット用の補正測定が必要です')
    actual=plan['sync_params_snapshot']
    for k in ['z_mode','zopt_profile','prc_n','feedback_tau','center','amplitude','kappa','current_observer']+[f'prc_{v}{n}' for n in range(5) for v in ('a','b')]:
        if actual.get(k,0)!=p.get(k,0):raise ValueError('補正測定の設定不一致: '+k)
    ref=offset.estimate(path,plan_id=CAL_ID,base_preset='60mm_mean_calibration')
    ref.update(calibration_preset=pid,block=s['research_plan']['block'])
    return ref


def completed_batches(experiments_dir=offset.EXPERIMENTS):
    result=[]
    for path in Path(experiments_dir).glob('sequence_*/sequence_plan.json'):
        try:
            plan=json.loads(path.read_text())['plan'];m=plan.get('research_plan',{})
            if m.get('id')!=LOCK_ID:continue
            validate(plan)
            st=json.loads(path.with_name('sequence_status.json').read_text());up=st.get('final_upload',{})
            if st.get('status')!='completed' or not Path(up['merged_csv']).is_file():continue
            if any(not up.get('log_end',{}).get(str(a),{}).get('ok') or up['log_end'][str(a)].get('records',0)!=18000 for a in (12,13)):continue
            for aid in (12,13):
                ack=[e for e in st['event_log'] if e.get('agent_id')==aid and e.get('event')=='UPLOAD_DONE'][-1]
                if ack.get('interaction_path')!='current_50hz_ma5_fixed_affine' or ack.get('records')!=18000:raise ValueError('回収内容不一致')
            result.append(dict(preset_id=m['preset_id'],session_id=up['session_id'],calibration_session=m['offset_calibration_session']))
        except (OSError,KeyError,ValueError,TypeError,IndexError):continue
    return result


def prepare(pid,sequence_path,params_path,backup_root,session_id=None,experiments_dir=offset.EXPERIMENTS):
    s,p=documents(pid);record=None
    if pid in LOCK_PRESETS:
        matches=[c for c in calibration_candidates(experiments_dir) if c['session_id']==session_id and c['block']==s['research_plan']['block']]
        if len(matches)!=1:raise ValueError('このセット用の新規補正ログを両機回収して選択してください')
        record=estimate(matches[0]['sequence_dir'])
        for aid,a in record['agents'].items():p['current_observer']['by_agent'][aid]['center_mA']=a['corrected_center_mA']
        p['current_observer']['session_offset_calibration']=record
        s['research_plan']['offset_calibration_session']=session_id
    with tempfile.TemporaryDirectory() as tmp:
        cat=Path(tmp);internal='60mm_mean_zopt_base7'
        (cat/f'{internal}.yaml').write_text(yaml.safe_dump(s,allow_unicode=True,sort_keys=False))
        (cat/f'params_{internal}.yaml').write_text(yaml.safe_dump(p,allow_unicode=True,sort_keys=False))
        backup=activate_preset(internal,sequence_path,params_path,backup_root,catalog=cat)
    return backup,record


def validate(plan):
    m=plan.get('research_plan',{})
    if m.get('id') not in PLAN_IDS:return
    pid=m.get('preset_id');s,p=documents(pid);cal=pid in CAL_PRESETS
    if m['id']!=(CAL_ID if cal else LOCK_ID) or m.get('block')!=s['research_plan']['block']:raise ValueError('セット・補正条件が不一致です')
    actual=plan['sync_params_snapshot']
    if cal:
        if (plan['kappa'],plan['trial_duration_ms'],plan['sequence_log_hz'],len(plan['phase_labels']))!=(0,60000,50,1) or abs(plan['partner_omegas_rad'][0]-2.9*math.pi)>1e-6 or (plan['analysis_window_start_s'],plan['analysis_window_end_s'])!=(10,50):raise ValueError('補正測定は従来同様60秒・κ0・50Hz・推定10–50秒です')
    else:
        if (plan['kappa'],plan['trial_duration_ms'],plan['sequence_log_hz'],plan['feedback_start_ms'])!=(1,120000,25,10000) or (plan['analysis_window_start_s'],plan['analysis_window_end_s'])!=(60,120):raise ValueError('本試行は従来同様120秒・κ1・25Hzです')
        if plan['sequence_type']!='phase_sweep' or plan['phase_labels']!=s['phase_labels'] or plan.get('trial_phase_offsets_rad')!=s['trial_phase_offsets_rad']:raise ValueError('初期位相差・試行順が不一致です')
        ref=actual.get('current_observer',{}).get('session_offset_calibration')
        if not ref or ref.get('session_id')!=m.get('offset_calibration_session') or ref.get('block')!=m['block']:raise ValueError('このセット用の補正値が未選択です')
        if estimate(ref['sequence_dir'])!=ref:raise ValueError('補正ログ・係数ハッシュが変更されています')
        for aid,a in ref['agents'].items():p['current_observer']['by_agent'][aid]['center_mA']=a['corrected_center_mA']
    for key in ['z_mode','zopt_profile','zsin_profile','current_only','feedback_source','feedback_tau','center','amplitude','kappa','kappa_init','prc_n','omega_by_agent']+[f'prc_{v}{n}' for n in range(5) for v in ('a','b')]:
        if actual.get(key,0)!=p.get(key,0):raise ValueError('位相ロック実験と設定が不一致: '+key)
    obs=actual.get('current_observer',{})
    for k in ('phase_cycles','mode','ma_samples','sample_hz','clip','by_agent'):
        if obs.get(k)!=p['current_observer'].get(k):raise ValueError('MA5・固定中心・固定幅が不一致: '+k)
    if plan['agent_ids']!=[12,13] or m.get('paddle_mm')!=60:raise ValueError('60mm・ID12/13に限定します')
    for k in ('first_start_delay_ms','inter_trial_pause_ms'):
        if plan[k]!=s[k]:raise ValueError('従来の待機時間と不一致です')


if __name__=='__main__':
    CATALOG.mkdir(exist_ok=True)
    for pid in PRESETS:
        s,p=documents(pid)
        for name,value in [(pid,s),('params_'+pid,p)]:
            (CATALOG/f'{name}.yaml').write_text(yaml.safe_dump(value,allow_unicode=True,sort_keys=False))
    print('Prepared five calibration + six-trial sets. Active settings and hardware unchanged.')
