// Current Z_opt lock compare — fleet/current_phase_sequence.yaml + sync_experiment_params.yaml only.
let currentZoptSelectionPending = false;
let currentZoptPlanSignature = '';

async function loadCurrentZoptPlan() {
    const active = document.getElementById('current-zopt-plan-active');
    const presets = document.getElementById('current-zopt-plan-presets');
    if (!presets || !active) return;
    try {
        const response = await fetch('/api/experiment/current-zopt-plan', { cache: 'no-store' });
        if (!response.ok) throw new Error(await readApiError(response));
        const data = await response.json();
        const signature = JSON.stringify([data.presets, data.active, data.busy, data.ready]);
        if (signature !== currentZoptPlanSignature) {
            presets.replaceChildren();
            const recoveredDetails = document.createElement('details');
            const recoveredSummary = document.createElement('summary');
            recoveredSummary.textContent = '回収済みの実験（展開して再実行）';
            recoveredDetails.append(recoveredSummary);
            let recoveredCount = 0;
            for (const size of [40, 60, 80]) {
                const row = document.createElement('div');
                row.style.marginBottom = '0.45rem';
                const label = document.createElement('strong');
                label.textContent = `${size} mm`;
                row.append(label);
                for (const preset of data.presets.filter(p => p.paddle_mm === size)) {
                    const button = document.createElement('button');
                    button.className = 'btn btn-outline';
                    button.style.marginLeft = '0.35rem';
                    button.dataset.preset = preset.preset_id;
                    const variant = preset.z_variant === 'A_single_trial' ? 'Zopt,1' : 'Zopt,10';
                    const batch = preset.batch === 'base7' ? '7点' : '追加6点';
                    const recovered = preset.recovered_sessions?.length > 0;
                    button.textContent = `${variant}・${batch}${recovered ? ' · 回収済' : ''}`;
                    button.title = `${preset.zopt_profile || ''} · OL ${preset.open_loop_session || '—'}`;
                    button.classList.toggle('paddle-selected', data.active === preset.preset_id);
                    button.setAttribute('aria-pressed', String(data.active === preset.preset_id));
                    button.addEventListener('click', () => selectCurrentZoptPreset(preset.preset_id));
                    if (recovered) {
                        recoveredCount++;
                        button.textContent = `${size} mm・${button.textContent}`;
                        recoveredDetails.append(button);
                    } else row.append(button);
                }
                presets.append(row);
            }
            if (recoveredCount) presets.append(recoveredDetails);
            currentZoptPlanSignature = signature;
        }
        presets.querySelectorAll('button').forEach(b => b.disabled = data.busy || currentZoptSelectionPending || data.preview_only);
        const current = data.presets.find(p => p.preset_id === data.active);
        active.textContent = current
            ? `選択中: ${current.preset_id}（${current.zopt_profile}・電流固定尺度・${current.batch === 'base7' ? '7点' : '追加6点'}・κ1・120秒・保存25 Hz）`
            : '電流 Z 比較条件は未選択です';
        const readyEl = document.getElementById('current-zopt-plan-ready');
        if (readyEl) {
            readyEl.textContent = data.preview_only ? '表示確認専用：実機通信・書き込み・開始は無効です（通常のFleet Managerを起動してください）' : data.ready ? `✓ ${data.readiness}` : data.readiness || '';
            readyEl.style.color = data.ready ? '#34d399' : '#fbbf24';
        }
        document.getElementById('current-zopt-plan-order').textContent =
            '各パドル：Zopt,1（7点→追加6点）→Zopt,10（7点→追加6点）。各回の終了後にSTOP ALLで2台分を回収。';
    } catch (err) {
        active.textContent = `電流 Z プラン読込失敗: ${err.message}`;
    }
}

async function selectCurrentZoptPreset(presetId) {
    if (currentZoptSelectionPending) return;
    currentZoptSelectionPending = true;
    currentZoptPlanSignature = '';
    document.querySelectorAll('#current-zopt-plan-presets button').forEach(b => b.disabled = true);
    try {
        const response = await fetch('/api/experiment/current-zopt-plan/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_id: presetId }),
        });
        if (!response.ok) throw new Error(await readApiError(response));
        if (typeof refreshFirmwareUploadProfile === 'function') await refreshFirmwareUploadProfile();
        if (typeof loadExperimentConfig === 'function') await loadExperimentConfig();
        if (typeof loadPhaseSequenceStatus === 'function') await loadPhaseSequenceStatus();
        await loadCurrentZoptPlan();
    } catch (err) {
        alert(err.message);
    } finally {
        currentZoptSelectionPending = false;
        loadCurrentZoptPlan();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('current-zopt-plan-presets')) {
        loadCurrentZoptPlan();
        setInterval(loadCurrentZoptPlan, 4000);
    }
});
