import os
import re
import yaml
import json
import asyncio
import subprocess
import csv
import math
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import serial.tools.list_ports
import socket
import time
import serial
from pydantic import BaseModel
import contextlib

import shutil

from log_receiver import (
    LogReceiver,
    MERGED_CHUNKS_DIR,
    SAVED_CHUNKS_DIR,
    EXPERIMENTS_DIR,
    register_fm_event_listener,
)
from sync_params import (
    Z_MODES,
    build_param_payload,
    effective_prc_params,
    invalidate_sync_params_cache,
    load_sync_params,
    parse_param_payload,
)
from experiment_sequence import (
    ExperimentSequenceController,
    SequenceValidationError,
    _resolve_even_odd_pair,
)
from paddle_plan import PRESET_IDS, read_preset, activate_preset, recovered_presets
from wifi_config_store import inject_for_firmware, import_from_pico_robot, load_saved, save as save_wifi_config

# --- UDP Auto-Discovery + high-rate log receiver ---
agent_live_state = {}
log_receiver = LogReceiver()
sequence_controller: ExperimentSequenceController | None = None

def update_agent_ip(agent_id: int, ip_address: str):
    if not AGENTS_YAML_PATH.exists():
        return
        
    with open(AGENTS_YAML_PATH, 'r') as f:
        data = yaml.safe_load(f)
        
    agents = data.get("agents", [])
    updated = False
    
    for a in agents:
        if a["id"] == agent_id:
            if a.get("ip") != ip_address:
                a["ip"] = ip_address
                updated = True
            break
            
    if updated:
        with open(AGENTS_YAML_PATH, 'w') as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        print(f"[Auto-Discovery] Updated agent {agent_id} IP to {ip_address}")

telemetry_clients = set()

def broadcast_telemetry(data_dict: dict):
    if not telemetry_clients:
        return
    
    # Broadcast to all connected websocket clients
    message = json.dumps(data_dict)
    
    # We must schedule this in the event loop since this is called from DatagramProtocol
    loop = asyncio.get_running_loop()
    for ws in list(telemetry_clients):
        # We need to send it asynchronously
        asyncio.run_coroutine_threadsafe(ws.send_text(message), loop)

RECORD_SIZE_V6 = 6
RECORD_SIZE_V7 = 7  # Volvocine compressed log record: micros24(3) + a0 + a1 + a2


def is_binary_log_packet(data: bytes) -> bool:
    """True only for Volvocine RAM log UDP packets, not STATUS/HELLO text."""
    if len(data) < 11:  # 5-byte header + at least one 6-byte record
        return False
    # Text telemetry / handshakes (STATUS starts with 'S' = 0x53, in 1..99 range)
    if data.startswith((b"STATUS,", b"HELLO", b"REQUEST")):
        return False
    if data[0] < 128 and chr(data[0]).isalpha():
        return False
    agent_id = data[0]
    if agent_id == 0 or agent_id > 99:
        return False
    payload = data[5:]
    if len(payload) == 0:
        return False
    return (len(payload) % RECORD_SIZE_V7 == 0) or (len(payload) % RECORD_SIZE_V6 == 0)


def parse_status_message(msg: str, agent_id: int, ip_address: str) -> dict:
    """Parse robot STATUS UDP line into a telemetry dict."""
    telemetry: dict = {"id": agent_id, "ip": ip_address, "timestamp": time.time()}
    for field in ["paused", "angle", "flex", "light", "current", "volt",
                  "kappa", "omega", "params", "src", "zmode", "flex_used"]:
        f_match = re.search(rf"{field}:([0-9.-]+)", msg)
        if f_match:
            telemetry[field] = float(f_match.group(1))
    mode_match = re.search(r"(?:^|,)mode:([^,\s]+)", msg)
    if mode_match:
        telemetry["mode"] = mode_match.group(1)
    fw_match = re.search(r"fw:([^,\s]+)", msg)
    if fw_match:
        telemetry["fw"] = fw_match.group(1)
    path_match = re.search(r"(?:^|,)interaction_path:([^,\s]+)", msg)
    if path_match:
        telemetry["interaction_path"] = path_match.group(1)
    return telemetry


def parse_sequence_event(msg: str, ip_address: str) -> dict:
    """Parse SEQ_EVENT key:value fields without losing string metadata."""
    event: dict = {"ip": ip_address, "received_timestamp": time.time()}
    for part in msg.split(",")[1:]:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if key in {"trial", "agent", "elapsed_ms", "feedback_start_ms", "duration_ms", "start_delay_ms",
                   "zmode", "flex_used", "current_window", "normalization_window",
                   "sample_period_us", "ina_ready", "ina_ok",
                   "ina_fail", "records", "upload_ok", "trial_start_micros24",
                   "trial_feedback_micros24", "max_start_late_ms"}:
            try:
                event[key] = int(float(value))
                continue
            except ValueError:
                pass
        if key in {"phase_offset", "phase_diff_target", "kappa", "kappa_now", "omega",
                   "feedback_tau", "servo_center", "servo_amplitude", "phase_correction"}:
            try:
                event[key] = float(value)
                continue
            except ValueError:
                pass
        event[key] = value
    return event


class UdpDiscoveryProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        # Volvocine compressed log: [agent_id:1][send_micros:4][records:6*n]
        if is_binary_log_packet(data):
            ack = log_receiver.handle_datagram(data, addr, self.transport)
            if ack is not None and hasattr(self, "transport"):
                self.transport.sendto(ack, addr)
            return

        try:
            msg = data.decode('utf-8')
            if msg.startswith("REQUEST_PARAMS"):
                id_m = re.search(r"id:(\d+)", msg)
                if id_m:
                    agent_id = int(id_m.group(1))
                    payload = build_param_payload(agent_id)
                    if hasattr(self, "transport"):
                        self.transport.sendto(payload.encode("utf-8"), addr)
                        print(f"[PARAMS] agent={agent_id} -> {payload[:120]}...")
                        log_receiver.record_params_served(
                            agent_id, payload, parse_param_payload(payload)
                        )
                return
            if msg.startswith("SEQ_EVENT,"):
                id_m = re.search(r"(?:^|,)agent:(\d+)", msg)
                if id_m:
                    agent_id = int(id_m.group(1))
                    event = parse_sequence_event(msg, addr[0])
                    log_receiver.record_experiment_event(agent_id, event)
                    if sequence_controller is not None:
                        sequence_controller.handle_robot_event(agent_id, event)
                    ev = str(event.get("event", ""))
                    if ev == "UPLOAD":
                        from log_receiver import fm_event

                        fm_event(
                            f"[ログ] agent={agent_id} 転送開始 seq={event.get('seq', '')}"
                        )
                    elif ev == "UPLOAD_DONE":
                        from log_receiver import fm_event

                        fm_event(
                            f"======== agent={agent_id} 転送完了 UPLOAD_DONE "
                            f"records={event.get('records', -1)} "
                            f"seq={event.get('seq', '')} ========"
                        )
                return
            if msg.startswith("LOG_END,"):
                id_m = re.search(r"id:(\d+)", msg)
                rec_m = re.search(r"records:(\d+)", msg)
                ok_m = re.search(r"ok:([01])", msg)
                seq_m = re.search(r"seq:([^,\s]+)", msg)
                trial_m = re.search(r"trial:(\d+)", msg)
                if id_m:
                    agent_id = int(id_m.group(1))
                    records = int(rec_m.group(1)) if rec_m else 0
                    ok = ok_m.group(1) == "1" if ok_m else False
                    sequence_id = seq_m.group(1) if seq_m else None
                    trial = int(trial_m.group(1)) if trial_m else None
                    if sequence_controller is not None:
                        # Update the sequence state before LogReceiver may merge
                        # synchronously on the second LOG_END.
                        sequence_controller.handle_log_end(
                            agent_id, records, ok, sequence_id, trial
                        )
                    log_receiver.handle_log_end(agent_id, records, ok, sequence_id, trial)
                return
            if msg.startswith("STATUS,id:"):
                # STATUS,id:1,paused:0,angle:110.0,flex:1024,light:512,current:256,volt:3.70
                match = re.search(r"id:(\d+)", msg)
                if match:
                    agent_id = int(match.group(1))
                    ip_address = addr[0]
                    update_agent_ip(agent_id, ip_address)
                    telemetry = parse_status_message(msg, agent_id, ip_address)
                    log_receiver.record_status(agent_id, telemetry)

                    # Update live state for volt (dashboard use)
                    agent_live_state[agent_id] = {
                        "volt": telemetry.get("volt"),
                        "paused": telemetry.get("paused"),
                        "kappa": telemetry.get("kappa"),
                        "omega": telemetry.get("omega"),
                        "params": telemetry.get("params"),
                        "src": telemetry.get("src"),
                        "zmode": telemetry.get("zmode"),
                        "flex_used": telemetry.get("flex_used"),
                        "mode": telemetry.get("mode"),
                        "fw": telemetry.get("fw"),
                        "interaction_path": telemetry.get("interaction_path"),
                        "ip": ip_address,
                        "last_seen": telemetry["timestamp"],
                    }

                    broadcast_telemetry(telemetry)
        except Exception as e:
            print(f"[UDP Error] {e}")

async def discovery_loop(transport):
    while True:
        try:
            for agent_id in range(1, 31):
                port = 5000 + agent_id
                msg = f"{agent_id}:DISCOVER".encode('utf-8')
                transport.sendto(msg, ('255.255.255.255', port))
        except Exception as e:
            pass
        await asyncio.sleep(2.0)

async def chunk_timeout_loop():
    while True:
        try:
            log_receiver.check_timeouts()
        except Exception as e:
            print(f"[Chunk] timeout check error: {e}")
        await asyncio.sleep(1.0)

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global sequence_controller
    loop = asyncio.get_running_loop()
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(('0.0.0.0', 5000))
    
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UdpDiscoveryProtocol(),
        sock=sock
    )
    print("[Auto-Discovery] UDP listener started on 0.0.0.0:5000 with SO_BROADCAST")

    sequence_config_path = Path(
        os.environ.get(
            "SEQUENCE_CONFIG_PATH",
            str(FLEET_MANAGER_ROOT.parent / "fleet" / "current_phase_sequence.yaml"),
        )
    )
    sequence_controller = ExperimentSequenceController(
        config_path=sequence_config_path,
        experiments_dir=EXPERIMENTS_DIR,
        log_receiver=log_receiver,
        target_provider=lambda agent_ids: [
            a for a in _agents_with_ips(online_only=True) if int(a.get("id")) in agent_ids
        ],
    )
    log_receiver.set_session_merged_callback(sequence_controller.handle_session_merged)

    def _mirror_fm_event_to_dashboard(line: str) -> None:
        broadcast_telemetry({"type": "fm_event", "line": line})

    register_fm_event_listener(_mirror_fm_event_to_dashboard)
    from log_receiver import fm_event

    fm_event("Fleet Manager 起動完了（ログはこのターミナルと data/fm_events.log に出ます）")
    
    discovery_task = asyncio.create_task(discovery_loop(transport))
    chunk_task = asyncio.create_task(chunk_timeout_loop())
    try:
        yield
    finally:
        if sequence_controller is not None:
            await sequence_controller.shutdown()
        log_receiver.set_session_merged_callback(None)
        sequence_controller = None
        chunk_task.cancel()
        discovery_task.cancel()
        transport.close()
        sock.close()

app = FastAPI(title="Modular Robots Fleet Manager", lifespan=lifespan)


@app.exception_handler(Exception)
async def json_internal_error_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    print(f"[HTTP 500] {request.method} {request.url.path}: {exc}", flush=True)
    return JSONResponse(status_code=500, content={"detail": str(exc)})

# CORS for local dev if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION (Environment Variables) ---
FLEET_MANAGER_ROOT = Path(__file__).resolve().parent
# FIRMWARE_ROOT_DIR points to the parent directory containing PIO projects (e.g. firmware/)
FIRMWARE_ROOT_DIR = Path(os.environ.get("PIO_FIRMWARE_ROOT_DIR", FLEET_MANAGER_ROOT.parent / "firmware"))
# FIRMWARE_DIR points to the default PlatformIO project directory (legacy fallback)
FIRMWARE_DIR = Path(os.environ.get("PIO_FIRMWARE_DIR", FIRMWARE_ROOT_DIR / "pico_robot"))
# AGENTS_YAML_PATH — canonical fleet registry lives in fleet/agents.yaml (see AGENTS.md)
AGENTS_YAML_PATH = Path(os.environ.get("PIO_AGENTS_PATH", FLEET_MANAGER_ROOT.parent / "fleet" / "agents.yaml"))
# RECORDS_CSV_PATH is where the robot memos/records are saved
RECORDS_CSV_PATH = FLEET_MANAGER_ROOT / "data" / "records.csv"
# Optional script to run after registration (e.g. to generate C++ headers)
POST_REG_SCRIPT = os.environ.get("PIO_POST_REG_SCRIPT", None)
SYNC_PARAMS_PATH = FLEET_MANAGER_ROOT.parent / "fleet" / "sync_experiment_params.yaml"
DEFAULT_SEQUENCE_CONFIG_PATH = FLEET_MANAGER_ROOT.parent / "fleet" / "current_phase_sequence.yaml"


def _sequence_config_path() -> Path:
    if sequence_controller is not None:
        return Path(sequence_controller.config_path)
    return Path(os.environ.get("SEQUENCE_CONFIG_PATH", str(DEFAULT_SEQUENCE_CONFIG_PATH)))


def _read_yaml_path(path: Path) -> dict:
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"設定ファイルがありません: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=500, detail=f"設定を読めません: {exc}") from exc


def _write_yaml_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as f:
            temp_path = Path(f.name)
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        os.replace(temp_path, path)
    except (OSError, yaml.YAMLError) as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"設定を保存できません: {exc}") from exc


def _experiment_config_view(sequence: dict, sync: dict, sequence_path: Path) -> dict:
    sequence_type = str(sequence.get("sequence_type", "phase_sweep")).lower()
    trial_count = len(sequence.get("delta_omega_rad", []) or sequence.get("phase_differences_rad", []))
    seq_kappa = float(sequence.get("kappa", sync.get("kappa", 5.0)))
    sync_kappa = float(sync.get("kappa", seq_kappa))
    return {
        "sequence_config_path": str(sequence_path),
        "sync_params_path": str(SYNC_PARAMS_PATH),
        "z_mode": str(sync.get("z_mode", "cos")).lower(),
        "sequence_type": sequence_type,
        "experiment_mode": str(sequence.get("experiment_mode", "synchronization")).lower(),
        "trial_count": trial_count,
        "trial_duration_s": float(sequence.get("trial_duration_ms", 60000)) / 1000.0,
        "kappa": seq_kappa,
        "sync_yaml_kappa": sync_kappa,
        "kappa_yaml_aligned": abs(seq_kappa - sync_kappa) < 1e-6,
    }


def _update_prc_mode(current_sync: dict, z_mode: str) -> dict:
    z_mode = str(z_mode).lower().strip()
    if z_mode not in Z_MODES:
        raise HTTPException(status_code=400, detail="z_mode は cos / zsin / zopt のいずれかです")
    sync = dict(current_sync)
    sync["z_mode"] = z_mode
    try:
        effective_prc_params(sync)
    except (FileNotFoundError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"PRC プロファイルを読めません: {exc}") from exc
    return sync

# Serve static files
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

@app.get("/")
def redirect_to_static(request: Request):
    from fastapi.responses import RedirectResponse
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/static/index.html{query}")

# --- Models ---
class AgentData(BaseModel):
    id: int
    uid: str
    hostname: str
    force: bool = False

class OtaRequest(BaseModel):
    ip: str

class UsbUploadRequest(BaseModel):
    port: str

class ChangeIdRequest(BaseModel):
    uid: str
    new_id: int

class TestServoRequest(BaseModel):
    agent_id: int
    ip: str

class TestServoUsbRequest(BaseModel):
    uid: str

class GlobalCommandRequest(BaseModel):
    command: str  # "START" or "STOP"

class WiFiConfigRequest(BaseModel):
    ssid: str
    password: str = ""

class PhaseSequenceStartRequest(BaseModel):
    agent_ids: list[int] = [11, 12]


class ExperimentConfigRequest(BaseModel):
    """Web UI exposes PRC (z_mode) only; sequence YAML is edited offline."""
    z_mode: str = "zopt"


class PaddlePresetRequest(BaseModel):
    preset_id: str

# --- Endpoints ---

def _paddle_agent_pair_label(sequence: dict) -> tuple[list[int], str]:
    agents = [int(a) for a in sequence.get("agents", [13, 12])]
    if len(agents) != 2:
        return agents, "φ偶−φ奇"
    ref, partner = _resolve_even_odd_pair(agents)
    return agents, f"φ{ref}−φ{partner}"


@app.get("/api/experiment/paddle-plan")
def get_paddle_plan():
    sequence = _read_yaml_path(_sequence_config_path())
    agent_ids, phase_delta_label = _paddle_agent_pair_label(sequence)
    recovered = recovered_presets(EXPERIMENTS_DIR)
    status = sequence_controller.get_status().get("status", "idle") if sequence_controller else "unavailable"
    busy = sequence_controller is not None and status in sequence_controller.ACTIVE_STATES | {"awaiting_upload"}
    readiness = "実機の停止状態とパラメータを確認してください"
    ready = False
    if sequence_controller is not None and not busy:
        try:
            plan = sequence_controller._load_plan(None)
            sequence_controller._validate_targets(plan["agent_ids"], plan)
            readiness = (
                f"ID {agent_ids[0]} / {agent_ids[1]} の停止・current-only・κ・ω・PRCを確認済み"
            )
            ready = True
        except (SequenceValidationError, OSError, ValueError) as exc:
            readiness = str(exc)
    presets = []
    for preset_id in PRESET_IDS:
        config, _ = read_preset(preset_id)
        presets.append({**config["research_plan"], "recovered_sessions": recovered.get(preset_id, [])})
    return {
        "active": sequence.get("research_plan", {}).get("preset_id"),
        "busy": bool(busy), "status": status, "presets": presets,
        "phase_labels": sequence.get("phase_labels", []),
        "agent_ids": agent_ids,
        "phase_delta_label": phase_delta_label,
        "ready": ready, "readiness": readiness,
    }


@app.post("/api/experiment/paddle-plan/select")
async def select_paddle_preset(req: PaddlePresetRequest):
    # No await between checking state and replacing files: START cannot interleave.
    if sequence_controller is not None:
        status = sequence_controller.get_status().get("status")
        if status in sequence_controller.ACTIVE_STATES | {"awaiting_upload"}:
            raise HTTPException(status_code=409, detail="実行中・ログ回収待ちは条件を切り替えられません")
    try:
        backup = activate_preset(req.preset_id, _sequence_config_path(), SYNC_PARAMS_PATH,
                                  EXPERIMENTS_DIR.parent / "paddle_plan_backups")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"設定を保存できません: {exc}") from exc
    invalidate_sync_params_cache()
    if sequence_controller is not None:
        sequence_controller._state = {"status": "idle"}
    return {"status": "prepared", "backup": str(backup),
            "message": "条件を準備しました。実機への開始指令は送信していません。", **get_paddle_plan()}

@app.get("/api/ports")
def get_serial_ports():
    ports = serial.tools.list_ports.comports()
    # Filter for typical Pico USB CDC
    result = []
    for p in ports:
        if "usbmodem" in p.device or "USB" in p.description:
            result.append({"device": p.device, "description": p.description})
    return result

@app.get("/api/fleet/usb_devices")
def get_usb_devices():
    """Returns a list of UIDs currently connected via USB."""
    ports = serial.tools.list_ports.comports()
    uids = []
    for p in ports:
        if "usbmodem" in p.device and p.serial_number:
            uids.append(p.serial_number.upper())
    return {"usb_uids": uids}

@app.get("/api/fleet/firmwares")
def get_firmwares():
    """Scans FIRMWARE_ROOT_DIR for PlatformIO projects."""
    firmwares = []
    if FIRMWARE_ROOT_DIR.exists() and FIRMWARE_ROOT_DIR.is_dir():
        for item in FIRMWARE_ROOT_DIR.iterdir():
            if item.is_dir() and (item / "platformio.ini").exists():
                firmwares.append(item.name)
    return {"firmwares": sorted(firmwares)}

@app.get("/api/fleet/wifi_config")
def get_wifi_config():
    """SSID/password for UI prefill. Falls back to pico_robot/include/wifi_config.local.h."""
    saved = load_saved()
    if saved:
        return {
            "ssid": saved["ssid"],
            "password": saved["password"],
            "source": "saved",
            "saved_at": saved.get("saved_at"),
        }
    imported = import_from_pico_robot(FIRMWARE_ROOT_DIR)
    if imported:
        return {"ssid": imported["ssid"], "password": imported["password"], "source": "pico_robot"}
    return {"ssid": "", "password": "", "source": "empty"}

@app.put("/api/fleet/wifi_config")
def put_wifi_config(req: WiFiConfigRequest):
    ssid = req.ssid.strip()
    if not ssid:
        raise HTTPException(status_code=400, detail="SSID is required")
    save_wifi_config(ssid, req.password)
    saved = load_saved()
    return {
        "status": "success",
        "ssid": ssid,
        "saved_at": saved.get("saved_at") if saved else None,
    }


@app.get("/api/experiment/config")
def get_experiment_config():
    sequence_path = _sequence_config_path()
    sequence = _read_yaml_path(sequence_path)
    sync = _read_yaml_path(SYNC_PARAMS_PATH)
    return _experiment_config_view(sequence, sync, sequence_path)


@app.post("/api/experiment/reload")
def reload_experiment_config():
    """Re-read fleet YAML from disk (no FM process restart). Robots still need reboot to latch new PRC."""
    if sequence_controller is not None:
        status = sequence_controller.get_status().get("status")
        if status in sequence_controller.ACTIVE_STATES | {"awaiting_upload", "running_autonomous"}:
            raise HTTPException(status_code=409, detail="実験実行中は設定を再読込できません")

    invalidate_sync_params_cache()
    sequence_path = _sequence_config_path()
    sequence = _read_yaml_path(sequence_path)
    sync = load_sync_params()
    preview: dict = {}
    if sequence_controller is not None:
        st = sequence_controller.get_status()
        for key in (
            "message",
            "total_trials",
            "z_mode",
            "expected_kappa",
            "kappa_yaml_aligned",
            "sequence_type",
        ):
            if key in st:
                preview[key] = st[key]
    return {
        "status": "reloaded",
        "message": (
            "シーケンス YAML と sync_experiment_params.yaml を再読み込みしました。"
            "実機の zmode/κ/ω は停止状態でリブート（または OTA 再起動）後に反映されます。"
        ),
        "sequence_preview": preview,
        **_experiment_config_view(sequence, sync, sequence_path),
    }


@app.put("/api/experiment/config")
def put_experiment_config(req: ExperimentConfigRequest):
    if sequence_controller is not None:
        status = sequence_controller.get_status().get("status")
        if status in sequence_controller.ACTIVE_STATES | {"awaiting_upload", "running_autonomous"}:
            raise HTTPException(status_code=409, detail="実験実行中は条件を変更できません")

    sequence_path = _sequence_config_path()
    sequence = _read_yaml_path(sequence_path)
    current_sync = _read_yaml_path(SYNC_PARAMS_PATH)
    sync = _update_prc_mode(current_sync, req.z_mode)

    _write_yaml_atomic(SYNC_PARAMS_PATH, sync)
    invalidate_sync_params_cache()
    return {
        "status": "saved",
        "message": "PRC を保存しました。ロボットを再起動してパラメータを再取得してください。",
        **_experiment_config_view(sequence, sync, sequence_path),
    }


@app.get("/api/discover")
def discover_device(port: str):
    """Listens to the serial port for 3 seconds to extract UID or agent_id."""
    try:
        ser = serial.Serial(port, 115200, timeout=0.1)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to open port: {e}")
    
    uid = None
    agent_id = None
    
    # Listen for up to 3 seconds
    import time
    start_time = time.time()
    buffer = ""
    while time.time() - start_time < 3.0:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore')
            buffer += line
            
            # Match UID
            # e.g. [ERROR] Unknown board! UID: E663A837CB999999
            uid_match = re.search(r"UID:\s*([A-F0-9]{16})", buffer)
            if uid_match:
                uid = uid_match.group(1)
                break
                
            # Match agent_id
            # e.g. agent_id = 7 or [HEARTBEAT] agent_id: 7
            id_match = re.search(r"agent_id\s*[=:]\s*(\d+)", buffer)
            if id_match:
                agent_id = int(id_match.group(1))
                # Might also print UID if we want to change firmware, but currently it just prints agent_id
                
        time.sleep(0.05)
    
    ser.close()
    
    if uid:
        return {"status": "unregistered", "uid": uid}
    elif agent_id is not None:
        return {"status": "registered", "agent_id": agent_id}
    else:
        return {"status": "unknown", "raw_output": buffer.strip()}

@app.get("/api/fleet")
def get_fleet():
    if not AGENTS_YAML_PATH.exists():
        return {"agents": []}
    
    with open(AGENTS_YAML_PATH, 'r') as f:
        data = yaml.safe_load(f)
        
    for a in data.get("agents", []):
        state = agent_live_state.get(a["id"])
        # If seen in the last 5 seconds, it's online
        if state and time.time() - state["last_seen"] < 5.0:
            a["volt"] = state["volt"]
            a["status"] = "online"
            a["kappa"] = state.get("kappa")
            a["omega"] = state.get("omega")
            a["params"] = state.get("params")
            a["src"] = state.get("src")
            a["zmode"] = state.get("zmode")
            a["flex_used"] = state.get("flex_used")
            a["mode"] = state.get("mode")
            a["fw"] = state.get("fw")
            a["interaction_path"] = state.get("interaction_path")
        else:
            a["volt"] = None
            a["status"] = "offline"
            a["kappa"] = None
            a["omega"] = None
            a["params"] = None
            a["src"] = None
            a["zmode"] = None
            a["flex_used"] = None
            a["mode"] = None
            a["fw"] = None
            a["interaction_path"] = None
            
    return data

@app.post("/api/fleet/add")
def add_agent(agent: AgentData):
    if not AGENTS_YAML_PATH.exists():
        raise HTTPException(status_code=404, detail="agents.yaml not found")
        
    with open(AGENTS_YAML_PATH, 'r') as f:
        data = yaml.safe_load(f)
        
    agents = data.get("agents", [])
    
    if not agent.force:
        for a in agents:
            if a["id"] == agent.id:
                raise HTTPException(status_code=409, detail=f"機体番号 {agent.id} はすでに登録されています。上書きしてよろしいですか？")
            if a["uid"] == agent.uid:
                raise HTTPException(status_code=409, detail=f"この基板のUID ({agent.uid}) はすでに別の機体番号 (ID: {a['id']}) として登録されています。新しい番号に移動させますか？")
                
    # Remove any existing agent with the same ID or same UID to allow overwriting
    agents = [a for a in agents if a["id"] != agent.id and a["uid"] != agent.uid]
            
    # Add new agent
    new_agent = {
        "id": agent.id,
        "uid": agent.uid,
        "hostname": agent.hostname,
        "ip": None
    }
    
    agents.append(new_agent)
    # Sort by ID to keep it neat
    data["agents"] = sorted(agents, key=lambda x: x["id"])
    
    with open(AGENTS_YAML_PATH, 'w') as f:
        yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        
    # Run post-registration script if configured
    if POST_REG_SCRIPT and os.path.exists(POST_REG_SCRIPT):
        try:
            subprocess.run(["python", POST_REG_SCRIPT], check=True, cwd=FIRMWARE_DIR)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Failed to run post-registration script: {e}")
        
    return {"status": "success", "agent": new_agent}

@app.post("/api/fleet/change_id")
def change_id(req: ChangeIdRequest):
    if not AGENTS_YAML_PATH.exists():
        raise HTTPException(status_code=404, detail="agents.yaml not found")
        
    with open(AGENTS_YAML_PATH, 'r') as f:
        data = yaml.safe_load(f)
        
    agents = data.get("agents", [])
    target_agent = None
    conflict_agent = None
    
    for a in agents:
        if a["uid"] == req.uid:
            target_agent = a
        elif a["id"] == req.new_id:
            conflict_agent = a
            
    if not target_agent:
        raise HTTPException(status_code=404, detail=f"UID {req.uid} not found in registry")
        
    old_id = target_agent["id"]
    if old_id == req.new_id:
        return {"status": "success"}
        
    # Swap IDs
    target_agent["id"] = req.new_id
    target_agent["hostname"] = f"robot-{req.new_id:02d}"
    
    if conflict_agent:
        conflict_agent["id"] = old_id
        conflict_agent["hostname"] = f"robot-{old_id:02d}"
        
    data["agents"] = sorted(agents, key=lambda x: x["id"])
    
    with open(AGENTS_YAML_PATH, 'w') as f:
        yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        
    # Run post-registration script if configured
    if POST_REG_SCRIPT and os.path.exists(POST_REG_SCRIPT):
        try:
            subprocess.run(["python", POST_REG_SCRIPT], check=True, cwd=FIRMWARE_DIR)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Failed to run post-registration script: {e}")
        
    return {"status": "success"}

def _agents_with_ips(online_only: bool = True) -> list[dict]:
    if not AGENTS_YAML_PATH.exists():
        return []
    try:
        with open(AGENTS_YAML_PATH, "r") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[CMD] Failed to read agents.yaml: {e}")
        return []

    agents = [a for a in data.get("agents", []) if a.get("ip") and a.get("id") is not None]
    if not online_only:
        return agents

    online = []
    now = time.time()
    # Keep a longer window: STATUS is suppressed during log upload.
    for a in agents:
        aid = int(a["id"])
        state = agent_live_state.get(aid)
        if state and now - state["last_seen"] < 60.0:
            entry = dict(a)
            if state.get("ip"):
                entry["ip"] = state["ip"]
            if state.get("paused") is not None:
                entry["paused"] = state["paused"]
            for field in ("params", "src", "zmode", "flex_used", "mode", "kappa", "omega", "fw", "interaction_path"):
                if state.get(field) is not None:
                    entry[field] = state[field]
            online.append(entry)
    # Never fall back to all agents.yaml IPs (causes Host-is-down spam).
    return online


def _send_udp(sock: socket.socket, payload: bytes, host: str, port: int) -> bool:
    try:
        sock.sendto(payload, (host, port))
        return True
    except OSError:
        # Offline robots are expected; do not spam the console.
        return False


@app.post("/api/fleet/command")
async def send_global_command(req: GlobalCommandRequest):
    if req.command not in ["START", "STOP"]:
        raise HTTPException(status_code=400, detail="Invalid command")

    seq_hint = (
        sequence_controller.get_status().get("status", "n/a")
        if sequence_controller is not None
        else "no-controller"
    )
    from log_receiver import fm_event

    fm_event(f"[CMD] HTTP {req.command} (sequence={seq_hint})")

    sequence_action: dict[str, Any] | None = None
    if sequence_controller is not None:
        seq_status = sequence_controller.get_status().get("status")
        if req.command == "STOP":
            if seq_status == "awaiting_upload":
                sequence_action = await sequence_controller.trigger_upload()
            elif seq_status == "completed":
                fm_event("[CMD] STOP ALL: シーケンスは既に completed（回収済み）です")
                sequence_action = sequence_controller.get_status()
            elif seq_status == "stopped":
                fm_event(
                    f"[CMD] STOP ALL: シーケンスは stopped です。"
                    " ログ回収待ちなら「自動シーケンス開始」後の完了まで待ってから再度 STOP ALL"
                )
                sequence_action = sequence_controller.get_status()
            elif seq_status in sequence_controller.ACTIVE_STATES:
                await sequence_controller.stop()
                sequence_action = sequence_controller.get_status()
            elif seq_status == "waiting_for_logs":
                fm_event("[CMD] STOP ALL: 既にログ転送待ちです（UDP STOP も送信します）")
                sequence_action = sequence_controller.get_status()
        elif req.command == "START" and seq_status in sequence_controller.ACTIVE_STATES:
            raise HTTPException(status_code=409, detail="位相差シーケンス実行中です")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    agents = _agents_with_ips(online_only=True)
    sent_unicast = 0
    target_desc = [
        f"{int(a['id'])}@{a['ip']}(paused={a.get('paused', '?')})" for a in agents
    ]

    try:
        # Prefer unicast to recently-seen agents only. Always broadcast as well.
        for attempt in range(2):
            for agent in agents:
                ip = str(agent["ip"])
                aid = int(agent["id"])
                port = 5000 + aid
                if _send_udp(sock, req.command.encode("utf-8"), ip, port):
                    sent_unicast += 1
                alias = f"{aid}:ON" if req.command == "START" else f"{aid}:OFF"
                if _send_udp(sock, alias.encode("utf-8"), ip, port):
                    sent_unicast += 1
            for port in range(5000, 5020):
                _send_udp(sock, req.command.encode("utf-8"), "255.255.255.255", port)
            time.sleep(0.05)
        fm_event(
            f"[CMD] {req.command} -> online=[{', '.join(target_desc) or 'none'}] "
            f"unicast_ok={sent_unicast} + broadcast"
        )
        body: dict[str, Any] = {
            "status": "success",
            "command": req.command,
            "unicast_targets": len(agents),
            "unicast_packets_ok": sent_unicast,
        }
        if sequence_action is not None:
            body["sequence"] = sequence_action
        return body
    except Exception as e:
        print(f"[CMD] {req.command} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        sock.close()


@app.get("/api/experiment/sequence")
def get_experiment_sequence():
    if sequence_controller is None:
        return {"status": "unavailable"}
    return sequence_controller.get_status()


@app.post("/api/experiment/sequence/start")
async def start_experiment_sequence(req: PhaseSequenceStartRequest):
    if sequence_controller is None:
        raise HTTPException(status_code=503, detail="実験シーケンス機能は起動完了後に利用できます")
    try:
        return await sequence_controller.start(req.agent_ids)
    except SequenceValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/experiment/sequence/stop")
async def stop_experiment_sequence():
    from log_receiver import fm_event

    fm_event("[CMD] HTTP シーケンス停止（赤い STOP ALL ではありません）")
    if sequence_controller is None:
        raise HTTPException(status_code=503, detail="実験シーケンス機能は起動完了後に利用できます")
    try:
        status = await sequence_controller.stop()
        fm_event(f"[CMD] シーケンス停止完了 status={status.get('status')}")
        return status
    except SequenceValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        print(f"[SEQ] stop endpoint error: {exc}", flush=True)
        raise HTTPException(status_code=500, detail=f"停止処理エラー: {exc}") from exc

@app.post("/api/fleet/test_servo")
def test_servo(req: TestServoRequest):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = 5000 + req.agent_id
        
        # Send ON
        msg_on = f"{req.agent_id}:ON".encode('utf-8')
        sock.sendto(msg_on, (req.ip, port))
        
        time.sleep(2.0)
        
        # Send OFF
        msg_off = f"{req.agent_id}:OFF".encode('utf-8')
        sock.sendto(msg_off, (req.ip, port))
        
        sock.close()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"UDP communication failed: {e}")

@app.post("/api/fleet/test_servo_usb")
def test_servo_usb(req: TestServoUsbRequest):
    ports = serial.tools.list_ports.comports()
    target_port = None
    for p in ports:
        if p.serial_number and p.serial_number.upper() == req.uid.upper():
            target_port = p.device
            break
            
    if not target_port:
        raise HTTPException(status_code=404, detail="USB device not found or disconnected")
        
    try:
        ser = serial.Serial(target_port, 115200, timeout=1)
        # Flush input/output and send command
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(b"TEST_SERVO\n")
        ser.flush()
        ser.close()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Serial communication failed: {e}")

@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    await websocket.accept()
    telemetry_clients.add(websocket)
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        telemetry_clients.remove(websocket)
    except Exception:
        if websocket in telemetry_clients:
            telemetry_clients.remove(websocket)

@app.websocket("/ws/upload")
async def websocket_upload(websocket: WebSocket):
    print("[WS] Upload connection requested")
    await websocket.accept()
    print("[WS] Connection accepted")

    last_returncode = 1
    try:
        data = await websocket.receive_json()
        log_data = {**data}
        if log_data.get("wifi_password"):
            log_data["wifi_password"] = "***"
        print(f"[WS] Received data: {log_data}")
        target = data.get("target")  # "ota", "usb", "ota_batch", or "build"
        port_or_ip = data.get("value")
        firmware_name = data.get("firmware", "pico_robot")
        wifi_ssid = data.get("wifi_ssid")
        wifi_password = data.get("wifi_password")

        cwd_dir = FIRMWARE_ROOT_DIR / firmware_name
        if not cwd_dir.exists() or not cwd_dir.is_dir():
            cwd_dir = FIRMWARE_DIR

        if not port_or_ip:
            print("[WS] Error: No port or IP")
            await websocket.send_text("ERROR: No port or IP specified\n")
            await websocket.close()
            return

        try:
            header_path, ssid_used = inject_for_firmware(
                FIRMWARE_ROOT_DIR, firmware_name, wifi_ssid, wifi_password
            )
            await websocket.send_text(
                f"[WiFi] Wrote {header_path.name} for '{firmware_name}' (SSID: {ssid_used})\n"
            )
        except ValueError as e:
            await websocket.send_text(f"ERROR: {e}\n")
            await websocket.close()
            return

        pio_path = shutil.which("pio")
        if not pio_path:
            pio_path = "/opt/homebrew/bin/pio"

        if target == "ota_batch":
            ips = port_or_ip.split(",")
            await websocket.send_text(
                f"[WS] Starting batch OTA for {len(ips)} devices using firmware '{firmware_name}'...\n"
            )
            batch_ok = True

            for i, ip in enumerate(ips):
                await websocket.send_text(f"\n{'='*40}\n")
                await websocket.send_text(f"[WS] Batch {i+1}/{len(ips)}: Uploading to {ip} ...\n")
                await websocket.send_text(f"{'='*40}\n\n")

                cmd = [pio_path, "run", "-e", "ota", "-t", "upload", "--upload-port", ip.strip()]
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd_dir,
                )

                try:
                    while True:
                        line = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
                        if not line:
                            break
                        await websocket.send_text(line.decode("utf-8", errors="ignore"))
                    await asyncio.wait_for(process.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    batch_ok = False
                    await websocket.send_text(
                        f"\n❌ [TIMEOUT] 通信タイムアウト ({ip})。プロセスを終了し次へ進みます...\n"
                    )
                    try:
                        process.kill()
                        await process.wait()
                    except Exception:
                        pass

                if process.returncode != 0 and process.returncode is not None:
                    batch_ok = False
                    await websocket.send_text(f"\n❌ [ERROR] Failed on {ip}. Continuing to next...\n")
                elif process.returncode == 0:
                    await websocket.send_text(f"\n✅ [SUCCESS] Finished {ip}.\n")

            last_returncode = 0 if batch_ok else 1
            await websocket.send_text("\n[WS] Batch upload completely finished!\n")
            print("[WS] Batch Subprocess finished")

        else:
            if target == "build":
                cmd = [pio_path, "run"]
            elif target == "ota":
                cmd = [pio_path, "run", "-e", "ota", "-t", "upload", "--upload-port", port_or_ip]
            else:
                cmd = [pio_path, "run", "-t", "upload"]
                if port_or_ip != "auto":
                    cmd.extend(["--upload-port", port_or_ip])

            print(f"[WS] Command ready: {cmd}")
            await websocket.send_text(
                f"Starting upload process using firmware '{firmware_name}'...\nCommand: {' '.join(cmd)}\n"
            )
            await websocket.send_text("-" * 40 + "\n")

            print(f"[WS] Launching subprocess in {cwd_dir}")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd_dir,
            )
            print(f"[WS] Subprocess launched, PID: {process.pid}")

            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                await websocket.send_text(line.decode("utf-8", errors="ignore"))

            await process.wait()
            last_returncode = process.returncode if process.returncode is not None else 1
            print(f"[WS] Subprocess finished with code {last_returncode}")

        if last_returncode == 0:
            await websocket.send_text("\n✅ SUCCESS: Upload completed successfully!\n")
        else:
            await websocket.send_text(f"\n❌ ERROR: Upload failed with code {last_returncode}\n")

    except WebSocketDisconnect:
        print("[WS] Client disconnected normally")
    except Exception as e:
        print(f"[WS] Exception occurred: {e}")
        try:
            await websocket.send_text(f"\n❌ Internal Server Error: {e}\n")
        except Exception:
            pass

    finally:
        print("[WS] Closing connection")
        try:
            await websocket.close()
        except Exception:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

from pydantic import BaseModel
class RecordRequest(BaseModel):
    robot_id: int
    memo: str

@app.post("/api/chunks/merge")
def merge_experiment_chunks():
    """Flush buffered UDP logs and merge into one CSV (Volvocine-compatible columns)."""
    log_receiver.flush_all()
    merged = log_receiver.merge_pending()
    if not merged:
        raise HTTPException(status_code=404, detail="No chunk data to merge")
    return {"merged_path": merged, "session": log_receiver.get_session_info()}

@app.get("/api/chunks/session")
def get_chunk_session():
    return log_receiver.get_session_info()

@app.get("/api/chunks/merged")
def list_merged_chunks():
    files: list[str] = []
    if MERGED_CHUNKS_DIR.exists():
        files.extend(
            str(p)
            for p in sorted(
                MERGED_CHUNKS_DIR.glob("merged_*.csv"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        )
    if EXPERIMENTS_DIR.exists():
        for exp_dir in sorted(EXPERIMENTS_DIR.iterdir(), reverse=True):
            if not exp_dir.is_dir():
                continue
            for merged in exp_dir.glob("merged_*.csv"):
                files.append(str(merged))
    return {"files": files}

@app.get("/api/records")
def get_records():
    if not RECORDS_CSV_PATH.exists():
        return {"records": []}
    records = []
    try:
        with open(RECORDS_CSV_PATH, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)
    except Exception as e:
        print(f"Error reading records: {e}")
    return {"records": records}

@app.post("/api/records")
def add_record(req: RecordRequest):
    file_exists = RECORDS_CSV_PATH.exists()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        with open(RECORDS_CSV_PATH, 'a', encoding='utf-8', newline='') as f:
            fieldnames = ['timestamp', 'robot_id', 'memo']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            if not file_exists:
                writer.writeheader()
                
            writer.writerow({
                'timestamp': timestamp,
                'robot_id': req.robot_id,
                'memo': req.memo
            })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save record: {e}")
        
    return {"status": "success", "timestamp": timestamp}

class EditRecordRequest(BaseModel):
    timestamp: str
    robot_id: int
    new_memo: str

@app.put("/api/records")
def edit_record(req: EditRecordRequest):
    if not RECORDS_CSV_PATH.exists():
        raise HTTPException(status_code=404, detail="Records file not found")
        
    records = []
    updated = False
    with open(RECORDS_CSV_PATH, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['timestamp'] == req.timestamp and str(row['robot_id']) == str(req.robot_id):
                row['memo'] = req.new_memo
                updated = True
            records.append(row)
            
    if not updated:
        raise HTTPException(status_code=404, detail="Record not found")
        
    with open(RECORDS_CSV_PATH, 'w', encoding='utf-8', newline='') as f:
        fieldnames = ['timestamp', 'robot_id', 'memo']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
        
    return {"status": "success"}

class DeleteRecordRequest(BaseModel):
    timestamp: str
    robot_id: int

@app.delete("/api/records")
def delete_record(req: DeleteRecordRequest):
    if not RECORDS_CSV_PATH.exists():
        raise HTTPException(status_code=404, detail="Records file not found")
        
    records = []
    deleted = False
    with open(RECORDS_CSV_PATH, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['timestamp'] == req.timestamp and str(row['robot_id']) == str(req.robot_id):
                deleted = True
                continue
            records.append(row)
            
    if not deleted:
        raise HTTPException(status_code=404, detail="Record not found")
        
    with open(RECORDS_CSV_PATH, 'w', encoding='utf-8', newline='') as f:
        fieldnames = ['timestamp', 'robot_id', 'memo']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
        
    return {"status": "success"}
