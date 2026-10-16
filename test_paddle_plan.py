"""Offline tests; never call a robot socket or sequence START."""
import asyncio
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from experiment_sequence import ExperimentSequenceController, SequenceValidationError
from paddle_plan import PRESET_IDS, PLAN_ID, activate_preset, read_preset, recovered_presets


class PaddlePlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sequence = self.root / "active.yaml"
        self.params = self.root / "params.yaml"
        activate_preset("40mm_A1", self.sequence, self.params, self.root / "backups")
        self.controller = ExperimentSequenceController(self.sequence, self.root, Mock(), lambda ids: [])

    def edit(self, path, **updates):
        data = yaml.safe_load(path.read_text())
        data.update(updates)
        path.write_text(yaml.safe_dump(data))

    def test_all_six_presets_fixed_phase_and_budget(self):
        for preset in PRESET_IDS:
            with self.subTest(preset=preset):
                activate_preset(preset, self.sequence, self.params, self.root / "backups")
                plan = self.controller._load_plan(None)
                self.assertEqual(plan["kappa"], 0)
                self.assertEqual(plan["sequence_type"], "phase_sweep")
                self.assertEqual(plan["sequence_log_hz"] * len(plan["phase_labels"]) * 30, 12000)
                self.assertEqual(sorted(round(v / math.pi * 4) for v in plan["phase_differences_rad"]), list(range(8)))
                self.assertEqual(plan["research_plan"]["preset_id"], preset)
                ui = self.controller.get_status()
                self.assertIn(preset[:2] + " mm", ui["ui_title"])
                self.assertIn("開ループ", ui["ui_title"])

    def test_reject_unequal_frequencies(self):
        self.edit(self.params, omega_by_agent={13: 3*math.pi, 12: 2.9*math.pi})
        with self.assertRaises(SequenceValidationError):
            self.controller._load_plan(None)

    def test_reject_feedback_in_open_loop(self):
        self.edit(self.sequence, kappa=5)
        self.edit(self.params, kappa=5)
        with self.assertRaises(SequenceValidationError):
            self.controller._load_plan(None)

    def test_buffer_overflow_and_invalid_window(self):
        self.edit(self.sequence, trial_duration_ms=60000)
        with self.assertRaises(SequenceValidationError):
            self.controller._load_plan(None)
        self.edit(self.sequence, trial_duration_ms=30000, analysis_window_start_s=35)
        with self.assertRaises(SequenceValidationError):
            self.controller._load_plan(None)

    def test_backup_and_invalid_preset(self):
        before = self.sequence.read_bytes()
        backup = activate_preset("80mm_A1", self.sequence, self.params, self.root / "backups")
        self.assertEqual((backup / "sequence.yaml").read_bytes(), before)
        after = self.sequence.read_bytes()
        with self.assertRaises(ValueError):
            activate_preset("../../outside", self.sequence, self.params, self.root / "backups")
        self.assertEqual(self.sequence.read_bytes(), after)

    def test_arm_packet_contains_all_eight_conditions_without_io(self):
        self.controller._plan = self.controller._load_plan(None)
        self.controller._targets = [{"id": 13}, {"id": 12}]
        plan = self.controller._plan
        with patch.object(self.controller, "_send_one") as send:
            self.controller._send_arm_commands("test", 1, plan["phase_labels"][0],
                                               plan["phase_differences_rad"][0], 8)
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            target, packet = call.args
            fields = dict(part.split(":", 1) for part in packet.split(",")[1:])
            self.assertEqual(fields["kappa"], "0.000000")
            self.assertEqual(fields["total_trials"], "8")
            self.assertLess(len(fields["conditions"]), 192)
            self.assertLess(len(fields["phase_offsets"]), 192)
            self.assertEqual(len(fields["conditions"].split(";")), 8)
            expected = [0.0]*8 if target["id"] == 12 else [-v for v in plan["phase_differences_rad"]]
            for value, phase in zip(fields["phase_offsets"].split(";"), expected):
                self.assertAlmostEqual(float(value), phase, places=5)

    def test_failed_second_replace_restores_both(self):
        import paddle_plan
        before = [p.read_bytes() for p in (self.sequence, self.params)]
        real_replace = paddle_plan.os.replace
        def replace(src, dst):
            if Path(dst) == self.params:
                raise OSError("simulated disk failure")
            return real_replace(src, dst)
        with patch("paddle_plan.os.replace", side_effect=replace):
            with self.assertRaises(OSError):
                activate_preset("80mm_A2", self.sequence, self.params, self.root / "backups")
        self.assertEqual([p.read_bytes() for p in (self.sequence, self.params)], before)

    def test_recovery_requires_both_agents_and_actual_file(self):
        folder = self.root / "sequence_test"
        folder.mkdir()
        plan = self.controller._load_plan(None)
        (folder / "sequence_plan.json").write_text(json.dumps({"plan": plan}))
        merged = folder / "merged.csv"
        merged.write_text("agent_id,a0\n13,0\n12,0\n")
        status = {"status": "completed", "final_upload": {"merged_csv": str(merged),
                  "session_id": "test", "log_end": {"13": {"ok": True, "records": 12000}}}}
        status_path = folder / "sequence_status.json"
        status_path.write_text(json.dumps(status))
        self.assertEqual(recovered_presets(self.root), {})
        status["final_upload"]["log_end"]["12"] = {"ok": True, "records": 12000}
        status_path.write_text(json.dumps(status))
        self.assertEqual(recovered_presets(self.root), {"40mm_A1": ["test"]})
        merged.unlink()
        self.assertEqual(recovered_presets(self.root), {})

    async def test_upload_keeps_existing_waiter(self):
        self.controller._plan = self.controller._load_plan(None)
        self.controller._state = {"status": "awaiting_upload", "sequence_id": "test"}
        self.controller._merged_event = asyncio.Event()
        event = self.controller._merged_event
        waiter = asyncio.create_task(event.wait())
        with patch.object(self.controller, "_send_stop_burst"):
            await self.controller.trigger_upload()
        self.assertIs(self.controller._merged_event, event)
        self.controller._merged_event.set()
        await asyncio.wait_for(waiter, timeout=0.1)

    async def test_busy_endpoint_cannot_change_config(self):
        import server
        self.controller._state = {"status": "awaiting_upload"}
        before = self.sequence.read_bytes()
        with patch.object(server, "sequence_controller", self.controller):
            with self.assertRaises(server.HTTPException) as raised:
                await server.select_paddle_preset(server.PaddlePresetRequest(preset_id="80mm_A1"))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.sequence.read_bytes(), before)

    def test_preflight_rejects_legacy_processing(self):
        plan = self.controller._load_plan(None)
        robots = [dict(id=a, ip="127.0.0.1", paused=1, params=1, src=1, zmode=0,
                       mode="current_only", flex_used=0, kappa=0, omega=3*math.pi,
                       interaction_path="legacy_500hz_trimmed_mean") for a in (13, 12)]
        self.controller.target_provider = lambda ids: robots
        with self.assertRaisesRegex(SequenceValidationError, "電流前処理"):
            self.controller._validate_targets([13, 12], plan)
        for robot in robots:
            robot["interaction_path"] = "current_50hz_ma5_rolling_p10_p90"
        self.assertEqual(len(self.controller._validate_targets([13, 12], plan)), 2)

    def test_status_processing_path_reaches_preflight(self):
        import server
        message = ("STATUS,id:12,paused:1,params:1,src:1,zmode:0,flex_used:0,"
                   "mode:current_only,kappa:0,omega:9.425,"
                   "interaction_path:current_50hz_ma5_rolling_p10_p90,fw:test")
        with patch.object(server, "update_agent_ip"), patch.object(server, "log_receiver", Mock()), \
                patch.object(server, "broadcast_telemetry"), patch.object(server, "agent_live_state", {}):
            server.UdpDiscoveryProtocol().datagram_received(message.encode(), ("127.0.0.1", 5012))
            self.assertEqual(server.agent_live_state[12]["interaction_path"],
                             "current_50hz_ma5_rolling_p10_p90")


if __name__ == "__main__":
    unittest.main()
