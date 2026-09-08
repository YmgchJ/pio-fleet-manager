"""Build REQUEST_PARAMS responses for the current-only sync experiment."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARAMS_PATH = ROOT / "fleet" / "sync_experiment_params.yaml"
PRC_PROFILES_DIR = ROOT / "fleet" / "zopt_profiles"

_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None

# Interaction sensor s. Must match SYNC_SRC_* in firmware/pico_robot/include/sync_experiment.h.
FEEDBACK_SOURCES = {"flex": 0, "current": 1}
# Keep zopt=1 for backward compatibility with already-served payloads.
Z_MODES = {"cos": 0, "zopt": 1, "zsin": 2}


def invalidate_sync_params_cache() -> None:
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = None


def load_sync_params(path: Path | None = None) -> dict[str, Any]:
    global _cache, _cache_mtime
    params_path = path or DEFAULT_PARAMS_PATH
    try:
        mtime = params_path.stat().st_mtime
    except OSError:
        mtime = None
    if path is None and _cache is not None and mtime == _cache_mtime:
        return _cache
    with open(params_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if path is None:
        _cache = data
        _cache_mtime = mtime
    return data


def load_prc_profile(profile_id: str) -> dict[str, Any]:
    path = PRC_PROFILES_DIR / f"{profile_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"PRC profile not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_zopt_profile(profile_id: str) -> dict[str, Any]:
    """Backward-compatible alias for callers using the old profile name."""
    return load_prc_profile(profile_id)


def effective_prc_params(params: dict[str, Any]) -> dict[str, Any]:
    """Merge the selected PRC mode/profile into Fourier coefficients."""
    out = dict(params)
    z_mode = str(out.get("z_mode", "cos")).lower()
    if z_mode in ("zopt", "zsin"):
        profile_key = "zopt_profile" if z_mode == "zopt" else "zsin_profile"
        default_profile = "big_current_E" if z_mode == "zopt" else "big_current_E_zsin"
        profile_id = str(out.get(profile_key, default_profile))
        profile = load_prc_profile(profile_id)
        prc_n = int(profile.get("prc_n", 10))
        out["prc_n"] = prc_n
        out["prc_a0"] = float(profile.get("prc_a0", 0.0))
        out["prc_b0"] = float(profile.get("prc_b0", 0.0))
        for n in range(1, prc_n + 1):
            out[f"prc_a{n}"] = float(profile.get(f"prc_a{n}", 0.0))
            out[f"prc_b{n}"] = float(profile.get(f"prc_b{n}", 0.0))
        out["_prc_profile_meta"] = {
            "profile_id": profile_id,
            "z_mode": z_mode,
            "q_star_deg": profile.get("q_star_deg"),
            "tau_star_deg": profile.get("tau_star_deg"),
            "R_ratio": profile.get("R_ratio"),
            "R_ratio_to_cos": profile.get("R_ratio_to_cos"),
            "source_session": profile.get("source_session"),
            "source_label": profile.get("source_label"),
        }
        # Preserve the old metadata key for existing session consumers.
        out["_zopt_profile_meta"] = out["_prc_profile_meta"]
    return out


def omega_for_agent(agent_id: int, params: dict[str, Any] | None = None) -> float:
    p = params or load_sync_params()
    by_agent = p.get("omega_by_agent") or {}
    if agent_id in by_agent:
        return float(by_agent[agent_id])
    if agent_id % 2 == 0:
        return float(p.get("default_omega_even", 2.40 * math.pi))
    return float(p.get("default_omega_odd", 2.50 * math.pi))


def build_param_payload(agent_id: int, params: dict[str, Any] | None = None) -> str:
    p = effective_prc_params(params or load_sync_params())
    omega = omega_for_agent(agent_id, p)
    source_name = "current" if bool(p.get("current_only", False)) else str(
        p.get("feedback_source", "flex")
    ).lower()
    z_mode = str(p.get("z_mode", "cos")).lower()
    if z_mode not in Z_MODES:
        z_mode = "cos"
    fields = [
        f"omega:{omega:.6f}",
        f"kappa:{float(p['kappa']):.6f}",
        f"center:{float(p['center']):.1f}",
        f"amplitude:{float(p['amplitude']):.1f}",
        f"stop_id:{int(p.get('stop_id', 0))}",
        f"stop_delay:{int(p.get('stop_delay', 0))}",
        f"feedback_tau:{float(p['feedback_tau']):.6f}",
        f"src:{FEEDBACK_SOURCES[source_name]}",
        f"zmode:{Z_MODES[z_mode]}",
        f"prc_n:{int(p.get('prc_n', 1))}",
    ]
    prc_n = int(p.get("prc_n", 1))
    for n in range(0, prc_n + 1):
        a_key = f"prc_a{n}"
        b_key = f"prc_b{n}"
        fields.append(f"{a_key}:{float(p.get(a_key, 0.0)):.6f}")
        fields.append(f"{b_key}:{float(p.get(b_key, 0.0)):.6f}")
    return ",".join(fields)


def parse_param_payload(payload: str) -> dict[str, Any]:
    """Decode a REQUEST_PARAMS response string into a dict."""
    out: dict[str, Any] = {}
    for part in payload.split(","):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        key = key.strip()
        val = val.strip()
        if key == "src":
            src_int = int(float(val))
            out["src"] = src_int
            out["feedback_source"] = {v: k for k, v in FEEDBACK_SOURCES.items()}.get(src_int, str(src_int))
        elif key == "zmode":
            z_int = int(float(val))
            out["zmode"] = z_int
            out["z_mode"] = {v: k for k, v in Z_MODES.items()}.get(z_int, str(z_int))
        elif key in ("stop_id", "stop_delay", "prc_n"):
            out[key] = int(float(val))
        else:
            try:
                out[key] = float(val)
            except ValueError:
                out[key] = val
    return out
