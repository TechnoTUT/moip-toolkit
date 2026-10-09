"""
core.rx
NDI Receiver, SDL2/OpenGL display logic, and playback loop.
"""
from __future__ import annotations

import enum
import functools
import time
import sys
from typing import NamedTuple, Optional, TYPE_CHECKING
from typing_extensions import Self

from cyndilib.wrapper.ndi_recv import RecvColorFormat, RecvBandwidth
from cyndilib.video_frame import VideoFrameSync
from cyndilib.receiver import Receiver
from cyndilib.finder import Finder
from core.reconnecting_receiver import ReconnectingReceiver
if TYPE_CHECKING:
    from cyndilib.finder import Source

import socket
import sdl2
import sdl2.ext
try:
    from OpenGL.GL import (
        GL_TEXTURE_2D, GL_PROJECTION, GL_MODELVIEW, GL_COLOR_BUFFER_BIT,
        GL_RGBA, GL_BGRA, GL_UNSIGNED_BYTE, GL_QUADS, GL_LINEAR, GL_NEAREST,
        GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER,
        glEnable, glViewport, glMatrixMode, glLoadIdentity, glOrtho,
        glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
        glTexSubImage2D, glClearColor, glClear, glBegin, glTexCoord2f,
        glVertex2f, glEnd, glDeleteTextures
    )
except Exception:
    pass


class RecvFmt(enum.Enum):
    uyvy = RecvColorFormat.UYVY_RGBA
    rgb = RecvColorFormat.RGBX_RGBA
    bgr = RecvColorFormat.BGRX_BGRA

    @classmethod
    def from_str(cls, name: str) -> Self:
        return cls.__members__[name]


class Bandwidth(enum.Enum):
    lowest = RecvBandwidth.lowest
    highest = RecvBandwidth.highest

    @classmethod
    def from_str(cls, name: str) -> Self:
        return cls.__members__[name]


class Options(NamedTuple):
    sender_name: str
    recv_fmt: RecvFmt = RecvFmt.rgb
    recv_bandwidth: Bandwidth = Bandwidth.highest
    fullscreen: bool = False


def get_source(finder: Finder, name: str, timeout_seconds: float = 10.0) -> Source:
    """Wait for and find a specific NDI source by full name or stream name."""
    max_loops = int(timeout_seconds * 10)
    for _ in range(max_loops):
        finder.wait_for_sources(0)
        for source in finder:
            if source.name == name or source.stream_name == name:
                return source
        time.sleep(0.1)
    raise Exception(f'Source "{name}" not found. Available sources: {finder.get_source_names()}')


def wait_for_first_frame(receiver: Receiver) -> None:
    """Block until first valid video frame is received."""
    vf = receiver.frame_sync.video_frame
    assert vf is not None
    while receiver.is_connected():
        receiver.frame_sync.capture_video()
        resolution = vf.get_resolution()
        if min(resolution) > 0 and vf.get_data_size() > 0:
            return
        time.sleep(0.01)


def render_texture(
    frame,  # bytes or 1-d uint8 buffer / ndarray
    tex_w: int,
    tex_h: int,
    win_w: int,
    win_h: int,
    texture_id: int,
    recv_fmt: RecvFmt,
    is_texture_initialized: bool
) -> bool:
    """Render raw frame bytes to SDL OpenGL window with aspect ratio scaling."""
    if frame is None or len(frame) == 0 or tex_w == 0 or tex_h == 0:
        render_waiting_message()
        return False

    gl_format = GL_BGRA if recv_fmt == RecvFmt.bgr else GL_RGBA
    glBindTexture(GL_TEXTURE_2D, texture_id)
    
    if not is_texture_initialized:
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, tex_w, tex_h, 0, gl_format, GL_UNSIGNED_BYTE, frame)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    else:
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, tex_w, tex_h, gl_format, GL_UNSIGNED_BYTE, frame)

    glClearColor(0.0, 0.0, 0.0, 1.0)
    glClear(GL_COLOR_BUFFER_BIT)

    src_ratio = tex_w / tex_h
    dst_ratio = win_w / win_h

    if src_ratio > dst_ratio:
        scale_x = 1.0
        scale_y = dst_ratio / src_ratio
    else:
        scale_x = src_ratio / dst_ratio
        scale_y = 1.0

    glBegin(GL_QUADS)
    glTexCoord2f(0.0, 1.0); glVertex2f(-scale_x, -scale_y)
    glTexCoord2f(1.0, 1.0); glVertex2f( scale_x, -scale_y)
    glTexCoord2f(1.0, 0.0); glVertex2f( scale_x,  scale_y)
    glTexCoord2f(0.0, 0.0); glVertex2f(-scale_x,  scale_y)
    glEnd()
    
    return True


def get_local_ip() -> str:
    """Detect primary local IPv4 address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


FONT_5X7 = {
    ' ': [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    '.': [0x00, 0x00, 0x00, 0x00, 0x00, 0x0c, 0x0c],
    ':': [0x00, 0x0c, 0x0c, 0x00, 0x0c, 0x0c, 0x00],
    '-': [0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00],
    '/': [0x01, 0x02, 0x04, 0x08, 0x10, 0x00, 0x00],
    '0': [0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e],
    '1': [0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e],
    '2': [0x0e, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1f],
    '3': [0x1f, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0e],
    '4': [0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02],
    '5': [0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e],
    '6': [0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e],
    '7': [0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
    '8': [0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e],
    '9': [0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c],
    'A': [0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11],
    'B': [0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e],
    'C': [0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e],
    'D': [0x1c, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1c],
    'E': [0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f],
    'F': [0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x10],
    'G': [0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f],
    'H': [0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11],
    'I': [0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e],
    'L': [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f],
    'M': [0x11, 0x1b, 0x15, 0x11, 0x11, 0x11, 0x11],
    'N': [0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11],
    'O': [0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e],
    'P': [0x1e, 0x11, 0x11, 0x1e, 0x10, 0x10, 0x10],
    'R': [0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11],
    'S': [0x0f, 0x10, 0x10, 0x0e, 0x01, 0x01, 0x1e],
    'T': [0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    'U': [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e],
    'V': [0x11, 0x11, 0x11, 0x11, 0x11, 0x0a, 0x04],
    'W': [0x11, 0x11, 0x11, 0x15, 0x15, 0x1b, 0x11],
    'X': [0x11, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x11],
    'Y': [0x11, 0x11, 0x0a, 0x04, 0x04, 0x04, 0x04],
}


@functools.lru_cache(maxsize=16)
def create_text_rgba_image(text: str, scale: int = 4, fg_color=(255, 255, 255, 255), bg_color=(20, 24, 33, 220)) -> tuple[bytes, int, int]:
    """Generates an RGBA byte buffer rendering the given text with a 5x7 font."""
    char_w = 6
    char_h = 9
    img_w = max(1, len(text) * char_w * scale + (char_w * scale))
    img_h = max(1, char_h * scale + (char_h * scale))

    buf = bytearray(img_w * img_h * 4)
    # Fill background
    for i in range(img_w * img_h):
        idx = i * 4
        buf[idx] = bg_color[0]
        buf[idx + 1] = bg_color[1]
        buf[idx + 2] = bg_color[2]
        buf[idx + 3] = bg_color[3]

    start_x = (char_w * scale) // 2
    start_y = (char_h * scale) // 2

    for ci, ch in enumerate(text.upper()):
        rows = FONT_5X7.get(ch, FONT_5X7[' '])
        for r_idx, row_byte in enumerate(rows):
            for c_idx in range(5):
                if (row_byte >> (4 - c_idx)) & 1:
                    px_base = start_x + ci * char_w * scale + c_idx * scale
                    py_base = start_y + r_idx * scale
                    for sy in range(scale):
                        for sx in range(scale):
                            px = px_base + sx
                            py = py_base + sy
                            if 0 <= px < img_w and 0 <= py < img_h:
                                offset = (py * img_w + px) * 4
                                buf[offset] = fg_color[0]
                                buf[offset + 1] = fg_color[1]
                                buf[offset + 2] = fg_color[2]
                                buf[offset + 3] = fg_color[3]

    return bytes(buf), img_w, img_h


def render_ip_banner(overlay_tex_id: int, ip_str: str, source_name: str, win_w: int, win_h: int):
    """Render top-left banner displaying Host IP address and status."""
    banner_text = f"IP: {ip_str}  |  NDI: {source_name}"
    rgba_bytes, img_w, img_h = create_text_rgba_image(banner_text, scale=3)

    glBindTexture(GL_TEXTURE_2D, overlay_tex_id)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, img_w, img_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba_bytes)

    # Position at top-left: orthographic coordinates from -1.0 to +1.0
    # Margin 20px, banner width and height in NDC coordinates
    pad_x = 24.0 / win_w * 2.0
    pad_y = 24.0 / win_h * 2.0
    bw = img_w / win_w * 2.0
    bh = img_h / win_h * 2.0

    x0 = -1.0 + pad_x
    x1 = x0 + bw
    y1 = 1.0 - pad_y
    y0 = y1 - bh

    glBegin(GL_QUADS)
    glTexCoord2f(0.0, 0.0); glVertex2f(x0, y1)
    glTexCoord2f(1.0, 0.0); glVertex2f(x1, y1)
    glTexCoord2f(1.0, 1.0); glVertex2f(x1, y0)
    glTexCoord2f(0.0, 1.0); glVertex2f(x0, y0)
    glEnd()


def render_waiting_message(overlay_tex_id: Optional[int] = None, ip_str: str = "", source_name: str = "", win_w: int = 1280, win_h: int = 720) -> None:
    """Render waiting screen with IP address and target source name."""
    glClearColor(0.05, 0.05, 0.08, 1.0)
    glClear(GL_COLOR_BUFFER_BIT)

    if overlay_tex_id is not None and ip_str:
        center_text = f"WAITING FOR NDI SOURCE: {source_name}"
        sub_text = f"HOST IP: {ip_str}:8000"
        full_text = f"{sub_text}  -  {center_text}"
        rgba_bytes, img_w, img_h = create_text_rgba_image(full_text, scale=3)

        glBindTexture(GL_TEXTURE_2D, overlay_tex_id)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, img_w, img_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba_bytes)

        bw = img_w / max(win_w, 1) * 2.0
        bh = img_h / max(win_h, 1) * 2.0
        x0 = -bw / 2.0
        x1 = bw / 2.0
        y0 = -bh / 2.0
        y1 = bh / 2.0

        glBegin(GL_QUADS)
        glTexCoord2f(0.0, 0.0); glVertex2f(x0, y1)
        glTexCoord2f(1.0, 0.0); glVertex2f(x1, y1)
        glTexCoord2f(1.0, 1.0); glVertex2f(x1, y0)
        glTexCoord2f(0.0, 1.0); glVertex2f(x0, y0)
        glEnd()


def init_window(title: str, width: int, height: int, fullscreen: bool):
    """Initialize SDL2 OpenGL window and context."""
    if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
        raise RuntimeError(f"SDL_Init Error: {sdl2.SDL_GetError()}")

    sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_MAJOR_VERSION, 2)
    sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_MINOR_VERSION, 1)
    
    flags = sdl2.SDL_WINDOW_OPENGL | sdl2.SDL_WINDOW_RESIZABLE
    if fullscreen:
        flags |= sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP

    window = sdl2.SDL_CreateWindow(
        title.encode('utf-8'),
        sdl2.SDL_WINDOWPOS_CENTERED, sdl2.SDL_WINDOWPOS_CENTERED,
        width, height,
        flags
    )
    if not window:
        raise RuntimeError(f"SDL_CreateWindow Error: {sdl2.SDL_GetError()}")

    sdl2.SDL_GL_CreateContext(window)
    # Set swap interval to 0 (immediate) or -1 (late swap tearing) to minimize frame display latency
    if sdl2.SDL_GL_SetSwapInterval(0) != 0:
        sdl2.SDL_GL_SetSwapInterval(1)
    sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE)
    return window


def play_sdl(options: Options, finder: Finder, log_fn=print):
    """Main SDL2 playback loop for CLI receiver."""
    window = init_window("NDI Viewer", 1280, 720, options.fullscreen)
    
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
    
    texture_id = glGenTextures(1)
    overlay_tex_id = glGenTextures(1)
    receiver: Optional[Receiver] = None
    running = True
    event = sdl2.SDL_Event()
    
    is_connected = False
    last_frame_w, last_frame_h = 0, 0
    is_texture_initialized = False
    start_time = time.time()
    local_ip = get_local_ip()

    vf = VideoFrameSync()
    rr = ReconnectingReceiver(
        options.recv_fmt.value,
        options.recv_bandwidth.value,
        vf,
        connect_timeout=5.0,
        retry_interval=2.0
    )

    def find_source():
        try:
            return get_source(finder, options.sender_name, timeout_seconds=0.5)
        except Exception:
            return None

    try:
        while running:
            while sdl2.SDL_PollEvent(event):
                if event.type == sdl2.SDL_QUIT:
                    running = False
                elif event.type == sdl2.SDL_KEYDOWN and event.key.keysym.sym == sdl2.SDLK_ESCAPE:
                    running = False
                elif event.type == sdl2.SDL_WINDOWEVENT and event.window.event == sdl2.SDL_WINDOWEVENT_RESIZED:
                    win_w, win_h = event.window.data1, event.window.data2
                    glViewport(0, 0, win_w, win_h)

            now = time.time()
            show_banner = (now - start_time < 30.0)

            if not rr.update(find_source, now):
                is_connected = False
                render_waiting_message(overlay_tex_id, local_ip, options.sender_name, win_w, win_h)
                sdl2.SDL_GL_SwapWindow(window)
                time.sleep(0.016)
                continue

            receiver = rr.receiver
            if not is_connected:
                is_connected = True
                log_fn(f"Connected to NDI source: {options.sender_name}")

            try:
                receiver.frame_sync.capture_video()
                tex_w, tex_h = vf.get_resolution()
                if tex_w > 0 and tex_h > 0 and vf.get_data_size() > 0:
                    if last_frame_w != tex_w or last_frame_h != tex_h:
                        is_texture_initialized = False
                    last_frame_w, last_frame_h = tex_w, tex_h

                    with memoryview(vf) as mv:
                        frame_arr = np.frombuffer(mv, dtype=np.uint8)
                        try:
                            is_texture_initialized = render_texture(
                                frame_arr, last_frame_w, last_frame_h, win_w, win_h,
                                texture_id, options.recv_fmt, is_texture_initialized
                            )
                        finally:
                            del frame_arr

                    if show_banner:
                        render_ip_banner(overlay_tex_id, local_ip, options.sender_name, win_w, win_h)
            except Exception as e:
                log_fn(f"Error during frame capture or rendering: {e}")
                rr.drop(now, cooldown=2.0)
                is_connected = False
                is_texture_initialized = False

            sdl2.SDL_GL_SwapWindow(window)

    finally:
        log_fn("Cleaning up resources...")
        rr.drop()
        receiver = None
        glDeleteTextures(2, [texture_id, overlay_tex_id])
        sdl2.SDL_DestroyWindow(window)
        sdl2.SDL_Quit()
        log_fn("Program terminated.")
