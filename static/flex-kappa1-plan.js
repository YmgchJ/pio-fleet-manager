// Flex κ=1 seven-point lock — fleet/current_phase_sequence.yaml + sync_experiment_params.yaml only.
let flexKappa1SelectionPending = false;
let flexKappa1PlanSignature = '';

async function loadFlexKappa1Plan() {
    const active = document.getElementById('flex-kappa1-plan-active');
    const presets = document.getElementById('flex-kappa1-plan-presets');
    if (!presets || !active) return;
    try {
        const response = await fetch('/api/experiment/flex-kappa1-plan', { cache: 'no-store' });
        if (!response.ok) throw new Error(await readApiError(response));
        const data = await response.json();
        const signature = JSON.stringify([data.presets, data.active, data.busy, data.ready]);
        if (signature !== flexKappa1PlanSignature) {
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
                    button.title = `MuJoCo 同型 κ=1・OL ${preset.open_loop_session || '—'}`;
                    button.classList.toggle('paddle-selected', data.active === preset.preset_id);
                    button.setAttribute('aria-pressed', String(data.active === preset.preset_id));
                    button.addEventListener('click', () => selectFlexKappa1Preset(preset.preset_id));
                    row.append(button);
                }
                presets.append(row);
            }
            flexKappa1PlanSignature = signature;
        }
        presets.querySelectorAll('button').forEach(b => b.disabled = data.busy || flexKappa1SelectionPending);
        const current = data.presets.find(p => p.preset_id === data.active);
        active.textContent = current
            ? `選択中: ${current.preset_id}（曲げ FB κ=1・Δω 7点 rad/s・120s・25 Hz）`
            : 'flex κ=1 比較条件は未選択です';
        const readyEl = document.getElementById('flex-kappa1-plan-ready');
        if (readyEl) {
            readyEl.textContent = data.ready ? `✓ ${data.readiness}` : data.readiness || '';
            readyEl.style.color = data.ready ? '#34d399' : '#fbbf24';
        }
        document.getElementById('flex-kappa1-plan-order').textContent =
            `推奨順: ${(data.preset_order || []).join(' → ')}`;
    } catch (err) {
        active.textContent = `flex κ=1 プラン読込失敗: ${err.message}`;
    }
}

async function selectFlexKappa1Preset(presetId) {
    if (flexKappa1SelectionPending) return;
    flexKappa1SelectionPending = true;
    flexKappa1PlanSignature = '';
    document.querySelectorAll('#flex-kappa1-plan-presets button').forEach(b => b.disabled = true);
    try {
        const response = await fetch('/api/experiment/flex-kappa1-plan/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_id: presetId }),
        });
        if (!response.ok) throw new Error(await readApiError(response));
        if (typeof loadExperimentConfig === 'function') await loadExperimentConfig();
        if (typeof loadPhaseSequenceStatus === 'function') await loadPhaseSequenceStatus();
        if (typeof refreshFirmwareUploadProfile === 'function') await refreshFirmwareUploadProfile();
        if (typeof loadFlexKappa1InterleavedPlan === 'function') await loadFlexKappa1InterleavedPlan();
        await loadFlexKappa1Plan();
    } catch (err) {
        alert(err.message);
    } finally {
        flexKappa1SelectionPending = false;
        loadFlexKappa1Plan();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('flex-kappa1-plan-presets')) {
        loadFlexKappa1Plan();
        setInterval(loadFlexKappa1Plan, 4000);
    }
});
