// Preset selection writes PC configuration only. Motion uses the existing START path.
let paddleSelectionPending = false;
let paddlePlanSignature = '';

async function loadPaddlePlan() {
    const active = document.getElementById('paddle-plan-active');
    const presets = document.getElementById('paddle-plan-presets');
    try {
        const response = await fetch('/api/experiment/paddle-plan', { cache: 'no-store' });
        if (!response.ok) throw new Error(await readApiError(response));
        const data = await response.json();
        const signature = JSON.stringify([data.presets, data.active]);
        if (signature !== paddlePlanSignature) {
            presets.replaceChildren();
            for (const size of [40, 80]) {
                const row = document.createElement('div');
                const label = document.createElement('strong');
                label.textContent = `${size} mm`;
                row.append(label);
                for (const preset of data.presets.filter(p => p.paddle_mm === size)) {
                    const button = document.createElement('button');
                    button.className = 'btn btn-outline';
                    button.dataset.preset = preset.preset_id;
                    const recovered = preset.recovered_sessions.length > 0;
                    button.textContent = `A${preset.repeat} ${preset.role === 'validation' ? '検証' : '設計'}${recovered ? ' · 回収済' : ''}`;
                    button.title = recovered ? `回収セッション: ${preset.recovered_sessions.join(', ')}` : '2台分のログはまだ回収されていません';
                    button.classList.toggle('paddle-selected', data.active === preset.preset_id);
                    button.setAttribute('aria-pressed', String(data.active === preset.preset_id));
                    button.addEventListener('click', () => selectPaddlePreset(preset.preset_id));
                    row.append(button);
                }
                presets.append(row);
            }
            paddlePlanSignature = signature;
        }
        presets.querySelectorAll('button').forEach(b => b.disabled = data.busy || paddleSelectionPending);
        const current = data.presets.find(p => p.preset_id === data.active);
        active.textContent = current
            ? `選択中：${current.paddle_mm} mm / A${current.repeat}（${current.role === 'validation' ? '未使用検証' : '設計'}）｜ログ回収 ${data.presets.filter(p => p.paddle_mm === current.paddle_mm && p.recovered_sessions.length).length}/3セット`
            : '別の実験設定が有効です。上のボタンで条件を準備できます。';
        document.getElementById('paddle-plan-order').textContent = current
            ? `今回の順序 δ=${data.phase_delta_label || 'φ偶−φ奇'}：${data.phase_labels.map(s => `${parseInt(s.replace('delta_', ''), 10)}°`).join(' → ')}` : '';
        const readiness = document.getElementById('paddle-plan-ready');
        readiness.style.color = data.ready ? '#34d399' : '#fbbf24';
        readiness.textContent = data.busy
            ? '実行中またはログ回収待ちです。回収終了まで条件選択をロックしています。'
            : `実機確認：${data.readiness}`;
        document.getElementById('phase-config-editor').hidden = Boolean(current);
        const globalStart = document.getElementById('btn-global-start');
        globalStart.disabled = Boolean(current);
        globalStart.textContent = current ? '開始は下の「自動シーケンス開始」' : '▶ START ALL (一斉開始)';
    } catch (err) {
        active.textContent = `条件を取得できません：${err.message}`;
        presets.querySelectorAll('button').forEach(b => b.disabled = true);
    }
}

async function selectPaddlePreset(presetId) {
    if (paddleSelectionPending) return;
    paddleSelectionPending = true;
    document.querySelectorAll('#paddle-plan-presets button').forEach(b => b.disabled = true);
    try {
        const response = await fetch('/api/experiment/paddle-plan/select', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_id: presetId })
        });
        if (!response.ok) throw new Error(await readApiError(response));
        await loadExperimentConfig();
        await loadPhaseSequenceStatus();
    } catch (err) {
        alert(`条件を準備できません：${err.message}`);
    } finally {
        paddleSelectionPending = false;
        await loadPaddlePlan();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    if (new URLSearchParams(location.search).get('experiment') === 'paddle') switchTab('monitor');
    loadPaddlePlan();
    setInterval(loadPaddlePlan, 4000);
});
