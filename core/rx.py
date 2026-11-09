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

from cyndilib.wrapper import FourCC

import os
import socket
import numpy as np
import sdl2
import sdl2.ext
try:
    from OpenGL.GL import (
        GL_TEXTURE_2D, GL_PROJECTION, GL_MODELVIEW, GL_COLOR_BUFFER_BIT,
        GL_RGBA, GL_RGBA8, GL_BGRA, GL_UNSIGNED_BYTE, GL_QUADS, GL_LINEAR, GL_NEAREST,
        GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
        GL_CLAMP_TO_EDGE, GL_UNPACK_ROW_LENGTH, GL_UNPACK_ALIGNMENT,
        GL_VERTEX_SHADER, GL_FRAGMENT_SHADER,
        glEnable, glViewport, glMatrixMode, glLoadIdentity, glOrtho,
        glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
        glTexSubImage2D, glClearColor, glClear, glBegin, glTexCoord2f,
        glVertex2f, glEnd, glDeleteTextures, glPixelStorei,
        glUseProgram, glGetUniformLocation, glUniform1i, glUniform2f, glUniform4f,
        glDeleteProgram,
    )
    from OpenGL.GL.shaders import compileProgram, compileShader
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
    recv_fmt: RecvFmt = RecvFmt.uyvy
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


_UYVY_VERT = """
#version 110
varying vec2 v_tc;
void main() {
    v_tc = gl_MultiTexCoord0.xy;
    gl_Position = ftransform();
}
"""

# UYVY 4:2:2 is uploaded as an RGBA texture of half width: each texel is one
# macropixel (r=U, g=Y0, b=V, a=Y1). The texture is sampled with GL_LINEAR
# exactly at macropixel centres horizontally, so the hardware only interpolates
# vertically; horizontal interpolation is done here per luma pixel. That keeps
# scaling smooth with two texture fetches per pixel, without mixing Y0/Y1 of
# neighbouring macropixels.
_UYVY_FRAG = """
#version 110
uniform sampler2D tex;
uniform vec2 size;   // frame size in luma pixels
uniform vec4 coef;   // (Rv, Gu, Gv, Bu) of the YCbCr -> RGB matrix
varying vec2 v_tc;

void main() {
    float half_w = size.x * 0.5;
    float p = v_tc.x * size.x - 0.5;
    float i = floor(p);
    float f = p - i;
    float x0 = clamp(i, 0.0, size.x - 1.0);
    float x1 = clamp(i + 1.0, 0.0, size.x - 1.0);
    float m0 = floor(x0 * 0.5);
    float m1 = floor(x1 * 0.5);
    vec4 t0 = texture2D(tex, vec2((m0 + 0.5) / half_w, v_tc.y));
    vec4 t1 = texture2D(tex, vec2((m1 + 0.5) / half_w, v_tc.y));
    float y0 = mix(t0.g, t0.a, x0 - m0 * 2.0);  // odd pixel -> Y1
    float y1 = mix(t1.g, t1.a, x1 - m1 * 2.0);
    float y = (mix(y0, y1, f) - 0.0627451) * 1.1643836;  // limited range 16..235
    vec2 uv = mix(t0.rb, t1.rb, f) - 0.5;
    gl_FragColor = vec4(y + coef.x * uv.y, y - coef.y * uv.x - coef.z * uv.y, y + coef.w * uv.x, 1.0);
}
"""

# NDI uses BT.709 for HD and BT.601 for SD (< 720 lines).
_COEF_BT709 = (1.7927411, 0.2132486, 0.5329093, 2.1124018)
_COEF_BT601 = (1.5960268, 0.3917623, 0.8129676, 2.0172321)



def _rgb_gl_format(fourcc) -> Optional[int]:
    if fourcc in (FourCC.RGBA, FourCC.RGBX):
        return GL_RGBA
    if fourcc in (FourCC.BGRA, FourCC.BGRX):
        return GL_BGRA
    return None


class FrameRenderer:
    """
    Uploads NDI video frames to a GL texture and draws them letterboxed.

    UYVY frames are uploaded as-is (2 bytes/pixel) and converted to RGB on the
    GPU, which halves the upload size and skips the CPU-side colour conversion
    the NDI SDK otherwise performs for RGBX/BGRX. RGBA/BGRA frames (e.g. sources
    with alpha when receiving with UYVY_RGBA) are uploaded directly.
    Requires a current GL context.
    """

    def __init__(self):
        self.texture_id = glGenTextures(1)
        self._tex_key = None  # (tex_w, tex_h, gl_format, filter)
        self._program = None
        self._loc_size = self._loc_coef = -1
        try:
            self._program = compileProgram(
                compileShader(_UYVY_VERT, GL_VERTEX_SHADER),
                compileShader(_UYVY_FRAG, GL_FRAGMENT_SHADER),
            )
            glUseProgram(self._program)
            glUniform1i(glGetUniformLocation(self._program, "tex"), 0)
            self._loc_size = glGetUniformLocation(self._program, "size")
            self._loc_coef = glGetUniformLocation(self._program, "coef")
            glUseProgram(0)
        except Exception as e:
            print(f"[RX] UYVY shader unavailable, falling back to RGB receive: {e}", flush=True)
            self._program = None

    @property
    def supports_uyvy(self) -> bool:
        return self._program is not None

    def resolve_recv_fmt(self, recv_fmt: RecvFmt) -> RecvFmt:
        """Return a receive format this renderer can display."""
        if recv_fmt == RecvFmt.uyvy and not self.supports_uyvy:
            return RecvFmt.rgb
        return recv_fmt

    def reset(self) -> None:
        """Force texture re-allocation on the next frame."""
        self._tex_key = None

    def draw(self, vf, win_w: int, win_h: int) -> bool:
        """Upload the current frame of `vf` and draw it. Returns False if nothing was drawn."""
        xres, yres = vf.get_resolution()
        if xres <= 0 or yres <= 0 or vf.get_data_size() <= 0:
            return False

        fourcc = vf.get_fourcc()
        stride = vf.get_line_stride() or 0
        if fourcc == FourCC.UYVY:
            if self._program is None:
                return False
            tex_w, gl_format, filt = xres // 2, GL_RGBA, GL_LINEAR
        else:
            gl_format = _rgb_gl_format(fourcc)
            if gl_format is None:
                return False
            tex_w, filt = xres, GL_LINEAR
        row_len = stride // 4 if stride else tex_w

        with memoryview(vf) as mv:
            frame = np.frombuffer(mv, dtype=np.uint8)
            try:
                if frame.size < row_len * 4 * (yres - 1) + tex_w * 4:
                    return False
                glBindTexture(GL_TEXTURE_2D, self.texture_id)
                glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
                glPixelStorei(GL_UNPACK_ROW_LENGTH, row_len if row_len != tex_w else 0)
                key = (tex_w, yres, gl_format, filt)
                if key != self._tex_key:
                    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, filt)
                    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, filt)
                    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, tex_w, yres, 0, gl_format, GL_UNSIGNED_BYTE, frame)
                    self._tex_key = key
                else:
                    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, tex_w, yres, gl_format, GL_UNSIGNED_BYTE, frame)
                glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
            finally:
                del frame

        glClearColor(0.0, 0.0, 0.0, 1.0)
        glClear(GL_COLOR_BUFFER_BIT)

        use_shader = fourcc == FourCC.UYVY
        if use_shader:
            glUseProgram(self._program)
            glUniform2f(self._loc_size, float(xres), float(yres))
            glUniform4f(self._loc_coef, *(_COEF_BT601 if yres < 720 else _COEF_BT709))
        try:
            _draw_letterboxed_quad(xres, yres, win_w, win_h)
        finally:
            if use_shader:
                glUseProgram(0)
        return True

    def delete(self) -> None:
        try:
            glDeleteTextures(1, [self.texture_id])
        except Exception:
            pass
        if self._program is not None:
            try:
                glDeleteProgram(self._program)
            except Exception:
                pass
            self._program = None


def _draw_letterboxed_quad(src_w: int, src_h: int, win_w: int, win_h: int) -> None:
    src_ratio = src_w / src_h
    dst_ratio = win_w / max(win_h, 1)
    if src_ratio > dst_ratio:
        scale_x, scale_y = 1.0, dst_ratio / src_ratio
    else:
        scale_x, scale_y = src_ratio / dst_ratio, 1.0

    glBegin(GL_QUADS)
    glTexCoord2f(0.0, 1.0); glVertex2f(-scale_x, -scale_y)
    glTexCoord2f(1.0, 1.0); glVertex2f( scale_x, -scale_y)
    glTexCoord2f(1.0, 0.0); glVertex2f( scale_x,  scale_y)
    glTexCoord2f(0.0, 0.0); glVertex2f(-scale_x,  scale_y)
    glEnd()


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


VIDEO_DRIVERS = ("auto", "kmsdrm", "wayland", "x11")


def apply_video_driver(name: Optional[str]) -> None:
    """
    Select the SDL video backend (must run before SDL_Init; inherited by child processes).
    "kmsdrm" renders straight to the display via DRM/KMS without a desktop
    compositor, removing the 1-2 frames of latency the compositor adds. It needs
    no running desktop session on that display and the user in the `video` group.
    """
    if name and name != "auto":
        os.environ["SDL_VIDEODRIVER"] = name


def init_window(title: str, width: int, height: int, fullscreen: bool):
    """Initialize SDL2 OpenGL window and context."""
    # Double instead of triple buffering where the driver supports it (KMSDRM):
    # one less queued frame between swap and scan-out.
    sdl2.SDL_SetHint(sdl2.SDL_HINT_VIDEO_DOUBLE_BUFFER, b"1")
    if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
        raise RuntimeError(f"SDL_Init Error: {sdl2.SDL_GetError()}")

    driver = (sdl2.SDL_GetCurrentVideoDriver() or b"").decode().lower()
    print(f"[SDL] video driver: {driver}", flush=True)
    if driver == "kmsdrm":
        fullscreen = True  # no window system: always take over the whole display

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


def frame_id(vf) -> tuple[float, float]:
    """Identity of the frame currently held by a VideoFrameSync (framesync repeats frames)."""
    return (vf.get_timecode_posix(), vf.get_timestamp_posix())


def frame_latency_ms(vf) -> Optional[float]:
    """Sender timestamp -> now, in ms. Only meaningful when sender/receiver clocks are synced."""
    ts = vf.get_timestamp_posix()
    if ts <= 0:
        return None
    return (time.time() - ts) * 1000.0


# Poll interval while waiting for the next frame. Short enough that the added
# delay between frame arrival and display is ~0.5ms on average.
FRAME_POLL_INTERVAL = 0.001


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
    
    renderer = FrameRenderer()
    overlay_tex_id = glGenTextures(1)
    recv_fmt = renderer.resolve_recv_fmt(options.recv_fmt)
    receiver: Optional[Receiver] = None
    running = True
    event = sdl2.SDL_Event()
    
    is_connected = False
    last_frame_id = None
    start_time = time.time()
    local_ip = get_local_ip()

    vf = VideoFrameSync()
    rr = ReconnectingReceiver(
        recv_fmt.value,
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
                    last_frame_id = None  # redraw at the new size

            now = time.time()
            show_banner = (now - start_time < 30.0)

            if not rr.update(find_source, now):
                is_connected = False
                last_frame_id = None
                render_waiting_message(overlay_tex_id, local_ip, options.sender_name, win_w, win_h)
                sdl2.SDL_GL_SwapWindow(window)
                time.sleep(0.016)
                continue

            receiver = rr.receiver
            if not is_connected:
                is_connected = True
                log_fn(f"Connected to NDI source: {options.sender_name}")

            drawn = False
            try:
                receiver.frame_sync.capture_video()
                fid = frame_id(vf)
                if fid != last_frame_id and renderer.draw(vf, win_w, win_h):
                    last_frame_id = fid
                    if show_banner:
                        render_ip_banner(overlay_tex_id, local_ip, options.sender_name, win_w, win_h)
                    sdl2.SDL_GL_SwapWindow(window)
                    drawn = True
            except Exception as e:
                log_fn(f"Error during frame capture or rendering: {e}")
                rr.drop(now, cooldown=2.0)
                is_connected = False
                last_frame_id = None
                renderer.reset()

            if not drawn:
                time.sleep(FRAME_POLL_INTERVAL)

    finally:
        log_fn("Cleaning up resources...")
        rr.drop()
        receiver = None
        renderer.delete()
        glDeleteTextures(1, [overlay_tex_id])
        sdl2.SDL_DestroyWindow(window)
        sdl2.SDL_Quit()
        log_fn("Program terminated.")
