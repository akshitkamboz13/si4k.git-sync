"""
Persistent Worker Loop for Home Server.
Continuously processes queued replication tasks, manages leases, and updates node telemetry.
"""

import signal
import sys
import threading
import time
from typing import Optional

from core.config import SyncConfig
from core.models import SyncJob
from core.queue_leaser import QueueLeaser
from core.logger import get_logger
from engine.health_checker import HealthChecker
from engine.replicator import GitReplicator, ReplicationError
from engine.status_exporter import StatusExporter

logger = get_logger("si4k.worker")


class SyncWorker:
    def __init__(self, config: Optional[SyncConfig] = None):
        self.config = config or SyncConfig.load()
        self.leaser = QueueLeaser(self.config.db_path)
        self.replicator = GitReplicator(self.config)
        self.health_checker = HealthChecker(self.config)
        self.exporter = StatusExporter(self.config)
        self._running = False
        self._current_job: Optional[SyncJob] = None
        self._last_health_check = 0.0

    def start(self, run_once: bool = False) -> None:
        self._running = True
        logger.info(f"Starting si4k.git-sync worker [{self.config.worker_id}] on DB: {self.config.db_path}")

        # Initial export of status
        try:
            self.exporter.export()
        except Exception as e:
            logger.debug(f"Initial status export notice: {e}")

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        while self._running:
            try:
                # 1. Periodic Health Check
                now = time.time()
                if now - self._last_health_check >= self.config.health_interval_sec:
                    try:
                        self.health_checker.probe_all()
                        self._last_health_check = now
                    except Exception as e:
                        logger.warning(f"Health check probe error: {e}")

                # Update live status file
                try:
                    self.exporter.export()
                except Exception as e:
                    logger.debug(f"Status export error: {e}")

                # 2. Acquire next eligible job from queue
                job = self.leaser.acquire_lease(
                    worker_id=self.config.worker_id,
                    lease_duration_sec=self.config.lease_duration_sec,
                )

                if job:
                    self._process_job(job)
                    try:
                        self.exporter.export()
                    except Exception:
                        pass
                    if run_once:
                        break
                    continue

                if run_once:
                    logger.info("No pending jobs found for run-once execution.")
                    break

                # Sleep before next poll
                time.sleep(self.config.poll_interval_sec)

            except Exception as e:
                logger.error(f"Unexpected error in worker loop: {e}", exc_info=True)
                time.sleep(self.config.poll_interval_sec)

        logger.info(f"Worker [{self.config.worker_id}] stopped cleanly.")

    def _process_job(self, job: SyncJob) -> None:
        self._current_job = job
        self.leaser.mark_in_progress(job.id, self.config.worker_id)
        
        # Start background heartbeater during long git operations
        stop_heartbeat = threading.Event()
        def heartbeater():
            while not stop_heartbeat.wait(self.config.lease_duration_sec / 2):
                self.leaser.heartbeat(
                    job.id,
                    self.config.worker_id,
                    extension_sec=self.config.lease_duration_sec,
                )
        hb_thread = threading.Thread(target=heartbeater, daemon=True)
        hb_thread.start()

        try:
            self.replicator.replicate(job)
            self.leaser.mark_completed(job.id, self.config.worker_id)
        except ReplicationError as e:
            self.leaser.mark_failed(job.id, self.config.worker_id, str(e))
        except Exception as e:
            self.leaser.mark_failed(job.id, self.config.worker_id, f"Fatal error: {e}")
        finally:
            stop_heartbeat.set()
            hb_thread.join(timeout=2.0)
            self._current_job = None

    def _handle_signal(self, signum, frame):
        logger.info(f"Received termination signal ({signum}). Initiating clean shutdown...")
        self._running = False
