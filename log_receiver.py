"""UDP compressed log receiver — Volvocine_Pico ServerTest / ChunkProcessor compatible."""

from __future__ import annotations

import logging
import re
import shutil
import struct
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from chunk_processor import build_dataframe_for_chunk, merge_and_save_chunks
from session_metadata import SessionMetadataRecorder

RECORD_STRUCT_V7 = struct.Struct("<7B")  # micros24 (3B) + a0 + a1 + a2 + a3
RECORD_STRUCT_V6 = struct.Struct("<6B")  # legacy Volcovine (no bus voltage)
RECORD_SIZE_V7 = RECORD_STRUCT_V7.size
RECORD_SIZE_V6 = RECORD_STRUCT_V6.size
# Only flush/merge on LOG_END, or after a long silence (full upload in progress).
CHUNK_TIMEOUT_SEC = 30.0
SESSION_MERGE_FALLBACK_SEC = 30.0
# Ignore orphan retransmit packets after a merge (robots stuck retrying ACK).
ORPHAN_RETRANSMIT_IGNORE_SEC = 20.0
MIN_RECORDS_TO_KEEP_SESSION = 200

DATA_ROOT = Path(__file__).resolve().parent / "data"
SAVED_CHUNKS_DIR = DATA_ROOT / "saved_chunks"
MERGED_CHUNKS_DIR = DATA_ROOT / "merged_chunks"
EXPERIMENTS_DIR = DATA_ROOT / "experiments"

CHUNK_AGENT_RE = re.compile(r"chunk_agent_(\d+)_")
FM_EVENTS_LOG = DATA_ROOT / "fm_events.log"
_fm_log = logging.getLogger("fm_event")
_fm_event_listeners: list[Callable[[str], None]] = []


def register_fm_event_listener(callback: Callable[[str], None]) -> None:
    """Optional hook (e.g. WebSocket mirror for the dashboard Terminal)."""
    if callback not in _fm_event_listeners:
        _fm_event_listeners.append(callback)


def fm_event(message: str) -> None:
    """Mirror important UDP events to stdout and an append-only audit file."""
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    _fm_log.info("%s", message)
    print(line, flush=True)
    try:
        FM_EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FM_EVENTS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    for listener in _fm_event_listeners:
        try:
            listener(line)
        except Exception:
            pass


class LogReceiver:
    """Buffers high-rate robot log packets and writes merged CSV for MATLAB."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffers: dict[int, tuple[list, list, list]] = {}
        self._last_recv: dict[int, float] = {}
        self._last_log_packet: float | None = None
        self._pending_files: list[str] = []
        self._session_id: str | None = None
        self._session_dir: Path | None = None
        self._log_end: dict[int, tuple[int, bool, float]] = {}
        self._last_raw_packet: dict[int, bytes] = {}
        self._metadata = SessionMetadataRecorder()
        self._sequence_context: dict[str, Any] | None = None
        self._on_session_merged: Callable[[str | None, str, dict[str, Any] | None], None] | None = None
        SAVED_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
        MERGED_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
        EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)

    def _chunk_save_dir(self) -> Path:
        if self._session_dir is not None:
            return self._session_dir / "chunks"
        return SAVED_CHUNKS_DIR

    def _ensure_session(self) -> None:
        if self._session_id is not None:
            return
        base_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_id = base_id
        suffix = 1
        while (EXPERIMENTS_DIR / self._session_id).exists():
            self._session_id = f"{base_id}_{suffix:02d}"
            suffix += 1
        self._session_dir = EXPERIMENTS_DIR / self._session_id
        (self._session_dir / "chunks").mkdir(parents=True, exist_ok=True)
        (self._session_dir / "archive").mkdir(parents=True, exist_ok=True)
        self._metadata.on_session_start(self._session_id, self._session_dir, self._sequence_context)
        fm_event(f"[Session] Started experiment session {self._session_id}")

    def _reset_session(self) -> None:
        self._session_id = None
        self._session_dir = None
        self._log_end.clear()
        self._last_log_packet = None
        self._sequence_context = None
        self._metadata.reset()

    def record_params_served(self, agent_id: int, payload: str, parsed: dict) -> None:
        self._metadata.record_params_served(agent_id, payload, parsed)

    def record_status(self, agent_id: int, telemetry: dict) -> None:
        self._metadata.record_status(agent_id, telemetry)

    def record_experiment_event(self, agent_id: int, event: dict[str, Any]) -> None:
        self._metadata.record_experiment_event(agent_id, event)

    def set_sequence_context(self, context: dict[str, Any] | None) -> None:
        with self._lock:
            if not context:
                self._sequence_context = None
                return
            # Buffers may already exist from an on-water auto-upload; still attach
            # sequence_id / agent_ids so merge + FM completion can run.
            self._sequence_context = dict(context)

    def snapshot_log_end(self) -> dict[int, tuple[int, bool]]:
        with self._lock:
            return {
                aid: (rec, ok)
                for aid, (rec, ok, _ts) in self._log_end.items()
            }

    def find_merged_csv_for_agents(
        self, agent_ids: set[int], since_epoch: float
    ) -> Optional[str]:
        """Latest merged CSV in experiments/ whose archive contains all agents."""
        if not EXPERIMENTS_DIR.is_dir():
            return None
        best: Optional[tuple[float, str]] = None
        for exp_dir in EXPERIMENTS_DIR.iterdir():
            if not exp_dir.is_dir():
                continue
            merged = exp_dir / f"merged_{exp_dir.name}.csv"
            if not merged.is_file():
                continue
            mtime = merged.stat().st_mtime
            if mtime < since_epoch:
                continue
            archive = exp_dir / "archive"
            if not archive.is_dir():
                continue
            found: set[int] = set()
            for chunk in archive.glob("chunk_agent_*.csv"):
                match = CHUNK_AGENT_RE.search(chunk.name)
                if match:
                    found.add(int(match.group(1)))
            if not agent_ids.issubset(found):
                continue
            if best is None or mtime > best[0]:
                best = (mtime, str(merged))
        return best[1] if best else None

    def set_session_merged_callback(
        self,
        callback: Callable[[str | None, str, dict[str, Any] | None], None] | None,
    ) -> None:
        self._on_session_merged = callback

    @staticmethod
    def _agents_from_chunk_paths(paths: list[str]) -> set[int]:
        agents: set[int] = set()
        for path in paths:
            m = CHUNK_AGENT_RE.search(Path(path).name)
            if m:
                agents.add(int(m.group(1)))
        return agents

    def _expected_agents(self) -> set[int] | None:
        ctx = self._sequence_context
        if not ctx:
            return None
        raw = ctx.get("agent_ids")
        if not raw:
            return None
        try:
            expected = {int(value) for value in raw}
        except (TypeError, ValueError):
            return None
        return expected if expected else None

    def _session_ready_to_merge(self) -> bool:
        """Return True if buffered chunks may be merged into one CSV."""
        agents_with_chunks = self._agents_from_chunk_paths(self._pending_files)
        agents_with_log_end = set(self._log_end.keys())
        fallback_ready = (
            self._last_log_packet is not None
            and (time.time() - self._last_log_packet) >= SESSION_MERGE_FALLBACK_SEC
        )
        expected = self._expected_agents()
        if expected:
            missing_log_end = expected - agents_with_log_end
            if missing_log_end:
                return False
            missing_chunks = expected - agents_with_chunks
            if missing_chunks and not fallback_ready:
                return False
            return bool(agents_with_chunks)
        return bool(agents_with_chunks) and (
            agents_with_chunks <= agents_with_log_end or fallback_ready
        )

    @staticmethod
    def _record_size_for_payload(payload_len: int) -> int | None:
        if payload_len > 0 and payload_len % RECORD_SIZE_V7 == 0:
            return RECORD_SIZE_V7
        if payload_len > 0 and payload_len % RECORD_SIZE_V6 == 0:
            return RECORD_SIZE_V6
        return None

    @staticmethod
    def _unpack_record(chunk: bytes, record_size: int) -> tuple:
        if record_size == RECORD_SIZE_V7:
            b0, b1, b2, a0, a1, a2, a3 = RECORD_STRUCT_V7.unpack(chunk)
            return (b0 | (b1 << 8) | (b2 << 16)) & 0xFFFFFF, a0, a1, a2, a3
        b0, b1, b2, a0, a1, a2 = RECORD_STRUCT_V6.unpack(chunk)
        return (b0 | (b1 << 8) | (b2 << 16)) & 0xFFFFFF, a0, a1, a2, 0

    def handle_datagram(self, data: bytes, addr: tuple, transport) -> Optional[bytes]:
        """Parse binary log packet; return ACK bytes if applicable."""
        if len(data) < 5:
            return None
        if data[0] == 0 or data[0] > 99:
            return None

        agent_id = int(data[0])
        send_micros = struct.unpack("<I", data[1:5])[0]
        raw = data[5:]
        record_size = self._record_size_for_payload(len(raw))
        if record_size is None:
            return None

        recv_time = time.time()
        records = []
        for i in range(len(raw) // record_size):
            chunk = raw[i * record_size : (i + 1) * record_size]
            records.append(self._unpack_record(chunk, record_size))

        with self._lock:
            self._ensure_session()
            chunk_data, send_list, recv_list = self._buffers.get(agent_id, ([], [], []))
            was_empty = len(chunk_data) == 0
            if self._last_raw_packet.get(agent_id) == raw:
                total = len(chunk_data)
            else:
                chunk_data.extend(records)
                send_list.append(send_micros)
                recv_list.append(recv_time)
                self._buffers[agent_id] = (chunk_data, send_list, recv_list)
                self._last_recv[agent_id] = recv_time
                self._last_log_packet = recv_time
                self._last_raw_packet[agent_id] = raw
                total = len(chunk_data)

        if was_empty and records:
            fm_event(f"[LOG] 受信開始 agent={agent_id} from {addr[0]} (+{len(records)} rec)")
        elif total <= 72 or total % 360 == 0:
            fm_event(f"[LOG] agent={agent_id} +{len(records)} from {addr[0]} (buf={total})")

        last = raw[-record_size:]
        if record_size == RECORD_SIZE_V7:
            b0, b1, b2, _, _, _, _ = RECORD_STRUCT_V7.unpack(last)
        else:
            b0, b1, b2, _, _, _ = RECORD_STRUCT_V6.unpack(last)
        last_micros24 = b0 | (b1 << 8) | (b2 << 16)
        ack = bytes([agent_id]) + last_micros24.to_bytes(3, "little")
        return ack

    def handle_log_end(
        self,
        agent_id: int,
        records: int,
        ok: bool,
        sequence_id: str | None = None,
        trial: int | None = None,
    ) -> None:
        with self._lock:
            # Firmware retries LOG_END up to 5×; after merge we reset the session.
            # Ignore late duplicates that would spawn an empty experiments/ folder.
            if self._session_id is None and agent_id not in self._buffers:
                fm_event(f"[LOG_END] Ignoring duplicate for agent={agent_id} (no active session)")
                return
            self._ensure_session()
            self._log_end[agent_id] = (records, ok, time.time())
            self._metadata.record_log_end(agent_id, records, ok, sequence_id, trial)
        status = "OK" if ok else "FAILED"
        seq_label = sequence_id or "-"
        fm_event(
            f"======== ログ受信完了 agent={agent_id} records={records} {status} "
            f"seq={seq_label} ========"
        )
        self.flush_agent(agent_id)
        with self._lock:
            expected = self._expected_agents()
            if expected:
                missing = expected - set(self._log_end.keys())
                if missing:
                    fm_event(
                        f"[Session] マージ待ち LOG_END: agents {sorted(missing)}"
                    )
                elif expected <= set(self._log_end.keys()):
                    fm_event(
                        f"[Session] 両機 LOG_END 揃いました → CSV マージします "
                        f"(agents {sorted(expected)})"
                    )
        self._maybe_finalize_session()

    def flush_agent(self, agent_id: int) -> Optional[str]:
        with self._lock:
            buf = self._buffers.pop(agent_id, None)
            self._last_recv.pop(agent_id, None)
            self._last_raw_packet.pop(agent_id, None)
        if not buf:
            return None
        chunk_data, send_list, recv_list = buf
        if not chunk_data:
            return None
        _, path = build_dataframe_for_chunk(
            agent_id, chunk_data, send_list, recv_list, save_dir=self._chunk_save_dir()
        )
        if path:
            with self._lock:
                self._pending_files.append(path)
        return path

    def flush_all(self) -> list[str]:
        with self._lock:
            agent_ids = list(self._buffers.keys())
        saved = []
        for aid in agent_ids:
            p = self.flush_agent(aid)
            if p:
                saved.append(p)
        return saved

    def merge_pending(self, output_dir: Path | None = None) -> Optional[str]:
        with self._lock:
            if not self._pending_files:
                return None
            files = self._pending_files[:]
            self._pending_files.clear()
            session_dir = self._session_dir
            session_id = self._session_id
            sequence_context = self._sequence_context

        if output_dir is None:
            if session_dir is not None:
                output_dir = session_dir
            else:
                output_dir = MERGED_CHUNKS_DIR

        archive_dir = None
        if session_dir is not None:
            archive_dir = session_dir / "archive"

        merged_name = f"merged_{session_id}.csv" if session_id else None
        merged = merge_and_save_chunks(
            files,
            output_dir=output_dir,
            archive_dir=archive_dir,
            merged_filename=merged_name,
            delete_chunks=archive_dir is None,
        )

        if merged and session_dir is not None:
            legacy = MERGED_CHUNKS_DIR / Path(merged).name
            try:
                shutil.copy2(merged, legacy)
            except OSError as e:
                print(f"[WARN] Could not copy merged CSV to legacy dir: {e}")
            try:
                self._metadata.finalize(
                    session_dir=session_dir,
                    session_id=session_id or Path(merged).stem.replace("merged_", ""),
                    merged_csv=Path(merged),
                    chunk_files=files,
                )
            except OSError as e:
                print(f"[WARN] Could not write session metadata: {e}")

        with self._lock:
            self._reset_session()
        if merged and self._on_session_merged:
            try:
                self._on_session_merged(session_id, str(merged), sequence_context)
            except Exception as exc:
                print(f"[Session] merge callback error: {exc}")
        return merged

    def _maybe_finalize_session(self) -> None:
        with self._lock:
            if self._buffers:
                return
            if not self._pending_files:
                return
            should_merge = self._session_ready_to_merge()

        if should_merge:
            merged = self.merge_pending()
            if merged:
                fm_event("======== CSV マージ完了 ========")
                fm_event(f"  path: {merged}")

    def check_timeouts(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                aid
                for aid, ts in self._last_recv.items()
                if (now - ts) > CHUNK_TIMEOUT_SEC
            ]
        for aid in stale:
            self.flush_agent(aid)
        self._maybe_finalize_session()

    def get_session_info(self) -> dict:
        with self._lock:
            return {
                "session_id": self._session_id,
                "session_dir": str(self._session_dir) if self._session_dir else None,
                "pending_chunks": len(self._pending_files),
                "log_end_agents": list(self._log_end.keys()),
                "active_buffers": list(self._buffers.keys()),
                "sequence": self._sequence_context,
            }
