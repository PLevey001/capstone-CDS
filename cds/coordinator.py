import json
import logging
import multiprocessing
import signal
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from cds.analysis import analyze

logger = logging.getLogger(__name__)


def initialize_worker():
    # The API handles Ctrl+C and drains active work; do not interrupt child parsers.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


class Coordinator:
    """Owns a bounded process pool; only this process saves analysis results."""

    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.store.recover_interrupted()
        self.thread = threading.Thread(target=self.run, name="cds-coordinator", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()

    def pool(self):
        return ProcessPoolExecutor(max_workers=self.settings.workers,
                                   mp_context=multiprocessing.get_context("spawn"),
                                   initializer=initialize_worker)

    def run(self):
        pool = self.pool()
        active = {}
        limits = {"tool_timeout": self.settings.tool_timeout, "max_artifacts": self.settings.max_artifacts}
        try:
            while not self.stop_event.is_set() or active:
                for future, (job, work_dir, last_progress) in list(active.items()):
                    if future.done():
                        try:
                            result = future.result()
                        except Exception as error:
                            result = {"error": f"Worker failed: {error}"[:2000]}
                        self.store.finish(job, result)
                        del active[future]
                    else:
                        try:
                            progress = json.loads((work_dir / "progress.json").read_text())
                            if progress != last_progress:
                                self.store.progress(job["job_id"], progress["stage"], progress["progress"])
                                active[future] = (job, work_dir, progress)
                        except (FileNotFoundError, json.JSONDecodeError):
                            pass
                while not self.stop_event.is_set() and len(active) < self.settings.workers:
                    job = self.store.claim()
                    if not job:
                        break
                    work_dir = self.settings.data_dir / "work" / job["job_id"]
                    work_dir.mkdir(exist_ok=True)
                    (work_dir / "progress.json").unlink(missing_ok=True)
                    try:
                        future = pool.submit(analyze, job, limits, str(work_dir))
                    except BrokenProcessPool:
                        # Existing futures are collected as failed next iteration.
                        pool.shutdown(wait=True)
                        pool = self.pool()
                        future = pool.submit(analyze, job, limits, str(work_dir))
                    active[future] = (job, work_dir, None)
                # Wait remains interruptible; drain existing jobs during shutdown.
                if self.stop_event.is_set():
                    threading.Event().wait(0.1)
                else:
                    self.stop_event.wait(0.15)
        except Exception:
            logger.exception("Coordinator stopped unexpectedly; queued jobs require a restart")
        finally:
            pool.shutdown(wait=True)
