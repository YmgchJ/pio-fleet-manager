"""UDP compressed log receiver — Volvocine_Pico ServerTest / ChunkProcessor compatible."""

from __future__ import annotations

import re
import shutil
import struct
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from chunk_processor import build_dataframe_for_chunk, merge_and_save_chunks

RECORD_STRUCT_V7 = struct.Struct("<7B")  # micros24 (3B) + a0 + a1 + a2 + a3
RECORD_STRUCT_V6 = struct.Struct("<6B")  # legacy Volcovine (no bus voltage)
RECORD_SIZE_V7 = RECORD_STRUCT_V7.size
RECORD_SIZE_V6 = RECORD_STRUCT_V6.size
CHUNK_TIMEOUT_SEC = 5.0
SESSION_MERGE_FALLBACK_SEC = 8.0

DATA_ROOT = Path(__file__).resolve().parent / "data"
SAVED_CHUNKS_DIR = DATA_ROOT / "saved_chunks"
MERGED_CHUNKS_DIR = DATA_ROOT / "merged_chunks"
EXPERIMENTS_DIR = DATA_ROOT / "experiments"

CHUNK_AGENT_RE = re.compile(r"chunk_agent_(\d+)_")


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
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_dir = EXPERIMENTS_DIR / self._session_id
        (self._session_dir / "chunks").mkdir(parents=True, exist_ok=True)
        (self._session_dir / "archive").mkdir(parents=True, exist_ok=True)
        print(f"[Session] Started experiment session {self._session_id}")

    def _reset_session(self) -> None:
        self._session_id = None
        self._session_dir = None
        self._log_end.clear()

    @staticmethod
    def _agents_from_chunk_paths(paths: list[str]) -> set[int]:
        agents: set[int] = set()
        for path in paths:
            m = CHUNK_AGENT_RE.search(Path(path).name)
            if m:
                agents.add(int(m.group(1)))
        return agents

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
            chunk_data.extend(records)
            send_list.append(send_micros)
            recv_list.append(recv_time)
            self._buffers[agent_id] = (chunk_data, send_list, recv_list)
            self._last_recv[agent_id] = recv_time
            self._last_log_packet = recv_time

        last = raw[-record_size:]
        if record_size == RECORD_SIZE_V7:
            b0, b1, b2, _, _, _, _ = RECORD_STRUCT_V7.unpack(last)
        else:
            b0, b1, b2, _, _, _ = RECORD_STRUCT_V6.unpack(last)
        last_micros24 = b0 | (b1 << 8) | (b2 << 16)
        ack = bytes([agent_id]) + last_micros24.to_bytes(3, "little")
        return ack

    def handle_log_end(self, agent_id: int, records: int, ok: bool) -> None:
        with self._lock:
            # Firmware retries LOG_END up to 5×; after merge we reset the session.
            # Ignore late duplicates that would spawn an empty experiments/ folder.
            if self._session_id is None and agent_id not in self._buffers:
                print(f"[LOG_END] Ignoring duplicate LOG_END for agent={agent_id} (no active session)")
                return
            self._ensure_session()
            self._log_end[agent_id] = (records, ok, time.time())
        status = "OK" if ok else "FAILED"
        print(f"[LOG_END] agent={agent_id} records={records} {status}")
        self.flush_agent(agent_id)
        self._maybe_finalize_session()

    def flush_agent(self, agent_id: int) -> Optional[str]:
        with self._lock:
            buf = self._buffers.pop(agent_id, None)
            self._last_recv.pop(agent_id, None)
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

        with self._lock:
            self._reset_session()
        return merged

    def _maybe_finalize_session(self) -> None:
        with self._lock:
            if self._buffers:
                return
            if not self._pending_files:
                return
            agents_with_chunks = self._agents_from_chunk_paths(self._pending_files)
            agents_with_log_end = set(self._log_end.keys())
            fallback_ready = (
                self._last_log_packet is not None
                and (time.time() - self._last_log_packet) >= SESSION_MERGE_FALLBACK_SEC
            )
            should_merge = bool(agents_with_chunks) and (
                agents_with_chunks <= agents_with_log_end or fallback_ready
            )

        if should_merge:
            merged = self.merge_pending()
            if merged:
                print(f"[Session] Auto-merged experiment CSV: {merged}")

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
            }
