"""
backend.settings
Manages server-side persistent settings (auto_start, last preset values) stored in JSON.
"""
from __future__ import annotations
import json
import os
import threading
from typing import Optional
from backend.models import AppSettings, RxPresetSettings, TxPresetSettings, RxStartRequest, TxStartRequest

CONFIG_PATH = os.path.expanduser("~/.config/utone-ndi/settings.json")


class SettingsManager:
    def __init__(self, file_path: str = CONFIG_PATH):
        self.file_path = file_path
        self.lock = threading.Lock()
        self.settings: AppSettings = self._load()

    def _load(self) -> AppSettings:
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return AppSettings.model_validate(data)
        except Exception as e:
            print(f"[SettingsManager] Warning: failed to load {self.file_path}: {e}")
        return AppSettings()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                f.write(self.settings.model_dump_json(indent=2))
        except Exception as e:
            print(f"[SettingsManager] Error saving settings to {self.file_path}: {e}")

    def get_settings(self) -> AppSettings:
        with self.lock:
            return self.settings.model_copy(deep=True)

    def update_settings(self, new_settings: AppSettings):
        with self.lock:
            self.settings = new_settings
            self.save()

    def on_rx_started(self, req: RxStartRequest):
        """Called whenever RX is started to save the last session preset."""
        with self.lock:
            self.settings.rx.sender_name = req.sender_name
            self.settings.rx.recv_fmt = req.recv_fmt
            self.settings.rx.recv_bandwidth = req.recv_bandwidth
            self.settings.rx.fullscreen = req.fullscreen
            self.save()

    def on_tx_started(self, req: TxStartRequest):
        """Called whenever TX is started to save the last session preset."""
        with self.lock:
            self.settings.tx.sender_name = req.sender_name
            self.settings.tx.video_device = req.video_device
            self.settings.tx.audio_device = req.audio_device
            self.settings.tx.no_audio = req.no_audio
            self.settings.tx.x_res = req.x_res
            self.settings.tx.y_res = req.y_res
            self.settings.tx.fps = req.fps
            self.settings.tx.pix_fmt = req.pix_fmt
            self.save()


settings_manager = SettingsManager()
