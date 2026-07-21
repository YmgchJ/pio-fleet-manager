import os
import re
import yaml
import json
import asyncio
import subprocess
import csv
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import serial.tools.list_ports
import socket
import time
import serial
from pydantic import BaseModel
import contextlib

import shutil

from log_receiver import LogReceiver, MERGED_CHUNKS_DIR, SAVED_CHUNKS_DIR, EXPERIMENTS_DIR
from wifi_config_store import inject_for_firmware, import_from_pico_robot, load_saved, save as save_wifi_config

# --- UDP Auto-Discovery + high-rate log receiver ---
agent_live_state = {}
log_receiver = LogReceiver()

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
            if msg.startswith("LOG_END,"):
                id_m = re.search(r"id:(\d+)", msg)
                rec_m = re.search(r"records:(\d+)", msg)
                ok_m = re.search(r"ok:([01])", msg)
                if id_m:
                    agent_id = int(id_m.group(1))
                    records = int(rec_m.group(1)) if rec_m else 0
                    ok = ok_m.group(1) == "1" if ok_m else False
                    log_receiver.handle_log_end(agent_id, records, ok)
                return
            if msg.startswith("STATUS,id:"):
                # STATUS,id:1,paused:0,angle:110.0,flex:1024,light:512,current:256,volt:3.70
                match = re.search(r"id:(\d+)", msg)
                if match:
                    agent_id = int(match.group(1))
                    ip_address = addr[0]
                    update_agent_ip(agent_id, ip_address)
                    
                    # Parse all fields
                    telemetry = {"id": agent_id, "timestamp": time.time()}
                    
                    for field in ["paused", "angle", "flex", "light", "current", "volt"]:
                        f_match = re.search(f"{field}:([0-9.-]+)", msg)
                        if f_match:
                            telemetry[field] = float(f_match.group(1))
                    
                    # Update live state for volt (dashboard use)
                    if "volt" in telemetry:
                        agent_live_state[agent_id] = {
                            "volt": telemetry["volt"],
                            "last_seen": telemetry["timestamp"]
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
    
    discovery_task = asyncio.create_task(discovery_loop(transport))
    chunk_task = asyncio.create_task(chunk_timeout_loop())
    yield
    chunk_task.cancel()
    discovery_task.cancel()
    transport.close()
    sock.close()

app = FastAPI(title="Modular Robots Fleet Manager", lifespan=lifespan)

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
# AGENTS_YAML_PATH is where the fleet registry is saved
AGENTS_YAML_PATH = Path(os.environ.get("PIO_AGENTS_PATH", FLEET_MANAGER_ROOT / "agents.yaml"))
# RECORDS_CSV_PATH is where the robot memos/records are saved
RECORDS_CSV_PATH = FLEET_MANAGER_ROOT / "data" / "records.csv"
# Optional script to run after registration (e.g. to generate C++ headers)
POST_REG_SCRIPT = os.environ.get("PIO_POST_REG_SCRIPT", None)

# Serve static files
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

@app.get("/")
def redirect_to_static():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/index.html")

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

# --- Endpoints ---

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
        else:
            a["volt"] = None
            a["status"] = "offline"
            
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

@app.post("/api/fleet/command")
def send_global_command(req: GlobalCommandRequest):
    if req.command not in ["START", "STOP"]:
        raise HTTPException(status_code=400, detail="Invalid command")
        
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    
    try:
        # Send command 3 times to ensure delivery over unreliable UDP
        for attempt in range(3):
            for port in range(5000, 5020):
                sock.sendto(req.command.encode('utf-8'), ('255.255.255.255', port))
                time.sleep(0.01)  # 10ms between individual robots
            time.sleep(0.1)       # 100ms between retries
        return {"status": "success", "command": req.command}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        sock.close()

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
