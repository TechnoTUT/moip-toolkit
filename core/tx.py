"""
core.tx
NDI Sender threads, video/audio capture, frame assembly loop.
"""
from __future__ import annotations

import enum
import queue
import sys
import threading
import time
from fractions import Fraction
from typing import NamedTuple, Optional
from typing_extensions import Self

import cv2
import numpy as np
try:
    import sounddevice as sd
except Exception:
    sd = None
from cyndilib.sender import Sender
from cyndilib.video_frame import VideoSendFrame
from cyndilib.audio_frame import AudioSendFrame
from cyndilib.wrapper.ndi_structs import FourCC


MAX_CAMERA_RETRIES = 100
QUEUE_TIMEOUT = 1.0
AUDIO_CHUNKS_PER_FRAME = 4


class PixFmt(enum.Enum):
    RGBA = (FourCC.RGBA, cv2.COLOR_BGR2RGBA)
    BGRA = (FourCC.BGRA, cv2.COLOR_BGR2BGRA)
    BGRX = (FourCC.BGRX, cv2.COLOR_BGR2BGRA)

    def __init__(self, four_cc: FourCC, cv_color_code: int):
        self.four_cc = four_cc
        self.cv_color_code = cv_color_code

    @classmethod
    def from_str(cls, name: str) -> Self:
        try:
            return cls[name.upper()]
        except KeyError:
            raise ValueError(f"Unsupported pixel format: {name}")


class Options(NamedTuple):
    pix_fmt: PixFmt
    xres: int
    yres: int
    fps: float
    video_device: int
    sender_name: str
    no_audio: bool
    audio_device: Optional[int]
    sample_rate: int
    audio_channels: int


def generate_color_bars(width: int, height: int, pix_fmt: PixFmt, text: str = "TEST PATTERN") -> np.ndarray:
    """Generate SMPTE-style HD color bars pattern with timestamp / label."""
    # 7 standard colors in BGR: White, Yellow, Cyan, Green, Magenta, Red, Blue
    colors = [
        (255, 255, 255),  # White
        (0, 255, 255),    # Yellow
        (255, 255, 0),    # Cyan
        (0, 255, 0),      # Green
        (255, 0, 255),    # Magenta
        (0, 0, 255),      # Red
        (255, 0, 0),      # Blue
    ]
    img = np.zeros((height, width, 3), dtype=np.uint8)
    bar_w = width // len(colors)
    
    # Top 75% standard bars
    h_top = int(height * 0.75)
    for i, col in enumerate(colors):
        x_start = i * bar_w
        x_end = width if i == len(colors) - 1 else (i + 1) * bar_w
        img[0:h_top, x_start:x_end] = col

    # Bottom 25% cast bars (Blue, Black, Magenta, Black, Cyan, Black, Gray)
    cast_colors = [
        (255, 0, 0), (0, 0, 0), (255, 0, 255), (0, 0, 0), (255, 255, 0), (0, 0, 0), (128, 128, 128)
    ]
    for i, col in enumerate(cast_colors):
        x_start = i * bar_w
        x_end = width if i == len(cast_colors) - 1 else (i + 1) * bar_w
        img[h_top:height, x_start:x_end] = col

    # Draw label box in center
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.8, width / 1280.0 * 1.2)
    thickness = max(2, int(font_scale * 2))
    text_size, _ = cv2.getTextSize(text, font, font_scale, thickness)
    tx = (width - text_size[0]) // 2
    ty = int(height * 0.45)

    cv2.rectangle(img, (tx - 20, ty - text_size[1] - 15), (tx + text_size[0] + 20, ty + 15), (0, 0, 0), -1)
    cv2.putText(img, text, (tx, ty), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)

    return cv2.cvtColor(img, pix_fmt.cv_color_code)


class VideoSourceThread(threading.Thread):
    def __init__(self, device_index: int, width: int, height: int, fps: float, out_queue: queue.Queue, pix_fmt: PixFmt):
        super().__init__()
        self.device_index = device_index
        self.out_queue = out_queue
        self.pix_fmt = pix_fmt
        self.fps = fps if fps > 0 else 30.0
        self.running = True
        self.daemon = True
        self.cap = None

        if device_index == -1:
            # Color Bars Test Pattern generator
            self.actual_xres = width
            self.actual_yres = height
            self.actual_fps = self.fps
        else:
            self.cap = cv2.VideoCapture(device_index, cv2.CAP_V4L2)
            if not self.cap.isOpened():
                raise IOError(f"Could not open video device: {device_index}")

            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self.cap.set(cv2.CAP_PROP_FPS, fps)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
            
            self.actual_xres = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.actual_yres = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.actual_fps = self.cap.get(cv2.CAP_PROP_FPS)

    def run(self):
        if self.device_index == -1:
            # Synthetic pattern loop
            frame_interval = 1.0 / self.actual_fps
            start_t = time.time()
            frame_count = 0
            while self.running:
                now_str = time.strftime("%H:%M:%S")
                label = f"NDI TEST PATTERN - {now_str}"
                processed_frame = generate_color_bars(self.actual_xres, self.actual_yres, self.pix_fmt, label)
                
                try:
                    self.out_queue.put(processed_frame, timeout=QUEUE_TIMEOUT)
                except queue.Full:
                    try:
                        self.out_queue.get_nowait()
                    except queue.Empty:
                        pass

                frame_count += 1
                target_time = start_t + frame_count * frame_interval
                sleep_dur = target_time - time.time()
                if sleep_dur > 0:
                    time.sleep(sleep_dur)
            return

        retry_count = 0
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                retry_count += 1
                if retry_count > MAX_CAMERA_RETRIES:
                    print("Error: Camera disconnected or failed to read repeatedly.", file=sys.stderr)
                    self.running = False
                    break
                time.sleep(0.01)
                continue
            
            retry_count = 0
            processed_frame = cv2.cvtColor(frame, self.pix_fmt.cv_color_code)
            
            while self.running:
                try:
                    self.out_queue.put(processed_frame, timeout=QUEUE_TIMEOUT)
                    break
                except queue.Full:
                    try:
                        self.out_queue.get_nowait()
                    except queue.Empty:
                        pass

    def stop(self):
        self.running = False
        self.join(timeout=2)
        if self.cap:
            self.cap.release()


class VideoSendThread(threading.Thread):
    def __init__(self, sender: Sender, in_queue: queue.Queue):
        super().__init__()
        self.sender = sender
        self.in_queue = in_queue
        self.running = True
        self.daemon = True

    def run(self):
        while self.running:
            try:
                video_frame = self.in_queue.get(timeout=QUEUE_TIMEOUT)
                self.sender.write_video_async(video_frame.ravel())
            except queue.Empty:
                continue

    def stop(self):
        self.running = False
        self.join(timeout=2)


class AudioSendThread(threading.Thread):
    def __init__(self, sender: Sender, in_queue: queue.Queue):
        super().__init__()
        self.sender = sender
        self.in_queue = in_queue
        self.running = True
        self.daemon = True

    def run(self):
        while self.running:
            try:
                audio_frame = self.in_queue.get(timeout=QUEUE_TIMEOUT)
                self.sender.write_audio(audio_frame)
            except queue.Empty:
                continue

    def stop(self):
        self.running = False
        self.join(timeout=2)


def parse_frame_rate(fr_str: str) -> Fraction:
    common_rates = {"23.98": Fraction(24000, 1001), "29.97": Fraction(30000, 1001), "59.94": Fraction(60000, 1001)}
    if fr_str in common_rates:
        return common_rates[fr_str]
    if "/" in fr_str:
        n, d = [int(s) for s in fr_str.split("/")]
        return Fraction(n, d)
    if "." in fr_str:
        return Fraction(fr_str).limit_denominator(2000)
    return Fraction(int(fr_str), 1)


def capture_and_send(opts: Options, log_fn=print) -> None:
    """Capture camera & audio and stream over NDI."""
    processed_video_queue = queue.Queue(maxsize=1)
    send_video_queue = queue.Queue(maxsize=1)
    send_audio_queue = queue.Queue(maxsize=2)
    
    try:
        video_source_thread = VideoSourceThread(
            opts.video_device, opts.xres, opts.yres, opts.fps, processed_video_queue, opts.pix_fmt
        )
    except IOError as e:
        log_fn(f"Fatal: Could not initialize video source thread. Error: {e}")
        return

    actual_xres = video_source_thread.actual_xres
    actual_yres = video_source_thread.actual_yres
    actual_fps = video_source_thread.actual_fps
    log_fn(f"Camera opened with settings: {actual_xres}x{actual_yres} @ {actual_fps:.2f} FPS")

    sender = Sender(opts.sender_name)
    vf = VideoSendFrame()
    vf.set_resolution(actual_xres, actual_yres)
    vf.set_frame_rate(Fraction(actual_fps).limit_denominator())
    vf.set_fourcc(opts.pix_fmt.four_cc)
    sender.set_video_frame(vf)

    audio_stream = None
    audio_queue = None

    if not opts.no_audio:
        audio_queue = queue.Queue(maxsize=4)
        if actual_fps == 0:
            raise ValueError("Actual FPS from camera is 0, cannot calculate audio samples per frame.")
        
        samples_per_frame = round(opts.sample_rate / actual_fps)
        samples_per_chunk = max(64, samples_per_frame // AUDIO_CHUNKS_PER_FRAME)
        if samples_per_chunk == 0:
            samples_per_chunk = 1

        af = AudioSendFrame()
        af.sample_rate = opts.sample_rate
        af.num_channels = opts.audio_channels
        af.set_max_num_samples(samples_per_frame)
        sender.set_audio_frame(af)
        
        def audio_callback(indata, frames, time_info, status):
            if status and 'input overflow' not in str(status):
                log_fn(f"Audio callback status: {status}")
            if audio_queue:
                while True:
                    try:
                        audio_queue.put(indata.copy().T, timeout=0.1)
                        break
                    except queue.Full:
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            break

        audio_stream = sd.InputStream(
            device=opts.audio_device, samplerate=opts.sample_rate,
            channels=opts.audio_channels, callback=audio_callback,
            blocksize=samples_per_chunk, dtype='float32',
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

    try:
        with sender:
            log_fn("Starting assembler loop...")
            if not opts.no_audio and audio_queue is not None:
                while True:
                    try:
                        video_data = processed_video_queue.get(timeout=2.0)
                        audio_chunks = [audio_queue.get(timeout=2.0) for _ in range(AUDIO_CHUNKS_PER_FRAME)]
                        audio_data = np.concatenate(audio_chunks, axis=1)
                        try:
                            send_video_queue.put_nowait(video_data)
                            send_audio_queue.put_nowait(audio_data)
                        except queue.Full:
                            with send_video_queue.mutex: send_video_queue.queue.clear()
                            with send_audio_queue.mutex: send_audio_queue.queue.clear()
                    except queue.Empty:
                        if not video_source_thread.running:
                            break
            else:
                while True:
                    try:
                        frame = processed_video_queue.get(timeout=2.0)
                        try:
                            send_video_queue.put_nowait(frame)
                        except queue.Full:
                            with send_video_queue.mutex: send_video_queue.queue.clear()
                    except queue.Empty:
                        if not video_source_thread.running:
                            break

    finally:
        log_fn("Stopping stream resources...")
        video_send_thread.stop()
        if audio_send_thread:
            audio_send_thread.stop()
        if audio_stream:
            audio_stream.stop()
            audio_stream.close()
        video_source_thread.stop()
        log_fn("Stream stopped.")
