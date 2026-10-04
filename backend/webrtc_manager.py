"""
backend.webrtc_manager
WebRTC signaling and streaming server for real-time, low-latency NDI preview.
Transfers video frames from NDI preview receiver to browser via WebRTC RTP/H.264/VP8.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Dict, Optional, Set
import numpy as np
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from backend.preview_manager import preview_manager

logger = logging.getLogger(__name__)


class NDIWebRTCVideoTrack(VideoStreamTrack):
    """
    aiortc VideoStreamTrack streaming video frames from an NDIPreviewSession.
    """
    def __init__(self, source_name: str, target_fps: int = 24, target_width: int = 640):
        super().__init__()
        self.source_name = source_name
        self.target_fps = target_fps
        self.target_width = target_width
        self.session = preview_manager.get_or_create_session(source_name)
        self.frame_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        # Register as raw subscriber to avoid JPEG encode/decode overhead
        self.session.add_subscriber(self.frame_queue, fps=target_fps, max_width=target_width, raw=True)
        self._start_time: Optional[float] = None

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        target_height = int(self.target_width * 9 / 16)

        try:
            # Wait up to 1 second for raw NumPy array (h, w, 4) from preview_manager
            arr = await asyncio.wait_for(self.frame_queue.get(), timeout=1.0)
            frame = av.VideoFrame.from_ndarray(arr, format='bgra').reformat(format='yuv420p')
        except (asyncio.TimeoutError, Exception) as e:
            # Fallback black frame if no frame received
            frame = av.VideoFrame(self.target_width, target_height, 'yuv420p')

        frame.pts = pts
        frame.time_base = time_base
        return frame

    def stop(self):
        super().stop()
        self.session.remove_subscriber(self.frame_queue)
        preview_manager.cleanup_idle_sessions()


class WebRTCManager:
    def __init__(self):
        self.pcs: Set[RTCPeerConnection] = set()

    async def handle_offer(self, source_name: str, sdp: str, sdp_type: str) -> dict:
        pc = RTCPeerConnection()
        self.pcs.add(pc)

        video_track = NDIWebRTCVideoTrack(source_name=source_name, target_fps=24, target_width=640)
        pc.addTrack(video_track)

        @pc.on("connectionstatechange")
        async def on_state_change():
            logger.info(f"WebRTC connection state ({source_name}): {pc.connectionState}")
            if pc.connectionState in ["failed", "closed"]:
                video_track.stop()
                await pc.close()
                self.pcs.discard(pc)

        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        }

    async def close_all(self):
        coros = [pc.close() for pc in self.pcs]
        await asyncio.gather(*coros, return_exceptions=True)
        self.pcs.clear()


webrtc_manager = WebRTCManager()
