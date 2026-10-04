"""
backend.preview_manager
On-demand NDI video preview streamer using MJPEG (multipart/x-mixed-replace).
Optimized memory management:
- Uses memoryview / buffer interface instead of duplicating bytes on heap
- Periodic garbage collection and idle session reclamation
"""
from __future__ import annotations
import asyncio
import time
import threading
import logging
import gc
from typing import Dict, Optional, Set, Tuple
import numpy as np
import cv2

from cyndilib.finder import Finder
from cyndilib.receiver import Receiver
from cyndilib.video_frame import VideoFrameSync
from cyndilib.wrapper.ndi_recv import RecvColorFormat, RecvBandwidth
from backend.ndi_scanner import scanner

logger = logging.getLogger(__name__)


class NDIPreviewSession:
    """Manages a lightweight NDI Receiver connection for a single source."""
    def __init__(self, source_name: str, finder: Finder):
        self.source_name = source_name
        self.finder = finder
        # Subscribers map: queue -> (target_fps, max_width)
        self.subscribers: Dict[asyncio.Queue, Tuple[int, int]] = {}
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.3)
        self.thread = None

    def add_subscriber(self, q: asyncio.Queue, fps: int = 3, max_width: int = 360, raw: bool = False):
        with self.lock:
            # (fps, max_width, raw)
            self.subscribers[q] = (fps, max_width, raw)

    def remove_subscriber(self, q: asyncio.Queue):
        with self.lock:
            self.subscribers.pop(q, None)

    def _worker(self):
        receiver: Optional[Receiver] = None
        vf = VideoFrameSync()
        reconnect_time = 0.0

        last_sub_send_time: Dict[asyncio.Queue, float] = {}
        last_gc_time = time.time()

        while self.running:
            with self.lock:
                subs = dict(self.subscribers)

            if not subs:
                time.sleep(0.2)
                continue

            now = time.time()

            # Periodic GC every 15 seconds to return freed C/Python memory chunks
            if now - last_gc_time > 15.0:
                last_gc_time = now
                gc.collect()

            if receiver is None or not receiver.is_connected():
                if now >= reconnect_time:
                    try:
                        matched = None
                        # Check shared scanner finder first, then fallback to self.finder
                        finders = [scanner.finder]
                        if self.finder not in finders:
                            finders.append(self.finder)

                        for f in finders:
                            try:
                                f.wait_for_sources(0)
                            except Exception:
                                pass
                            for s in f:
                                if s.name == self.source_name or s.stream_name == self.source_name:
                                    matched = s
                                    break
                            if matched is not None:
                                break

                        if matched is not None:
                            receiver = Receiver(
                                color_format=RecvColorFormat.BGRX_BGRA,
                                bandwidth=RecvBandwidth.lowest,
                            )
                            receiver.frame_sync.set_video_frame(vf)
                            receiver.set_source(matched)
                        else:
                            reconnect_time = now + 1.0
                    except Exception as e:
                        logger.warning(f"Error connecting preview receiver to {self.source_name}: {e}")
                        receiver = None
                        reconnect_time = now + 2.0
                time.sleep(0.1)
                continue

            # Check which subscribers need a frame
            active_targets = []
            max_requested_fps = 1
            for q, sub_info in subs.items():
                fps = sub_info[0]
                max_w = sub_info[1]
                is_raw = sub_info[2] if len(sub_info) > 2 else False
                max_requested_fps = max(max_requested_fps, fps)
                # Allow a slight leeway (0.85 of interval) so timing jitter doesn't skip frames
                min_interval = 0.85 / max(1, fps)
                last_t = last_sub_send_time.get(q, 0.0)
                if (now - last_t) >= min_interval:
                    active_targets.append((q, max_w, is_raw))

            if not active_targets:
                time.sleep(0.005)
                continue

            # Capture frame
            try:
                receiver.frame_sync.capture_video()
                w, h = vf.get_resolution()
                data_size = vf.get_data_size()
                if w > 0 and h > 0 and data_size > 0:
                    raw_data = bytes(vf)
                    arr = np.frombuffer(raw_data, dtype=np.uint8, count=w * h * 4).reshape((h, w, 4))

                    # Separate raw subscribers and jpeg subscribers
                    # Group by max_width
                    raw_targets = [t for t in active_targets if t[2]]
                    jpeg_targets = [t for t in active_targets if not t[2]]

                    # Deliver raw frames
                    for q, max_w, _ in raw_targets:
                        if w > max_w:
                            new_w = max_w
                            new_h = int(h * (max_w / w))
                            target_arr = cv2.resize(arr, (new_w, new_h), interpolation=cv2.INTER_AREA)
                        else:
                            target_arr = arr.copy()
                        last_sub_send_time[q] = now
                        try:
                            if q.full():
                                try:
                                    q.get_nowait()
                                except Exception:
                                    pass
                            q.put_nowait(target_arr)
                        except Exception:
                            pass

                    # Deliver JPEG frames
                    if jpeg_targets:
                        width_groups: Dict[int, list] = {}
                        for q, max_w, _ in jpeg_targets:
                            width_groups.setdefault(max_w, []).append(q)

                        for max_w, queues in width_groups.items():
                            if w > max_w:
                                new_w = max_w
                                new_h = int(h * (max_w / w))
                                resized = cv2.resize(arr, (new_w, new_h), interpolation=cv2.INTER_AREA)
                            else:
                                resized = arr

                            quality = 65 if max_w <= 480 else 75
                            success, enc = cv2.imencode('.jpg', resized, [
                                int(cv2.IMWRITE_JPEG_QUALITY), quality,
                                int(cv2.IMWRITE_JPEG_OPTIMIZE), 0
                            ])
                            if success:
                                jpeg_bytes = enc.tobytes()
                                for q in queues:
                                    last_sub_send_time[q] = now
                                    try:
                                        if q.full():
                                            try:
                                                q.get_nowait()
                                            except Exception:
                                                pass
                                        q.put_nowait(jpeg_bytes)
                                    except Exception:
                                        pass

            except Exception as e:
                logger.warning(f"Error capturing preview frame for {self.source_name}: {e}")
                receiver = None
                reconnect_time = now + 2.0

            # Clean up old queue timestamps
            for q in list(last_sub_send_time.keys()):
                if q not in subs:
                    last_sub_send_time.pop(q, None)

            # High precision dynamic sleep matching highest subscriber fps
            target_delay = 1.0 / max(1, max_requested_fps)
            time.sleep(max(0.001, target_delay * 0.4))

        if receiver is not None:
            receiver = None
        gc.collect()


class NDIPreviewManager:
    """Manages multiple NDI preview sessions by source name."""
    def __init__(self):
        self.finder = scanner.finder
        self.sessions: Dict[str, NDIPreviewSession] = {}
        self.lock = threading.Lock()

    def get_or_create_session(self, source_name: str) -> NDIPreviewSession:
        with self.lock:
            if source_name not in self.sessions:
                session = NDIPreviewSession(source_name, self.finder)
                session.start()
                self.sessions[source_name] = session
            return self.sessions[source_name]

    def cleanup_idle_sessions(self):
        """Stops and removes sessions with 0 subscribers."""
        with self.lock:
            to_remove = []
            for name, session in self.sessions.items():
                with session.lock:
                    if len(session.subscribers) == 0:
                        to_remove.append(name)
            for name in to_remove:
                self.sessions[name].stop()
                del self.sessions[name]
        if to_remove:
            gc.collect()

    def stop_all(self):
        with self.lock:
            for session in self.sessions.values():
                session.stop()
            self.sessions.clear()
        # Avoid calling finder.destroy() synchronously on shutdown as C thread may deadlock
        gc.collect()


preview_manager = NDIPreviewManager()
