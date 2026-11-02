import unittest
from unittest.mock import patch
import server


class PrecalibratedTelemetryTests(unittest.TestCase):
    def test_sequence_calibration_fields_keep_types_and_units(self):
        event=server.parse_sequence_event('SEQ_EVENT,agent:12,event:UPLOAD_DONE,dual_cal:cal1,'
            'dual_ready:1,dual_configured:0,dual_fault:0,flex_unit:adc12,flex_c:700.5,'
            'flex_d:80,flex_cal_n:2001,dual_cur_c:150.1,dual_cur_d:241.495512','127.0.0.1')
        self.assertEqual(event['dual_ready'],1)
        self.assertEqual(event['flex_cal_n'],2001)
        self.assertEqual(event['flex_unit'],'adc12')
        self.assertEqual(event['flex_c'],700.5)
        self.assertEqual(event['dual_cal'],'cal1')

    def test_snapshot_parser_retains_both_signals_and_corrections(self):
        data=server.parse_dual_status_message('DUAL_STATUS,id:12,cal:cal1,cal_ready:1,'
            'configured:1,profile:local_joint,fault:0,flex_unit:adc12,flex_c:700,flex_d:80,'
            'cal_n:2001,cur_c:150,cur_d:241.5,s_flex:-0.2,s_current:0.7,'
            'u_flex:0.1,u_current:-0.3,u_total:-0.2,build:test','12','127.0.0.1')
        self.assertEqual(data['dual_profile'],'local_joint')
        self.assertEqual(data['dual_s_flex'],-.2)
        self.assertEqual(data['dual_s_current'],.7)
        self.assertEqual(data['dual_u_total'],-.2)

    def test_ordinary_status_preserves_dual_readback(self):
        protocol=server.UdpDiscoveryProtocol()
        with patch.dict(server.agent_live_state,{},clear=True), \
             patch.object(server.log_receiver,'record_status'), \
             patch.object(server,'broadcast_telemetry'),patch.object(server,'update_agent_ip'):
            protocol.datagram_received(b'DUAL_STATUS,id:12,configured:1,profile:local_joint,flex_unit:adc12',('127.0.0.1',5012))
            protocol.datagram_received(b'STATUS,id:12,paused:1,params:1,mode:precalibrated_two_sensor',('127.0.0.1',5012))
            self.assertEqual(server.agent_live_state[12]['dual_profile'],'local_joint')
            self.assertEqual(server.agent_live_state[12]['dual_configured'],1)

    def test_switch_to_current_only_clears_stale_dual_configuration(self):
        protocol=server.UdpDiscoveryProtocol()
        with patch.dict(server.agent_live_state,{12:{'mode':'precalibrated_two_sensor',
              'dual_profile':'local_joint','dual_configured':1,'last_seen':0}},clear=True), \
             patch.object(server.log_receiver,'record_status'), \
             patch.object(server,'broadcast_telemetry'),patch.object(server,'update_agent_ip'):
            protocol.datagram_received(b'STATUS,id:12,paused:1,params:1,mode:current_only',('127.0.0.1',5012))
            self.assertNotIn('dual_configured',server.agent_live_state[12])


if __name__=='__main__':unittest.main()
