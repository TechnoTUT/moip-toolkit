"""
backend.signage_runner
Process controller for local Digital Signage.
Runs the SDL2 / OpenGL image display in a dedicated subprocess.
"""
from __future__ import annotations
import multiprocessing as mp
import os
import queue
import time
from typing import Optional
from backend.models import SignageStatus, SignageStartRequest


def _signage_worker(command_q: mp.Queue, status_q: mp.Queue, image_path: str, fullscreen: bool, use_framebuffer: bool = False, fb_device: str = "/dev/fb0"):
    from core.signage import run_signage
    try:
        run_signage(
            image_path=image_path,
            fullscreen=fullscreen,
            command_q=command_q,
            status_q=status_q,
            use_framebuffer=use_framebuffer,
            fb_device=fb_device
        )
    except Exception as e:
        status_q.put({"type": "error", "error": str(e)})


class SignageRunner:
    def __init__(self):
        self.process: Optional[mp.Process] = None
        self.command_q: Optional[mp.Queue] = None
        self.status_q: Optional[mp.Queue] = None
        self._ctx = mp.get_context("spawn")
        self._status = SignageStatus()

    def get_status(self) -> SignageStatus:
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
                msg_type = msg.get("type")
                if msg_type == "started":
                    self._status.running = True
                    self._status.current_image = msg.get("image_path")
                    self._status.filename = os.path.basename(msg.get("image_path", ""))
                    self._status.width = msg.get("width", 0)
                    self._status.height = msg.get("height", 0)
                    self._status.fullscreen = msg.get("fullscreen", True)
                    self._status.error = None
                elif msg_type == "switched":
                    self._status.current_image = msg.get("image_path")
                    self._status.filename = os.path.basename(msg.get("image_path", ""))
                    self._status.width = msg.get("width", 0)
                    self._status.height = msg.get("height", 0)
                elif msg_type == "stopped":
                    self._status.running = False
                elif msg_type == "error":
                    self._status.error = msg.get("error")
            except queue.Empty:
                break

    def start(self, image_path: str, fullscreen: bool = True, use_framebuffer: bool = False, fb_device: str = "/dev/fb0"):
        self.stop()

        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Signage image does not exist: {image_path}")

        self.command_q = self._ctx.Queue()
        self.status_q = self._ctx.Queue()

        self._status = SignageStatus(
            running=True,
            current_image=image_path,
            filename=os.path.basename(image_path),
            fullscreen=fullscreen,
            error=None
        )

        self.process = self._ctx.Process(
            target=_signage_worker,
            args=(self.command_q, self.status_q, image_path, fullscreen, use_framebuffer, fb_device),
            daemon=True
        )
        self.process.start()

        # Check startup result briefly
        time.sleep(0.3)
        self._update_status_from_queue()
        if self._status.error:
            self.stop()
            raise RuntimeError(self._status.error)

    def switch_image(self, image_path: str):
        if not self.process or not self.process.is_alive():
            self.start(image_path, fullscreen=self._status.fullscreen)
            return

        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Signage image does not exist: {image_path}")

        if self.command_q:
            self.command_q.put({"action": "switch_image", "image_path": image_path})

    def stop(self):
        if self.command_q and self.process and self.process.is_alive():
            try:
                self.command_q.put({"action": "stop"})
            except Exception:
                pass

        if self.process:
            self.process.join(timeout=1.5)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1.0)
            self.process = None

        self._status.running = False
        self.command_q = None
        self.status_q = None


signage_runner = SignageRunner()
