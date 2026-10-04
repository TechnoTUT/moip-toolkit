"""
core.multiview
Multi-source NDI Receiver and Grid Renderer using SDL2 / OpenGL.
Supports auto grid layout (1x1, 1x2, 2x2, 3x3), NDI source labels, and real-time audio meters.
"""
from __future__ import annotations
import math
import time
from typing import List, Optional, Dict
import numpy as np

import sdl2
import sdl2.ext
try:
    from OpenGL.GL import (
        GL_TEXTURE_2D, GL_PROJECTION, GL_MODELVIEW, GL_COLOR_BUFFER_BIT,
        GL_RGBA, GL_BGRA, GL_UNSIGNED_BYTE, GL_QUADS, GL_LINES, GL_LINEAR, GL_NEAREST,
        GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER,
        glEnable, glDisable, glViewport, glMatrixMode, glLoadIdentity, glOrtho,
        glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
        glTexSubImage2D, glClearColor, glClear, glBegin, glTexCoord2f,
        glVertex2f, glEnd, glColor4f, glDeleteTextures
    )
except Exception:
    pass

from cyndilib.receiver import Receiver
from cyndilib.finder import Finder
from cyndilib.video_frame import VideoFrameSync
try:
    from cyndilib.audio_frame import AudioFrameSync
except ImportError:
    AudioFrameSync = None
from cyndilib.wrapper.ndi_recv import RecvColorFormat, RecvBandwidth

from core.rx import create_text_rgba_image, get_local_ip, init_window


class SlotState:
    """State for a single video slot in the multiview grid."""
    def __init__(self, source_name: str):
        self.source_name = source_name
        self.receiver: Optional[Receiver] = None
        self.vf = VideoFrameSync()
        self.af = AudioFrameSync() if AudioFrameSync is not None else None
        self.texture_id = glGenTextures(1)
        self.is_connected = False
        self.reconnect_cooldown = 0.0
        self.connect_timeout = 0.0
        self.last_connected_time = 0.0
        self.frame_data: Optional[bytes] = None
        self.frame_w = 0
        self.frame_h = 0
        self.last_timecode = -1.0
        self.is_tex_init = False
        self.audio_levels = [-60.0, -60.0]
        self.audio_peaks = [-60.0, -60.0]

    def cleanup(self):
        self.receiver = None
        if self.texture_id:
            try:
                glDeleteTextures(1, [self.texture_id])
            except Exception:
                pass


def compute_grid_dimensions(count: int) -> tuple[int, int]:
    """Calculate (cols, rows) for a given number of video sources."""
    if count <= 1:
        return (1, 1)
    elif count == 2:
        return (2, 1)
    elif count <= 4:
        return (2, 2)
    elif count <= 6:
        return (3, 2)
    elif count <= 9:
        return (3, 3)
    else:
        cols = int(math.ceil(math.sqrt(count)))
        rows = int(math.ceil(count / cols))
        return (cols, rows)


def render_slot_osd(
    slot: SlotState,
    label_tex_id: int,
    x0: float, y0: float, x1: float, y1: float,
    win_w: int, win_h: int
):
    """
    Renders border, NDI source name banner, and audio level bars in normalized device coords [-1, 1].
    """
    glDisable(GL_TEXTURE_2D)

    # 1. Tile border lines
    glColor4f(0.2, 0.25, 0.35, 0.8)
    glBegin(GL_LINES)
    glVertex2f(x0, y0); glVertex2f(x1, y0)
    glVertex2f(x1, y0); glVertex2f(x1, y1)
    glVertex2f(x1, y1); glVertex2f(x0, y1)
    glVertex2f(x0, y1); glVertex2f(x0, y0)
    glEnd()

    # 2. Audio Meter Bar (Bottom of slot)
    # L / R channel horizontal bar
    slot_w = x1 - x0
    slot_h = y1 - y0
    meter_h = min(0.04, slot_h * 0.08)
    meter_y0 = y0 + 0.01
    meter_y1 = meter_y0 + meter_h

    # Meter background
    glColor4f(0.08, 0.1, 0.14, 0.9)
    glBegin(GL_QUADS)
    glVertex2f(x0 + 0.01, meter_y0)
    glVertex2f(x1 - 0.01, meter_y0)
    glVertex2f(x1 - 0.01, meter_y1)
    glVertex2f(x0 + 0.01, meter_y1)
    glEnd()

    # Active audio fill
    bar_w = (slot_w - 0.02)
    # Left channel
    db_l = max(-60.0, min(0.0, slot.audio_levels[0]))
    frac_l = (db_l + 60.0) / 60.0
    # Right channel
    db_r = max(-60.0, min(0.0, slot.audio_levels[1]))
    frac_r = (db_r + 60.0) / 60.0

    half_h = (meter_y1 - meter_y0) / 2.0

    # Draw Ch L (Top half)
    if frac_l > 0.01:
        if db_l > -3.0:
            glColor4f(0.9, 0.2, 0.2, 1.0)
        elif db_l > -12.0:
            glColor4f(0.9, 0.7, 0.1, 1.0)
        else:
            glColor4f(0.1, 0.8, 0.4, 1.0)

        glBegin(GL_QUADS)
        glVertex2f(x0 + 0.01, meter_y0 + half_h)
        glVertex2f(x0 + 0.01 + bar_w * frac_l, meter_y0 + half_h)
        glVertex2f(x0 + 0.01 + bar_w * frac_l, meter_y1)
        glVertex2f(x0 + 0.01, meter_y1)
        glEnd()

    # Draw Ch R (Bottom half)
    if frac_r > 0.01:
        if db_r > -3.0:
            glColor4f(0.9, 0.2, 0.2, 1.0)
        elif db_r > -12.0:
            glColor4f(0.9, 0.7, 0.1, 1.0)
        else:
            glColor4f(0.1, 0.8, 0.4, 1.0)

        glBegin(GL_QUADS)
        glVertex2f(x0 + 0.01, meter_y0)
        glVertex2f(x0 + 0.01 + bar_w * frac_r, meter_y0)
        glVertex2f(x0 + 0.01 + bar_w * frac_r, meter_y0 + half_h)
        glVertex2f(x0 + 0.01, meter_y0 + half_h)
        glEnd()

    # 3. Label text (Top-left of slot)
    glEnable(GL_TEXTURE_2D)
    label_text = slot.source_name[:24]
    rgba_bytes, img_w, img_h = create_text_rgba_image(label_text, scale=2)

    glBindTexture(GL_TEXTURE_2D, label_tex_id)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, img_w, img_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba_bytes)

    lbl_w = (img_w / max(win_w, 1)) * 2.0
    lbl_h = (img_h / max(win_h, 1)) * 2.0

    lx0 = x0 + 0.01
    lx1 = lx0 + lbl_w
    ly1 = y1 - 0.01
    ly0 = ly1 - lbl_h

    glColor4f(1.0, 1.0, 1.0, 1.0)
    glBegin(GL_QUADS)
    glTexCoord2f(0.0, 0.0); glVertex2f(lx0, ly1)
    glTexCoord2f(1.0, 0.0); glVertex2f(lx1, ly1)
    glTexCoord2f(1.0, 1.0); glVertex2f(lx1, ly0)
    glTexCoord2f(0.0, 1.0); glVertex2f(lx0, ly0)
    glEnd()


def play_multiview(
    source_names: Optional[List[str]] = None,
    fullscreen: bool = False,
    auto_discover: bool = True,
    log_fn=print
):
    """Main SDL2 playback loop for Multi-Viewer display."""
    finder = Finder()
    time.sleep(0.5)

    window = init_window("NDI Multi-Viewer", 1280, 720, fullscreen)
    w_ptr, h_ptr = sdl2.c_int(), sdl2.c_int()
    sdl2.SDL_GetWindowSize(window, w_ptr, h_ptr)
    win_w, win_h = w_ptr.value, h_ptr.value

    glEnable(GL_TEXTURE_2D)
    glViewport(0, 0, win_w, win_h)
    glMatrixMode(GL_PROJECTION)
    glLoadIdentity()
    glOrtho(-1, 1, -1, 1, -1, 1)
    glMatrixMode(GL_MODELVIEW)
    glLoadIdentity()

    label_tex_id = glGenTextures(1)
    slots: Dict[str, SlotState] = {}

    running = True
    event = sdl2.SDL_Event()
    last_discovery_time = 0.0

    try:
        while running:
            # 1. Event processing
            while sdl2.SDL_PollEvent(event):
                if event.type == sdl2.SDL_QUIT:
                    running = False
                elif event.type == sdl2.SDL_KEYDOWN and event.key.keysym.sym == sdl2.SDLK_ESCAPE:
                    running = False
                elif event.type == sdl2.SDL_WINDOWEVENT and event.window.event == sdl2.SDL_WINDOWEVENT_RESIZED:
                    win_w, win_h = event.window.data1, event.window.data2
                    glViewport(0, 0, win_w, win_h)

            now = time.time()

            # 2. Source discovery & slot sync
            if auto_discover and (now - last_discovery_time > 2.0):
                last_discovery_time = now
                finder.wait_for_sources(0)
                current_discovered = [s.name for s in finder]
                if not source_names:
                    target_sources = current_discovered
                else:
                    target_sources = [s for s in source_names if s in current_discovered] or source_names

                # Remove slots that are gone
                for name in list(slots.keys()):
                    if name not in target_sources:
                        slots[name].cleanup()
                        del slots[name]

                # Add new slots
                for name in target_sources:
                    if name not in slots:
                        slots[name] = SlotState(name)
            elif not slots and source_names:
                for name in source_names:
                    slots[name] = SlotState(name)

            active_slots = list(slots.values())
            num_slots = max(1, len(active_slots))
            cols, rows = compute_grid_dimensions(num_slots)

            # Clear background to dark studio charcoal
            glClearColor(0.04, 0.05, 0.07, 1.0)
            glClear(GL_COLOR_BUFFER_BIT)

            cell_w = 2.0 / cols
            cell_h = 2.0 / rows

            # 3. Capture & Render each slot
            for idx, slot in enumerate(active_slots):
                col_idx = idx % cols
                row_idx = idx // cols

                x0 = -1.0 + col_idx * cell_w
                x1 = x0 + cell_w
                y1 = 1.0 - row_idx * cell_h
                y0 = y1 - cell_h

                # Manage Receiver connection
                if not slot.is_connected:
                    if slot.receiver is None:
                        if now >= slot.reconnect_cooldown:
                            try:
                                matched = None
                                for s in finder:
                                    s_name = getattr(s, "name", "")
                                    s_stream = getattr(s, "stream_name", "")
                                    if (
                                        s_name == slot.source_name
                                        or s_stream == slot.source_name
                                        or slot.source_name in s_name
                                        or (s_stream and s_stream in slot.source_name)
                                    ):
                                        matched = s
                                        break
                                if matched is not None:
                                    rec = Receiver(
                                        color_format=RecvColorFormat.RGBX_RGBA,
                                        bandwidth=RecvBandwidth.highest,
                                    )
                                    rec.frame_sync.set_video_frame(slot.vf)
                                    if slot.af is not None:
                                        try:
                                            rec.frame_sync.set_audio_frame(slot.af)
                                        except Exception:
                                            pass
                                    rec.set_source(matched)
                                    slot.receiver = rec
                                    slot.connect_timeout = now + 5.0
                                else:
                                    slot.reconnect_cooldown = now + 1.5
                            except Exception:
                                slot.receiver = None
                                slot.reconnect_cooldown = now + 2.0
                    else:
                        # Asynchronous connection handshake in progress
                        if slot.receiver.is_connected():
                            slot.last_connected_time = now
                            try:
                                slot.receiver.frame_sync.capture_video()
                                tw, th = slot.vf.get_resolution()
                                if tw > 0 and th > 0 and slot.vf.get_data_size() > 0:
                                    slot.frame_data = bytes(slot.vf)
                                    slot.frame_w, slot.frame_h = tw, th
                                    slot.is_connected = True
                            except Exception:
                                pass
                        elif now >= slot.connect_timeout:
                            slot.receiver = None
                            slot.reconnect_cooldown = now + 2.0
                else:
                    if slot.receiver.is_connected():
                        slot.last_connected_time = now
                    elif now - slot.last_connected_time > 4.0:
                        slot.is_connected = False
                        slot.receiver = None
                        slot.reconnect_cooldown = now + 2.0

                    if slot.receiver is not None:
                        # Capture video
                        try:
                            slot.receiver.frame_sync.capture_video()
                            tw, th = slot.vf.get_resolution()
                            tc = slot.vf.get_timecode_posix()
                            if tw > 0 and th > 0 and slot.vf.get_data_size() > 0:
                                if tc != slot.last_timecode or slot.frame_data is None:
                                    slot.last_timecode = tc
                                    slot.frame_data = bytes(slot.vf)
                                    if slot.frame_w != tw or slot.frame_h != th:
                                        slot.is_tex_init = False
                                    slot.frame_w, slot.frame_h = tw, th
                        except Exception:
                            pass

                        # Capture audio
                        if slot.af is not None:
                            try:
                                num_samples = slot.receiver.frame_sync.capture_audio(1024)
                                if num_samples and num_samples > 0:
                                    raw_af = bytes(slot.af)
                                    audio_arr = np.frombuffer(raw_af, dtype=np.float32)
                                    num_ch = slot.af.num_channels if hasattr(slot.af, "num_channels") and slot.af.num_channels > 0 else 2
                                    if audio_arr.size >= num_ch:
                                        audio_arr = audio_arr[: num_samples * num_ch].reshape((-1, num_ch))
                                        ch_levels = []
                                        for ch in range(min(2, num_ch)):
                                            ch_data = audio_arr[:, ch]
                                            r_val = np.sqrt(np.mean(np.square(ch_data))) if ch_data.size > 0 else 0.0
                                            r_db = 20.0 * np.log10(max(1e-4, float(r_val)))
                                            ch_levels.append(round(float(max(-60.0, min(0.0, r_db))), 1))
                                        if len(ch_levels) == 1:
                                            ch_levels.append(ch_levels[0])
                                        slot.audio_levels = ch_levels[:2]
                            except Exception:
                                pass

                # Render Video Quad
                if slot.frame_data and slot.frame_w > 0 and slot.frame_h > 0:
                    glEnable(GL_TEXTURE_2D)
                    glBindTexture(GL_TEXTURE_2D, slot.texture_id)
                    if not slot.is_tex_init:
                        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, slot.frame_w, slot.frame_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, slot.frame_data)
                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
                        slot.is_tex_init = True
                    else:
                        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, slot.frame_w, slot.frame_h, GL_RGBA, GL_UNSIGNED_BYTE, slot.frame_data)

                    # Compute aspect-preserving quad inside cell
                    src_ratio = slot.frame_w / slot.frame_h
                    pixel_cell_w = (win_w / cols)
                    pixel_cell_h = (win_h / rows)
                    dst_ratio = pixel_cell_w / pixel_cell_h

                    if src_ratio > dst_ratio:
                        qx_scale = 1.0
                        qy_scale = dst_ratio / src_ratio
                    else:
                        qx_scale = src_ratio / dst_ratio
                        qy_scale = 1.0

                    mid_x = (x0 + x1) / 2.0
                    mid_y = (y0 + y1) / 2.0
                    half_w = (cell_w / 2.0) * qx_scale
                    half_h = (cell_h / 2.0) * qy_scale

                    glColor4f(1.0, 1.0, 1.0, 1.0)
                    glBegin(GL_QUADS)
                    glTexCoord2f(0.0, 1.0); glVertex2f(mid_x - half_w, mid_y - half_h)
                    glTexCoord2f(1.0, 1.0); glVertex2f(mid_x + half_w, mid_y - half_h)
                    glTexCoord2f(1.0, 0.0); glVertex2f(mid_x + half_w, mid_y + half_h)
                    glTexCoord2f(0.0, 0.0); glVertex2f(mid_x - half_w, mid_y + half_h)
                    glEnd()

                # Render OSD: Border, Audio Meter, and Label
                render_slot_osd(slot, label_tex_id, x0, y0, x1, y1, win_w, win_h)

            sdl2.SDL_GL_SwapWindow(window)

    finally:
        for slot in slots.values():
            slot.cleanup()
        if label_tex_id:
            glDeleteTextures(1, [label_tex_id])
        sdl2.SDL_DestroyWindow(window)
        sdl2.SDL_Quit()
        log_fn("Multi-Viewer terminated.")
