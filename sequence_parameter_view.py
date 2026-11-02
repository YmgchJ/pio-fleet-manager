"""Read-only comparison of a selected sequence with observed robot parameters."""
import math
from sync_params import Z_MODES


def parameter_view(plan, targets):
    params=plan['sync_params_snapshot'];ids=plan['agent_ids'];by={int(t['id']):t for t in targets};obs=params.get('current_observer',{});omegas=params.get('omega_by_agent',{})
    def number(v):
        try:
            v=float(v)
            return v if math.isfinite(v) else None
        except (TypeError,ValueError):return None
    def display(v):
        if v is None:return '—'
        if isinstance(v,(int,float)):return f'{v:.6g}'
        return str(v)
    rows=[]
    def row(label,expected,fields,tolerance=1e-3):
        cells=[]
        for index,aid in enumerate(ids):
            want=expected[index];actual=by.get(aid,{});key=fields[index] if isinstance(fields,list) else fields
            got=actual.get(key);a,b=number(got),number(want)
            state='unknown' if got is None or want is None else ('match' if a is not None and b is not None and abs(a-b)<=tolerance else 'mismatch')
            cells.append(dict(value=display(got),state=state))
        same=all(x==expected[0] for x in expected)
        rows.append(dict(label=label,expected=display(expected[0]) if same else ' / '.join(map(display,expected)),actual=cells))
    row('κ',[plan['kappa']]*len(ids),'kappa')
    row('ω [rad/s]',[omegas.get(a,omegas.get(str(a))) for a in ids],'omega')
    for label,key,field in [('中心 c [mA]','center_mA','cur_center'),('幅 d [mA]','width_mA','cur_width')]:
        if obs:row(label,[(obs.get('by_agent',{}).get(str(a),{})).get(key) for a in ids],field)
    if obs:
        row('周期平均',[obs.get('phase_cycles',1)]*len(ids),'cur_cycles',0)
        expected_path='current_50hz_ma5_cycle2_fixed_affine' if obs.get('phase_cycles',1)==2 else 'current_50hz_ma5_fixed_affine'
        # Match symbolic input paths without interpreting missing telemetry as agreement.
        cells=[]
        for aid in ids:
            got=by.get(aid,{}).get('interaction_path')
            cells.append(dict(value=('MA5・固定正規化' if got==expected_path else got or '—'),state='unknown' if got is None else 'match' if got==expected_path else 'mismatch'))
        rows.append(dict(label='電流処理',expected='MA5・固定正規化',actual=cells))
    row('指令中心 [°]',[params.get('center')]*len(ids),'s_center',2e-5)
    row('指令振幅 [°]',[params.get('amplitude')]*len(ids),'s_amplitude',2e-5)
    profile=params.get('zopt_profile') if params.get('z_mode')=='zopt' else params.get('z_mode')
    zcells=[]
    fields={'zmode':Z_MODES.get(params.get('z_mode')),'p_n':params.get('prc_n'),'p_a0':params.get('prc_a0',0)}
    for n in range(1,int(params.get('prc_n',0))+1):
        for v in ('a','b'):fields[f'p_{v}{n}']=params.get(f'prc_{v}{n}',0)
    for aid in ids:
        actual=by.get(aid,{});missing=False;mismatch=False
        for k,want in fields.items():
            got=number(actual.get(k));want=number(want)
            if got is None or want is None:missing=True
            elif abs(got-want)>2e-5:mismatch=True
        state='mismatch' if mismatch else 'unknown' if missing else 'match'
        zcells.append(dict(value={'match':'係数一致','mismatch':'係数不一致','unknown':'未確認'}[state],state=state))
    profile_label = ('Zopt,10' if str(profile).startswith('cur_zopt_') and str(profile).endswith('mean10') else 'Zopt,1' if str(profile).startswith('cur_zopt_') and str(profile).endswith('single01') else profile or '—')
    rows.insert(0,dict(label='Z',expected=profile_label,actual=zcells))
    return dict(agents=[dict(label=f'機体{i+1}',online=a in by) for i,a in enumerate(ids)],rows=rows,
                timing=dict(trials=len(plan['phase_labels']),duration_s=plan['trial_duration_ms']/1000,feedback_s=plan['feedback_start_ms']/1000,save_hz=plan['sequence_log_hz']))
