"""Offline presets and log-recovery progress. This module never sends commands."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "fleet" / "paddle_sync_minimal"
PLAN_ID = "paddle_sync_minimal_v1"
PRESET_IDS = tuple(f"{size}mm_A{repeat}" for size in (40, 60, 80) for repeat in (1, 2, 3)) + (
    "80mm_D1",
    "80mm_E1",
    "80mm_E2",
    "80mm_E3",
    "40mm_E2",
    "40mm_E3",
    "40mm_F1",
    "40mm_G1",
    "40mm_G2",
    "40mm_G3",
    "60mm_H1",
    "60mm_H2",
    "60mm_H3",
    "60mm_J1",
    "60mm_J2",
    "60mm_J3",
)
PHASE_LOCK_OUTER_PRESETS = (
    "40mm_G1",
    "40mm_G2",
    "40mm_G3",
    "60mm_J1",
    "60mm_J2",
    "60mm_J3",
)
PHASE_LOCK_INNER_PRESETS = ("60mm_H1", "60mm_H2", "60mm_H3")
OPEN_LOOP_MID_PRESETS = ("60mm_A1", "60mm_A2", "60mm_A3")


def read_preset(preset_id: str, catalog: Path = CATALOG) -> tuple[dict, dict]:
    if preset_id not in PRESET_IDS:
        raise ValueError("未登録の条件です。A1/A2/A3またはA/D/E/Fの登録条件から選んでください")
    sequence = yaml.safe_load((catalog / f"{preset_id}.yaml").read_text())
    params_name = {
        "80mm_E1": "closed_loop_alpha92_params.yaml",
        "80mm_E2": "closed_loop_alpha92_kappa1_params.yaml",
        "80mm_E3": "closed_loop_alpha92_kappa1_params.yaml",
        "40mm_E2": "closed_loop_alpha92_kappa1_params.yaml",
        "40mm_E3": "closed_loop_alpha92_kappa1_params.yaml",
        "40mm_F1": "closed_loop_alpha92_kappa1_params.yaml",
        "40mm_G1": "phase_lock_std_cos_params.yaml",
        "40mm_G2": "phase_lock_std_zopt_params.yaml",
        "40mm_G3": "phase_lock_std_zsin_params.yaml",
        "60mm_H1": "phase_lock_mid_cos_params.yaml",
        "60mm_H2": "phase_lock_mid_zopt_params.yaml",
        "60mm_H3": "phase_lock_mid_zsin_params.yaml",
        "60mm_J1": "phase_lock_mid_cos_params.yaml",
        "60mm_J2": "phase_lock_mid_zopt_params.yaml",
        "60mm_J3": "phase_lock_mid_zsin_params.yaml",
        "60mm_A1": "mid_open_loop_params.yaml",
        "60mm_A2": "mid_open_loop_params.yaml",
        "60mm_A3": "mid_open_loop_params.yaml",
    }.get(preset_id, "open_loop_params.yaml")
    params = yaml.safe_load((catalog / params_name).read_text())
    return sequence, params


def activate_preset(preset_id: str, sequence_path: Path, params_path: Path,
                    backup_root: Path, catalog: Path = CATALOG) -> Path:
    """Back up both files, then replace; restore both if either replacement fails."""
    sequence, params = read_preset(preset_id, catalog)
    # Use the active parameter file for both REQUEST_PARAMS and plan validation.
    try:
        sequence["parameter_file"] = str(params_path.resolve().relative_to(ROOT))
    except ValueError:
        sequence["parameter_file"] = str(params_path.resolve())
    backup = backup_root / f"{time.time_ns()}_{preset_id}"
    backup.mkdir(parents=True, exist_ok=False)
    paths = (sequence_path, params_path)
    originals = [p.read_bytes() if p.exists() else None for p in paths]
    for name, content in zip(("sequence.yaml", "params.yaml"), originals):
        if content is not None:
            (backup / name).write_bytes(content)
    staged = []
    try:
        for path, data in zip(paths, (sequence, params)):
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                             encoding="utf-8") as handle:
                staged.append(Path(handle.name))
                yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
        for temp, path in zip(staged, paths):
            os.replace(temp, path)
    except Exception:
        for path, content in zip(paths, originals):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    finally:
        for temp in staged:
            temp.unlink(missing_ok=True)
    return backup


def recovered_presets(experiments_dir: Path, canonical_manifest: Path | None = None) -> dict[str, list[str]]:
    """A timer or single-agent upload must never count as paired log recovery."""
    recovered: dict[str, list[str]] = {}
    for plan_path in experiments_dir.glob("sequence_*/sequence_plan.json"):
        try:
            snapshot = json.loads(plan_path.read_text())
            plan = snapshot["plan"]
            research = plan.get("research_plan", {})
            preset = research.get("preset_id")
            if research.get("id") != PLAN_ID or preset not in PRESET_IDS:
                continue
            status = json.loads(plan_path.with_name("sequence_status.json").read_text())
            upload = status.get("final_upload", {})
            ends = upload.get("log_end", {})
            agents = plan["agent_ids"]
            if status.get("status") != "completed" or len(agents) != 2:
                continue
            if any(not ends.get(str(a), {}).get("ok") or
                   int(ends[str(a)].get("records", 0)) <= 0 for a in agents):
                continue
            merged = Path(upload.get("merged_csv", ""))
            if not merged.is_file() or merged.stat().st_size == 0:
                continue
            recovered.setdefault(preset, []).append(str(upload.get("session_id", "")))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Recovered canonical sessions can be complete even if the original sequence
    # task was restarted before it persisted "completed". Check the registry
    # against actual metadata instead of losing their recovery badge.
    canonical_manifest = canonical_manifest or ROOT / "reports/impulsive_interaction/20261015_paddle_sync_minimal/canonical_sessions.json"
    try:
        registry = json.loads(canonical_manifest.read_text())
        for preset, item in registry.get("sessions", {}).items():
            if item.get("status") != "canonical" or preset not in PRESET_IDS:
                continue
            sid = item["session_id"]
            if Path(sid).name != sid:
                continue
            folder = experiments_dir / sid
            meta = json.loads((folder / "session_meta.json").read_text())
            seq = meta.get("sequence", {})
            merged = folder / f"merged_{sid}.csv"
            if seq.get("sequence_id") != item["sequence_id"] or seq.get("research_plan", {}).get("preset_id") != preset:
                continue
            if not merged.is_file() or merged.stat().st_size == 0:
                continue
            if any(not meta.get("agents", {}).get(str(a), {}).get("log_end", {}).get("ok")
                   or meta["agents"][str(a)]["log_end"].get("records") != item["log_end_records"][str(a)]
                   or meta["agents"][str(a)]["log_end"].get("sequence_id") != item["sequence_id"]
                   for a in (12,13)):
                continue
            if sid not in recovered.setdefault(preset, []):
                recovered[preset].append(sid)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return recovered
