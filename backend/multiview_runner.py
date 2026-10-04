"""
backend.multiview_runner
Process controller for SDL2 Multi-Viewer.
Manages running the multiview window in a dedicated subprocess.
"""
from __future__ import annotations
import multiprocessing as mp
import queue
import time
from typing import Optional, List
from backend.models import MultiviewStatus, MultiviewStartRequest, MultiviewSlotStatus


def _multiview_worker(command_q: mp.Queue, status_q: mp.Queue, options: dict):
    from core.multiview import play_multiview
    sources = options.get("sources")
    fullscreen = options.get("fullscreen", False)
    
    try:
        status_q.put({"type": "status", "running": True, "fullscreen": fullscreen, "slots": []})
        play_multiview(
            source_names=sources,
            fullscreen=fullscreen,
            auto_discover=True,
            log_fn=lambda msg: None
        )
    except Exception as e:
        status_q.put({"type": "error", "error": str(e)})
    finally:
        status_q.put({"type": "status", "running": False, "fullscreen": False, "slots": []})


class MultiviewRunner:
    def __init__(self):
        self.process: Optional[mp.Process] = None
        self.command_q: Optional[mp.Queue] = None
        self.status_q: Optional[mp.Queue] = None
        self._ctx = mp.get_context("spawn")
        self._status = MultiviewStatus()

    def get_status(self) -> MultiviewStatus:
        self._update_status_from_queue()
        if self.process and not self.process.is_alive():
            self._status.running = False
            self.process = None
        return self._status

    def _update_status_from_queue(self):
        if not self.status_q:
            return
        while not self.status_q.empty():
            try:
                msg = self.status_q.get_nowait()
                if msg.get("type") == "status":
                    self._status.running = msg.get("running", False)
                    self._status.fullscreen = msg.get("fullscreen", False)
                elif msg.get("type") == "error":
                    self._status.error = msg.get("error")
            except queue.Empty:
                break

    def start(self, req: MultiviewStartRequest):
        if self.process and self.process.is_alive():
            raise RuntimeError("Multi-Viewer is already running.")

        self.command_q = self._ctx.Queue()
        self.status_q = self._ctx.Queue()
        self._status = MultiviewStatus(
            running=True,
            fullscreen=req.fullscreen,
            slots=[],
            error=None
        )

        self.process = self._ctx.Process(
            target=_multiview_worker,
            args=(self.command_q, self.status_q, req.model_dump()),
            daemon=True
        )
        self.process.start()

    def stop(self):
        if self.process and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1.0)
            if self.process.is_alive():
                self.process.kill()

        self.process = None
        self.command_q = None
        self.status_q = None
        self._status = MultiviewStatus(running=False, fullscreen=False, slots=[], error=None)


multiview_runner = MultiviewRunner()
