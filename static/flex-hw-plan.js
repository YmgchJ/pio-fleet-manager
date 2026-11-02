// Flex HW lock presets — writes fleet/current_phase_sequence.yaml + sync_experiment_params.yaml only.
let flexHwSelectionPending = false;
let flexHwPlanSignature = '';

async function loadFlexHwPlan() {
    const active = document.getElementById('flex-hw-plan-active');
    const presets = document.getElementById('flex-hw-plan-presets');
    if (!presets || !active) return;
    try {
        const response = await fetch('/api/experiment/flex-hw-plan', { cache: 'no-store' });
        if (!response.ok) throw new Error(await readApiError(response));
        const data = await response.json();
        const signature = JSON.stringify([data.presets, data.active, data.busy, data.ready]);
        if (signature !== flexHwPlanSignature) {
            presets.replaceChildren();
            for (const size of [60, 40, 80]) {
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
                    const prc = preset.prc === 'zopt' ? 'Z_opt' : 'Z_sin';
                    const recovered = preset.recovered_sessions?.length > 0;
                    button.textContent = `${prc}${recovered ? ' · 回収済' : ''}`;
                    button.title = recovered
                        ? `回収: ${preset.recovered_sessions.join(', ')}`
                        : `同定 ${preset.open_loop_session || '—'} rep${preset.open_loop_rep ?? 1}`;
                    button.classList.toggle('paddle-selected', data.active === preset.preset_id);
                    button.setAttribute('aria-pressed', String(data.active === preset.preset_id));
                    button.addEventListener('click', () => selectFlexHwPreset(preset.preset_id));
                    row.append(button);
                }
                presets.append(row);
            }
            flexHwPlanSignature = signature;
        }
        presets.querySelectorAll('button').forEach(b => b.disabled = data.busy || flexHwSelectionPending);
        const current = data.presets.find(p => p.preset_id === data.active);
        active.textContent = current
            ? `選択中: ${current.preset_id}（曲げ FB・Δω 13点・25 Hz・同定 ${current.open_loop_session || '—'}）`
            : 'flex HW 条件は未選択です（下のボタンで 6 条件のいずれかを選ぶ）';
        const readyEl = document.getElementById('flex-hw-plan-ready');
        if (readyEl) {
            readyEl.textContent = data.ready ? `✓ ${data.readiness}` : data.readiness || '';
            readyEl.style.color = data.ready ? '#34d399' : '#fbbf24';
        }
        document.getElementById('flex-hw-plan-order').textContent =
            `推奨順: ${(data.preset_order || []).join(' → ')}`;
    } catch (err) {
        active.textContent = `flex HW プラン読込失敗: ${err.message}`;
    }
}

async function selectFlexHwPreset(presetId) {
    if (flexHwSelectionPending) return;
    flexHwSelectionPending = true;
    flexHwPlanSignature = '';
    document.querySelectorAll('#flex-hw-plan-presets button').forEach(b => b.disabled = true);
    try {
        const response = await fetch('/api/experiment/flex-hw-plan/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_id: presetId }),
        });
        if (!response.ok) throw new Error(await readApiError(response));
        if (typeof loadExperimentConfig === 'function') await loadExperimentConfig();
        if (typeof loadPhaseSequenceStatus === 'function') await loadPhaseSequenceStatus();
        if (typeof refreshFirmwareUploadProfile === 'function') await refreshFirmwareUploadProfile();
        await loadFlexHwPlan();
    } catch (err) {
        alert(err.message);
    } finally {
        flexHwSelectionPending = false;
        loadFlexHwPlan();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('flex-hw-plan-presets')) {
        loadFlexHwPlan();
        setInterval(loadFlexHwPlan, 4000);
    }
});
