"""Automatic six-condition current-feedback experiment controller.

Robots receive the full six-trial plan while still on the surface, then run all
conditions autonomously at 50 Hz without any UDP link underwater.  The robot
keeps the active log in RAM and checkpoints completed trials to LittleFS; the PC
only waits on a timer and finally sends STOP after surfacing to upload/merge once.

Legacy note: START/STOP robot events are logged when they arrive but are never
required for progression.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from sync_params import Z_MODES, effective_prc_params, load_sync_params

AUTONOMOUS_MARGIN_S = 30.0
MAX_SEQUENCE_TRIALS = 7
SEQUENCE_LOG_HZ = 50.0
LOG_BUFFER_RECORD_CAPACITY = 21000


class SequenceValidationError(ValueError):
    """The requested sequence is not safe to start with the current fleet state."""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_duration_jp(seconds: float) -> str:
    total_seconds = max(0, int(math.ceil(seconds)))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}時間{minutes:02d}分{remaining_seconds:02d}秒"
    return f"{minutes}分{remaining_seconds:02d}秒"


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _resolve_even_odd_pair(agent_ids: list[int]) -> tuple[int, int]:
    """Reference = even ID (phase_offset=0), partner = odd ID (phase_offset=-Δφ)."""
    if len(agent_ids) != 2 or len(set(agent_ids)) != 2:
        raise SequenceValidationError("sequence requires exactly two different robot IDs")
    first, second = agent_ids
    if first % 2 == second % 2:
        raise SequenceValidationError(
            f"同期ペアは偶数IDと奇数IDの組み合わせが必要です（指定: {sorted(agent_ids)}）"
        )
    reference = first if first % 2 == 0 else second
    partner = second if reference == first else first
    return reference, partner


def _phase_difference_definition(reference_agent: int, partner_agent: int) -> str:
    return f"phase_agent_{reference_agent}_minus_phase_agent_{partner_agent}"


def _sequence_ui_copy(
    config: dict[str, Any],
    sequence_type: str,
    reference_agent: int,
    partner_agent: int,
) -> dict[str, str]:
    """Human-readable dashboard copy derived from the active sequence YAML."""
    sync_params = load_sync_params()
    z_mode = str(sync_params.get("z_mode", "cos")).lower()
    z_label = {
        "zopt": "z_opt（逆設計）",
        "zsin": "z_sin（最適正弦）",
    }.get(z_mode, "cos(ψ)")
    labels = config.get(
        "delta_omega_labels" if sequence_type == "delta_omega_sweep" else "phase_labels",
        [],
    )
    total_trials = len(labels) or MAX_SEQUENCE_TRIALS
    duration_s = float(config.get("trial_duration_ms", 60000)) / 1000.0
    feedback_start_s = float(config.get("feedback_start_ms", 10000)) / 1000.0
    pause_s = float(config.get("inter_trial_pause_ms", 10000)) / 1000.0
    first_delay_s = float(config.get("first_start_delay_ms", 20000)) / 1000.0
    autonomous_s = first_delay_s + total_trials * (pause_s + duration_s)
    pair_text = (
        f"偶数ID {reference_agent}（基準・ω 固定）と奇数ID {partner_agent}（パートナー・ω 変更）のペア。"
    )
    timing_text = (
        f"開始予約後{first_delay_s:.0f} sは水中投入・姿勢確認、その後{pause_s:.0f} sは中央停止、"
        f"各条件は{duration_s:.0f} s（0–{feedback_start_s:.0f} s: κ=0、"
        f"{feedback_start_s:.0f}–{duration_s:.0f} s: 電流フィードバック κ=5）。"
        f"最初の動作までは{first_delay_s + pause_s:.0f} s、全{total_trials}条件は約{_format_duration_jp(autonomous_s)}です。"
    )
    common_tail = (
        "曲げセンサは使用せず、current-only ファームウェアであることを開始前に検査します。"
        f" Z(ψ)={z_label}（fleet/sync_experiment_params.yaml）。"
    )
    if sequence_type == "delta_omega_sweep":
        omega_base = float(config.get("omega_base_rad_s", 3.5 * math.pi))
        labels = [str(v) for v in config.get("delta_omega_labels", [])]
        condition_text = ", ".join(labels) if labels else "Δω = 0 … 0.25π"
        analysis_window = (
            "解析窓は各試行の最終60 s（Eq.36 用）。"
            if duration_s >= 120.0
            else "解析窓は各試行 10–50 s（定常区間）。"
        )
        return {
            "ui_title": f"自動実験：大パドル・電流同期・Δω両側スイープ{total_trials}条件",
            "ui_description": (
                f"{pair_text} {timing_text}"
                f"条件は partner の自然角速度を段階変更（{condition_text}）。"
                f"基準 ω≈{omega_base:.3f} rad/s（3.5π）。{analysis_window}"
                f" {common_tail}"
            ),
            "ui_confirm_hint": f"開始予約後{first_delay_s:.0f} s以内に水中へ入れ、電池・防水を確認してください。",
        }
    return {
        "ui_title": f"自動実験：大パドル・電流同期・初期位相差{total_trials}条件",
        "ui_description": (
            f"偶数ID（基準・phase_offset=0）と奇数ID（パートナー・−Δφ）のペアを使用"
            f"（デフォルト {partner_agent} / {reference_agent}）。 {timing_text}"
            f"条件は{total_trials}件を順に自動実行します。"
            f" {common_tail}"
        ),
        "ui_confirm_hint": f"開始予約後{first_delay_s:.0f} s以内に水中へ入れ、電池・防水を確認してください。",
    }


class ExperimentSequenceController:
    """Run one to six autonomous conditions and persist the complete plan/state."""

    ACTIVE_STATES = {
        "preparing",
        "arming",
        "armed",
        "running",
        "running_autonomous",
        "waiting_for_logs",
        "stopping",
    }
    RUNNING_STATES = {"preparing", "arming", "armed", "running", "running_autonomous"}

    def __init__(
        self,
        config_path: Path,
        experiments_dir: Path,
        log_receiver: Any,
        target_provider: Callable[[list[int]], list[dict[str, Any]]],
    ) -> None:
        self.config_path = Path(config_path)
        self.experiments_dir = Path(experiments_dir)
        self.log_receiver = log_receiver
        self.target_provider = target_provider

        self._plan: dict[str, Any] | None = None
        self._targets: list[dict[str, Any]] = []
        self._sequence_dir: Path | None = None
        self._status_path: Path | None = None
        self._task: asyncio.Task | None = None
        self._merged_event: asyncio.Event | None = None
        self._armed_agents: set[int] = set()
        self._go_ack_agents: set[int] = set()
        self._started_agents: set[int] = set()
        self._trial_stopped_agents: set[int] = set()
        self._trial_log_end: dict[int, dict[str, Any]] = {}
        self._trial_upload_done: set[int] = set()
        self._trial_start_events: dict[int, dict[str, Any]] = {}
        self._trial_start_skew_ms: float | None = None
        self._trial_failed = False
        self._stop_requested = False
        self._state: dict[str, Any] = {"status": "idle"}

    def _set_status(self, status: str, **updates: Any) -> None:
        self._state["status"] = status
        self._state["updated_at"] = _utc_iso()
        self._state.update(updates)
        self._save_status()

    def _save_status(self) -> None:
        if self._status_path is None:
            return
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._status_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
                f.write("\n")
            temp_path.replace(self._status_path)
        except OSError as exc:
            # Never block stop/abort when the disk is full.
            print(f"[SEQ] status save failed: {exc}", flush=True)

    def get_status(self) -> dict[str, Any]:
        # Ensure the HTTP response is always JSON-serializable.
        status = json.loads(json.dumps(self._state, ensure_ascii=False, default=str))
        try:
            status.update(self._config_preview())
        except SequenceValidationError:
            pass
        return status

    def _read_sequence_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SequenceValidationError(f"sequence config not found: {self.config_path}")
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except OSError as exc:
            raise SequenceValidationError(f"could not read sequence config: {exc}") from exc

    def _config_preview(self) -> dict[str, Any]:
        config = self._read_sequence_config()
        default_agents = [int(a) for a in config.get("agents", [11, 12])]
        pairing_rule = str(config.get("pairing_rule", "even_odd")).lower()
        sequence_type = str(config.get("sequence_type", "phase_sweep")).lower()
        if pairing_rule == "even_odd":
            reference_agent, partner_agent = _resolve_even_odd_pair(default_agents)
        else:
            reference_agent = int(config.get("phase_reference_agent", default_agents[0]))
            partner_agent = int(config.get("phase_partner_agent", default_agents[1]))
        condition_count = len(
            config.get(
                "delta_omega_labels" if sequence_type == "delta_omega_sweep" else "phase_labels",
                [],
            )
        ) or MAX_SEQUENCE_TRIALS
        if sequence_type == "delta_omega_sweep":
            message = (
                f"設定済み：ID {reference_agent}(偶・基準) / {partner_agent}(奇)｜"
                f"Δω スイープ｜{condition_count}条件"
            )
        else:
            message = (
                f"設定済み：ID {reference_agent}(偶・基準) / {partner_agent}(奇)｜"
                f"Δφ=φ_{reference_agent}−φ_{partner_agent}｜"
                f"{condition_count}条件"
            )
        ui = _sequence_ui_copy(config, sequence_type, reference_agent, partner_agent)
        return {
            "default_agent_ids": default_agents,
            "pairing_rule": pairing_rule,
            "sequence_type": sequence_type,
            "total_trials": condition_count,
            "phase_reference_agent": reference_agent,
            "phase_partner_agent": partner_agent,
            "phase_difference_definition": _phase_difference_definition(
                reference_agent, partner_agent
            ),
            "message": message,
            "z_mode": str(load_sync_params().get("z_mode", "cos")).lower(),
            **ui,
        }

    def _autonomous_duration_s(self) -> float:
        assert self._plan is not None
        total_trials = len(self._plan["phase_differences_rad"])
        return (
            self._plan["first_start_delay_ms"] / 1000.0
            + total_trials * self._plan["inter_trial_pause_ms"] / 1000.0
            + total_trials * self._plan["trial_duration_ms"] / 1000.0
        )

    def _load_plan(self, requested_agent_ids: list[int] | None) -> dict[str, Any]:
        config = self._read_sequence_config()

        configured_agents = [int(a) for a in config.get("agents", [11, 12])]
        requested = [int(a) for a in (requested_agent_ids or configured_agents)]
        pairing_rule = str(config.get("pairing_rule", "even_odd")).lower()

        if pairing_rule == "even_odd":
            reference_agent, partner_agent = _resolve_even_odd_pair(requested)
            agent_ids = [reference_agent, partner_agent]
            phase_difference_definition = _phase_difference_definition(
                reference_agent, partner_agent
            )
        elif pairing_rule == "fixed":
            agent_ids = configured_agents
            if requested_agent_ids and requested != configured_agents:
                raise SequenceValidationError(
                    f"この実験は agents={configured_agents} の組で実施します（要求: {requested}）"
                )
            if len(agent_ids) != 2 or len(set(agent_ids)) != 2:
                raise SequenceValidationError("sequence requires exactly two different robot IDs")
            reference_agent = int(config.get("phase_reference_agent", agent_ids[0]))
            partner_agent = int(config.get("phase_partner_agent", agent_ids[1]))
            if (reference_agent, partner_agent) != (agent_ids[0], agent_ids[1]):
                raise SequenceValidationError(
                    "phase reference/partner must match the configured agent order"
                )
            phase_difference_definition = str(
                config.get(
                    "phase_difference_definition",
                    _phase_difference_definition(reference_agent, partner_agent),
                )
            )
        else:
            raise SequenceValidationError(f"unknown pairing_rule: {pairing_rule}")

        sequence_type = str(config.get("sequence_type", "phase_sweep")).lower()
        if sequence_type not in ("phase_sweep", "delta_omega_sweep"):
            raise SequenceValidationError(f"unknown sequence_type: {sequence_type}")

        omega_base_rad_s: float | None = None
        partner_omegas_rad: list[float] | None = None

        if sequence_type == "delta_omega_sweep":
            deltas = [float(v) for v in config.get("delta_omega_rad", [])]
            labels = [str(v) for v in config.get("delta_omega_labels", [])]
            if not 1 <= len(deltas) <= MAX_SEQUENCE_TRIALS or len(labels) != len(deltas):
                raise SequenceValidationError(
                    f"delta_omega sequence must contain 1-{MAX_SEQUENCE_TRIALS} matching conditions"
                )
            if any(not math.isfinite(v) for v in deltas):
                raise SequenceValidationError("delta_omega_rad values must be finite")
            omega_base_rad_s = float(config.get("omega_base_rad_s", 3.5 * math.pi))
            if not math.isfinite(omega_base_rad_s) or omega_base_rad_s <= 0.0:
                raise SequenceValidationError("omega_base_rad_s must be a positive finite value")
            phases = [0.0] * len(deltas)
            partner_omegas_rad = [omega_base_rad_s - delta for delta in deltas]
            if any(value <= 0.0 for value in partner_omegas_rad):
                raise SequenceValidationError("partner omega must stay positive for all delta_omega conditions")
        else:
            phases = [float(v) for v in config.get("phase_differences_rad", [])]
            labels = [str(v) for v in config.get("phase_labels", [])]
            if not 1 <= len(phases) <= MAX_SEQUENCE_TRIALS or len(labels) != len(phases):
                raise SequenceValidationError(
                    f"phase sequence must contain 1-{MAX_SEQUENCE_TRIALS} matching conditions"
                )
            if any(not math.isfinite(v) or v < 0.0 or v > 2.0 * math.pi + 1e-5 for v in phases):
                raise SequenceValidationError("phase differences must be in [0, 2π]")

        duration_ms = int(config.get("trial_duration_ms", 60000))
        feedback_start_ms = int(config.get("feedback_start_ms", 10000))
        first_delay_ms = int(config.get("first_start_delay_ms", 20000))
        inter_trial_pause_ms = int(config.get("inter_trial_pause_ms", 10000))
        kappa = float(config.get("kappa", 5.0))
        upload_strategy = str(config.get("upload_strategy", "batch_on_stop")).lower()
        if duration_ms <= 0 or duration_ms > 3600000:
            raise SequenceValidationError("trial_duration_ms is outside the safe range")
        expected_records = math.ceil(len(phases) * duration_ms / 1000.0 * SEQUENCE_LOG_HZ)
        if expected_records > LOG_BUFFER_RECORD_CAPACITY:
            raise SequenceValidationError(
                f"sequence would need about {expected_records} log records; "
                f"split it into runs of at most {LOG_BUFFER_RECORD_CAPACITY} records"
            )
        if feedback_start_ms < 0 or feedback_start_ms >= duration_ms:
            raise SequenceValidationError("feedback_start_ms must be before trial end")
        if first_delay_ms < 0 or first_delay_ms > 3600000:
            raise SequenceValidationError("first_start_delay_ms is outside the safe range")
        if inter_trial_pause_ms < 0 or inter_trial_pause_ms > 3600000:
            raise SequenceValidationError("inter_trial_pause_ms is outside the safe range")
        if not math.isfinite(kappa) or abs(kappa - 5.0) > 1e-6:
            raise SequenceValidationError("この実験の κ は 5.0 に固定してください")
        if upload_strategy != "batch_on_stop":
            raise SequenceValidationError("upload_strategy は batch_on_stop のみ対応しています")

        sync_params = copy.deepcopy(effective_prc_params(load_sync_params()))
        sync_source = str(sync_params.get("feedback_source", "")).lower()
        if not bool(config.get("current_only", False)):
            raise SequenceValidationError("sequence config must set current_only: true")
        if not bool(sync_params.get("current_only", False)) or sync_source != "current":
            raise SequenceValidationError(
                "sync_experiment_params.yaml が current-only/current になっていません"
            )
        if abs(float(sync_params.get("kappa", float("nan"))) - kappa) > 1e-6:
            raise SequenceValidationError("sequence config と sync parameter の κ が一致しません")
        omega_by_agent = sync_params.get("omega_by_agent", {})
        try:
            pair_omegas = [
                float(omega_by_agent.get(agent_id, omega_by_agent.get(str(agent_id))))
                for agent_id in agent_ids
            ]
        except (AttributeError, TypeError, ValueError):
            raise SequenceValidationError("sync parameter に対象エージェントの ω がありません")
        if any(not math.isfinite(value) or value <= 0.0 for value in pair_omegas):
            raise SequenceValidationError("対象エージェントの ω が不正です")
        if sequence_type == "delta_omega_sweep":
            assert omega_base_rad_s is not None
            ref_omega = pair_omegas[0] if reference_agent == agent_ids[0] else pair_omegas[1]
            if abs(ref_omega - omega_base_rad_s) > 1e-6:
                raise SequenceValidationError(
                    f"基準エージェント ID {reference_agent} の ω={ref_omega:.6f} が "
                    f"omega_base_rad_s={omega_base_rad_s:.6f} と一致しません"
                )
            partner_omega = pair_omegas[0] if partner_agent == agent_ids[0] else pair_omegas[1]
            if abs(partner_omega - omega_base_rad_s) > 1e-6:
                raise SequenceValidationError(
                    f"パートナー ID {partner_agent} の初期 ω={partner_omega:.6f} は "
                    f"Δω=0 条件用に omega_base={omega_base_rad_s:.6f} と一致させてください"
                )
        elif abs(pair_omegas[0] - pair_omegas[1]) > 1e-6:
            raise SequenceValidationError(
                f"同期実験の ω が不一致です（ID {agent_ids[0]}={pair_omegas[0]:.6f}, "
                f"ID {agent_ids[1]}={pair_omegas[1]:.6f} rad/s）"
            )

        plan: dict[str, Any] = {
            "name": str(config.get("name", "current_phase_sequence")),
            "sequence_type": sequence_type,
            "agent_ids": agent_ids,
            "paddle": str(config.get("paddle", "big")),
            "current_only": True,
            "flex_sensor_present": True,
            "upload_strategy": upload_strategy,
            "one_session_per_sequence": True,
            "first_start_delay_ms": first_delay_ms,
            "inter_trial_pause_ms": inter_trial_pause_ms,
            "trial_duration_ms": duration_ms,
            "feedback_start_ms": feedback_start_ms,
            "kappa": kappa,
            "feedback_source": "current",
            "phase_reference_agent": reference_agent,
            "phase_partner_agent": partner_agent,
            "phase_difference_definition": phase_difference_definition,
            "pairing_rule": pairing_rule,
            "phase_differences_rad": phases,
            "phase_labels": labels,
            "parameter_file": str(config.get("parameter_file", "fleet/sync_experiment_params.yaml")),
            "sync_params_snapshot": sync_params,
        }
        if partner_omegas_rad is not None:
            plan["partner_omegas_rad"] = partner_omegas_rad
        if omega_base_rad_s is not None:
            plan["omega_base_rad_s"] = omega_base_rad_s
        return plan

    def _validate_targets(
        self,
        agent_ids: list[int],
        plan: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        raw_targets = self.target_provider(agent_ids)
        by_id = {int(item.get("id")): item for item in raw_targets}
        validation_plan = plan or self._plan
        if validation_plan is None:
            raise SequenceValidationError("sequence plan is not loaded")
        omega_by_agent = validation_plan["sync_params_snapshot"].get("omega_by_agent", {})
        expected_z_mode = str(
            validation_plan["sync_params_snapshot"].get("z_mode", "cos")
        ).lower()
        expected_z_mode_code = Z_MODES.get(expected_z_mode)
        targets: list[dict[str, Any]] = []
        problems: list[str] = []
        if expected_z_mode_code is None:
            raise SequenceValidationError(f"unknown z_mode: {expected_z_mode}")
        for agent_id in agent_ids:
            item = by_id.get(agent_id)
            if item is None:
                problems.append(f"ID {agent_id}: オンライン状態を確認できません")
                continue
            if not item.get("ip"):
                problems.append(f"ID {agent_id}: IPアドレスなし")
                continue
            if _as_int(item.get("paused")) != 1:
                problems.append(f"ID {agent_id}: 停止状態ではありません")
            if _as_int(item.get("params")) != 1:
                problems.append(f"ID {agent_id}: パラメータ未取得")
            if _as_int(item.get("src")) != 1:
                problems.append(f"ID {agent_id}: feedback source が current ではありません")
            observed_z_mode = _as_int(item.get("zmode"))
            if observed_z_mode != expected_z_mode_code:
                problems.append(
                    f"ID {agent_id}: zmode={item.get('zmode')} "
                    f"（設定 {expected_z_mode}={expected_z_mode_code} と不一致）"
                )
            if str(item.get("mode", "")) != "current_only":
                problems.append(f"ID {agent_id}: current-only ファームウェアではありません")
            if _as_int(item.get("flex_used")) != 0:
                problems.append(f"ID {agent_id}: flex_used が 0 ではありません")
            if item.get("kappa") is None:
                problems.append(f"ID {agent_id}: κ を確認できません")
            else:
                try:
                    if abs(float(item["kappa"]) - 5.0) > 1e-3:
                        problems.append(f"ID {agent_id}: κ={item['kappa']}（5.0ではありません）")
                except (TypeError, ValueError):
                    problems.append(f"ID {agent_id}: κ の値が不正です")
            expected_omega = omega_by_agent.get(agent_id, omega_by_agent.get(str(agent_id)))
            observed_omega = item.get("omega")
            if expected_omega is None:
                problems.append(f"ID {agent_id}: 設定側の ω がありません")
            elif observed_omega is None:
                problems.append(f"ID {agent_id}: 実機の ω を確認できません")
            else:
                try:
                    if abs(float(observed_omega) - float(expected_omega)) > 1e-3:
                        problems.append(
                            f"ID {agent_id}: ω={float(observed_omega):.6f} "
                            f"（設定値 {float(expected_omega):.6f} と不一致）"
                        )
                except (TypeError, ValueError):
                    problems.append(f"ID {agent_id}: ω の値が不正です")
            targets.append({"id": agent_id, "ip": str(item["ip"])})

        if problems:
            raise SequenceValidationError("; ".join(problems))
        return targets

    def _sequence_context(self, sequence_id: str) -> dict[str, Any]:
        assert self._plan is not None
        context: dict[str, Any] = {
            "name": self._plan["name"],
            "sequence_id": sequence_id,
            "sequence_type": self._plan.get("sequence_type", "phase_sweep"),
            "upload_strategy": self._plan["upload_strategy"],
            "total_trials": len(self._plan["phase_differences_rad"]),
            "agent_ids": list(self._plan["agent_ids"]),
            "paddle": self._plan["paddle"],
            "current_only": True,
            "flex_sensor_present": True,
            "feedback_source": "current",
            "kappa": self._plan["kappa"],
            "kappa_init": float(self._plan["sync_params_snapshot"].get("kappa_init", 0.0)),
            "feedback_start_ms": self._plan["feedback_start_ms"],
            "duration_ms": self._plan["trial_duration_ms"],
            "first_start_delay_ms": self._plan["first_start_delay_ms"],
            "inter_trial_pause_ms": self._plan["inter_trial_pause_ms"],
            "phase_difference_definition": self._plan["phase_difference_definition"],
            "phase_reference_agent": self._plan["phase_reference_agent"],
            "phase_partner_agent": self._plan["phase_partner_agent"],
            "phase_differences_rad": list(self._plan["phase_differences_rad"]),
            "phase_labels": list(self._plan["phase_labels"]),
            "parameter_file": self._plan["parameter_file"],
            "sync_params_snapshot": copy.deepcopy(self._plan["sync_params_snapshot"]),
            "sequence_plan_file": str(self._sequence_dir / "sequence_plan.json") if self._sequence_dir else None,
            "sequence_status_file": str(self._status_path) if self._status_path else None,
        }
        if "partner_omegas_rad" in self._plan:
            context["partner_omegas_rad"] = list(self._plan["partner_omegas_rad"])
        if "omega_base_rad_s" in self._plan:
            context["omega_base_rad_s"] = float(self._plan["omega_base_rad_s"])
        return context

    async def start(self, requested_agent_ids: list[int] | None = None) -> dict[str, Any]:
        if self._state.get("status") in self.ACTIVE_STATES | {"awaiting_upload"}:
            raise SequenceValidationError("sequence is already running")

        plan = self._load_plan(requested_agent_ids)
        targets = self._validate_targets(plan["agent_ids"], plan)
        sequence_id = "ps" + datetime.now(timezone.utc).strftime("%y%m%d_%H%M%S")
        sequence_dir = self.experiments_dir / f"sequence_{sequence_id}"
        sequence_dir.mkdir(parents=True, exist_ok=True)

        self._plan = plan
        self._targets = targets
        self._sequence_dir = sequence_dir
        self._status_path = sequence_dir / "sequence_status.json"
        self._stop_requested = False
        self._trial_failed = False
        total_trials = len(plan["phase_differences_rad"])
        autonomous_duration_s = self._autonomous_duration_s()
        self._state = {
            "status": "preparing",
            "sequence_id": sequence_id,
            "created_at": _utc_iso(),
            "updated_at": _utc_iso(),
            "current_trial": 0,
            "total_trials": total_trials,
            "upload_strategy": plan["upload_strategy"],
            "next_phase_difference_rad": plan["phase_differences_rad"][0],
            "next_condition": plan["phase_labels"][0],
            "targets": copy.deepcopy(targets),
            "trial_results": [],
            "event_log": [],
            "autonomous_duration_s": autonomous_duration_s,
            "timing": {
                "first_start_delay_ms": plan["first_start_delay_ms"],
                "inter_trial_pause_ms": plan["inter_trial_pause_ms"],
                "trial_duration_ms": plan["trial_duration_ms"],
                "feedback_start_ms": plan["feedback_start_ms"],
            },
            "message": (
                f"水上で全{total_trials}条件を予約します（約{_format_duration_jp(autonomous_duration_s)}の自律実行）。"
                "入水後は通信不要です"
            ),
        }

        plan_snapshot = {
            "sequence_id": sequence_id,
            "created_at": self._state["created_at"],
            "targets": copy.deepcopy(targets),
            "plan": plan,
        }
        with open(sequence_dir / "sequence_plan.json", "w", encoding="utf-8") as f:
            json.dump(plan_snapshot, f, ensure_ascii=False, indent=2)
            f.write("\n")
        self._state["sequence_plan_file"] = str(sequence_dir / "sequence_plan.json")
        self._save_status()

        self.log_receiver.set_sequence_context(self._sequence_context(sequence_id))
        self._task = asyncio.create_task(self._run_sequence())
        return self.get_status()

    async def _run_sequence(self) -> None:
        try:
            assert self._plan is not None
            total_trials = len(self._plan["phase_differences_rad"])
            if not await self._arm_and_start_autonomous():
                return
            if self._stop_requested:
                return

            duration_s = self._autonomous_duration_s()
            motion_end_epoch = time.time() + duration_s
            self._set_status(
                "running_autonomous",
                current_trial=1,
                total_trials=total_trials,
                condition=self._plan["phase_labels"][0],
                phase_difference_target_rad=self._plan["phase_differences_rad"][0],
                motion_start_epoch=self._state.get("start_at_epoch"),
                robot_motion_end_epoch=motion_end_epoch,
                expected_end_epoch=motion_end_epoch,
                message=(
                    f"水中自律実行中（約 {_format_duration_jp(duration_s)}）。"
                    f"ロボットは{total_trials}条件完了後に自動で中央停止します"
                ),
            )
            await asyncio.sleep(duration_s)
            if self._stop_requested:
                return

            self._record_all_planned_trials()
            self._set_status(
                "awaiting_upload",
                current_trial=total_trials,
                message=f"{total_trials}条件の記録が完了しました。水上で「STOP ALL」を押してログを一括アップロードしてください",
            )
            # Short buffer so the UI can flip to awaiting_upload before surfacing.
            await asyncio.sleep(AUTONOMOUS_MARGIN_S)
            self._merged_event = asyncio.Event()
            await asyncio.wait_for(self._merged_event.wait(), timeout=3600.0)
            if self._trial_failed:
                return

            self._set_status(
                "completed",
                completed_at=_utc_iso(),
                current_trial=total_trials,
                message=f"{total_trials}条件の一括ログ保存・マージが完了しました",
            )
        except asyncio.CancelledError:
            if self._state.get("status") not in {"stopped", "error"}:
                self._set_status("stopped", message="sequence task was cancelled")
        except Exception as exc:
            self._trial_failed = True
            self._set_status("error", error=str(exc), message="シーケンスを中止しました")
            self._send_to_targets("SEQ_ABORT")
        finally:
            if self._task is asyncio.current_task():
                self._task = None
            self._save_status()

    def _record_all_planned_trials(self) -> None:
        assert self._plan is not None
        results: list[dict[str, Any]] = []
        for trial_index, (label, phase_difference) in enumerate(
            zip(self._plan["phase_labels"], self._plan["phase_differences_rad"], strict=True),
            start=1,
        ):
            results.append(
                {
                    "trial": trial_index,
                    "condition": label,
                    "phase_difference_target_rad": phase_difference,
                    "planned_autonomous": True,
                    "stopped_at": _utc_iso(),
                }
            )
        self._state["trial_results"] = results
        self._save_status()

    async def _arm_and_start_autonomous(self) -> bool:
        assert self._plan is not None
        sequence_id = str(self._state["sequence_id"])
        total_trials = len(self._plan["phase_differences_rad"])
        phase_difference = float(self._plan["phase_differences_rad"][0])
        condition = str(self._plan["phase_labels"][0])

        self._armed_agents.clear()
        self._go_ack_agents.clear()
        self._started_agents.clear()
        self._trial_start_events.clear()
        self._trial_start_skew_ms = None

        self._set_status(
            "arming",
            current_trial=1,
            total_trials=total_trials,
            condition=condition,
            phase_difference_target_rad=phase_difference,
            next_phase_difference_rad=phase_difference,
            next_condition=condition,
            start_at_epoch=None,
            message=f"水上で全{total_trials}条件を両機へ送信しています",
        )

        for attempt in range(2):
            self._send_arm_commands(sequence_id, 1, condition, phase_difference, total_trials)
            if attempt == 0:
                await asyncio.sleep(0.08)
        await self._wait_for_agents(self._armed_agents, "ARMED", timeout=5.0)

        start_delay_ms = self._plan["first_start_delay_ms"]
        pause_ms = int(self._plan.get("inter_trial_pause_ms", 0))
        motion_delay_ms = start_delay_ms + pause_ms
        start_at_epoch = time.time() + motion_delay_ms / 1000.0 if motion_delay_ms else None
        if start_delay_ms:
            start_message = (
                f"投入後 {start_delay_ms / 1000.0:.0f} s + 停止 {pause_ms / 1000.0:.0f} s = "
                f"最初の動作まで {motion_delay_ms / 1000.0:.0f} s。"
                "入水後は通信しません"
            )
        else:
            start_message = f"両機の準備完了。{total_trials}条件の自律実行を開始します"
        self._set_status(
            "armed",
            start_at_epoch=start_at_epoch,
            message=start_message,
        )

        go_command = f"SEQ_GO,seq:{sequence_id},trial:1,delay_ms:{start_delay_ms}"
        expected_go = set(self._plan["agent_ids"])
        for _ in range(4):
            self._send_go_burst(go_command)
            await asyncio.sleep(0.2)
            if expected_go <= self._go_ack_agents:
                break
        deadline = time.time() + 3.0
        while time.time() < deadline and self._go_ack_agents < expected_go:
            self._send_go_burst(go_command)
            await asyncio.sleep(0.25)
        missing_go = sorted(expected_go - self._go_ack_agents)
        if missing_go:
            self._send_to_targets("SEQ_ABORT")
            self._send_stop_burst()
            self._set_status(
                "error",
                go_ack_agents=sorted(self._go_ack_agents),
                error=f"GO未確認: agents {missing_go}",
                message=(
                    f"ID {missing_go} が開始指示を確認できません（サーボが動かない）。"
                    "OTA・Wi-Fiを確認してから再試行してください"
                ),
            )
            return False
        ack_message = "両機GO確認済（サーボが一度ピコっと動きました）"
        merged_message = f"{start_message} · {ack_message}"
        self._set_status(
            "armed",
            start_at_epoch=start_at_epoch,
            go_ack_agents=sorted(self._go_ack_agents),
            message=merged_message,
        )
        return True
        # Do not wait for START events: robots run locally without Wi-Fi underwater.

    async def _wait_for_agents(self, received: set[int], event_name: str, timeout: float) -> None:
        assert self._plan is not None
        expected = set(self._plan["agent_ids"])
        deadline = asyncio.get_running_loop().time() + timeout
        while not expected.issubset(received):
            if self._stop_requested:
                raise asyncio.CancelledError
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                missing = sorted(expected - received)
                raise SequenceValidationError(f"{event_name} timeout; missing agents={missing}")
            await asyncio.sleep(min(0.05, remaining))

    def _send_arm_commands(
        self,
        sequence_id: str,
        trial: int,
        condition: str,
        phase_difference: float,
        total_trials: int,
    ) -> None:
        assert self._plan is not None
        reference_agent = int(self._plan["phase_reference_agent"])
        phase_differences = [float(v) for v in self._plan["phase_differences_rad"]]
        labels = [str(v) for v in self._plan["phase_labels"]]
        phase_offsets_by_agent: dict[int, list[float]] = {}
        for agent_id in self._plan["agent_ids"]:
            offsets = []
            for phase_diff in phase_differences:
                offsets.append(0.0 if int(agent_id) == reference_agent else -phase_diff)
            phase_offsets_by_agent[int(agent_id)] = offsets

        for target in self._targets:
            agent_id = int(target["id"])
            offsets = phase_offsets_by_agent[agent_id]
            command = (
                f"SEQ_ARM,seq:{sequence_id},trial:{trial},condition:{condition},"
                f"phase_offset:{offsets[0]:.6f},phase_diff:{phase_difference:.6f},"
                f"phase_offsets:{';'.join(f'{v:.6f}' for v in offsets)},"
                f"conditions:{';'.join(labels)},"
                f"feedback_start_ms:{self._plan['feedback_start_ms']},"
                f"duration_ms:{self._plan['trial_duration_ms']},"
                f"inter_trial_pause_ms:{self._plan['inter_trial_pause_ms']},"
                f"kappa:{self._plan['kappa']:.6f},"
                f"total_trials:{total_trials},"
                "src:current,mode:current_only"
            )
            partner_omegas = self._plan.get("partner_omegas_rad")
            if partner_omegas:
                command += f",partner_omegas:{';'.join(f'{v:.6f}' for v in partner_omegas)}"
            self._send_one(target, command)

    def _send_one(self, target: dict[str, Any], command: str) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(command.encode("utf-8"), (str(target["ip"]), 5000 + int(target["id"])))
            return True
        except OSError as exc:
            self._append_event({"event": "COMMAND_SEND_ERROR", "agent": target.get("id"), "error": str(exc)})
            return False

    def _send_to_targets(self, command: str) -> int:
        sent = 0
        for target in self._targets:
            if self._send_one(target, command):
                sent += 1
        return sent

    def _send_stop_burst(self) -> int:
        """Send STOP + OFF like the legacy manual STOP ALL command."""
        sent = 0
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for _ in range(2):
                    for target in self._targets:
                        ip = str(target["ip"])
                        port = 5000 + int(target["id"])
                        agent_id = int(target["id"])
                        for payload in (b"STOP", f"{agent_id}:OFF".encode("utf-8")):
                            try:
                                sock.sendto(payload, (ip, port))
                                sent += 1
                            except OSError as exc:
                                self._append_event(
                                    {
                                        "event": "COMMAND_SEND_ERROR",
                                        "command": payload.decode("utf-8", errors="replace"),
                                        "agent": agent_id,
                                        "error": str(exc),
                                    }
                                )
        except OSError as exc:
            self._append_event({"event": "COMMAND_SEND_ERROR", "command": "STOP", "error": str(exc)})
        return sent

    def _send_go_burst(self, command: str) -> int:
        """Send GO to both robots in two immediate bursts to preserve phase."""
        sent = 0
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for _ in range(2):
                    for target in self._targets:
                        try:
                            sock.sendto(
                                command.encode("utf-8"),
                                (str(target["ip"]), 5000 + int(target["id"])),
                            )
                            sent += 1
                        except OSError as exc:
                            self._append_event(
                                {
                                    "event": "COMMAND_SEND_ERROR",
                                    "command": "SEQ_GO",
                                    "agent": target.get("id"),
                                    "error": str(exc),
                                }
                            )
        except OSError as exc:
            self._append_event(
                {"event": "COMMAND_SEND_ERROR", "command": "SEQ_GO", "error": str(exc)}
            )
        return sent

    def _append_event(self, event: dict[str, Any]) -> None:
        log = self._state.setdefault("event_log", [])
        log.append({"received_at": _utc_iso(), **copy.deepcopy(event)})
        if len(log) > 256:
            del log[:-256]
        self._save_status()

    def _matches_current_trial(self, sequence_id: str | None, trial: int | None) -> bool:
        return (
            self._plan is not None
            and self._state.get("status") in self.RUNNING_STATES
            and sequence_id == self._state.get("sequence_id")
            and trial is not None
            and 1 <= trial <= int(self._state.get("total_trials", 0))
        )

    def _matches_upload(self, sequence_id: str | None) -> bool:
        return (
            self._plan is not None
            and self._state.get("status") == "waiting_for_logs"
            and sequence_id == self._state.get("sequence_id")
        )

    def handle_robot_event(self, agent_id: int, event: dict[str, Any]) -> None:
        sequence_id = str(event.get("seq", ""))
        trial = _as_int(event.get("trial"))
        event_name = str(event.get("event", ""))
        entry = {"agent_id": agent_id, **event}
        self._append_event(entry)
        if event_name == "UPLOAD_DONE" and self._matches_upload(sequence_id):
            self._trial_upload_done.add(agent_id)
            self._state["upload_done_agents"] = sorted(self._trial_upload_done)
            self._save_status()
            return
        if sequence_id != self._state.get("sequence_id"):
            return
        if event_name == "ARMED":
            self._armed_agents.add(agent_id)
        elif event_name == "GO_ACK":
            self._go_ack_agents.add(agent_id)
            self._state["go_ack_agents"] = sorted(self._go_ack_agents)
            self._save_status()
        elif event_name == "START":
            self._started_agents.add(agent_id)
            self._trial_start_events[agent_id] = copy.deepcopy(event)
        elif event_name == "STOP":
            self._trial_stopped_agents.add(agent_id)

    def handle_log_end(
        self,
        agent_id: int,
        records: int,
        ok: bool,
        sequence_id: str | None = None,
        trial: int | None = None,
    ) -> None:
        if not self._matches_upload(sequence_id):
            return
        self._trial_log_end[agent_id] = {"records": records, "ok": ok, "at": _utc_iso(), "trial": trial}
        self._state["log_end_agents"] = sorted(self._trial_log_end)
        self._save_status()
        if not ok:
            self._trial_failed = True
            self._set_status(
                "error",
                error=f"agent {agent_id} log upload failed",
                message="一括ログ転送に失敗しました",
            )
            self._send_to_targets("SEQ_ABORT")
            if self._task is not None and not self._task.done():
                self._task.cancel()

    def handle_session_merged(
        self, session_id: str | None, merged_path: str, context: dict[str, Any] | None
    ) -> None:
        if not context or self._plan is None:
            return
        sequence_id = str(context.get("sequence_id", ""))
        if not self._matches_upload(sequence_id):
            return
        expected = set(self._plan["agent_ids"])
        if set(self._trial_log_end) != expected or any(
            not bool(item.get("ok")) for item in self._trial_log_end.values()
        ):
            return

        self._state["final_upload"] = {
            "session_id": session_id,
            "merged_csv": str(merged_path),
            "log_end": copy.deepcopy(self._trial_log_end),
            "upload_done_agents": sorted(self._trial_upload_done),
            "merged_at": _utc_iso(),
        }
        self._state["last_merged_session_id"] = session_id
        self._state["last_merged_csv"] = str(merged_path)
        self._save_status()
        if self._sequence_dir is not None and session_id:
            session_dir = Path(merged_path).parent
            for name in ("sequence_plan.json", "sequence_status.json"):
                src = self._sequence_dir / name
                if src.exists():
                    try:
                        shutil.copy2(src, session_dir / name)
                    except OSError as exc:
                        self._append_event({"event": "COPY_SEQUENCE_FILE_ERROR", "file": name, "error": str(exc)})
        if self._merged_event is not None:
            self._merged_event.set()

    async def trigger_upload(self) -> dict[str, Any]:
        if self._state.get("status") != "awaiting_upload":
            raise SequenceValidationError(
                "ログのアップロードは予定時刻経過後（awaiting_upload）にのみ開始できます"
            )
        self._trial_log_end.clear()
        self._trial_upload_done.clear()
        self._merged_event = asyncio.Event()
        sequence_id = str(self._state["sequence_id"])
        upload_context = self._sequence_context(sequence_id)
        upload_context["trial_results"] = copy.deepcopy(self._state.get("trial_results", []))
        self.log_receiver.set_sequence_context(upload_context)
        self._set_status(
            "waiting_for_logs",
            message="STOPを送信しました。両機の一括ログアップロード・マージを待っています",
        )
        self._send_stop_burst()
        return self.get_status()

    def _emergency_stop_robots(self) -> None:
        """Best-effort STOP to sequence targets; never raises."""
        try:
            self._send_to_targets("SEQ_ABORT")
        except Exception as exc:
            print(f"[SEQ] SEQ_ABORT send failed: {exc}", flush=True)
        try:
            self._send_stop_burst()
        except Exception as exc:
            print(f"[SEQ] STOP burst failed: {exc}", flush=True)

    async def stop(self) -> dict[str, Any]:
        status = self._state.get("status")
        # Always try to stop robots first — even if status bookkeeping fails later.
        if status in self.ACTIVE_STATES | {"error", "running_autonomous"}:
            self._stop_requested = True
            self._emergency_stop_robots()
        if status == "awaiting_upload":
            return self.get_status()
        if status == "error":
            if self._task is not None and not self._task.done():
                self._task.cancel()
            self._set_status("stopped", stopped_at=_utc_iso(), message="エラー状態を解除しました")
            return self.get_status()
        if status not in self.ACTIVE_STATES:
            return self.get_status()
        self._set_status("stopping", message="停止信号を両機へ送信しています")
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
        self._set_status("stopped", stopped_at=_utc_iso(), message="自動シーケンスを停止しました")
        return self.get_status()

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
