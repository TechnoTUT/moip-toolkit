"""
backend.tx_runner
Process / Thread controller for NDI Sender (TX).
Runs camera capture, audio capture, and NDI streaming with start/stop control.
"""
from __future__ import annotations
import multiprocessing as mp
import queue
import time
from typing import Optional
from backend.models import TxStatus, TxStartRequest


def _tx_worker_process(command_q: mp.Queue, status_q: mp.Queue, opts_dict: dict):
    from core.tx import PixFmt, Options, VideoSourceThread, VideoSendThread, AudioSendThread, parse_frame_rate
    from cyndilib.sender import Sender
    from cyndilib.video_frame import VideoSendFrame
    from cyndilib.audio_frame import AudioSendFrame
    import sounddevice as sd
    import numpy as np
    from fractions import Fraction
    import queue as std_queue

    AUDIO_CHUNKS_PER_FRAME = 4

    try:
        frame_rate = float(parse_frame_rate(opts_dict["fps"]))
        opts = Options(
            pix_fmt=PixFmt.from_str(opts_dict.get("pix_fmt", "BGRX")),
            xres=int(opts_dict.get("x_res", 1920)),
            yres=int(opts_dict.get("y_res", 1080)),
            fps=frame_rate,
            video_device=int(opts_dict.get("video_device", 0)),
            sender_name=str(opts_dict.get("sender_name", "TX")),
            no_audio=bool(opts_dict.get("no_audio", False)),
            audio_device=opts_dict.get("audio_device"),
            sample_rate=int(opts_dict.get("sample_rate", 48000)),
            audio_channels=int(opts_dict.get("audio_channels", 2)),
        )

        processed_video_queue = std_queue.Queue(maxsize=1)
        send_video_queue = std_queue.Queue(maxsize=1)
        send_audio_queue = std_queue.Queue(maxsize=2)

        try:
            video_source_thread = VideoSourceThread(
                opts.video_device, opts.xres, opts.yres, opts.fps, processed_video_queue, opts.pix_fmt
            )
        except Exception as e:
            status_q.put({"type": "error", "error": f"Failed to initialize video device {opts.video_device}: {e}"})
            return

        actual_xres = video_source_thread.actual_xres
        actual_yres = video_source_thread.actual_yres
        actual_fps = video_source_thread.actual_fps

        status_q.put({
            "type": "status",
            "running": True,
            "sender_name": opts.sender_name,
            "video_device": opts.video_device,
            "audio_device": opts.audio_device,
            "no_audio": opts.no_audio,
            "actual_width": actual_xres,
            "actual_height": actual_yres,
            "actual_fps": actual_fps,
            "sample_rate": opts.sample_rate,
            "audio_channels": opts.audio_channels,
        })

        sender = Sender(opts.sender_name)
        vf = VideoSendFrame()
        vf.set_resolution(actual_xres, actual_yres)
        vf.set_frame_rate(Fraction(actual_fps).limit_denominator())
        vf.set_fourcc(opts.pix_fmt.four_cc)
        sender.set_video_frame(vf)

        audio_stream = None
        audio_queue = None

        last_audio_levels = [-60.0, -60.0]
        last_audio_peaks = [-60.0, -60.0]

        if not opts.no_audio:
            audio_queue = std_queue.Queue(maxsize=4)
            if actual_fps > 0:
                samples_per_frame = round(opts.sample_rate / actual_fps)
                samples_per_chunk = max(64, samples_per_frame // AUDIO_CHUNKS_PER_FRAME)
                af = AudioSendFrame()
                af.sample_rate = opts.sample_rate
                af.num_channels = opts.audio_channels
                af.set_max_num_samples(samples_per_frame)
                sender.set_audio_frame(af)

                def audio_callback(indata, frames, time_info, status):
                    nonlocal last_audio_levels, last_audio_peaks
                    if audio_queue:
                        try:
                            audio_queue.put_nowait(indata.copy().T)
                        except std_queue.Full:
                            pass
                    try:
                        ch_levels = []
                        ch_peaks = []
                        num_ch = indata.shape[1] if len(indata.shape) > 1 else 1
                        for ch in range(num_ch):
                            channel_samples = indata[:, ch] if len(indata.shape) > 1 else indata
                            peak_val = np.max(np.abs(channel_samples)) if len(channel_samples) > 0 else 0.0
                            rms_val = np.sqrt(np.mean(np.square(channel_samples))) if len(channel_samples) > 0 else 0.0
                            peak_db = 20.0 * np.log10(max(1e-4, float(peak_val)))
                            rms_db = 20.0 * np.log10(max(1e-4, float(rms_val)))
                            ch_peaks.append(round(float(max(-60.0, min(0.0, peak_db))), 1))
                            ch_levels.append(round(float(max(-60.0, min(0.0, rms_db))), 1))
                        if len(ch_levels) == 1:
                            ch_levels.append(ch_levels[0])
                            ch_peaks.append(ch_peaks[0])
                        last_audio_levels = ch_levels[:2]
                        last_audio_peaks = ch_peaks[:2]
                    except Exception:
                        pass

                audio_stream = sd.InputStream(
                    device=opts.audio_device,
                    samplerate=opts.sample_rate,
                    channels=opts.audio_channels,
                    callback=audio_callback,
                    blocksize=samples_per_chunk,
                    dtype="float32",
                    latency=0.01
                )

        video_send_thread = VideoSendThread(sender, send_video_queue)
        audio_send_thread = AudioSendThread(sender, send_audio_queue) if not opts.no_audio else None

        video_source_thread.start()
        if audio_stream:
            audio_stream.start()
        video_send_thread.start()
        if audio_send_thread:
            audio_send_thread.start()

        frames_sent = 0
        last_stat_time = time.time()

        with sender:
            while True:
                # Check for stop command
                try:
                    cmd = command_q.get_nowait()
                    if cmd.get("action") == "stop":
                        break
                except std_queue.Empty:
                    pass

                if not video_source_thread.running:
                    status_q.put({"type": "error", "error": "Camera disconnected or thread stopped"})
                    break

                try:
                    frame = processed_video_queue.get(timeout=0.5)
                    try:
                        send_video_queue.put_nowait(frame)
                        frames_sent += 1
                    except std_queue.Full:
                        with send_video_queue.mutex:
                            send_video_queue.queue.clear()
                except std_queue.Empty:
                    continue

                if not opts.no_audio and audio_queue:
                    try:
                        audio_chunks = [audio_queue.get_nowait() for _ in range(min(audio_queue.qsize(), AUDIO_CHUNKS_PER_FRAME))]
                        if audio_chunks:
                            audio_data = np.concatenate(audio_chunks, axis=1)
                            try:
                                send_audio_queue.put_nowait(audio_data)
                            except std_queue.Full:
                                with send_audio_queue.mutex:
                                    send_audio_queue.queue.clear()
                    except (std_queue.Empty, ValueError):
                        pass

                now = time.time()
                elapsed = now - last_stat_time
                if elapsed >= 0.5:
                    real_fps = round(frames_sent / elapsed, 1)
                    frames_sent = 0
                    last_stat_time = now
                    status_q.put({
                        "type": "metrics",
                        "fps_real": real_fps,
                        "audio_level_l": last_audio_levels[0],
                        "audio_level_r": last_audio_levels[1],
                        "audio_peak_l": last_audio_peaks[0],
                        "audio_peak_r": last_audio_peaks[1],
                    })

    except Exception as e:
        status_q.put({"type": "error", "error": str(e)})
    finally:
        try:
            video_send_thread.stop()
            if audio_send_thread:
                audio_send_thread.stop()
            if audio_stream:
                audio_stream.stop()
                audio_stream.close()
            video_source_thread.stop()
        except Exception:
            pass
        status_q.put({"type": "status", "running": False})


class TxRunner:
    def __init__(self):
        self.process: Optional[mp.Process] = None
        self.command_q: Optional[mp.Queue] = None
        self.status_q: Optional[mp.Queue] = None
        self._current_status = TxStatus(running=False)

    def get_status(self) -> TxStatus:
        self._update_status_from_queue()
        if self.process and not self.process.is_alive():
            self._current_status.running = False
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
                    if msg.get("running", False):
                        self._current_status.sender_name = msg.get("sender_name")
                        self._current_status.video_device = msg.get("video_device")
                        self._current_status.audio_device = msg.get("audio_device")
                        self._current_status.no_audio = msg.get("no_audio", False)
                        self._current_status.actual_width = msg.get("actual_width", 0)
                        self._current_status.actual_height = msg.get("actual_height", 0)
                        self._current_status.actual_fps = msg.get("actual_fps", 0.0)
                        self._current_status.sample_rate = msg.get("sample_rate", 0)
                        self._current_status.audio_channels = msg.get("audio_channels", 0)
                elif msg.get("type") == "metrics":
                    self._current_status.fps_real = msg.get("fps_real", 0.0)
                    self._current_status.audio_level_l = msg.get("audio_level_l", -60.0)
                    self._current_status.audio_level_r = msg.get("audio_level_r", -60.0)
                    self._current_status.audio_peak_l = msg.get("audio_peak_l", -60.0)
                    self._current_status.audio_peak_r = msg.get("audio_peak_r", -60.0)
                elif msg.get("type") == "error":
                    self._current_status.error = msg.get("error")
            except queue.Empty:
                break

    def start(self, req: TxStartRequest):
        if self.process and self.process.is_alive():
            raise RuntimeError("TX is already running. Please stop first.")

        self.command_q = mp.Queue()
        self.status_q = mp.Queue()
        self._current_status = TxStatus(
            running=True,
            sender_name=req.sender_name,
            video_device=req.video_device,
            audio_device=req.audio_device,
            no_audio=req.no_audio,
            error=None
        )

        self.process = mp.Process(
            target=_tx_worker_process,
            args=(self.command_q, self.status_q, req.model_dump()),
            daemon=True
        )
        self.process.start()

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
        self._current_status = TxStatus(
            running=False,
            sender_name=None,
            video_device=None,
            audio_device=None,
            no_audio=False,
            actual_width=0,
            actual_height=0,
            actual_fps=0.0,
            sample_rate=0,
            audio_channels=0,
            error=None
        )


tx_runner = TxRunner()
