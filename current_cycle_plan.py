"""60 mm Zopt10: new offset calibration, then a two-cycle-mean 7+6 sweep."""
from __future__ import annotations
import copy
import json
import math
import tempfile
from pathlib import Path
import yaml
import current_offset_plan as offset
from current_zopt_plan import ROOT, activate_preset
from experiment_sequence import CURRENT_CYCLE_CAL_ID, CURRENT_CYCLE_LOCK_ID

CAL_ID, LOCK_ID = CURRENT_CYCLE_CAL_ID, CURRENT_CYCLE_LOCK_ID
PLAN_IDS = (CAL_ID, LOCK_ID)
CAL_PRESET = '60mm_mean_cycle2_calibration'
LOCK_PRESETS = ('60mm_mean_cycle2_base7', '60mm_mean_cycle2_extra6')
PRESETS = (CAL_PRESET, *LOCK_PRESETS)
CATALOG = ROOT/'fleet/current_cycle2_lock_60mm'


def documents(pid):
    if pid not in PRESETS:
        raise ValueError('未登録の2周期平均条件です')
    cal = pid == CAL_PRESET
    old = '60mm_mean_calibration' if cal else f'60mm_mean_zopt_{pid.rsplit("_",1)[1]}'
    s,p = offset.documents(old)
    s,p = copy.deepcopy(s),copy.deepcopy(p)
    s['name'] = 'current_cycle2_'+pid
    s['research_plan'].update(id=CAL_ID if cal else LOCK_ID,preset_id=pid,
        logical_condition='60mm_Zopt10_cycle2',phase_cycles=1 if cal else 2,
        lock_criterion_rad=.2*math.pi,
        lock_criterion='max circular phase deviation <=0.2pi (36deg) over60-120s',
        current_preprocess='MA5_then_own_phase_cycle_mean_then_fixed_affine',
        calibration_scope='one_new_calibration_frozen_for_cycle2_base7_plus_extra6')
    p['current_observer']['phase_cycles'] = 1 if cal else 2
    return s,p


def calibration_candidates(experiments_dir=offset.EXPERIMENTS):
    result=[]
    for path in experiments_dir.glob('sequence_*/sequence_plan.json'):
        try:
            plan=json.loads(path.read_text())['plan']
            if plan.get('research_plan',{}).get('id') != CAL_ID:
                continue
            st=json.loads(path.with_name('sequence_status.json').read_text())
            up=st.get('final_upload',{})
            if st.get('status')!='completed' or any(not up.get('log_end',{}).get(str(i),{}).get('ok') for i in (12,13)):
                continue
            if not Path(up['merged_csv']).is_file():
                continue
            result.append(dict(preset_id=CAL_PRESET,session_id=up['session_id'],
                               sequence_dir=str(path.parent),completed_at=st['completed_at']))
        except (OSError,KeyError,ValueError,TypeError):
            continue
    return sorted(result,key=lambda x:x['completed_at'],reverse=True)


def estimate(path):
    path=Path(path)
    plan=json.loads((path/'sequence_plan.json').read_text())['plan']
    if plan['research_plan'].get('preset_id')!=CAL_PRESET:
        raise ValueError('今回の60mm・2周期平均用に補正を測り直してください')
    snapshot=plan['sync_params_snapshot']
    expected=documents(CAL_PRESET)[1]
    for key in ['z_mode','zopt_profile','prc_n','feedback_tau','center','amplitude','kappa']+[f'prc_{v}{n}' for n in range(5) for v in ('a','b')]:
        if snapshot.get(key,0)!=expected.get(key,0):
            raise ValueError('補正測定のZopt10・動作設定が不一致です: '+key)
    if snapshot.get('current_observer')!=expected['current_observer']:
        raise ValueError('補正測定の元中心・幅・MA5が不一致です')
    if snapshot.get('current_observer',{}).get('phase_cycles')!=1:
        raise ValueError('補正測定は周期平均を入れる前のMA5電流で行います')
    st=json.loads((path/'sequence_status.json').read_text())
    for aid in (12,13):
        events=[e for e in st['event_log'] if e.get('agent_id')==aid and e.get('event')=='UPLOAD_DONE']
        if not events:raise ValueError('2台分の補正ログ回収イベントが必要です')
        ack=events[-1]
        if ack.get('cur_cycles')!=1 or ack.get('cycle_faults')!=0:
            raise ValueError('補正測定のFW・平均周期数・異常数が未確認です')
    record=offset.estimate(path,plan_id=CAL_ID,base_preset='60mm_mean_calibration')
    record.update(calibration_preset=CAL_PRESET,phase_cycles_calibration=1,
                  phase_cycles_closed_loop=2,lock_criterion_rad=.2*math.pi)
    return record


def completed_batches(experiments_dir=offset.EXPERIMENTS):
    result=[]
    for path in experiments_dir.glob('sequence_*/sequence_plan.json'):
        try:
            plan=json.loads(path.read_text())['plan'];m=plan.get('research_plan',{})
            st=json.loads(path.with_name('sequence_status.json').read_text());up=st.get('final_upload',{})
            if m.get('id')!=LOCK_ID or st.get('status')!='completed':continue
            validate(plan,check_completed=False)
            if any(not up.get('log_end',{}).get(str(i),{}).get('ok') or up['log_end'][str(i)].get('records',0)<=0 for i in (12,13)):continue
            if not Path(up['merged_csv']).is_file():continue
            good=True
            for aid in (12,13):
                ack=[e for e in st['event_log'] if e.get('agent_id')==aid and e.get('event')=='UPLOAD_DONE'][-1]
                if (ack.get('interaction_path'),ack.get('cur_cycles'),ack.get('cycle_faults')) != ('current_50hz_ma5_cycle2_fixed_affine',2,0):good=False
            if good:result.append(dict(preset_id=m['preset_id'],session_id=up['session_id'],calibration_session=m['offset_calibration_session']))
        except (OSError,KeyError,ValueError,TypeError,IndexError):continue
    return result


def prepare(pid,sequence_path,params_path,backup_root,session_id=None,experiments_dir=offset.EXPERIMENTS):
    s,p=documents(pid);record=None
    if pid!=CAL_PRESET:
        choices=[x for x in calibration_candidates(experiments_dir) if x['session_id']==session_id]
        if len(choices)!=1:raise ValueError('今回の2周期平均用の新規補正ログを回収・選択してください')
        if pid.endswith('base7') and any(x['preset_id']==LOCK_PRESETS[0] and x['calibration_session']==session_id for x in completed_batches(experiments_dir)):
            raise ValueError('この補正値の7点は回収済みです。追加6点へ進むか、補正を測り直してください')
        if pid.endswith('extra6') and not any(x['preset_id']==LOCK_PRESETS[0] and x['calibration_session']==session_id for x in completed_batches(experiments_dir)):
            raise ValueError('同じ補正値で2周期平均7点を完了・回収してください')
        record=estimate(choices[0]['sequence_dir'])
        for aid,a in record['agents'].items():p['current_observer']['by_agent'][aid]['center_mA']=a['corrected_center_mA']
        p['current_observer']['session_offset_calibration']=record
        s['research_plan']['offset_calibration_session']=session_id
    # Use the existing atomic selection/backup implementation with its internal alias.
    with tempfile.TemporaryDirectory() as tmp:
        cat=Path(tmp);internal='60mm_mean_zopt_base7'
        (cat/f'{internal}.yaml').write_text(yaml.safe_dump(s,allow_unicode=True,sort_keys=False))
        (cat/f'params_{internal}.yaml').write_text(yaml.safe_dump(p,allow_unicode=True,sort_keys=False))
        backup=activate_preset(internal,sequence_path,params_path,backup_root,catalog=cat)
    return backup,record


def validate(plan, *, check_completed=True):
    m=plan.get('research_plan',{})
    if m.get('id') not in PLAN_IDS:return
    pid=m.get('preset_id');s,p=documents(pid);cal=pid==CAL_PRESET
    if m['id']!=(CAL_ID if cal else LOCK_ID):raise ValueError('2周期平均の補正／閉ループ条件が不一致です')
    if m.get('phase_cycles')!=(1 if cal else 2):raise ValueError('実験条件の平均周期数が不一致です')
    if abs(float(m.get('lock_criterion_rad',0))-.2*math.pi)>1e-12:
        raise ValueError('今回の主判定は36度です')
    actual=plan['sync_params_snapshot']
    if cal:
        if (plan['kappa'],plan['trial_duration_ms'],plan['sequence_log_hz'],len(plan['phase_labels']))!=(0,60000,50,1):
            raise ValueError('補正測定はκ0・60秒・50Hzです')
        if (plan['analysis_window_start_s'],plan['analysis_window_end_s'])!=(10,50) or abs(plan['partner_omegas_rad'][0]-2.9*math.pi)>1e-6:
            raise ValueError('補正測定は10–50秒・ω13=2.9πです')
    else:
        ref=actual.get('current_observer',{}).get('session_offset_calibration')
        if not ref or ref.get('session_id')!=m.get('offset_calibration_session'):
            raise ValueError('新規の補正ログが未選択です')
        if estimate(ref['sequence_dir'])!=ref:raise ValueError('補正ログ・出典ハッシュ・条件が変更されています')
        if check_completed and pid.endswith('extra6') and not any(x['preset_id']==LOCK_PRESETS[0] and x['calibration_session']==ref['session_id'] for x in completed_batches()):
            raise ValueError('同じ補正値で7点を完了・回収してください')
        for aid,a in ref['agents'].items():p['current_observer']['by_agent'][aid]['center_mA']=a['corrected_center_mA']
    for key in ['z_mode','zopt_profile','feedback_tau','center','amplitude','kappa','kappa_init','prc_n']+[f'prc_{v}{n}' for n in range(5) for v in ('a','b')]:
        if actual.get(key,0)!=p.get(key,0):raise ValueError('Zopt10または制御条件が不一致です: '+key)
    obs=actual.get('current_observer',{})
    if any(obs.get(k)!=p['current_observer'].get(k) for k in ('phase_cycles','mode','ma_samples','sample_hz','clip','by_agent')):
        raise ValueError('平均周期数・MA5・固定中心・固定幅が不一致です')
    if plan['agent_ids']!=[12,13] or m.get('paddle_mm')!=60:
        raise ValueError('今回の実験は60mm・ID12/13です')


if __name__=='__main__':
    CATALOG.mkdir(exist_ok=True)
    for pid in PRESETS:
        s,p=documents(pid)
        # Exported lock presets are deliberately unusable without selected new calibration.
        s['parameter_file']=str((CATALOG/f'params_{pid}.yaml').relative_to(ROOT))
        (CATALOG/f'{pid}.yaml').write_text(yaml.safe_dump(s,allow_unicode=True,sort_keys=False))
        (CATALOG/f'params_{pid}.yaml').write_text(yaml.safe_dump(p,allow_unicode=True,sort_keys=False))
    print('Exported three separate cycle2 presets; active config and robots unchanged.')
