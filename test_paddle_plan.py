"""Offline tests; never call a robot socket or sequence START."""
import asyncio
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from experiment_sequence import (
    ExperimentSequenceController,
    ROBOT_UDP_COMMAND_MAX_BYTES_LEGACY,
    SequenceValidationError,
)
from paddle_plan import (
    OPEN_LOOP_MID_PRESETS,
    PHASE_LOCK_INNER_PRESETS,
    PHASE_LOCK_OUTER_PRESETS,
    PLAN_ID,
    PRESET_IDS,
    activate_preset,
    read_preset,
    recovered_presets,
)


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

    def test_all_presets_fixed_phase_and_budget(self):
        for preset in PRESET_IDS:
            with self.subTest(preset=preset):
                activate_preset(preset, self.sequence, self.params, self.root / "backups")
                plan = self.controller._load_plan(None)
                if preset in PHASE_LOCK_OUTER_PRESETS:
                    self.assertEqual(plan["sequence_type"], "delta_omega_sweep")
                    self.assertEqual(plan["kappa"], 5.0)
                    self.assertEqual(len(plan["phase_labels"]), 6)
                    self.assertEqual(plan["trial_duration_ms"], 60000)
                    self.assertEqual(plan["feedback_start_ms"], 10000)
                    self.assertEqual(plan["inter_trial_pause_ms"], 20000)
                    self.assertAlmostEqual(plan["omega_base_rad_s"], 9.4247779608)
                    self.assertEqual(plan["sequence_log_hz"] * 6 * 60, 18000)
                    continue
                if preset in PHASE_LOCK_INNER_PRESETS:
                    self.assertEqual(plan["sequence_type"], "delta_omega_sweep")
                    self.assertEqual(plan["kappa"], 5.0)
                    self.assertEqual(len(plan["phase_labels"]), 7)
                    self.assertEqual(plan["trial_duration_ms"], 60000)
                    self.assertEqual(plan["feedback_start_ms"], 10000)
                    self.assertEqual(plan["inter_trial_pause_ms"], 20000)
                    self.assertEqual(plan["sequence_log_hz"], 50)
                    self.assertEqual(plan["paddle"], "mid")
                    self.assertAlmostEqual(plan["omega_base_rad_s"], 9.4247779608)
                    self.assertEqual(plan["sequence_log_hz"] * 7 * 60, 21000)
                    arm = self.controller.build_arm_command(
                        "ps000000_000000",
                        1,
                        plan["phase_labels"][0],
                        plan["phase_differences_rad"][0],
                        len(plan["phase_labels"]),
                        plan["agent_ids"][0],
                        plan,
                    )
                    self.assertLessEqual(
                        len(arm.encode("utf-8")),
                        ROBOT_UDP_COMMAND_MAX_BYTES_LEGACY,
                        msg=f"{preset} SEQ_ARM must fit legacy 512 B UDP buffer",
                    )
                    continue
                if preset in OPEN_LOOP_MID_PRESETS:
                    self.assertEqual(plan["sequence_type"], "delta_omega_sweep")
                    self.assertEqual(plan["kappa"], 0.0)
                    self.assertEqual(plan["paddle"], "mid")
                    n_trials = 3 if preset == "60mm_A1" else 2 if preset == "60mm_A2" else 1
                    self.assertEqual(len(plan["phase_labels"]), n_trials)
                    self.assertEqual(plan["trial_duration_ms"], 140000)
                    self.assertEqual(plan["inter_trial_pause_ms"], 20000)
                    self.assertEqual(plan["sequence_log_hz"], 50)
                    self.assertAlmostEqual(plan["omega_base_rad_s"], 9.4247779608)
                    self.assertEqual(
                        plan["sequence_log_hz"] * n_trials * 140,
                        50 * n_trials * 140,
                    )
                    continue
                self.assertEqual(plan["kappa"], 1 if preset in ("80mm_E2", "80mm_E3", "40mm_E2", "40mm_E3", "40mm_F1") else 5 if preset == "80mm_E1" else 0)
                self.assertEqual(plan["sequence_type"], "phase_sweep")
                expected = [2] if preset == '40mm_F1' else [2,6] if preset in ('80mm_E1', '80mm_E2', '80mm_E3', '40mm_E2', '40mm_E3') else [0,2,4,6,4,4] if preset == '80mm_D1' else list(range(8))
                duration = 20 if preset == "40mm_F1" else 110 if preset in ("80mm_E2", "80mm_E3", "40mm_E2", "40mm_E3", "40mm_F1") else 90 if preset == "80mm_E1" else 30
                records = plan["sequence_log_hz"] * len(plan["phase_labels"]) * duration
                self.assertEqual(records, (1000 if preset == "40mm_F1" else 5500 if preset in ("80mm_E2", "80mm_E3", "40mm_E2", "40mm_E3", "40mm_F1") else 4500 if preset == "80mm_E1" else 1500)*len(expected))
                self.assertLessEqual(records, 21000)
                if preset in ("80mm_E1", "80mm_E2", "80mm_E3", "40mm_E2", "40mm_E3", "40mm_F1"):
                    params = plan["sync_params_snapshot"]
                    self.assertEqual(params["prc_n"], 1)
                    self.assertAlmostEqual(params["prc_a1"], math.cos(math.radians(92)))
                    self.assertAlmostEqual(params["prc_b1"], -math.sin(math.radians(92)))
                    self.assertEqual(plan["feedback_start_ms"], 10000)
                actual = [round(v / math.pi * 4) for v in plan["phase_differences_rad"]]
                self.assertEqual(actual if preset == '80mm_D1' else sorted(actual), expected)
                self.assertEqual(plan["research_plan"]["preset_id"], preset)
                ui = self.controller.get_status()
                self.assertIn(preset[:2] + " mm", ui["ui_title"])
                if preset not in ("80mm_E1", "80mm_E2", "80mm_E3", "40mm_E2", "40mm_E3", "40mm_F1"):
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

    def test_F1_requires_capability_and_reads_back_coefficients(self):
        activate_preset("40mm_F1", self.sequence, self.params, self.root / "backups")
        plan = self.controller._load_plan(None)
        self.assertTrue(plan["diagnostic"])
        self.assertEqual(plan["trial_duration_ms"], 20000)
        self.controller._plan = plan
        self.controller._targets = [{"id": 12}, {"id": 13}]
        with patch.object(self.controller, "_send_one") as send:
            self.controller._send_arm_commands("test", 1, "delta_090deg", math.pi/2, 1)
        self.assertIn("diagnostic:1", send.call_args.args[1].split(","))
        # Fresh status required; an ordinary FW must not pass diagnostic preflight.
        statuses=[dict(id=a,ip="127.0.0.1",paused=1,params=1,src=1,zmode=1,
                       mode="current_only",interaction_path="current_50hz_ma5_rolling_p10_p90",
                       flex_used=0,kappa=1,omega=9.425) for a in [12,13]]
        self.controller.target_provider=lambda ids: statuses
        with self.assertRaises(SequenceValidationError):
            self.controller._validate_targets([12,13],plan)
        self.edit(self.sequence, trial_duration_ms=30000, analysis_window_end_s=30)
        with self.assertRaises(SequenceValidationError): self.controller._load_plan(None)

    def test_40mm_uses_identical_controller_and_timing(self):
        big, bp = read_preset("80mm_E2")
        small, sp = read_preset("40mm_E2")
        self.assertEqual(bp, sp)
        for key in big:
            if key not in ("name", "paddle", "research_plan"):
                self.assertEqual(big[key], small[key], key)
        self.assertEqual(small["paddle"], "standard")
        self.assertEqual(small["research_plan"]["paddle_mm"], 40)

    def test_E3_replication_has_identical_control_and_timing(self):
        first, params_first = read_preset("80mm_E2")
        repeat, params_repeat = read_preset("80mm_E3")
        self.assertEqual(params_first, params_repeat)
        for key in first:
            if key not in ("name", "research_plan"):
                self.assertEqual(first[key], repeat[key], key)
        self.assertEqual(repeat["research_plan"]["replicates_preset"], "80mm_E2")

    def test_E2_gain_is_specific_and_arm_packet_matches(self):
        activate_preset("80mm_E2", self.sequence, self.params, self.root / "backups")
        plan = self.controller._load_plan(None)
        self.controller._plan = plan
        self.controller._targets = [{"id": 12}, {"id": 13}]
        with patch.object(self.controller, "_send_one") as send:
            self.controller._send_arm_commands("test", 1, plan["phase_labels"][0],
                                               plan["phase_differences_rad"][0], 2)
        for call in send.call_args_list:
            fields = dict(part.split(":", 1) for part in call.args[1].split(",")[1:])
            self.assertEqual(float(fields["kappa"]), 1.0)
            self.assertEqual(fields["duration_ms"], "110000")
            self.assertEqual(fields["feedback_start_ms"], "10000")
        self.edit(self.sequence, kappa=5)
        with self.assertRaises(SequenceValidationError):
            self.controller._load_plan(None)
        activate_preset("80mm_E1", self.sequence, self.params, self.root / "backups")
        self.edit(self.sequence, kappa=1)
        self.edit(self.params, kappa=1)
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
            self.assertEqual(float(fields["kappa"]), 0.0)
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
        with self.assertRaisesRegex(SequenceValidationError, "移動P10/P90は使用禁止"):
            self.controller._validate_targets([13, 12], plan)
        for robot in robots:
            robot["interaction_path"] = "current_50hz_ma5_fixed_affine"
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

    def test_packet_clock_saved_and_does_not_leak_to_next_session(self):
        from session_metadata import SessionMetadataRecorder
        recorder = SessionMetadataRecorder()
        recorder.on_session_start('first', self.root)
        recorder.record_packet_clock(12, 123456, 1000.125, 10, 20, 72)
        path = recorder.finalize(self.root, 'first', self.root/'merged.csv', [])
        meta = json.loads(path.read_text())
        self.assertEqual(meta['agents']['12']['packet_clock_samples'][0]['send_micros32'],123456)
        recorder.reset()
        recorder.on_session_start('second', self.root)
        recorder.record_packet_clock(13, 543210, 2000.125, 30, 40, 72)
        path = recorder.finalize(self.root,'second',self.root/'second.csv',[])
        meta = json.loads(path.read_text())
        self.assertNotIn('12',meta['agents'])
        self.assertEqual(len(meta['agents']['13']['packet_clock_samples']),1)

    def test_canonical_recovery_matches_both_log_ends_and_sequence(self):
        folder=self.root/'canonical';folder.mkdir()
        (folder/'merged_canonical.csv').write_text('agent_id\n12\n13\n')
        item=dict(status='canonical',session_id='canonical',sequence_id='seq',log_end_records={'12':100,'13':100})
        registry=self.root/'registry.json';registry.write_text(json.dumps({'sessions':{'40mm_A1':item}}))
        meta=dict(sequence=dict(sequence_id='seq',research_plan=dict(preset_id='40mm_A1')),
                  agents={str(a):dict(log_end=dict(ok=True,records=100,sequence_id='seq')) for a in (12,13)})
        path=folder/'session_meta.json';path.write_text(json.dumps(meta))
        self.assertEqual(recovered_presets(self.root,registry),{'40mm_A1':['canonical']})
        meta['agents']['13']['log_end']['sequence_id']='mixed'
        path.write_text(json.dumps(meta))
        self.assertEqual(recovered_presets(self.root,registry),{})


if __name__ == "__main__":
    unittest.main()
