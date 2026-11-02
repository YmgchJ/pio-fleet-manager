import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import log_receiver as m

class ReceiverIsolationTests(unittest.TestCase):
 def setUp(self):
  td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup);self.root=Path(td.name)
  for name in ['EXPERIMENTS_DIR','SAVED_CHUNKS_DIR','MERGED_CHUNKS_DIR']:
   q=patch.object(m,name,self.root/name);q.start();self.addCleanup(q.stop)
  q=patch.object(m,'fm_event');self.events=q.start();self.addCleanup(q.stop)
  self.r=m.LogReceiver();self.r.set_sequence_context(dict(sequence_id='old',agent_ids=[12,13]))
 def test_new_sequence_isolates_previous_chunks_and_notifications(self):
  with self.r._lock:self.r._ensure_session()
  old=self.r._session_dir;chunk=old/'chunks/chunk_agent_13_old.csv';chunk.write_text('saved raw data')
  self.r._pending_files=[str(chunk)];self.r._log_end={13:(200,True,1)};self.r._received_counts={13:200}
  self.r.set_sequence_context(dict(sequence_id='new',agent_ids=[12,13]))
  self.assertTrue(chunk.exists());self.assertTrue((old/'incomplete_upload.json').exists())
  self.assertEqual(self.r._pending_files,[]);self.assertEqual(self.r.snapshot_log_end(),{})
  self.r.handle_log_end(13,200,True,'old',1);self.assertEqual(self.r.snapshot_log_end(),{})
 def test_one_agent_or_mismatched_counts_never_ready(self):
  self.r._pending_files=['chunk_agent_12_a.csv','chunk_agent_13_b.csv'];self.r._log_end={12:(10,True,1),13:(20,True,1)}
  self.r._received_counts={12:10};self.assertFalse(self.r._session_ready_to_merge())
  self.r._received_counts[13]=19;self.assertFalse(self.r._session_ready_to_merge())
  self.r._received_counts[13]=20;self.assertTrue(self.r._session_ready_to_merge())
  self.r._log_end[13]=(20,False,1);self.assertFalse(self.r._session_ready_to_merge())
 def test_old_snapshot_and_duplicate_log_end(self):
  self.r._log_end={12:(10,True,1)};self.assertEqual(self.r.snapshot_log_end('other'),{})
  with patch.object(self.r,'flush_agent') as flush:
   self.r.handle_log_end(12,10,True,'old',1);flush.assert_not_called()
if __name__=='__main__':unittest.main()
