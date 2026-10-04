"""
backend.devices
Utilities to scan and list available video and audio devices.
"""
from __future__ import annotations
import os
import cv2
try:
    import sounddevice as sd
except Exception:
    sd = None
from typing import List
from backend.models import VideoDeviceItem, AudioDeviceItem


def list_video_devices(max_scan: int = 8) -> List[VideoDeviceItem]:
    devices: List[VideoDeviceItem] = [
        VideoDeviceItem(
            index=-1,
            name="Color Bars (Test Pattern)",
            is_available=True
        )
    ]
    
    # 1. First check /dev/video* to find valid candidates
    candidates = []
    if os.path.exists("/dev"):
        for entry in sorted(os.listdir("/dev")):
            if entry.startswith("video") and entry[5:].isdigit():
                candidates.append(int(entry[5:]))
    
    if not candidates:
        candidates = list(range(max_scan))

    for idx in sorted(set(candidates))[:max_scan]:
        try:
            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            if cap.isOpened():
                devices.append(VideoDeviceItem(
                    index=idx,
                    name=f"Camera {idx}",
                    is_available=True
                ))
                cap.release()
        except Exception:
            pass

    return devices


def list_audio_devices() -> List[AudioDeviceItem]:
    devices: List[AudioDeviceItem] = []
    if sd is None:
        return devices
    try:
        all_devs = sd.query_devices()
        for idx, dev in enumerate(all_devs):
            # Only list devices capable of audio input
            if dev.get("max_input_channels", 0) > 0:
                devices.append(AudioDeviceItem(
                    index=idx,
                    name=str(dev.get("name", f"Device {idx}")),
                    max_input_channels=int(dev.get("max_input_channels", 0)),
                    default_samplerate=float(dev.get("default_samplerate", 44100.0))
                ))
    except Exception as e:
        print(f"Error querying audio devices: {e}")
    return devices
