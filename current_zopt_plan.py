"""Offline presets for current-FB Z_opt single vs trial-mean lock compare (current_zopt_lock_v1)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import yaml

from experiment_sequence import CURRENT_ZOPT_13POINT_PLAN_ID

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "fleet" / "current_zopt_lock_compare_13point"
PLAN_ID = CURRENT_ZOPT_13POINT_PLAN_ID
PRESET_IDS = tuple(f"{mm}mm_{kind}_zopt_{batch}"
                   for mm in (40,60,80) for kind in ("single","mean") for batch in ("base7","extra6"))
PRESET_ORDER = PRESET_IDS


def read_preset(preset_id: str, catalog: Path = CATALOG) -> tuple[dict, dict]:
    if preset_id not in PRESET_IDS:
        raise ValueError(f"未登録の電流 Z 比較条件です: {preset_id}")
    sequence = yaml.safe_load((catalog / f"{preset_id}.yaml").read_text())
    params = yaml.safe_load((catalog / f"params_{preset_id}.yaml").read_text())
    return sequence, params


def activate_preset(
    preset_id: str,
    sequence_path: Path,
    params_path: Path,
    backup_root: Path,
    catalog: Path = CATALOG,
) -> Path:
    sequence, params = read_preset(preset_id, catalog)
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
            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
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


def recovered_presets(experiments_dir: Path) -> dict[str, list[str]]:
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
            # Completion means successful log recovery; nominal sample counts
            # are a separate data-quality check (the sampler is not exact-rate).
            if any(
                not ends.get(str(a), {}).get("ok") or int(ends[str(a)].get("records", 0)) <= 0
                for a in agents
            ):
                continue
            merged = Path(upload.get("merged_csv", ""))
            if not merged.is_file() or merged.stat().st_size == 0:
                continue
            recovered.setdefault(preset, []).append(str(upload.get("session_id", "")))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return recovered
