"""60mm B/C pilot: seven120s delta-omega conditions per controller, fresh60s calibration."""
from pathlib import Path
import copy,json,math,tempfile,hashlib,sys
import yaml
import current_offset_plan as offset
from current_zopt_plan import ROOT,activate_preset

CAL_ID='dual60_precalibration_v1'
LOCK_ID='dual60_pilot_v1'
PLAN_IDS=(CAL_ID,LOCK_ID)
PRESETS=('b_calibration','b_once','b_base7','c_calibration','c_once','c_base7')
GRID=[-.3,-.2,-.1,0.,.1,.2,.3]
CATALOG=ROOT/'fleet/dual_sensor_pilot_60mm'
MODEL=ROOT/'reports/impulsive_interaction/20261030_precalibrated_two_sensor_60mm'
INITIAL_SOURCE=ROOT/'fleet_manager/data/experiments/sequence_ps261029_070825/sequence_plan.json'
PROFILES={'b':'local_joint','c':'noise_fixed_return_joint'}


def build_id():return json.loads((MODEL/'firmware_integration.json').read_text())['build_id']
def initial_offsets():
    plan=json.loads(INITIAL_SOURCE.read_text())['plan']
    idx=min(range(len(plan['partner_omegas_rad'])),key=lambda j:abs(plan['partner_omegas_rad'][j]-3*math.pi))
    if abs(plan['partner_omegas_rad'][idx]-3*math.pi)>1e-9:raise ValueError('Δω0 reference not found')
    return {str(i):[plan['trial_phase_offsets_rad'][str(i)][idx]] for i in [12,13]}


def documents(pid):
    if pid not in PRESETS:raise ValueError('未登録の併用予備実験です')
    kind=pid[0];cal=pid.endswith('calibration');sweep=pid.endswith('base7')
    display=[-.1*math.pi] if cal else GRID if sweep else [0.]
    phases={'12':[0.],'13':[0.]} if cal else {k:v*len(display) for k,v in initial_offsets().items()}
    s,p=offset.documents('60mm_mean_calibration' if cal else '60mm_mean_zopt_base7')
    s,p=copy.deepcopy(s),copy.deepcopy(p)
    s.update(name='dual60_'+pid,current_only=False,feedback_source='dual',kappa=0. if cal else 1.,
        delta_omega_rad=[-v for v in display],delta_omega_labels=[kind+'_cal'] if cal else [kind+'_'+str(j+1) for j in range(len(display))],
        trial_duration_ms=60000 if cal else 120000,sequence_log_hz=50 if cal else 25,
        analysis_window_start_s=10. if cal else 60.,analysis_window_end_s=50. if cal else 120.,
        phase_reference_agent=12,phase_partner_agent=13,phase_difference_definition='phi13-phi12',
        trial_phase_offsets_rad=phases)
    s['research_plan']=dict(id=CAL_ID if cal else LOCK_ID,preset_id=pid,paddle_mm=60,kind=kind,
        dual_profile=PROFILES[kind],repetitions=1,external_perturbation=False,
        display_delta_omega_rad_s=display,initial_phase_policy='same_offsets_all_conditions',
        initial_phase_source=str(INITIAL_SOURCE.relative_to(ROOT)),
        initial_phase_source_sha256=hashlib.sha256(INITIAL_SOURCE.read_bytes()).hexdigest(),
        design_sha256=hashlib.sha256((MODEL/'designs.npz').read_bytes()).hexdigest(),expected_fw_build=build_id())
    p['kappa']=s['kappa'];p['current_observer']['phase_cycles']=1
    p['dual_controller']=dict(profile=PROFILES[kind],calibration=None)
    return s,p


def estimate(path):
    sys.path.insert(0,str(ROOT/'analysis/python'))
    from calibrate_precalibrated_two_sensor_60mm_20261030 import estimate as recover
    path=Path(path);snap=json.loads((path/'sequence_plan.json').read_text());m=snap['plan']['research_plan']
    if m.get('id')!=CAL_ID:raise ValueError('今回の併用予備実験用の校正測定が必要です')
    if m.get('expected_fw_build')!=build_id():raise ValueError('校正後にFWが変更されています')
    record=recover(path)
    if record.get('schema')!=2:raise ValueError('ADC生値で校正する専用FWが必要です')
    st=json.loads((path/'sequence_status.json').read_text())
    for aid in [12,13]:
        ack=[e for e in st['event_log'] if e.get('agent_id')==aid and e.get('event')=='UPLOAD_DONE'][-1]
        # Existing dual FW emits build in STATUS/DUAL_STATUS, not SEQ_EVENT.
        # Reject an explicit mismatch; missing event build must be checked live
        # together with the calibration ID before apply/start (validate_targets).
        if ack.get('build') is not None and ack['build']!=build_id():raise ValueError('校正FWのbuild IDが不一致です')
    record['build_verification']='expected build in plan; live build and calibration ID required before apply/start'
    record.update(sequence_dir=str(path.resolve()),kind=m['kind'],session_id=st['final_upload']['session_id'])
    return record


def calibrations(experiments_dir=offset.EXPERIMENTS,*,include_invalid=False):
    rows=[]
    for path in experiments_dir.glob('sequence_*/sequence_plan.json'):
        try:
            plan=json.loads(path.read_text())['plan']
            if plan.get('research_plan',{}).get('id')!=CAL_ID:continue
            r=estimate(path.parent)
            snap=json.loads(path.read_text());status=json.loads(path.with_name('sequence_status.json').read_text())
            rows.append(dict(session_id=r['session_id'],kind=r['kind'],sequence_dir=str(path.parent),agents=r['agents'],valid=True,
                sequence_created_at=snap.get('created_at'),motion_start_epoch=status.get('motion_start_epoch'),completed_at=status.get('completed_at')))
        except (ValueError,KeyError,OSError,TypeError,IndexError) as exc:
            if include_invalid:
                try:
                    meta=json.loads(path.read_text())['plan']['research_plan']
                    if meta.get('id')!=CAL_ID:continue
                    st=json.loads(path.with_name('sequence_status.json').read_text())
                    rows.append(dict(session_id=(st.get('final_upload') or {}).get('session_id',path.parent.name),kind=meta.get('kind'),valid=False,error=str(exc)))
                except (ValueError,KeyError,OSError,TypeError):pass
    return sorted(rows,key=lambda r:r['session_id'],reverse=True)


def prepare(pid,sequence_path,params_path,backup_root,session_id=None,experiments_dir=offset.EXPERIMENTS):
    s,p=documents(pid);record=None
    if not pid.endswith('calibration'):
        found=[r for r in calibrations(experiments_dir) if r['session_id']==session_id and r['kind']==pid[0]]
        if len(found)!=1:raise ValueError('この条件専用の新規校正を回収・選択してください')
        record=estimate(found[0]['sequence_dir']);p['dual_controller']['calibration']=record
        s['research_plan']['calibration_session']=session_id
        for aid,row in record['agents'].items():p['current_observer']['by_agent'][aid]['center_mA']=row['current']['corrected_center_mA']
    with tempfile.TemporaryDirectory() as td:
        cat=Path(td);alias='60mm_mean_zopt_base7'
        (cat/f'{alias}.yaml').write_text(yaml.safe_dump(s,allow_unicode=True,sort_keys=False))
        (cat/f'params_{alias}.yaml').write_text(yaml.safe_dump(p,allow_unicode=True,sort_keys=False))
        backup=activate_preset(alias,sequence_path,params_path,backup_root,catalog=cat)
    return backup,record


def validate(plan):
    m=plan.get('research_plan',{});pid=m.get('preset_id');s,p=documents(pid);cal=m['id']==CAL_ID
    if m['id']!=(CAL_ID if pid.endswith('calibration') else LOCK_ID):raise ValueError('校正と本試行の条件不一致')
    if plan['agent_ids']!=[12,13] or plan['paddle']!='mid' or len(plan['phase_labels'])!=len(s['delta_omega_rad']):raise ValueError('ID12/13・60mm・指定条件数にそろえてください')
    for k in ['kappa','trial_duration_ms','sequence_log_hz','feedback_start_ms','analysis_window_start_s','analysis_window_end_s']:
        if plan[k]!=s[k]:raise ValueError('併用予備実験の設定不一致: '+k)
    if plan.get('trial_phase_offsets_rad')!=s['trial_phase_offsets_rad']:raise ValueError('開始位相が指定値と異なります')
    omegas=[3*math.pi-v for v in s['delta_omega_rad']]
    if len(plan['partner_omegas_rad'])!=len(omegas) or any(abs(a-b)>1e-9 for a,b in zip(plan['partner_omegas_rad'],omegas)):raise ValueError('周波数差が不一致です')
    if m.get('display_delta_omega_rad_s')!=s['research_plan']['display_delta_omega_rad_s']:raise ValueError('表示周波数差が不一致です')
    if m.get('design_sha256')!=s['research_plan']['design_sha256'] or m.get('expected_fw_build')!=build_id():raise ValueError('Z設計またはFWが変更されています')
    if plan.get('current_only') is not False or plan.get('feedback_source')!='dual' or plan.get('phase_difference_definition')!='phi13-phi12':raise ValueError('センサ経路・位相差符号が不一致です')
    if m.get('kind')!=pid[0] or m.get('dual_profile')!=PROFILES[pid[0]]:raise ValueError('B/C制御則の条件不一致')
    actual=plan['sync_params_snapshot']
    if actual.get('current_observer',{}).get('phase_cycles')!=1:raise ValueError('周期平均は使いません')
    for aid in ['12','13']:
        entry=actual.get('current_observer',{}).get('by_agent',{}).get(aid,{})
        base=p['current_observer']['by_agent'][aid]
        if entry.get('width_mA')!=base['width_mA']:raise ValueError('固定電流幅が不一致です')
    if actual.get('dual_controller',{}).get('profile')!=PROFILES[pid[0]]:raise ValueError('制御則が不一致です')
    if not cal:
        ref=actual.get('dual_controller',{}).get('calibration')
        if not ref or ref.get('kind')!=pid[0] or estimate(ref['sequence_dir'])!=ref:raise ValueError('校正出典・値が変更されています')
        for aid in ['12','13']:
            if actual['current_observer']['by_agent'][aid]['center_mA']!=ref['agents'][aid]['current']['corrected_center_mA']:raise ValueError('電流補正中心が不一致です')
    if actual.get('feedback_tau')!=0 or actual.get('center')!=110 or actual.get('amplitude')!=75:raise ValueError('指令条件または追加フィルタが不一致')


def validate_targets(robots,plan,*,require_applied=True):
    cal=plan['research_plan']['id']==CAL_ID;errors=[];targets=[]
    ref=plan['sync_params_snapshot'].get('dual_controller',{}).get('calibration')
    for aid in [12,13]:
        row=next((r for r in robots if int(r.get('id',0))==aid),{})
        expected={'paused':1,'params':1,'mode':'precalibrated_two_sensor','flex_used':1,
            'interaction_path':'dual60_precalibrated_flex_current_ma5','build':build_id()}
        for key,value in expected.items():
            if row.get(key)!=value:errors.append(f'ID{aid}: {key}が未確認/不一致')
        if not row.get('ip'):errors.append(f'ID{aid}: IPなし')
        for key,value in [('p_tau',0.),('s_center',110.),('s_amplitude',75.)]:
            if row.get(key) is None or not math.isfinite(float(row[key])) or abs(float(row[key])-value)>1e-3:errors.append(f'ID{aid}: {key}不一致')
        if not cal:
            if not ref:raise ValueError('校正ログを選択してください')
            for key,value in [('dual_cal',ref['calibration_id']),('dual_cal_ready',1),('dual_flex_unit','adc12')]:
                if row.get(key)!=value:errors.append(f'ID{aid}: 校正ID/単位の読戻し不一致（再起動した場合は再校正）')
            rr=ref['agents'][str(aid)]
            checks={'dual_flex_c':rr['flex']['center'],'dual_flex_d':rr['flex']['width']}
            if require_applied:checks.update(dual_cur_c=rr['current']['corrected_center_mA'],dual_cur_d=rr['current']['width_mA'],kappa=1.,omega=3*math.pi)
            for key,value in checks.items():
                if row.get(key) is None or not math.isfinite(float(row[key])) or abs(float(row[key])-value)>1e-3:errors.append(f'ID{aid}: {key}読戻し不一致')
            if require_applied and (row.get('dual_configured')!=1 or row.get('dual_fault')!=0 or row.get('dual_profile')!=PROFILES[plan['research_plan']['kind']]):errors.append(f'ID{aid}: 併用制御則の適用が未確認')
        targets.append(dict(id=aid,ip=row.get('ip')))
    if errors:raise ValueError('; '.join(errors))
    return targets


def export():
    CATALOG.mkdir(exist_ok=True)
    for pid in PRESETS:
        s,p=documents(pid);s['parameter_file']=str((CATALOG/f'params_{pid}.yaml').relative_to(ROOT))
        (CATALOG/f'{pid}.yaml').write_text(yaml.safe_dump(s,allow_unicode=True,sort_keys=False))
        (CATALOG/f'params_{pid}.yaml').write_text(yaml.safe_dump(p,allow_unicode=True,sort_keys=False))

if __name__=='__main__':export()
