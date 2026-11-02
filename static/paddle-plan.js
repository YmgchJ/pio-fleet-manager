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
            for (const size of [40, 60, 80]) {
                const row = document.createElement('div');
                const label = document.createElement('strong');
                label.textContent = `${size} mm`;
                row.append(label);
                for (const preset of data.presets.filter(p => p.paddle_mm === size)) {
                    const button = document.createElement('button');
                    button.className = 'btn btn-outline';
                    button.dataset.preset = preset.preset_id;
                    const recovered = preset.recovered_sessions.length > 0;
                    const roleLabel = preset.role === 'exploratory' ? '閉ループ方向確認'
                        : preset.role === 'diagnostic' ? '時刻・再現性診断'
                        : preset.role === 'phase_lock' && preset.stage === 'H' ? 'Δω内側7点'
                        : preset.role === 'phase_lock' && preset.stage === 'J' ? 'Δω外側6点'
                        : preset.role === 'phase_lock' ? 'Δω外側6点'
                        : preset.role === 'validation' ? '検証' : '設計';
                    button.textContent = `${preset.stage}${preset.repeat} ${roleLabel}${recovered ? ' · 回収済' : ''}`;
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
        const diagnostic = current && current.stage === 'D';
        const internal = current && current.stage === 'F';
        const exploratory = current && current.stage === 'E';
        const phaseLockOuter = current && (current.stage === 'G' || current.stage === 'J');
        const phaseLockInner60 = current && current.stage === 'H';
        const openLoopMid60 = current && current.paddle_mm === 60 && current.stage === 'A';
        const phaseLock = phaseLockOuter || phaseLockInner60;
        const lockSummary = 'ID12/13、ω=3π、κ=5、50Hzログ、試行60s（10sκ=0→50sFB）、試行間20s、解析10–50s。';
        const weak = current && ['80mm_E2', '80mm_E3', '40mm_E2', '40mm_E3'].includes(current.preset_id);
        const prcLabel = current && current.prc === 'zopt' ? 'Z_opt' : current && current.prc === 'zsin' ? 'Z_sin' : 'cos θ';
        document.getElementById('paddle-plan-heading').textContent = internal
            ? '40 mm F1：内部信号の記録確認（20秒）' : openLoopMid60
            ? `60 mm A${current.repeat}：開ループ S 同定（Δω=+0.1π）`
            : phaseLockInner60
            ? `60 mm H${current.repeat}：Δωロック・内側7点（${prcLabel}）`
            : phaseLockOuter
            ? `${current.paddle_mm} mm ${current.stage}${current.repeat}：Δωロック・外側6点（${prcLabel}）`
            : diagnostic
            ? '80 mm D1：時刻合わせ・再現性の確認（開ループ）' : exploratory ? `${current.paddle_mm} mm E${current.repeat}：閉ループの方向確認（探索）` : 'パドル比較 A：固定位相差の開ループ測定';
        document.getElementById('paddle-plan-summary').textContent = internal ? '40 mm、ID12/13、κ=1、α=92°、90°の1条件20秒。最初10秒κ=0、次の10秒κ=1。全体約50秒。診断FWへの更新が必要です。動画は不要です。' : openLoopMid60
            ? `ID12/13、ω_ref=3π・パートナー2.9π（Δω=+0.1π）、κ=0、50Hz。試行140s（10sκ=0→130s開ループ）、試行間20s、解析10–130s。A1は3試行（設計）、A2は2試行、A3は1試行（検証）。合計約${current.repeat === 1 ? '7分40秒' : current.repeat === 2 ? '5分' : '2分40秒'}。`
            : phaseLock
            ? (phaseLockInner60
                ? `${lockSummary}60 mm、${prcLabel}。Δω/π=±0.15まで0.05π刻み・7条件、約10分。`
                : `${lockSummary}${current.paddle_mm} mm、${prcLabel}。Δω/π=±0.20/0.25/0.30の6条件、約8分。内側セッションとマージで13点。`)
            : diagnostic
            ? 'ID 12 / 13、両機 ω=3π rad/s、全区間 κ=0。0°→90°→180°→270°→180°→180°の6区間 × 30秒。解析10–30秒、合計約4分20秒、最初の動作は開始予約から30秒後です。両パドルが同時に映る動画を開始前から撮影してください。'
            : exploratory ? `ID12/13、${current.paddle_mm} mm、α=92°、κ=${weak ? 1 : 5}。90°→270°の2条件、各${weak ? 110 : 90}秒（最初10秒κ=0、その後${weak ? 100 : 80}秒κ=${weak ? 1 : 5}）。合計約${weak ? '4分20秒' : '3分40秒'}。両機を同時撮影してください。` : 'ID 12 / 13、両機 ω=3π rad/s、κ=0。8位相差 × 30秒、解析10–30秒。合計約5分40秒、最初の動作は30秒後。A1・A2は設計用、A3は検証用です。';
        document.getElementById('paddle-plan-instructions').textContent = internal ? 'pico_robotの新しい診断FWを両機へ書き込み、再起動後の一致を確認してください。従来ログとは別にPDG1を停止後に回収します。回収が終わるまで電源を切らないでください。これは同期の検証ではなく記録の動作確認です。' : openLoopMid60
            ? '推奨は A1（3試行）。停止後に step8 解析と MATLAB で z_opt 係数を更新し、H2 内側7点で z_opt ロック幅を再確認してください。手順は docs/protocols/paddle_60mm_open_loop_identification_20261016.md。'
            : phaseLock
            ? (phaseLockInner60
                ? '各PRCで H→J の順（H1→J1→H2→J2→H3→J3 でも可）。H2/H3は mid_current_Bmid 係数済み。zmode/prc_n は40/80 mm同型（z_opt:1/10, z_sin:2）。6試行FWで外側、7試行で内側。'
                : '40 mmは内側(20261001)の後に G1–G3。60 mmは H の後に J。各セッション前に再起動し κ=5・ω≈9.425 を確認。κ=1閉ループ(E)とは別系列。')
            : diagnostic
            ? '目的は、時計の対応づけによる位相差のずれと、電流応答そのものの変化を調べることです。80 mmの配置・水深・電池電圧・開始前の再起動の有無を記録し、両パドルを同じ画面で撮影してください（可能なら60 fps）。180°の3区間を比較します。閉ループ試験ではありません。α=92°は記録後の解析条件で、κ=0の実験中には使用しません。'
            : exploratory ? `κ=${weak ? 1 : 5}・zmode=1の一致を確認してください。不一致の場合は停止状態で両機を再起動して取得させてください。同じ制御器で90°・270°からの位相差変化を比較します。目標位相への収束を保証する検証ではありません。ID12/13を見分けられる状態で撮影し、異常時はSTOP ALL。` : 'パドル寸法・連結配置・水深・電池電圧・動画の時刻を記録してください。入水順を記録し、A3を設計に転用しないでください。電流sはログからの再構成値です。';
        active.textContent = current
            ? `選択中：${current.paddle_mm} mm / ${current.stage}${current.repeat}（${internal ? '内部診断' : openLoopMid60 ? (current.role === 'validation' ? '開ループ検証' : '開ループ設計') : phaseLockInner60 ? 'Δω内側' : phaseLockOuter ? 'Δω外側' : exploratory ? '探索' : diagnostic ? '診断' : current.role === 'validation' ? '検証' : '設計'}）｜${(internal || diagnostic || exploratory || phaseLock || openLoopMid60) ? (current.recovered_sessions.length ? 'ログ回収済' : 'ログ未回収') : `Aログ回収 ${data.presets.filter(p => p.stage === 'A' && p.paddle_mm === current.paddle_mm && p.recovered_sessions.length).length}/3セット`}`
            : '別の実験設定が有効です。上のボタンで条件を準備できます。';
        document.getElementById('paddle-plan-order').textContent = current
            ? (phaseLock
                ? `今回の順序 Δω：${data.phase_labels.join(' → ')}`
                : `今回の順序 δ=${data.phase_delta_label || 'φ偶−φ奇'}：${data.phase_labels.map(s => `${parseInt(s.replace('delta_', ''), 10)}°`).join(' → ')}`)
            : '';
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
        if (typeof refreshFirmwareUploadProfile === 'function') await refreshFirmwareUploadProfile();
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
