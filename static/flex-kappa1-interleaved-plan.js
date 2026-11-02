// Flex κ=1 interleaved six-point add-on — fleet/current_phase_sequence.yaml + sync_experiment_params.yaml only.
let flexKappa1InterleavedSelectionPending = false;
let flexKappa1InterleavedPlanSignature = '';

async function loadFlexKappa1InterleavedPlan() {
    const active = document.getElementById('flex-kappa1-interleaved-plan-active');
    const presets = document.getElementById('flex-kappa1-interleaved-plan-presets');
    if (!presets || !active) return;
    try {
        const response = await fetch('/api/experiment/flex-kappa1-interleaved-plan', { cache: 'no-store' });
        if (!response.ok) throw new Error(await readApiError(response));
        const data = await response.json();
        const signature = JSON.stringify([data.presets, data.active, data.busy, data.ready, data.active_plan_id]);
        if (signature !== flexKappa1InterleavedPlanSignature) {
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
                    const ref = preset.reference_hw_session_7point || '—';
                    button.textContent = `${prc}${recovered ? ' · 回収済' : ''}`;
                    button.title = `7点参照 ${ref}・seed 20261024+mm`;
                    button.classList.toggle('paddle-selected', data.active === preset.preset_id
                        && data.active_plan_id === 'flex_hw_kappa1_interleaved_6point_v1');
                    button.setAttribute('aria-pressed', String(data.active === preset.preset_id
                        && data.active_plan_id === 'flex_hw_kappa1_interleaved_6point_v1'));
                    button.addEventListener('click', () => selectFlexKappa1InterleavedPreset(preset.preset_id));
                    row.append(button);
                }
                presets.append(row);
            }
            flexKappa1InterleavedPlanSignature = signature;
        }
        presets.querySelectorAll('button').forEach(b => b.disabled = data.busy || flexKappa1InterleavedSelectionPending);
        const current = data.presets.find(p => p.preset_id === data.active)
            && data.active_plan_id === 'flex_hw_kappa1_interleaved_6point_v1'
            ? data.presets.find(p => p.preset_id === data.active)
            : null;
        active.textContent = current
            ? `選択中: ${current.preset_id}（曲げ FB κ=1・追加点 Δω 6・120s・25 Hz）`
            : 'flex κ=1 追加点は未選択です';
        const readyEl = document.getElementById('flex-kappa1-interleaved-plan-ready');
        if (readyEl) {
            readyEl.textContent = data.ready ? `✓ ${data.readiness}` : data.readiness || '';
            readyEl.style.color = data.ready ? '#34d399' : '#fbbf24';
        }
        document.getElementById('flex-kappa1-interleaved-plan-order').textContent =
            `推奨順: ${(data.preset_order || []).join(' → ')}`;
    } catch (err) {
        active.textContent = `flex κ=1 追加点読込失敗: ${err.message}`;
    }
}

async function selectFlexKappa1InterleavedPreset(presetId) {
    if (flexKappa1InterleavedSelectionPending) return;
    flexKappa1InterleavedSelectionPending = true;
    flexKappa1InterleavedPlanSignature = '';
    document.querySelectorAll('#flex-kappa1-interleaved-plan-presets button').forEach(b => b.disabled = true);
    try {
        const response = await fetch('/api/experiment/flex-kappa1-interleaved-plan/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_id: presetId }),
        });
        if (!response.ok) throw new Error(await readApiError(response));
        if (typeof loadExperimentConfig === 'function') await loadExperimentConfig();
        if (typeof loadPhaseSequenceStatus === 'function') await loadPhaseSequenceStatus();
        if (typeof refreshFirmwareUploadProfile === 'function') await refreshFirmwareUploadProfile();
        if (typeof loadFlexKappa1Plan === 'function') await loadFlexKappa1Plan();
        await loadFlexKappa1InterleavedPlan();
    } catch (err) {
        alert(err.message);
    } finally {
        flexKappa1InterleavedSelectionPending = false;
        loadFlexKappa1InterleavedPlan();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('flex-kappa1-interleaved-plan-presets')) {
        loadFlexKappa1InterleavedPlan();
        setInterval(loadFlexKappa1InterleavedPlan, 4000);
    }
});
