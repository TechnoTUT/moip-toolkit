"""
backend.models
Pydantic schemas and dataclasses for API communication.
"""
from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, Field


class NDISourceItem(BaseModel):
    name: str
    stream_name: Optional[str] = None
    host_name: Optional[str] = None


class VideoDeviceItem(BaseModel):
    index: int
    name: str
    is_available: bool


class AudioDeviceItem(BaseModel):
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float


class RxStartRequest(BaseModel):
    sender_name: str
    recv_fmt: str = Field(default="rgb", description="uyvy, rgb, or bgr")
    recv_bandwidth: str = Field(default="highest", description="lowest or highest")
    fullscreen: bool = False


class SystemStatus(BaseModel):
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0
    load_avg: List[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])


class RxSwitchRequest(BaseModel):
    sender_name: str


class RxStatus(BaseModel):
    running: bool
    is_connected: bool
    current_source: Optional[str] = None
    recv_fmt: str = "rgb"
    recv_bandwidth: str = "highest"
    fullscreen: bool = False
    width: int = 0
    height: int = 0
    fps: float = 0.0
    fps_real: float = 0.0
    dropped_frames: int = 0
    audio_level_l: float = -60.0
    audio_level_r: float = -60.0
    audio_peak_l: float = -60.0
    audio_peak_r: float = -60.0
    error: Optional[str] = None


class TxStartRequest(BaseModel):
    video_device: int = 0
    audio_device: Optional[int] = None
    no_audio: bool = False
    sender_name: str = "TX"
    x_res: int = 1920
    y_res: int = 1080
    fps: str = "30"
    pix_fmt: str = "BGRX"
    sample_rate: int = 48000
    audio_channels: int = 2


class TxStatus(BaseModel):
    running: bool
    sender_name: Optional[str] = None
    video_device: Optional[int] = None
    audio_device: Optional[int] = None
    no_audio: bool = False
    actual_width: int = 0
    actual_height: int = 0
    actual_fps: float = 0.0
    fps_real: float = 0.0
    sample_rate: int = 0
    audio_channels: int = 0
    audio_level_l: float = -60.0
    audio_level_r: float = -60.0
    audio_peak_l: float = -60.0
    audio_peak_r: float = -60.0
    error: Optional[str] = None


class RxPresetSettings(BaseModel):
    auto_start: bool = False
    sender_name: Optional[str] = None
    recv_fmt: str = "rgb"
    recv_bandwidth: str = "highest"
    fullscreen: bool = False


class TxPresetSettings(BaseModel):
    auto_start: bool = False
    sender_name: str = "TX"
    video_device: int = 0
    audio_device: Optional[int] = None
    no_audio: bool = False
    x_res: int = 1920
    y_res: int = 1080
    fps: str = "30"
    pix_fmt: str = "BGRX"


class AppSettings(BaseModel):
    rx: RxPresetSettings = Field(default_factory=RxPresetSettings)
    tx: TxPresetSettings = Field(default_factory=TxPresetSettings)


class MultiviewSlotStatus(BaseModel):
    source_name: str
    is_connected: bool = False
    width: int = 0
    height: int = 0
    audio_level_l: float = -60.0
    audio_level_r: float = -60.0


class MultiviewStartRequest(BaseModel):
    sources: Optional[List[str]] = Field(default=None, description="List of source names, or null for auto-discovery")
    fullscreen: bool = False


class MultiviewStatus(BaseModel):
    running: bool = False
    fullscreen: bool = False
    slots: List[MultiviewSlotStatus] = Field(default_factory=list)
    error: Optional[str] = None


class WebRTCOfferRequest(BaseModel):
    source: str
    sdp: str
    type: str


class WebRTCAnswerResponse(BaseModel):
    sdp: str
    type: str


