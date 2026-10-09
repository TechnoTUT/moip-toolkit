"""
core.reconnecting_receiver
Wraps a single, long-lived cyndilib Receiver with connect / drop / retry handling.

Re-creating a Receiver on every reconnect leaks native NDI buffers (Receiver <-> FrameSync
reference cycle delays NDIlib_recv_destroy), so one instance is created lazily and reused;
reconnecting is done via disconnect() + set_source().
"""
from __future__ import annotations

from typing import Callable, Optional

from cyndilib.receiver import Receiver


class ReconnectingReceiver:
    def __init__(
        self,
        color_format,
        bandwidth,
        video_frame,
        audio_frame=None,
        connect_timeout: float = 10.0,
        retry_interval: float = 1.0,
    ):
        self._color_format = color_format
        self._bandwidth = bandwidth
        self._vf = video_frame
        self._af = audio_frame
        self.connect_timeout = connect_timeout
        self.retry_interval = retry_interval

        self._rx: Optional[Receiver] = None
        self.receiver: Optional[Receiver] = None  # active (attached) receiver or None
        self._deadline = 0.0
        self._retry_at = 0.0

    def _ensure(self) -> Receiver:
        if self._rx is None:
            self._rx = Receiver(color_format=self._color_format, bandwidth=self._bandwidth)
            self._rx.frame_sync.set_video_frame(self._vf)
            if self._af is not None:
                try:
                    self._rx.frame_sync.set_audio_frame(self._af)
                except Exception:
                    pass
        return self._rx

    def attach(self, source, now: float) -> Receiver:
        """Connect the shared Receiver to `source` and start the connect-timeout clock."""
        rx = self._ensure()
        rx.set_source(source)
        self.receiver = rx
        self._deadline = now + self.connect_timeout
        return rx

    def drop(self, now: float = 0.0, cooldown: Optional[float] = None) -> None:
        """Disconnect explicitly and block new attempts for `cooldown` seconds."""
        if self.receiver is not None:
            try:
                self.receiver.disconnect()
            except Exception:
                pass
        self.receiver = None
        self._retry_at = now + (self.retry_interval if cooldown is None else cooldown)

    @property
    def attached(self) -> bool:
        return self.receiver is not None

    def is_connected(self) -> bool:
        return self.receiver is not None and self.receiver.is_connected()

    def update(self, find_source: Callable[[], object], now: float) -> bool:
        """
        Drive (re)connection. Returns True while connected.
        `find_source` returns a Source or None; it's only called when a new attempt is due.
        """
        if self.receiver is not None:
            if self.receiver.is_connected():
                return True
            if now < self._deadline:
                return False  # handshake still in progress
            self.drop(now)  # timed out / lost
        if now < self._retry_at:
            return False
        try:
            src = find_source()
        except Exception:
            src = None
        if src is None:
            self._retry_at = now + self.retry_interval
            return False
        try:
            self.attach(src, now)
        except Exception:
            self.drop(now, cooldown=2.0)
        return False
