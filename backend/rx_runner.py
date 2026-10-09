"""
backend.rx_runner
Process / Thread controller for NDI Receiver.
Runs SDL2 window in a controlled process/thread with dynamic source switching.
"""
from __future__ import annotations
import multiprocessing as mp
import time
import queue
from typing import Optional
from backend.models import RxStatus


class RxWorker:
    def __init__(self, command_q: mp.Queue, status_q: mp.Queue, init_options: dict):
        self.command_q = command_q
        self.status_q = status_q
        self.init_options = init_options
        self.running = True

        # Late imports inside subprocess to avoid OpenGL / SDL context sharing issues
        import sdl2
        import sdl2.ext
        from OpenGL.GL import (
            GL_TEXTURE_2D, GL_PROJECTION, GL_MODELVIEW, GL_COLOR_BUFFER_BIT,
            GL_RGBA, GL_BGRA, GL_UNSIGNED_BYTE, GL_QUADS, GL_LINEAR,
            glEnable, glViewport, glMatrixMode, glLoadIdentity, glOrtho,
            glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
            glTexSubImage2D, glClearColor, glClear, glBegin, glTexCoord2f,
            glVertex2f, glEnd, glDeleteTextures
        )
        from cyndilib.receiver import Receiver
        from cyndilib.video_frame import VideoFrameSync
        try:
            from cyndilib.audio_frame import AudioFrameSync
        except ImportError:
            AudioFrameSync = None
        from cyndilib.finder import Finder
        from core.reconnecting_receiver import ReconnectingReceiver
        from core.rx import (
            RecvFmt, Bandwidth, Options, render_texture, render_waiting_message,
            render_ip_banner, get_local_ip, init_window
        )
        import numpy as np

        self.sdl2 = sdl2
        self.glDeleteTextures = glDeleteTextures
        self.render_texture = render_texture
        self.render_waiting_message = render_waiting_message
        self.render_ip_banner = render_ip_banner
        self.np = np

        self.finder = Finder()
        try:
            self.finder.open()
        except Exception:
            pass

        self.options = Options(
            sender_name=init_options["sender_name"],
            recv_fmt=RecvFmt.from_str(init_options.get("recv_fmt", "rgb")),
            recv_bandwidth=Bandwidth.from_str(init_options.get("recv_bandwidth", "highest")),
            fullscreen=init_options.get("fullscreen", False),
        )

        self.window = None
        self.texture_id = None
        self.overlay_tex_id = None
        self.win_w = 1280
        self.win_h = 720

        self.vf = VideoFrameSync()
        self.af = AudioFrameSync() if AudioFrameSync is not None else None
        self.rr = ReconnectingReceiver(
            self.options.recv_fmt.value, self.options.recv_bandwidth.value, self.vf, self.af,
            connect_timeout=10.0,
        )
        self.receiver = None

        self.current_source_name = self.options.sender_name
        self.is_connected = False
        self.reconnect_cooldown_until = 0.0
        self.connect_timeout_until = 0.0
        self.last_connected_time = 0.0

        self.has_frame = False
        self.last_frame_w = 0
        self.last_frame_h = 0
        self.last_timecode = -1.0
        self.is_texture_initialized = False
        self.last_status_report = 0.0
        self.last_audio_calc_time = 0.0
        self.frames_rendered = 0
        self.last_audio_levels = [-60.0, -60.0]
        self.last_audio_peaks = [-60.0, -60.0]
        self.start_time = time.time()
        self.local_ip = get_local_ip()

        self._init_gl(init_window, glEnable, glViewport, glMatrixMode, glLoadIdentity, glOrtho, glGenTextures, GL_TEXTURE_2D, GL_PROJECTION, GL_MODELVIEW)

    def _init_gl(self, init_window, glEnable, glViewport, glMatrixMode, glLoadIdentity, glOrtho, glGenTextures, GL_TEXTURE_2D, GL_PROJECTION, GL_MODELVIEW):
        self.window = init_window("NDI Viewer", 1280, 720, self.options.fullscreen)
        w_ptr, h_ptr = self.sdl2.c_int(), self.sdl2.c_int()
        self.sdl2.SDL_GetWindowSize(self.window, w_ptr, h_ptr)
        self.win_w, self.win_h = w_ptr.value, h_ptr.value

        glEnable(GL_TEXTURE_2D)
        glViewport(0, 0, self.win_w, self.win_h)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(-1, 1, -1, 1, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()

        self.texture_id = glGenTextures(1)
        self.overlay_tex_id = glGenTextures(1)

    def drop_receiver(self):
        self.rr.drop(time.time(), cooldown=0.0)
        self.receiver = None

    def handle_commands(self):
        while not self.command_q.empty():
            try:
                cmd = self.command_q.get_nowait()
                action = cmd.get("action")
                if action == "stop":
                    self.running = False
                    break
                elif action == "switch":
                    new_source = cmd.get("sender_name")
                    if new_source and new_source != self.current_source_name:
                        self.current_source_name = new_source
                        self.drop_receiver()
                        self.is_connected = False
                        self.reconnect_cooldown_until = 0.0
                        self.is_texture_initialized = False
                        self.has_frame = False
                        self.start_time = time.time()
                elif action == "toggle_fullscreen":
                    flags = self.sdl2.SDL_GetWindowFlags(self.window)
                    is_fs = bool(flags & self.sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP)
                    self.sdl2.SDL_SetWindowFullscreen(
                        self.window,
                        0 if is_fs else self.sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP
                    )
            except queue.Empty:
                break

    def handle_events(self, event):
        while self.sdl2.SDL_PollEvent(event):
            if event.type == self.sdl2.SDL_QUIT:
                self.running = False
            elif event.type == self.sdl2.SDL_KEYDOWN and event.key.keysym.sym == self.sdl2.SDLK_ESCAPE:
                self.running = False
            elif event.type == self.sdl2.SDL_WINDOWEVENT and event.window.event == self.sdl2.SDL_WINDOWEVENT_RESIZED:
                self.win_w, self.win_h = event.window.data1, event.window.data2
                from OpenGL.GL import glViewport
                glViewport(0, 0, self.win_w, self.win_h)

    def update_connection(self, now: float):
        self.render_waiting_message(self.overlay_tex_id, self.local_ip, self.current_source_name, self.win_w, self.win_h)
        if self.receiver is None:
            if now >= self.reconnect_cooldown_until:
                try:
                    try:
                        self.finder.wait_for_sources(0)
                    except Exception:
                        pass

                    matched = None
                    available = list(self.finder)
                    for s in available:
                        s_name = getattr(s, "name", "")
                        s_stream = getattr(s, "stream_name", "")
                        if (
                            s_name == self.current_source_name
                            or s_stream == self.current_source_name
                            or self.current_source_name in s_name
                            or (s_stream and s_stream in self.current_source_name)
                        ):
                            matched = s
                            break

                    if matched is not None:
                        self.receiver = self.rr.attach(matched, now)
                        self.connect_timeout_until = now + 10.0
                        self.last_connected_time = now
                        print(f"[RX RUNNER] Found source {matched.name}, initiating connection...", flush=True)
                    else:
                        self.reconnect_cooldown_until = now + 0.5
                except Exception as e:
                    print(f"[RX RUNNER] Error creating receiver for {self.current_source_name}: {e}", flush=True)
                    self.drop_receiver()
                    self.reconnect_cooldown_until = now + 1.0
        else:
            if self.receiver.is_connected():
                self.last_connected_time = now
                try:
                    self.receiver.frame_sync.capture_video()
                    tw, th = self.vf.get_resolution()
                    if tw > 0 and th > 0 and self.vf.get_data_size() > 0:
                        self.has_frame = True
                        self.last_frame_w, self.last_frame_h = tw, th
                        self.is_connected = True
                        print(f"[RX RUNNER] First frame captured ({tw}x{th}). Connected to {self.current_source_name}!", flush=True)
                except Exception as e:
                    print(f"[RX RUNNER] Capture attempt exception: {e}", flush=True)
            elif now >= self.connect_timeout_until:
                print(f"[RX RUNNER] Connection timeout waiting for {self.current_source_name}", flush=True)
                self.drop_receiver()
                self.reconnect_cooldown_until = now + 1.0

        self.sdl2.SDL_GL_SwapWindow(self.window)
        time.sleep(0.016)

    def render_active_frame(self, now: float, show_banner: bool):
        if self.receiver.is_connected():
            self.last_connected_time = now
        elif now - self.last_connected_time > 4.0:
            print(f"[RX RUNNER] Connection lost to {self.current_source_name}", flush=True)
            self.is_connected = False
            self.drop_receiver()
            self.reconnect_cooldown_until = now + 1.0
            self.has_frame, self.last_frame_w, self.last_frame_h = False, 0, 0
            self.last_timecode = -1.0
            self.is_texture_initialized = False

        if self.receiver is not None:
            new_frame = False
            try:
                self.receiver.frame_sync.capture_video()
                tex_w, tex_h = self.vf.get_resolution()
                curr_timecode = self.vf.get_timecode_posix()
                if tex_w > 0 and tex_h > 0 and self.vf.get_data_size() > 0:
                    if curr_timecode != self.last_timecode or not self.has_frame:
                        new_frame = True
                        self.last_timecode = curr_timecode
                        self.has_frame = True
                        if self.last_frame_w != tex_w or self.last_frame_h != tex_h:
                            self.is_texture_initialized = False
                        self.last_frame_w, self.last_frame_h = tex_w, tex_h
                        self.frames_rendered += 1

                        with memoryview(self.vf) as mv:
                            frame_arr = self.np.frombuffer(mv, dtype=self.np.uint8)
                            try:
                                self.is_texture_initialized = self.render_texture(
                                    frame_arr, self.last_frame_w, self.last_frame_h, self.win_w, self.win_h,
                                    self.texture_id, self.options.recv_fmt, self.is_texture_initialized
                                )
                            finally:
                                del frame_arr
                        if show_banner:
                            self.render_ip_banner(self.overlay_tex_id, self.local_ip, self.current_source_name, self.win_w, self.win_h)
                        self.sdl2.SDL_GL_SwapWindow(self.window)
            except Exception as e:
                import traceback
                print(f"[RX RUNNER] Error rendering frame: {e}", flush=True)
                traceback.print_exc()
                self.drop_receiver()
                self.is_connected = False
                self.reconnect_cooldown_until = now + 1.0
                self.has_frame = False
                self.last_timecode = -1.0
                self.is_texture_initialized = False
            if not new_frame:
                time.sleep(0.002)

            if self.af is not None:
                try:
                    num_samples = self.receiver.frame_sync.capture_audio(1024)
                    if num_samples and num_samples > 0 and (now - self.last_audio_calc_time) >= 0.06:
                        self.last_audio_calc_time = now
                        raw_af = bytes(self.af)
                        audio_arr = self.np.frombuffer(raw_af, dtype=self.np.float32)
                        num_ch = self.af.num_channels if hasattr(self.af, "num_channels") and self.af.num_channels > 0 else 2
                        if audio_arr.size >= num_ch:
                            audio_arr = audio_arr[: num_samples * num_ch].reshape((-1, num_ch))
                            ch_levels = []
                            ch_peaks = []
                            for ch in range(min(2, num_ch)):
                                ch_data = audio_arr[:, ch]
                                p_val = self.np.max(self.np.abs(ch_data)) if ch_data.size > 0 else 0.0
                                r_val = self.np.sqrt(self.np.mean(self.np.square(ch_data))) if ch_data.size > 0 else 0.0
                                p_db = 20.0 * self.np.log10(max(1e-4, float(p_val)))
                                r_db = 20.0 * self.np.log10(max(1e-4, float(r_val)))
                                ch_peaks.append(round(float(max(-60.0, min(0.0, p_db))), 1))
                                ch_levels.append(round(float(max(-60.0, min(0.0, r_db))), 1))
                            if len(ch_levels) == 1:
                                ch_levels.append(ch_levels[0])
                                ch_peaks.append(ch_peaks[0])
                            self.last_audio_levels = ch_levels[:2]
                            self.last_audio_peaks = ch_peaks[:2]
                except Exception:
                    pass

    def report_status(self, now: float):
        elapsed = now - self.last_status_report
        if elapsed >= 0.5:
            real_fps = round(self.frames_rendered / elapsed, 1) if elapsed > 0 else 0.0
            self.frames_rendered = 0
            self.last_status_report = now
            self.status_q.put({
                "type": "status",
                "running": True,
                "is_connected": self.is_connected,
                "current_source": self.current_source_name,
                "width": self.last_frame_w,
                "height": self.last_frame_h,
                "fps_real": real_fps,
                "audio_level_l": self.last_audio_levels[0],
                "audio_level_r": self.last_audio_levels[1],
                "audio_peak_l": self.last_audio_peaks[0],
                "audio_peak_r": self.last_audio_peaks[1],
            })

    def run(self):
        event = self.sdl2.SDL_Event()
        while self.running:
            self.handle_commands()
            if not self.running:
                break
            self.handle_events(event)
            now = time.time()
            show_banner = (now - self.start_time < 30.0)
            if not self.is_connected:
                self.update_connection(now)
            else:
                self.render_active_frame(now, show_banner)
            self.report_status(now)

    def cleanup(self):
        self.receiver = None
        if hasattr(self.finder, "close") and getattr(self.finder, "is_open", False):
            try:
                self.finder.close()
            except Exception:
                pass
        if self.texture_id is not None and self.overlay_tex_id is not None:
            try:
                self.glDeleteTextures(2, [self.texture_id, self.overlay_tex_id])
            except Exception:
                pass
        if self.window is not None:
            try:
                self.sdl2.SDL_DestroyWindow(self.window)
            except Exception:
                pass
        try:
            self.sdl2.SDL_Quit()
        except Exception:
            pass
        self.status_q.put({
            "type": "status", "running": False, "is_connected": False,
            "current_source": None, "width": 0, "height": 0,
            "audio_level_l": -60.0, "audio_level_r": -60.0
        })


def _rx_worker_process(command_q: mp.Queue, status_q: mp.Queue, init_options: dict):
    worker = None
    try:
        worker = RxWorker(command_q, status_q, init_options)
        worker.run()
    except Exception as e:
        status_q.put({"type": "error", "error": str(e)})
    finally:
        if worker is not None:
            worker.cleanup()


class RxRunner:
    def __init__(self):
        self.process: Optional[mp.Process] = None
        self.command_q: Optional[mp.Queue] = None
        self.status_q: Optional[mp.Queue] = None
        self._ctx = mp.get_context("spawn")
        self._current_status = RxStatus(
            running=False,
            is_connected=False,
            current_source=None,
            width=0,
            height=0
        )

    def get_status(self) -> RxStatus:
        self._update_status_from_queue()
        if self.process and not self.process.is_alive():
            self._current_status.running = False
            self._current_status.is_connected = False
            self.process = None
        return self._current_status

    def _update_status_from_queue(self):
        if not self.status_q:
            return
        while not self.status_q.empty():
            try:
                msg = self.status_q.get_nowait()
                if msg.get("type") == "status":
                    self._current_status.running = msg.get("running", False)
                    self._current_status.is_connected = msg.get("is_connected", False)
                    self._current_source = msg.get("current_source")
                    self._current_status.current_source = msg.get("current_source")
                    self._current_status.width = msg.get("width", 0)
                    self._current_status.height = msg.get("height", 0)
                    self._current_status.fps_real = msg.get("fps_real", 0.0)
                    self._current_status.audio_level_l = msg.get("audio_level_l", -60.0)
                    self._current_status.audio_level_r = msg.get("audio_level_r", -60.0)
                    self._current_status.audio_peak_l = msg.get("audio_peak_l", -60.0)
                    self._current_status.audio_peak_r = msg.get("audio_peak_r", -60.0)
                elif msg.get("type") == "error":
                    self._current_status.error = msg.get("error")
            except queue.Empty:
                break

    def start(self, sender_name: str, recv_fmt: str = "rgb", recv_bandwidth: str = "highest", fullscreen: bool = False):
        if self.process and self.process.is_alive():
            raise RuntimeError("RX is already running. Please switch source or stop first.")

        self.command_q = self._ctx.Queue()
        self.status_q = self._ctx.Queue()
        self._current_status = RxStatus(
            running=True,
            is_connected=False,
            current_source=sender_name,
            recv_fmt=recv_fmt,
            recv_bandwidth=recv_bandwidth,
            fullscreen=fullscreen,
            width=0,
            height=0,
            error=None
        )

        init_opts = {
            "sender_name": sender_name,
            "recv_fmt": recv_fmt,
            "recv_bandwidth": recv_bandwidth,
            "fullscreen": fullscreen
        }

        self.process = self._ctx.Process(
            target=_rx_worker_process,
            args=(self.command_q, self.status_q, init_opts),
            daemon=True
        )
        self.process.start()

    def switch_source(self, sender_name: str):
        if not self.process or not self.process.is_alive():
            # If not running, start it
            self.start(sender_name=sender_name)
            return

        if self.command_q:
            self.command_q.put({"action": "switch", "sender_name": sender_name})
            self._current_status.current_source = sender_name

    def stop(self):
        if self.command_q:
            try:
                self.command_q.put_nowait({"action": "stop"})
            except Exception:
                pass

        if self.process and self.process.is_alive():
            self.process.join(timeout=1.0)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=0.5)

        self.process = None
        self.command_q = None
        self.status_q = None
        self._current_status = RxStatus(
            running=False,
            is_connected=False,
            current_source=None,
            width=0,
            height=0,
            error=None
        )


rx_runner = RxRunner()
