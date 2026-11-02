import tempfile,unittest,struct,json
from pathlib import Path
from feedback_diagnostic import HEADER,RECORD,DiagnosticReceiver,token_for
class DiagnosticTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.rx=DiagnosticReceiver(self.root)
 def packet(self,at=0,total=10,seq='ps261016_000000',value=.1):
  h=HEADER.pack(b'PDG1',token_for(seq),12,44,at,total,seq.encode(),b'pdg1-test',1,9.424778,0,-.034899,-.999391,110,75,0,1,1700000000000,1200)
  return h+b''.join(RECORD.pack(20000*(at+i),20000*(at+i),20000,1,10,1,2,3,4,.2,value,value) for i in range(min(8,total-at)))
 def test_reordered_retry_and_restart_are_durable(self):
  self.assertIsNotNone(self.rx.handle(self.packet(8)))
  self.rx=DiagnosticReceiver(self.root)
  self.assertIsNotNone(self.rx.handle(self.packet(0)))
  folder=self.root/'ps261016_000000/agent_12'
  self.assertEqual(json.loads((folder/'complete.json').read_text())['records'],10)
  self.assertEqual(len((folder/'records.bin').read_bytes()),440)
  self.assertIsNotNone(self.rx.handle(self.packet(0)))
  self.assertIsNone(self.rx.handle(self.packet(0,value=.2)))
 def test_malformed_schema_path_and_size_are_rejected(self):
  self.assertIsNone(self.rx.handle(self.packet()[:-1]))
  self.assertIsNone(self.rx.handle(self.packet(seq='../bad')))
  self.assertIsNone(self.rx.handle(self.packet(total=1101)))
  self.assertIsNone(self.rx.handle(b'PDG1'))
