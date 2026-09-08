"""Persist experiment session metadata next to merged CSVs."""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sync_params import DEFAULT_PARAMS_PATH, load_sync_params

SRC_LABELS = {0: "flex", 1: "current"}
SEQUENCE_CONFIG_PATH = DEFAULT_PARAMS_PATH.parent / "current_phase_sequence.yaml"


def _build_experiment_conditions(
    sequence_context: dict[str, Any] | None,
    yaml_data: dict[str, Any] | None,
    agents: dict[str, Any],
) -> dict[str, Any]:
    """Human-readable experiment contract stored beside every merged CSV."""
    ctx = sequence_context or {}
    params = yaml_data or {}
    agent_ids = [int(a) for a in ctx.get("agent_ids", [])]
    raw_omega = params.get("omega_by_agent") or {}
    omega_by_agent: dict[str, float] = {}
    for aid in agent_ids:
        value = raw_omega.get(aid, raw_omega.get(str(aid)))
        if value is not None:
            omega_by_agent[str(aid)] = float(value)

    return {
        "experiment_name": ctx.get("name") or params.get("name"),
        "agents": agent_ids,
        "paddle": ctx.get("paddle"),
        "upload_strategy": ctx.get("upload_strategy", "batch_on_stop"),
        "feedback": {
            "source": ctx.get("feedback_source") or params.get("feedback_source", "current"),
            "current_only": bool(ctx.get("current_only", params.get("current_only", False))),
            "kappa": float(ctx.get("kappa", params.get("kappa", 0))),
            "kappa_init": float(params.get("kappa_init", 0.0)),
            "kappa_active_after_ms": int(ctx.get("feedback_start_ms", params.get("feedback_start_ms", 10000))),
            "feedback_tau_s": float(params.get("feedback_tau", 1.0)),
            "interaction_law": "u = kappa * cos(psi) * normalize(current_smoothed)",
            "flex_sensor": {
                "physically_present": bool(ctx.get("flex_sensor_present", True)),
                "sampled_in_firmware": True,
                "used_in_feedback": False,
                "firmware_compile_flag": "SYNC_CURRENT_ONLY=1, SYNC_LOG_FLEX_SENSOR=1",
                "status_flex_used_expected": 0,
                "log_column_a2": "GP28 flex ADC (12-bit >> 4 in byte); not used for control",
            },
        },
        "timing": {
            "first_start_delay_ms": int(ctx.get("first_start_delay_ms", 0)),
            "inter_trial_pause_ms": int(ctx.get("inter_trial_pause_ms", 0)),
            "trial_duration_ms": int(ctx.get("duration_ms", 60000)),
            "total_trials": int(ctx.get("total_trials", 6)),
            "feedback_start_ms": int(ctx.get("feedback_start_ms", 10000)),
        },
        "phase_sweep": {
            "definition": ctx.get("phase_difference_definition"),
            "reference_agent": ctx.get("phase_reference_agent"),
            "partner_agent": ctx.get("phase_partner_agent"),
            "differences_rad": ctx.get("phase_differences_rad", []),
            "labels": ctx.get("phase_labels", []),
        },
        "servo": {
            "center_deg": float(params.get("center", 110.0)),
            "amplitude_deg": float(params.get("amplitude", 75.0)),
        },
        "omega_rad_s_by_agent": omega_by_agent,
        "log": {
            "sample_rate_hz": 50,
            "note": "Sequence mode uses saveInterval=10 at 500 Hz control",
            "columns": {
                "a0": "phase psi mod 2pi (0..255 -> 0..2pi rad)",
                "a1": "INA226 shunt current [mA], signed byte 128=0, +/-1000 mA",
                "a2": "flex GP28 ADC (12-bit >> 4); logged only, not in feedback",
                "a3": "INA226 bus voltage [V], 0..255 -> 0..5.1 V",
            },
        },
        "trial_results": ctx.get("trial_results", []),
        "config_files": {
            "sequence_yaml": str(SEQUENCE_CONFIG_PATH),
            "sync_params_yaml": str(DEFAULT_PARAMS_PATH),
            "sequence_plan_file": ctx.get("sequence_plan_file"),
            "sequence_status_file": ctx.get("sequence_status_file"),
        },
        "preflight_status_at_upload": _agent_status_snapshot(agents, agent_ids),
    }


def _agent_status_snapshot(agents: dict[str, Any], agent_ids: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for aid in agent_ids:
        bucket = agents.get(str(aid), {})
        status = bucket.get("status_recording") or bucket.get("status_last") or {}
        out[str(aid)] = {
            "mode": status.get("mode"),
            "src": status.get("src"),
            "feedback_source": status.get("feedback_source"),
            "flex_used": status.get("flex_used"),
            "kappa": status.get("kappa"),
            "omega": status.get("omega"),
            "fw": status.get("fw"),
        }
    return out


def _utc_iso(ts: float | None = None) -> str:
    if ts is None:
        ts = datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class SessionMetadataRecorder:
    """Collect parameters, telemetry, sequence events and log endings per session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._session_dir: Path | None = None
        self._started_at: str | None = None
        self._agents: dict[int, dict[str, Any]] = {}
        self._sequence_context: dict[str, Any] | None = None
        self._pending_events: list[dict[str, Any]] = []

    def _agent_bucket(self, agent_id: int) -> dict[str, Any]:
        if agent_id not in self._agents:
            self._agents[agent_id] = {
                "params_served": [],
                "status_last": None,
                "status_recording": None,
                "log_end": None,
                "experiment_events": [],
            }
        return self._agents[agent_id]

    @staticmethod
    def _event_matches_context(event: dict[str, Any], context: dict[str, Any] | None) -> bool:
        if not context:
            return False
        if str(event.get("seq", "")) != str(context.get("sequence_id", "")):
            return False
        if str(context.get("upload_strategy", "")).lower() == "batch_on_stop":
            return True
        if "trial" not in context:
            return True
        try:
            return int(float(event.get("trial"))) == int(context.get("trial"))
        except (TypeError, ValueError):
            return False

    def on_session_start(
        self,
        session_id: str,
        session_dir: Path,
        sequence_context: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._session_id = session_id
            self._session_dir = session_dir
            self._started_at = _utc_iso()
            # REQUEST_PARAMS is normally served while the robot is paused, before
            # its first binary packet creates the session. Preserve those records
            # instead of clearing the evidence that was used for this trial.
            pre_session_agents = self._agents
            self._agents = {}
            for agent_id, old_bucket in pre_session_agents.items():
                params_served = old_bucket.get("params_served", [])
                if params_served:
                    self._agent_bucket(agent_id)["params_served"] = params_served
            self._sequence_context = json.loads(json.dumps(sequence_context)) if sequence_context else None

            # START/ARMED can arrive just before the first binary packet creates
            # the receiver session. Attach only events belonging to this exact
            # sequence/trial; late packets from the previous trial cannot leak in.
            pending = self._pending_events
            self._pending_events = []
            for event in pending:
                if self._event_matches_context(event, self._sequence_context):
                    agent_id = int(event.get("agent_id", event.get("agent", -1)))
                    self._agent_bucket(agent_id)["experiment_events"].append(event)

    def reset(self) -> None:
        with self._lock:
            self._session_id = None
            self._session_dir = None
            self._started_at = None
            self._agents.clear()
            self._sequence_context = None
            self._pending_events.clear()

    def record_params_served(self, agent_id: int, payload: str, parsed: dict[str, Any]) -> None:
        entry = {
            "at": _utc_iso(),
            "payload": payload,
            "parsed": parsed,
        }
        with self._lock:
            bucket = self._agent_bucket(agent_id)
            bucket["params_served"].append(entry)

    def record_status(self, agent_id: int, telemetry: dict[str, Any]) -> None:
        entry = {
            "at": _utc_iso(),
            **telemetry,
        }
        if "src" in entry and isinstance(entry["src"], (int, float)):
            src_int = int(entry["src"])
            entry["feedback_source"] = SRC_LABELS.get(src_int, str(src_int))

        with self._lock:
            if self._session_id is None:
                return
            bucket = self._agent_bucket(agent_id)
            bucket["status_last"] = entry
            paused = entry.get("paused")
            if paused is not None and int(paused) == 0:
                bucket["status_recording"] = entry

    def record_experiment_event(self, agent_id: int, event: dict[str, Any]) -> None:
        entry = {
            "received_at": _utc_iso(),
            "agent_id": agent_id,
            **event,
        }
        with self._lock:
            if self._session_id is None:
                # The first event is normally sent immediately before the first
                # binary log packet. Keep a bounded pre-session queue so it can be
                # attached when _ensure_session() runs.
                self._pending_events.append(entry)
                if len(self._pending_events) > 128:
                    del self._pending_events[:-128]
                return
            self._agent_bucket(agent_id)["experiment_events"].append(entry)

    def record_log_end(
        self,
        agent_id: int,
        records: int,
        ok: bool,
        sequence_id: str | None = None,
        trial: int | None = None,
    ) -> None:
        entry = {
            "at": _utc_iso(),
            "records": records,
            "ok": ok,
        }
        if sequence_id is not None:
            entry["sequence_id"] = sequence_id
        if trial is not None:
            entry["trial"] = trial
        with self._lock:
            bucket = self._agent_bucket(agent_id)
            bucket["log_end"] = entry

    def finalize(
        self,
        session_dir: Path,
        session_id: str,
        merged_csv: Path,
        chunk_files: list[str],
    ) -> Path | None:
        with self._lock:
            started_at = self._started_at
            agents = json.loads(json.dumps(self._agents))  # deep copy
            sequence_context = json.loads(json.dumps(self._sequence_context)) if self._sequence_context else None

        params_path = DEFAULT_PARAMS_PATH
        yaml_snapshot_name = "sync_experiment_params.snapshot.yaml"
        yaml_snapshot_path = session_dir / yaml_snapshot_name
        yaml_data: dict[str, Any] | None = None
        yaml_mtime: float | None = None
        try:
            yaml_mtime = params_path.stat().st_mtime
            shutil.copy2(params_path, yaml_snapshot_path)
            yaml_data = load_sync_params(params_path)
        except OSError as exc:
            print(f"[SessionMeta] WARN: could not snapshot sync params: {exc}")

        sequence_snapshot_name = "current_phase_sequence.snapshot.yaml"
        sequence_snapshot_path = session_dir / sequence_snapshot_name
        try:
            if SEQUENCE_CONFIG_PATH.exists():
                shutil.copy2(SEQUENCE_CONFIG_PATH, sequence_snapshot_path)
        except OSError as exc:
            print(f"[SessionMeta] WARN: could not snapshot sequence config: {exc}")

        experiment_conditions = _build_experiment_conditions(sequence_context, yaml_data, agents)

        meta = {
            "session_id": session_id,
            "started_at": started_at,
            "finalized_at": _utc_iso(),
            "merged_csv": merged_csv.name,
            "merged_csv_path": str(merged_csv),
            "chunk_files": [Path(p).name for p in chunk_files],
            "experiment_conditions": experiment_conditions,
            "sync_params": {
                "source_path": str(params_path),
                "snapshot_file": yaml_snapshot_name,
                "mtime_unix": yaml_mtime,
                "values": yaml_data,
            },
            "sequence_config": {
                "source_path": str(SEQUENCE_CONFIG_PATH),
                "snapshot_file": sequence_snapshot_name if SEQUENCE_CONFIG_PATH.exists() else None,
            },
            "sequence": sequence_context,
            "agents": {str(aid): data for aid, data in sorted(agents.items())},
        }

        meta_path = session_dir / "session_meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"[SessionMeta] Wrote {meta_path}")
        return meta_path
