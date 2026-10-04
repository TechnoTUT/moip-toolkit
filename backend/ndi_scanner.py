"""
backend.ndi_scanner
NDI Finder wrapper to scan and list available NDI sources asynchronously.
"""
from __future__ import annotations
import threading
import time
from typing import List, Dict
from cyndilib.finder import Finder
from backend.models import NDISourceItem


class NDIScanner:
    def __init__(self):
        self.finder = Finder()
        self._lock = threading.Lock()
        self._sources: Dict[str, NDISourceItem] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.1)
        # Avoid calling finder.destroy() synchronously on shutdown as C thread may deadlock

    def _scan_loop(self):
        while self._running:
            try:
                # Non-blocking wait to avoid hanging C library
                self.finder.wait_for_sources(0)
                current_list = list(self.finder)
                new_map: Dict[str, NDISourceItem] = {}
                for src in current_list:
                    name = getattr(src, "name", "")
                    stream_name = getattr(src, "stream_name", None)
                    host_name = getattr(src, "host_name", None)
                    if name:
                        new_map[name] = NDISourceItem(
                            name=name,
                            stream_name=stream_name,
                            host_name=host_name
                        )
                with self._lock:
                    self._sources = new_map
            except Exception as e:
                # Keep loop alive even if cyndilib raises
                pass
            if self._stop_event.wait(0.5):
                break

    def get_sources(self) -> List[NDISourceItem]:
        with self._lock:
            return list(self._sources.values())


scanner = NDIScanner()
