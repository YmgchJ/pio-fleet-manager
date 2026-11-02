"""Validate flex HW lock fleet presets against ExperimentSequenceController."""
from __future__ import annotations

import math
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from unittest.mock import Mock

from experiment_sequence import ExperimentSequenceController
from flex_hw_plan import PRESET_IDS, activate_preset, read_preset
from sync_params import effective_prc_params, load_sync_params


class FlexHwLockPlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sequence = self.root / "sequence.yaml"
        self.params = self.root / "params.yaml"
        self._target_provider = lambda ids, zmode_code=1: [
            {
                "id": aid,
                "ip": "192.168.1.1",
                "paused": 1,
                "params": 1,
                "src": 0,
                "zmode": zmode_code,
                "mode": "selectable",
                "flex_used": 1,
                "kappa": 5.0,
                "omega": 9.4247779608,
            }
            for aid in ids
        ]
        self.controller = ExperimentSequenceController(
            self.sequence,
            self.root / "experiments",
            Mock(),
            lambda ids: self._target_provider(ids, 1),
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_all_presets_load_and_budget(self):
        for preset in PRESET_IDS:
            with self.subTest(preset=preset):
                seq, par = read_preset(preset)
                zmode_code = 1 if par.get("z_mode") == "zopt" else 2
                self.controller.target_provider = lambda ids, zc=zmode_code: self._target_provider(ids, zc)
                activate_preset(preset, self.sequence, self.params, self.root / "backups")
                plan = self.controller._load_plan(None)
                self.assertEqual(plan["research_plan"]["id"], "flex_hw_lock_v1")
                self.assertEqual(plan["sequence_type"], "delta_omega_sweep")
                self.assertFalse(plan["current_only"])
                self.assertEqual(plan["feedback_source"], "flex")
                self.assertEqual(plan["kappa"], 5.0)
                self.assertEqual(len(plan["phase_labels"]), 13)
                self.assertEqual(plan["sequence_log_hz"], 25)
                self.assertEqual(plan["sequence_log_hz"] * 13 * 60, 19500)
                self.assertAlmostEqual(plan["omega_base_rad_s"], 3 * math.pi)
                self.controller._validate_targets(plan["agent_ids"], plan)
                eff = effective_prc_params(load_sync_params(self.params))
                profile_key = "zopt_profile" if par["z_mode"] == "zopt" else "zsin_profile"
                self.assertEqual(eff["_prc_profile_meta"]["profile_id"], par[profile_key])

    def test_preset_order_matches_runbook(self):
        from flex_hw_plan import PRESET_ORDER

        self.assertEqual(
            PRESET_ORDER,
            ("60mm_zopt", "60mm_zsin", "40mm_zopt", "40mm_zsin", "80mm_zopt", "80mm_zsin"),
        )


if __name__ == "__main__":
    unittest.main()
