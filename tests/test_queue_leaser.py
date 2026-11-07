"""
Unit tests for QueueLeaser atomic locking and state transitions.
"""

import os
import tempfile
from pathlib import Path
import unittest

from database.db import init_db
from core.models import SyncStatus
from core.queue_leaser import QueueLeaser


class TestQueueLeaser(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test-sync.sqlite"
        init_db(self.db_path)
        self.leaser = QueueLeaser(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_enqueue_and_acquire(self):
        job = self.leaser.enqueue(
            project_name="TestProject",
            source_node="laptop_gitlab",
            target_node="server_gitlab",
            branch_name="main",
            commit_sha="a1b2c3d4e5f6",
        )
        self.assertEqual(job.status, SyncStatus.QUEUED)
        self.assertEqual(job.project_name, "TestProject")

        # Worker 1 acquires lease
        worker_1 = "worker-node-1"
        leased_job = self.leaser.acquire_lease(worker_1, lease_duration_sec=30)
        self.assertIsNotNone(leased_job)
        self.assertEqual(leased_job.id, job.id)
        self.assertEqual(leased_job.status, SyncStatus.LOCKED)
        self.assertEqual(leased_job.locked_by, worker_1)

        # Worker 2 attempts to acquire same job -> should return None
        worker_2 = "worker-node-2"
        no_job = self.leaser.acquire_lease(worker_2, lease_duration_sec=30)
        self.assertIsNone(no_job)

    def test_complete_job(self):
        job = self.leaser.enqueue(
            project_name="TestProject",
            source_node="laptop_gitlab",
            target_node="github",
            branch_name="main",
            commit_sha="112233445566",
        )
        leased = self.leaser.acquire_lease("worker-1", 60)
        self.assertTrue(self.leaser.mark_completed(leased.id, "worker-1"))

        # Verify job is synced
        jobs = self.leaser.list_jobs(status="synced")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].id, job.id)
        self.assertIsNone(jobs[0].locked_by)

    def test_failure_retry_and_dead_letter(self):
        job = self.leaser.enqueue(
            project_name="FailingProject",
            source_node="server_gitlab",
            target_node="laptop_gitlab",
            branch_name="main",
            commit_sha="deadbeef1234",
        )
        
        # Simulate 5 failures
        for i in range(5):
            leased = self.leaser.acquire_lease("worker-err", 60)
            if leased is None:
                # Force next_retry_at to past to simulate time passing for retry
                with self.leaser.db_path.open() as _:
                    from database.db import get_connection
                    with get_connection(self.leaser.db_path) as conn:
                        conn.execute("UPDATE sync_queue SET next_retry_at = datetime('now', '-1 minute') WHERE id = ?", (job.id,))
                        conn.commit()
                leased = self.leaser.acquire_lease("worker-err", 60)
            
            self.assertIsNotNone(leased)
            self.leaser.mark_failed(leased.id, "worker-err", f"Simulated failure {i+1}")

        # Check that after max_retries it transitioned to dead_letter
        jobs = self.leaser.list_jobs(status="dead_letter")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].retry_count, 5)


if __name__ == "__main__":
    unittest.main()
