document.addEventListener('DOMContentLoaded', () => {
    loadFleet();
    loadPorts();
    
    document.getElementById('btn-batch-ota').addEventListener('click', async () => {
        if (!confirm("オンラインの全機体に一括でOTA書き込みを開始しますか？（順番に処理されます）")) return;
        
        try {
            const res = await fetch('/api/fleet');
            const data = await res.json();
            
            // Filter online agents with a valid IP
            const onlineIps = Object.values(data)
                .filter(agent => agent.is_online && agent.ip)
                .map(agent => agent.ip);
                
            if (onlineIps.length === 0) {
                alert("現在オンラインの機体がありません。");
                return;
            }
            
            startUpload("ota_batch", onlineIps.join(","));
            
        } catch(e) {
            alert("エラーが発生しました: " + e.message);
        }
    });
});

// Load fleet registry
async function loadFleet(silent = false) {
    const list = document.getElementById('fleet-list');
    if (!silent) {
        list.innerHTML = '<div class="loader"></div>';
    }
    
    try {
        const [fleetRes, usbRes] = await Promise.all([
            fetch('/api/fleet'),
            fetch('/api/fleet/usb_devices')
        ]);
        const data = await fleetRes.json();
        const usbData = await usbRes.json();
        const usbUids = usbData.usb_uids || [];
        
        const newHtml = document.createElement('div');
        if (!data.agents || data.agents.length === 0) {
            list.innerHTML = '<div class="empty-state">ロボットが登録されていません。</div>';
            document.getElementById('global-fleet-status').innerHTML = '<div style="font-size: 0.8rem; color: var(--text-muted);">モジュールなし</div>';
            return;
        }
        
        let miniStatusHtml = '';

        data.agents.forEach(agent => {
            const hasIp = agent.ip && agent.ip !== 'null';
            const isOnline = agent.status === 'online';
            const isUsbConnected = usbUids.includes(agent.uid.toUpperCase());
            
            // Build mini status for header
            miniStatusHtml += `
                <div onclick="document.getElementById('card-${agent.id}').scrollIntoView({behavior: 'smooth', block: 'center'})" style="cursor: pointer; display: flex; align-items: center; gap: 0.4rem; background: rgba(255,255,255,0.05); padding: 0.25rem 0.5rem; border-radius: 6px; font-size: 0.75rem; border: 1px solid ${(isOnline || isUsbConnected) ? 'rgba(255,255,255,0.1)' : 'rgba(255,255,255,0.02)'}; opacity: ${(isOnline || isUsbConnected) ? '1' : '0.5'}; transition: all 0.2s ease;" onmouseover="this.style.background='rgba(255,255,255,0.1)'" onmouseout="this.style.background='rgba(255,255,255,0.05)'">
                    <span style="font-weight: 600; color: #fff;">ID:${agent.id}</span>
                    <span style="color: ${isUsbConnected ? '#10b981' : '#4b5563'}; font-weight: bold;">USB</span>
                    <span style="color: ${isOnline ? '#3b82f6' : '#4b5563'}; font-weight: bold;">OTA</span>
                </div>
            `;
            
            const statusClass = isOnline ? 'online' : '';
            const statusText = isOnline ? 'Wi-Fi接続済' : 'オフライン';

            const card = document.createElement('div');
            card.className = 'device-card';
            card.id = `card-${agent.id}`;
            let batteryHtml = '';
            if (agent.volt !== undefined && agent.volt !== null) {
                if (agent.volt < 0) {
                    // I2C sensor not found
                    batteryHtml = `
                        <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.75rem; font-weight: 500; color: #8b92a5; background: rgba(255,255,255,0.05); padding: 0.2rem 0.6rem; border-radius: 1rem; border: 1px dashed rgba(255,255,255,0.2);">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <circle cx="12" cy="12" r="10"></circle>
                                <line x1="12" y1="8" x2="12" y2="12"></line>
                                <line x1="12" y1="16" x2="12.01" y2="16"></line>
                            </svg>
                            断線 (I2C通信エラー)
                        </div>
                    `;
                } else if (agent.volt === 0.0) {
                    // Sensor alive but battery unplugged
                    batteryHtml = `
                        <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.75rem; font-weight: 500; color: #ef4444; background: rgba(239,68,68,0.1); padding: 0.2rem 0.6rem; border-radius: 1rem; border: 1px solid rgba(239,68,68,0.3);">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
                                <line x1="12" y1="9" x2="12" y2="13"></line>
                                <line x1="12" y1="17" x2="12.01" y2="17"></line>
                            </svg>
                            電池未接続 (0.00V)
                        </div>
                    `;
                } else {
                    const pct = Math.max(0, Math.min(100, Math.round((agent.volt - 3.2) / (4.2 - 3.2) * 100)));
                    let batColor = '#10b981'; // green
                    if (pct < 20) batColor = '#ef4444'; // red
                    else if (pct < 50) batColor = '#f59e0b'; // orange
                    
                    batteryHtml = `
                        <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.75rem; font-weight: 500; color: ${batColor}; background: rgba(255,255,255,0.05); padding: 0.2rem 0.6rem; border-radius: 1rem; border: 1px solid rgba(255,255,255,0.1);">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <rect x="2" y="7" width="16" height="10" rx="2" ry="2"></rect>
                                <line x1="22" y1="11" x2="22" y2="13"></line>
                            </svg>
                            ${pct}% (${agent.volt.toFixed(2)}V)
                        </div>
                    `;
                }
            }

            card.innerHTML = `
                <div class="device-info" style="display: flex; justify-content: space-between; align-items: flex-start;">
                    <div>
                        <h3 style="margin: 0 0 0.4rem 0; font-size: 1.1rem;">ID: ${agent.id} | ${agent.hostname}</h3>
                        <div style="font-size: 0.85rem; color: var(--text-muted); line-height: 1.4;">
                            <div>UID: ${agent.uid}</div>
                            <div>IP: ${hasIp ? agent.ip : 'Not Assigned'}</div>
                        </div>
                    </div>
                    ${batteryHtml}
                </div>
                <div style="width: 100%;">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 0.2rem;">
                        
                        <!-- Column 1: USB (Wired) -->
                        <div style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 0.75rem; display: flex; flex-direction: column; gap: 0.6rem;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <div style="font-size: 0.75rem; color: #8b92a5; font-weight: 600; letter-spacing: 0.05em;">USB (WIRED)</div>
                                <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.7rem; color: ${isUsbConnected ? '#10b981' : '#6b7280'}; font-weight: 500;">
                                    <span style="display:inline-block; width:6px; height:6px; border-radius:50%; background-color: ${isUsbConnected ? '#10b981' : '#4b5563'}; box-shadow: ${isUsbConnected ? '0 0 6px rgba(16,185,129,0.8)' : 'none'};"></span>
                                    ${isUsbConnected ? 'Connected' : 'Offline'}
                                </div>
                            </div>
                            <button class="btn" style="background: #10b981; color: white; padding: 0.4rem 0.5rem; font-size: 0.8rem; border: none;" onclick="startUpload('usb', 'auto')" ${!isUsbConnected ? 'disabled' : ''}>Flash Firmware</button>
                            <button class="btn" style="background: transparent; border: 1px solid #10b981; color: #10b981; padding: 0.4rem 0.5rem; font-size: 0.8rem;" onclick="testServoUsb('${agent.uid}')" ${!isUsbConnected ? 'disabled' : ''}>Test Servo</button>
                        </div>

                        <!-- Column 2: Wi-Fi (OTA) -->
                        <div style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 0.75rem; display: flex; flex-direction: column; gap: 0.6rem;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <div style="font-size: 0.75rem; color: #8b92a5; font-weight: 600; letter-spacing: 0.05em;">Wi-Fi (OTA)</div>
                                <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.7rem; color: ${isOnline ? '#3b82f6' : '#6b7280'}; font-weight: 500;">
                                    <span style="display:inline-block; width:6px; height:6px; border-radius:50%; background-color: ${isOnline ? '#3b82f6' : '#4b5563'}; box-shadow: ${isOnline ? '0 0 6px rgba(59,130,246,0.8)' : 'none'};"></span>
                                    ${isOnline ? 'Connected' : (hasIp ? 'Offline (IP保持)' : 'Offline')}
                                </div>
                            </div>
                            <button class="btn" style="background: #3b82f6; color: white; padding: 0.4rem 0.5rem; font-size: 0.8rem; border: none;" onclick="startUpload('ota', '${agent.ip}')" ${!isOnline ? 'disabled' : ''}>Flash Firmware</button>
                            <button class="btn" style="background: transparent; border: 1px solid #3b82f6; color: #3b82f6; padding: 0.4rem 0.5rem; font-size: 0.8rem;" onclick="testServo(${agent.id}, '${agent.ip}')" ${!isOnline ? 'disabled' : ''}>Test Servo</button>
                        </div>
                        
                    </div>
                </div>
            `;
            newHtml.appendChild(card);
        });
        
        list.innerHTML = newHtml.innerHTML;
        document.getElementById('global-fleet-status').innerHTML = miniStatusHtml;
    } catch (err) {
        if (!silent) {
            list.innerHTML = `<div class="empty-state" style="color: #ef4444;">台帳の読み込みに失敗しました: ${err}</div>`;
        }
    }
}

// Auto-refresh fleet every 5 seconds if the tab is visible
setInterval(() => {
    const tabFleet = document.getElementById('tab-fleet');
    if (!document.hidden && tabFleet.style.display !== 'none') {
        loadFleet(true);
    }
}, 5000);

// Load USB ports
async function loadPorts() {
    const select = document.getElementById('port-select');
    try {
        const res = await fetch('/api/ports');
        const ports = await res.json();
        
        select.innerHTML = '<option value="">ポートを選択...</option>';
        ports.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.device;
            opt.textContent = `${p.device} - ${p.description}`;
            select.appendChild(opt);
        });
    } catch (err) {
        console.error("Failed to load ports", err);
    }
}

// Scan USB port
document.getElementById('btn-scan').addEventListener('click', async () => {
    const port = document.getElementById('port-select').value;
    if (!port) return alert("USBポートを選択してください");

    const btn = document.getElementById('btn-scan');
    btn.innerHTML = '<div class="loader"></div> スキャン中...';
    btn.disabled = true;

    const resultDiv = document.getElementById('scan-result');
    resultDiv.style.display = 'block';
    resultDiv.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem;">シリアルポートを監視中...</p>';

    try {
        const res = await fetch(`/api/discover?port=${encodeURIComponent(port)}`);
        const data = await res.json();
        
        if (data.status === 'unregistered') {
            resultDiv.innerHTML = `
                <div style="background: rgba(16,185,129,0.1); border: 1px solid var(--accent-green); padding: 1rem; border-radius: 8px; margin-bottom: 1rem;">
                    <p style="color: var(--accent-green); margin-bottom: 0.5rem; font-weight: 500;">✓ 未登録のボードを検出しました</p>
                    <p style="font-family: monospace; font-size: 0.85rem;">UID: ${data.uid}</p>
                </div>
                
                <div class="form-group">
                    <label>割り当てる機体番号 (ID) (例: 8)</label>
                    <input type="number" id="reg-id" placeholder="8">
                </div>
                <div class="form-group">
                    <label>ホスト名</label>
                    <input type="text" id="reg-host" placeholder="robot-08">
                </div>
                
                <button class="btn btn-primary" style="width: 100%;" onclick="registerDevice('${data.uid}')">
                    台帳に登録する
                </button>
            `;
        } else if (data.status === 'registered') {
            resultDiv.innerHTML = `
                <div style="background: rgba(59,130,246,0.1); border: 1px solid var(--accent-blue); padding: 1rem; border-radius: 8px;">
                    <p style="color: var(--accent-blue); font-weight: 500;">すでに登録済みのデバイスです</p>
                    <p style="font-size: 0.85rem; margin-top: 0.5rem; margin-bottom: 0;">現在の機体番号 (ID): ${data.agent_id}</p>
                    
                    <div style="margin-top: 1rem; padding-top: 1rem; border-top: 1px solid rgba(255,255,255,0.1);">
                        <label style="font-size: 0.85rem; color: #8b92a5;">機体番号を変更する</label>
                        <div style="display: flex; gap: 0.5rem; margin-top: 0.5rem;">
                            <input type="number" id="change-id-val" placeholder="新ID" style="width: 80px; background: rgba(0,0,0,0.2); border: 1px solid var(--border-light); color: white; border-radius: 4px; padding: 0.5rem;">
                            <button class="btn btn-primary" onclick="changeDeviceId('${data.uid}', '${port}')">変更して書き込み</button>
                        </div>
                    </div>

                    <button class="btn btn-usb" style="margin-top: 1rem; width: 100%;" onclick="startUpload('usb', '${port}')">
                        現在のIDのまま有線書き込み (USB)
                    </button>
                </div>
            `;
        } else {
            resultDiv.innerHTML = `
                <div style="color: #ef4444; margin-bottom: 0.5rem; font-size: 0.9rem;">
                    ⚠️ 新規ロボットのUIDを検出できませんでした。（既に稼働中の機体は無言になるためここでは検出されません）
                </div>
                <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.8rem;">
                    もしこのポートに繋がっているのが登録済みの機体であれば、下のリストの各カードから書き込んでください。<br>
                    ※それでもここで強制的に上書きしたい場合は以下のボタンを押してください。
                </div>
                <button class="btn" style="background: transparent; border: 1px solid #ef4444; color: #ef4444; width: 100%;" onclick="startUpload('usb', '${port}')">
                    強制的に上書き書き込みを実行 (非推奨)
                </button>
            `;
        }
    } catch (err) {
        resultDiv.innerHTML = `<p style="color: #ef4444; font-size: 0.85rem;">エラー: ${err}</p>`;
    } finally {
        btn.innerHTML = 'ポートをスキャン';
        btn.disabled = false;
    }
});

// Register Device
async function registerDevice(uid, force = false) {
    const idVal = document.getElementById('reg-id').value;
    const hostVal = document.getElementById('reg-host').value;

    if (!idVal || !hostVal) return alert("すべての項目を入力してください");

    try {
        const res = await fetch('/api/fleet/add', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                id: parseInt(idVal),
                uid: uid,
                hostname: hostVal,
                force: force
            })
        });

        if (!res.ok) {
            const err = await res.json();
            if (res.status === 409) {
                if (confirm(err.detail)) {
                    return registerDevice(uid, true);
                } else {
                    return; // user cancelled
                }
            }
            throw new Error(err.detail);
        }

        alert("登録が完了しました！");
        document.getElementById('scan-result').style.display = 'none';
        loadFleet();
        
    } catch (err) {
        alert("登録に失敗しました: " + err.message);
    }
}

// Change Device ID
async function changeDeviceId(uid, port) {
    const newIdVal = document.getElementById('change-id-val').value;
    if (!newIdVal) return alert("新しいIDを入力してください");

    try {
        const res = await fetch('/api/fleet/change_id', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                uid: uid,
                new_id: parseInt(newIdVal)
            })
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail);
        }

        alert("IDの変更が完了しました！続いて有線書き込みを開始します。");
        document.getElementById('scan-result').style.display = 'none';
        loadFleet();
        startUpload('usb', port);
        
    } catch (err) {
        alert("IDの変更に失敗しました: " + err.message);
    }
}

// Test Servo
async function testServo(agent_id, ip) {
    try {
        const res = await fetch('/api/fleet/test_servo', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ agent_id, ip })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail);
        }
        // Success (silently)
    } catch (err) {
        alert("テスト信号の送信に失敗しました: " + err.message);
    }
}

// Test Servo (USB)
async function testServoUsb(uid) {
    try {
        const res = await fetch('/api/fleet/test_servo_usb', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ uid })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail);
        }
        // Success (silently)
    } catch (err) {
        alert("テスト信号の送信に失敗しました: " + err.message);
    }
}

// Upload Firmware (WebSocket)
let ws = null;
function startUpload(target, value) {
    const terminal = document.getElementById('terminal');
    terminal.innerHTML = '';
    
    function logToTerminal(msg) {
        const line = document.createElement('div');
        line.className = 'terminal-line';
        line.style.color = '#f59e0b'; // Amber for debug
        line.textContent = '[DEBUG] ' + msg;
        terminal.appendChild(line);
        terminal.scrollTop = terminal.scrollHeight;
    }
    
    logToTerminal('startUpload called with target=' + target + ', value=' + value);

    if (ws) {
        logToTerminal('Closing existing ws connection...');
        ws.close();
    }

    // Reset and show progress bar
    const progressContainer = document.getElementById('upload-progress-container');
    const progressBar = document.getElementById('upload-progress-bar');
    const progressText = document.getElementById('upload-status-text');
    const progressPercent = document.getElementById('upload-percent');
    
    if (progressContainer) {
        progressBar.style.width = '0%';
        progressBar.style.background = 'var(--accent-blue)'; // Reset color to blue
        progressText.innerText = 'Initializing...';
        progressText.style.color = '#fff';
        progressPercent.innerText = '0%';
        progressPercent.style.color = '#fff';
        logToTerminal('Progress UI reset');
    }
    
    const wsUrl = `ws://${window.location.host}/ws/upload`;
    logToTerminal('Connecting to ' + wsUrl + ' ...');
    
    try {
        ws = new WebSocket(wsUrl);
    } catch (e) {
        logToTerminal('WebSocket creation failed: ' + e);
        return;
    }
    
    ws.onopen = () => {
        logToTerminal('WebSocket connected! Sending data...');
        ws.send(JSON.stringify({ target, value }));
        logToTerminal('Data sent.');
    };
    
    ws.onerror = (error) => {
        logToTerminal('WebSocket error occurred!');
        console.error(error);
    };
    
    ws.onmessage = (event) => {
        const text = event.data;
        const line = document.createElement('div');
        line.className = 'terminal-line';
        line.textContent = text;
        
        // Parse progress
        if (progressContainer) {
            const progressMatch = text.match(/(?:Loading into Flash|Verifying Flash|Uploading):.*?(\d+)%/);
            if (progressMatch) {
                const percent = progressMatch[1];
                progressBar.style.width = percent + '%';
                progressPercent.innerText = percent + '%';
                if (text.includes('Verifying')) {
                    progressText.innerText = 'Verifying...';
                } else {
                    progressText.innerText = 'Flashing...';
                }
            }
            
            if (text.includes('SUCCESS') || text.includes('成功')) {
                progressBar.style.background = '#10b981'; // Green
                progressText.innerText = 'Success!';
                progressText.style.color = '#10b981';
                progressBar.style.width = '100%';
                progressPercent.innerText = '100%';
                progressPercent.style.color = '#10b981';
            } else if (text.includes('ERROR') || text.includes('FAILED')) {
                progressBar.style.background = '#ef4444'; // Red
                progressText.innerText = 'Error!';
                progressText.style.color = '#ef4444';
                progressPercent.style.color = '#ef4444';
            }
        }

        terminal.appendChild(line);
        terminal.scrollTop = terminal.scrollHeight;
    };

    ws.onclose = (event) => {
        logToTerminal('WebSocket closed with code: ' + event.code + ', reason: ' + event.reason);
        const line = document.createElement('div');
        line.className = 'terminal-line';
        line.style.color = '#8b92a5';
        line.textContent = '--- 通信終了 ---';
        terminal.appendChild(line);
        terminal.scrollTop = terminal.scrollHeight;
    };
}

// --- Global Command ---
async function sendGlobalCommand(cmd) {
    try {
        const res = await fetch('/api/fleet/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail);
        }
        console.log(`Global command ${cmd} sent successfully.`);
    } catch (err) {
        alert("コマンド送信に失敗しました: " + err.message);
    }
}

// --- Telemetry & Charting ---
let telemetryWs = null;
const maxDataPoints = 100;

const chartConfigs = {
    Angle: { id: 'chartAngle', color: '#3b82f6', min: 0, max: 180 },
    Flex: { id: 'chartFlex', color: '#10b981', min: 500, max: 900 },
    Light: { id: 'chartLight', color: '#f59e0b', min: 0, max: 4095 },
    Current: { id: 'chartCurrent', color: '#ef4444', min: 0, max: 4095 },
    Power: { id: 'chartPower', color: '#8b5cf6', min: 0, max: 50000 },
    Voltage: { id: 'chartVoltage', color: '#ec4899', min: 0, max: 10 }
};

const charts = {};
const chartData = {};

function updateChartScale(key) {
    if (!charts[key]) return;
    const minInput = document.getElementById(`min${key}`);
    const maxInput = document.getElementById(`max${key}`);
    if (minInput && maxInput) {
        const minVal = parseFloat(minInput.value);
        const maxVal = parseFloat(maxInput.value);
        if (!isNaN(minVal) && !isNaN(maxVal) && minVal < maxVal) {
            charts[key].options.scales.y.min = minVal;
            charts[key].options.scales.y.max = maxVal;
            charts[key].update();
            
            // Save to localStorage
            localStorage.setItem(`chartScale_${key}`, JSON.stringify({ min: minVal, max: maxVal }));
        }
    }
}

function initCharts() {
    for (const [key, config] of Object.entries(chartConfigs)) {
        const ctx = document.getElementById(config.id);
        if (!ctx) continue;
        
        // Load from localStorage if exists
        const saved = localStorage.getItem(`chartScale_${key}`);
        if (saved) {
            try {
                const parsed = JSON.parse(saved);
                config.min = parsed.min;
                config.max = parsed.max;
                
                // Update DOM inputs
                const minInput = document.getElementById(`min${key}`);
                const maxInput = document.getElementById(`max${key}`);
                if (minInput) minInput.value = parsed.min;
                if (maxInput) maxInput.value = parsed.max;
            } catch (e) {
                console.error("Failed to parse saved chart scale", e);
            }
        }
        
        chartData[key] = {
            labels: Array(maxDataPoints).fill(''),
            datasets: []
        };
        
        charts[key] = new Chart(ctx, {
            type: 'line',
            data: chartData[key],
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                scales: {
                    x: { display: false },
                    y: { 
                        min: config.min, 
                        max: config.max,
                        grid: { color: 'rgba(255,255,255,0.1)' },
                        ticks: { color: '#8b92a5' }
                    }
                },
                plugins: {
                    legend: {
                        labels: { color: '#fff' }
                    }
                }
            }
        });
    }
}

function connectTelemetryWs() {
    const wsUrl = `ws://${window.location.host}/ws/telemetry`;
    telemetryWs = new WebSocket(wsUrl);
    
    telemetryWs.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            updateCharts(data);
        } catch (e) {
            console.error("Failed to parse telemetry:", e);
        }
    };
    
    telemetryWs.onclose = () => {
        setTimeout(connectTelemetryWs, 2000); // Reconnect
    };
}

function updateCharts(data) {
    const agentId = data.id;
    const label = `Robot ${agentId}`;
    
    // Calculate Power (Voltage * Current)
    let power = 0;
    if (data.current !== undefined && data.volt !== undefined) {
        power = Math.abs(data.current * data.volt); // If current is raw/mA, power is proportional
    }

    // Map telemetry fields to chart keys
    const fieldMapping = {
        Angle: data.angle,
        Flex: data.flex,
        Light: data.light,
        Current: data.current,
        Power: power,
        Voltage: data.volt
    };
    
    for (const [key, value] of Object.entries(fieldMapping)) {
        if (value === undefined || !charts[key]) continue;
        
        let dataset = chartData[key].datasets.find(ds => ds.label === label);
        if (!dataset) {
            // Pick a color based on agent ID
            const hue = (agentId * 137.5) % 360;
            const color = `hsl(${hue}, 70%, 50%)`;
            
            dataset = {
                label: label,
                data: Array(maxDataPoints).fill(null),
                borderColor: color,
                borderWidth: 2,
                pointRadius: 0,
                tension: 0.2
            };
            chartData[key].datasets.push(dataset);
        }
        
        dataset.data.push(value);
        dataset.data.shift();
        
        charts[key].update();
    }
}

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    connectTelemetryWs();
});


// --- Records Management ---
async function loadRecords() {
    try {
        const response = await fetch('/api/records');
        const data = await response.json();
        
        const tbody = document.getElementById('records-tbody');
        tbody.innerHTML = '';
        
        if (!data.records || data.records.length === 0) {
            tbody.innerHTML = '<tr><td colspan="3" style="text-align: center; padding: 2rem; color: var(--text-muted);">まだ記録がありません</td></tr>';
            return;
        }
        
        // Sort by robot_id (asc), then by timestamp (asc)
        const sortedRecords = [...data.records].sort((a, b) => {
            const idDiff = parseInt(a.robot_id) - parseInt(b.robot_id);
            if (idDiff !== 0) return idDiff;
            return new Date(a.timestamp) - new Date(b.timestamp);
        });
        
        let lastRobotId = null;
        
        sortedRecords.forEach(record => {
            // Add a visual separator when robot ID changes
            if (lastRobotId !== null && lastRobotId !== record.robot_id) {
                const sep = document.createElement('tr');
                sep.innerHTML = `<td colspan="3" style="border-bottom: 2px solid rgba(255,255,255,0.2);"></td>`;
                tbody.appendChild(sep);
            }
            lastRobotId = record.robot_id;
            
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td style="padding: 0.75rem; border-bottom: 1px solid rgba(255,255,255,0.05); color: #8b92a5;">${record.timestamp}</td>
                <td style="padding: 0.75rem; border-bottom: 1px solid rgba(255,255,255,0.05);"><span class="badge" style="background: rgba(59,130,246,0.2); color: #60a5fa; font-size: 1.1rem; padding: 4px 8px;">#${record.robot_id}</span></td>
                <td style="padding: 0.75rem; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 1.1rem;">${record.memo}</td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error("Failed to load records", e);
    }
}

async function submitRecord() {
    const robotId = document.getElementById('record-robot-id').value;
    const memo = document.getElementById('record-memo').value;
    
    if (!robotId || !memo) {
        alert("ロボット番号とメモを入力してください。");
        return;
    }
    
    try {
        const response = await fetch('/api/records', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                robot_id: parseInt(robotId),
                memo: memo
            })
        });
        
        if (response.ok) {
            document.getElementById('record-memo').value = '';
            loadRecords(); // Refresh table
        } else {
            alert("エラーが発生しました。");
        }
    } catch (e) {
        console.error("Failed to submit record", e);
    }
}

// Populate robot ID dropdown (1-20)
function initRecordsDropdown() {
    const select = document.getElementById('record-robot-id');
    if (!select) return;
    
    select.innerHTML = '<option value="">選択...</option>';
    for (let i = 1; i <= 20; i++) {
        const option = document.createElement('option');
        option.value = i;
        option.textContent = i;
        select.appendChild(option);
    }
}

// Load records on init
document.addEventListener('DOMContentLoaded', () => {
    initRecordsDropdown();
    loadRecords();
});
