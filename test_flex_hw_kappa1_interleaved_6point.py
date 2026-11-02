"""Validate flex κ=1 interleaved six-point fleet presets."""
from __future__ import annotations

import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from experiment_sequence import ExperimentSequenceController
from flex_hw_kappa1_interleaved_6point_plan import PRESET_IDS, PRESET_ORDER, activate_preset, read_preset
import server


class FlexKappa1InterleavedPlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sequence = self.root / "sequence.yaml"
        self.params = self.root / "params.yaml"
        self.controller = ExperimentSequenceController(
            self.sequence,
            self.root / "experiments",
            Mock(),
            lambda ids: [
                {
                    "id": aid,
                    "ip": "192.168.1.1",
                    "paused": 1,
                    "params": 1,
                    "src": 0,
                    "zmode": 1,
                    "mode": "selectable",
                    "flex_used": 1,
                    "kappa": 1.0,
                    "omega": 3 * math.pi,
                }
                for aid in ids
            ],
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_presets_load_and_log_budget(self):
        for preset in PRESET_IDS:
            with self.subTest(preset=preset):
                seq, par = read_preset(preset)
                zmode_code = 1 if par.get("z_mode") == "zopt" else 2
                self.controller.target_provider = lambda ids, zc=zmode_code: [
                    {
                        "id": aid,
                        "ip": "192.168.1.1",
                        "paused": 1,
                        "params": 1,
                        "src": 0,
                        "zmode": zc,
                        "mode": "selectable",
                        "flex_used": 1,
                        "kappa": 1.0,
                        "omega": 3 * math.pi,
                    }
                    for aid in ids
                ]
                activate_preset(preset, self.sequence, self.params, self.root / "backups")
                plan = self.controller._load_plan(None)
                self.assertEqual(plan["research_plan"]["id"], "flex_hw_kappa1_interleaved_6point_v1")
                self.assertEqual(len(plan["phase_labels"]), 6)
                self.assertEqual(plan["trial_duration_ms"], 120000)
                self.assertEqual(plan["kappa"], 1.0)
                self.assertEqual(plan["sequence_log_hz"], 25)
                self.assertEqual(plan["sequence_log_hz"] * 6 * 120, 18000)
                self.controller._validate_targets(plan["agent_ids"], plan)
                arm = self.controller.build_arm_command(
                    "ps000000_000000",
                    1,
                    plan["phase_labels"][0],
                    plan["phase_differences_rad"][0],
                    6,
                    12,
                    plan,
                )
                self.assertIn("phase_offsets:", arm)

    def test_preset_order(self):
        self.assertEqual(len(PRESET_ORDER), 6)

    def test_fm_ota_profile_selects_flex_fb(self):
        activate_preset("80mm_zopt", self.sequence, self.params, self.root / "backups")
        prev = os.environ.get("SEQUENCE_CONFIG_PATH")
        os.environ["SEQUENCE_CONFIG_PATH"] = str(self.sequence)
        try:
            self.assertTrue(server._flex_hw_sequence_active())
            self.assertEqual(server._pio_ota_env_name(), "ota_flex_fb")
        finally:
            if prev is None:
                os.environ.pop("SEQUENCE_CONFIG_PATH", None)
            else:
                os.environ["SEQUENCE_CONFIG_PATH"] = prev


if __name__ == "__main__":
    unittest.main()
