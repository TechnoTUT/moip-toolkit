"""
core.reconnecting_receiver
Wraps a long-lived cyndilib Receiver with connect / drop / retry handling.

Re-creating a Receiver on every reconnect leaks native NDI buffers (Receiver <-> FrameSync
reference cycle delays NDIlib_recv_destroy), so one instance is created lazily and reused;
reconnecting is done via disconnect() + set_source().

Stall watchdog (opt-in, `stall_timeout > 0`)
-------------------------------------------
After a network outage NDI can keep a dead session reporting is_connected() == True while
no frames arrive (the sender's TCP retransmit back-off: observed 10 s to 2+ min). Tearing
such a session down with disconnect() blocks for ~16 s *with the GIL held* (cyndilib calls
NDIlib_recv_connect(NULL) without releasing it), freezing every thread of the process.

So when a connected receiver delivers no new frame (reported via note_frame()) for
`stall_timeout` seconds, it is not torn down: it is set aside ("buried") and a fresh
Receiver with fresh frame objects connects instead, which takes ~0.3 s. A buried receiver is
destroyed once that is cheap again: when its session comes back to life and frames flow
(measured ~30 ms). Destroying one that merely went "not connected" still blocks for ~13 s,
since NDI keeps retrying the connection internally; such a receiver revives later and is
reaped then. Because the frame objects are replaced, callers must read `video_frame` /
`audio_frame` from this object on every use instead of keeping their own reference.
"""
from __future__ import annotations

from typing import Callable, Optional
import ctypes
import ctypes.util
import gc
import logging
import time

from cyndilib.receiver import Receiver

logger = logging.getLogger(__name__)

try:  # glibc: hand the freed NDI frame buffers back to the OS after releasing receivers
    _malloc_trim = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6").malloc_trim
except (OSError, AttributeError):
    _malloc_trim = None

# Above this many buried receivers fall back to the blocking in-place disconnect.
MAX_BURIED = 4
# Retry interval after a stall doubles up to this (sender legitimately paused, etc.).
MAX_STALL_INTERVAL = 30.0
# drop() buries instead of disconnecting a connected receiver that has been silent this long.
SILENT_DROP_S = 0.5
# How often buried receivers are checked for revival. A revived one decodes the stream nobody
# reads, and its memory use climbs quickly (hundreds of MB/s observed), so reap it promptly.
REAP_INTERVAL = 0.1


class ReconnectingReceiver:
    def __init__(
        self,
        color_format,
        bandwidth,
        video_frame,
        audio_frame=None,
        connect_timeout: float = 10.0,
        retry_interval: float = 1.0,
        stall_timeout: float = 0.0,
        log: Optional[Callable[[str], None]] = None,
    ):
        self._color_format = color_format
        self._bandwidth = bandwidth
        self._vf = video_frame
        self._af = audio_frame
        self.connect_timeout = connect_timeout
        self.retry_interval = retry_interval
        self.stall_timeout = stall_timeout
        self._log = log or logger.info

        self._rx: Optional[Receiver] = None
        self.receiver: Optional[Receiver] = None  # active (attached) receiver or None
        self._deadline = 0.0
        self._retry_at = 0.0
        self.source = None  # last attached Source

        # Stall bookkeeping uses time.monotonic(), independent of the callers' `now`.
        self._last_frame = 0.0
        self._stall_interval = stall_timeout
        self._grace_until = 0.0
        self._buried: list[tuple[Receiver, int]] = []  # (receiver, frames_total when buried)
        self._next_reap = 0.0

    @property
    def video_frame(self):
        return self._vf

    @property
    def audio_frame(self):
        return self._af

    def _ensure(self) -> Receiver:
        if self._rx is None:
            self._rx = Receiver(color_format=self._color_format, bandwidth=self._bandwidth)
            self._rx.frame_sync.set_video_frame(self._vf)
            if self._af is not None:
                try:
                    self._rx.frame_sync.set_audio_frame(self._af)
                except Exception as e:
                    logger.debug("Failed to set audio frame on receiver: %s", e)
        return self._rx

    def attach(self, source, now: float) -> Receiver:
        """Connect the shared Receiver to `source` and start the connect-timeout clock."""
        rx = self._ensure()
        rx.set_source(source)
        self.receiver = rx
        self.source = source
        self._deadline = now + self.connect_timeout
        self._last_frame = time.monotonic()
        self._grace_until = 0.0
        return rx

    def note_frame(self, now: float = 0.0) -> None:
        """Report that a new video frame was received (feeds the stall watchdog)."""
        self._last_frame = time.monotonic()
        self._stall_interval = self.stall_timeout
        self._grace_until = 0.0

    # ------------------------------------------------------------------ stall handling
    @staticmethod
    def _frames_total(rx: Receiver) -> int:
        return int(rx.get_performance_data()["video"]["frames_total"])

    def _bury(self) -> None:
        """Set the current Receiver aside without tearing it down; the next attach()
        creates a fresh Receiver. Frame objects are bound to their first FrameSync for
        freeing, so they are replaced too."""
        old = self._rx
        try:
            total = self._frames_total(old)
        except Exception:
            total = 0
        self._buried.append((old, total))
        self._rx = None
        self.receiver = None
        self._vf = type(self._vf)()
        if self._af is not None:
            self._af = type(self._af)()

    def _silent_for(self) -> float:
        return time.monotonic() - self._last_frame

    def reap(self) -> None:
        """Destroy buried receivers whose session revived (cheap to tear down then)."""
        if not self._buried:
            return
        mono = time.monotonic()
        if mono < self._next_reap:
            return
        self._next_reap = mono + REAP_INTERVAL
        keep, reaped = [], 0
        for rx, total in self._buried:
            try:
                done = self._frames_total(rx) > total + 1
            except Exception:
                done = False
            if not done:
                keep.append((rx, total))
                continue
            try:
                rx.disconnect()
            except Exception as e:
                logger.debug("Error during receiver disconnect: %s", e)
            reaped += 1
        self._buried = keep
        if reaped:
            rx = None
            gc.collect()  # Receiver <-> FrameSync cycle: free the native instance now
            if _malloc_trim is not None:
                _malloc_trim(0)
            self._log(f"[NDI] Released {reaped} stalled receiver(s) in {time.monotonic() - mono:.3f}s "
                      f"({len(keep)} still pending)")

    def check_stall(self, now: float, find_source: Optional[Callable[[], object]] = None) -> bool:
        """
        If the receiver claims to be connected but no new frame was reported for the stall
        interval, reconnect with a fresh Receiver (see module docstring). Returns True when
        it reconnected; the caller keeps showing its last frame meanwhile.
        `find_source` may supply a fresh Source (e.g. the sender restarted on a new port);
        if it returns None the last attached Source is reused.
        """
        self.reap()
        if (self.stall_timeout <= 0 or self.receiver is None
                or self._silent_for() < self._stall_interval or not self.receiver.is_connected()):
            return False
        src = None
        if find_source is not None:
            try:
                src = find_source()
            except Exception as e:
                logger.debug("Error resolving NDI source: %s", e)
        src = src or self.source
        silent = self._silent_for()
        if len(self._buried) < MAX_BURIED:
            self._bury()
        else:
            self._log(f"[NDI] {len(self._buried)} stalled receivers pending; disconnecting in place (may block)")
            self._disconnect_current()
        self.attach(src, now)
        self._stall_interval = min(self._stall_interval * 2, MAX_STALL_INTERVAL)
        self._grace_until = now + min(self.connect_timeout, 4.0)
        self._log(f"[NDI] No new frames for {silent:.1f}s on a connected receiver; reconnecting to "
                  f"{getattr(src, 'name', src)}")
        return True

    def _disconnect_current(self) -> None:
        if self.receiver is not None:
            try:
                self.receiver.disconnect()
            except Exception as e:
                logger.debug("Error during receiver disconnect: %s", e)
        self.receiver = None

    def drop(self, now: float = 0.0, cooldown: Optional[float] = None) -> None:
        """Disconnect explicitly and block new attempts for `cooldown` seconds."""
        if self.receiver is not None:
            stalled = False
            if self.stall_timeout > 0 and self._silent_for() > SILENT_DROP_S and len(self._buried) < MAX_BURIED:
                try:
                    stalled = self.receiver.is_connected()
                except Exception:
                    pass
            if stalled:
                self._bury()  # disconnect() on a dead session would block for ~16 s
            else:
                self._disconnect_current()
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
        self.reap()
        if self.receiver is not None:
            if self.check_stall(now, find_source):
                return True  # reconnecting; keep showing the last frame
            if self.receiver.is_connected() or now < self._grace_until:
                return True
            if now < self._deadline:
                return False  # handshake still in progress
            self.drop(now)  # timed out / lost
        if now < self._retry_at:
            return False
        try:
            src = find_source()
        except Exception as e:
            logger.debug("Error resolving NDI source: %s", e)
            src = None
        if src is None:
            self._retry_at = now + self.retry_interval
            return False
        try:
            self.attach(src, now)
        except Exception as e:
            logger.warning("Error attaching NDI source %s: %s", getattr(src, 'name', src), e)
            self.drop(now, cooldown=2.0)
        return False
