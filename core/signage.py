"""
core.signage
Local digital signage display using SDL2/OpenGL or Linux framebuffer.
Renders static images in fullscreen or windowed mode with aspect ratio preservation.
"""
from __future__ import annotations

import os
import sys
import time
import fcntl
import mmap
import struct
from typing import Optional
import cv2
import numpy as np

import sdl2
import sdl2.ext
try:
    from OpenGL.GL import (
        GL_TEXTURE_2D, GL_PROJECTION, GL_MODELVIEW, GL_COLOR_BUFFER_BIT,
        GL_RGBA, GL_UNSIGNED_BYTE, GL_QUADS, GL_LINEAR, GL_NEAREST,
        GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER,
        glEnable, glViewport, glMatrixMode, glLoadIdentity, glOrtho,
        glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
        glTexSubImage2D, glClearColor, glClear, glBegin, glTexCoord2f,
        glVertex2f, glEnd, glDeleteTextures, glEnable as glEnable2D
    )
except Exception:
    pass


# ---------------------------------------------------------------------------
# Framebuffer constants
# ---------------------------------------------------------------------------
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602


class FramebufferInfo:
    """Framebuffer device information."""
    def __init__(self, fd: int):
        self.fd = fd
        # struct fb_var_screeninfo (160 bytes on 64-bit)
        vinfo = fcntl.ioctl(fd, FBIOGET_VSCREENINFO, b"\x00" * 160)
        (
            self.xres, self.yres, self.xres_virtual, self.yres_virtual,
            self.xoffset, self.yoffset, self.bits_per_pixel, self.gray,
            self.red_offset, self.red_length, self.red_msb_right,
            self.green_offset, self.green_length, self.green_msb_right,
            self.blue_offset, self.blue_length, self.blue_msb_right,
            self.transp_offset, self.transp_length, self.transp_msb_right,
            self.nonstd, self.activate, self.height, self.width,
            self.accel_flags, self.pixclock, self.left_margin, self.right_margin,
            self.upper_margin, self.lower_margin, self.hsync_len, self.vsync_len,
            self.sync, self.vmode, self.rotate, self.colorspace,
        ) = struct.unpack("IIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIII", vinfo[:160])

        # struct fb_fix_screeninfo (72 bytes on 64-bit)
        finfo = fcntl.ioctl(fd, FBIOGET_FSCREENINFO, b"\x00" * 72)
        (
            self.id, self.smem_start, self.smem_len, self.type, self.type_aux,
            self.visual, self.xpanstep, self.ypanstep, self.ywrapstep,
            self.line_length, self.mmio_start, self.mmio_len, self.accel,
            self.capabilities, self.reserved0, self.reserved1,
        ) = struct.unpack("16sQIIIIIHHIIQIIHH", finfo[:72])


def load_image(image_path: str) -> tuple[bytes, int, int]:
    """Load an image file and convert it to RGBA byte array and dimensions."""
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    # Read image with OpenCV
    img = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"Failed to decode image: {image_path}")

    # Handle various channels
    if len(img.shape) == 2:
        # Grayscale -> RGBA
        rgba = cv2.cvtColor(img, cv2.COLOR_GRAY2RGBA)
    elif img.shape[2] == 3:
        # BGR -> RGBA
        rgba = cv2.cvtColor(img, cv2.COLOR_BGR2RGBA)
    elif img.shape[2] == 4:
        # BGRA -> RGBA
        rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
    else:
        raise ValueError(f"Unsupported image format with shape {img.shape}")

    h, w = rgba.shape[:2]
    return rgba.tobytes(), w, h


def create_texture_from_rgba(raw_bytes: bytes, w: int, h: int) -> int:
    """Create a GL texture from an RGBA byte buffer."""
    texture_ids = glGenTextures(1)
    texture_id = int(texture_ids[0]) if hasattr(texture_ids, "__getitem__") else int(texture_ids)
    glBindTexture(GL_TEXTURE_2D, texture_id)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, raw_bytes)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    return texture_id


def update_texture_from_rgba(texture_id: int, raw_bytes: bytes, w: int, h: int) -> None:
    glBindTexture(GL_TEXTURE_2D, texture_id)
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, raw_bytes)


def calculate_dest_rect(img_w: int, img_h: int, win_w: int, win_h: int) -> sdl2.SDL_Rect:
    """Calculate centered destination rectangle preserving aspect ratio."""
    src_ratio = img_w / max(img_h, 1)
    dst_ratio = win_w / max(win_h, 1)

    if src_ratio > dst_ratio:
        draw_w = win_w
        draw_h = int(win_w / src_ratio)
        draw_x = 0
        draw_y = (win_h - draw_h) // 2
    else:
        draw_h = win_h
        draw_w = int(win_h * src_ratio)
        draw_x = (win_w - draw_w) // 2
        draw_y = 0

    return sdl2.SDL_Rect(draw_x, draw_y, draw_w, draw_h)


def draw_texture(texture_id: int, tex_w: int, tex_h: int, win_w: int, win_h: int) -> None:
    """Draw the RGBA texture with aspect-ratio-preserving centered scaling."""
    glEnable(GL_TEXTURE_2D)
    glViewport(0, 0, win_w, win_h)
    glMatrixMode(GL_PROJECTION)
    glLoadIdentity()
    glOrtho(-1, 1, -1, 1, -1, 1)
    glMatrixMode(GL_MODELVIEW)
    glLoadIdentity()

    glBindTexture(GL_TEXTURE_2D, texture_id)
    glClearColor(0.0, 0.0, 0.0, 1.0)
    glClear(GL_COLOR_BUFFER_BIT)

    src_ratio = tex_w / max(tex_h, 1)
    dst_ratio = win_w / max(win_h, 1)
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


def run_signage_framebuffer(
    image_path: str,
    command_q=None,
    status_q=None,
    fb_device: str = "/dev/fb0"
):
    """Main loop for digital signage display using Linux framebuffer."""
    try:
        raw_bytes, img_w, img_h = load_image(image_path)
    except Exception as e:
        if status_q:
            status_q.put({"type": "error", "error": str(e)})
        raise

    # Open framebuffer device
    try:
        fb_fd = os.open(fb_device, os.O_RDWR)
    except OSError as e:
        err = f"Failed to open framebuffer device {fb_device}: {e}"
        if status_q:
            status_q.put({"type": "error", "error": err})
        raise RuntimeError(err)

    try:
        fb_info = FramebufferInfo(fb_fd)
        fb_width = fb_info.xres
        fb_height = fb_info.yres
        fb_bpp = fb_info.bits_per_pixel
        fb_size = fb_info.smem_len

        if status_q:
            status_q.put({
                "type": "started",
                "image_path": image_path,
                "width": img_w,
                "height": img_h,
                "fullscreen": True,
                "framebuffer": fb_device,
                "fb_width": fb_width,
                "fb_height": fb_height,
                "fb_bpp": fb_bpp,
            })

        # Memory-map the framebuffer
        fb_mmap = mmap.mmap(fb_fd, fb_size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

        # Convert image to framebuffer format
        img = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"Failed to decode image: {image_path}")

        # Convert to BGR/RGBA for framebuffer
        if len(img.shape) == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

        # Resize to framebuffer resolution with aspect ratio preservation
        src_ratio = img.shape[1] / max(img.shape[0], 1)
        dst_ratio = fb_width / max(fb_height, 1)

        if src_ratio > dst_ratio:
            new_w = fb_width
            new_h = int(fb_width / src_ratio)
        else:
            new_h = fb_height
            new_w = int(fb_height * src_ratio)

        img_resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # Create black canvas and center the image
        canvas = np.zeros((fb_height, fb_width, 3), dtype=np.uint8)
        y_offset = (fb_height - new_h) // 2
        x_offset = (fb_width - new_w) // 2
        canvas[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = img_resized

        # Convert to framebuffer pixel format
        if fb_bpp == 32:
            # 32-bit: BGRA or BGRX
            img_fb = cv2.cvtColor(canvas, cv2.COLOR_BGR2BGRA)
        elif fb_bpp == 16:
            # 16-bit: RGB565
            img_fb = cv2.cvtColor(canvas, cv2.COLOR_BGR2BGR565)
        else:
            raise ValueError(f"Unsupported framebuffer bits_per_pixel: {fb_bpp}")

        # Write to framebuffer (considering line_length)
        line_length = fb_info.line_length
        bytes_per_pixel = fb_bpp // 8
        # Ensure img_fb has the correct shape for indexing
        if fb_bpp == 16:
            # BGR565 returns (height, width) - reshape to (height, width, 1)
            img_fb = img_fb.reshape(fb_height, fb_width, 1)
        elif fb_bpp == 32:
            # BGRA returns (height, width, 4)
            img_fb = img_fb.reshape(fb_height, fb_width, 4)
        for y in range(fb_height):
            row_start = y * line_length
            row_data = img_fb[y].tobytes()
            fb_mmap.seek(row_start)
            fb_mmap.write(row_data)
        fb_mmap.flush()

        # Main loop - keep process alive, handle IPC commands
        running = True
        while running:
            if command_q is not None:
                while not command_q.empty():
                    try:
                        cmd = command_q.get_nowait()
                        action = cmd.get("action")
                        if action == "stop":
                            running = False
                            break
                        elif action == "switch_image":
                            new_path = cmd.get("image_path")
                            try:
                                new_img = cv2.imread(new_path, cv2.IMREAD_UNCHANGED)
                                if new_img is None:
                                    raise ValueError(f"Failed to decode image: {new_path}")

                                if len(new_img.shape) == 2:
                                    new_img = cv2.cvtColor(new_img, cv2.COLOR_GRAY2BGR)
                                elif new_img.shape[2] == 4:
                                    new_img = cv2.cvtColor(new_img, cv2.COLOR_BGRA2BGR)

                                # Resize with aspect ratio preservation
                                src_ratio = new_img.shape[1] / max(new_img.shape[0], 1)
                                dst_ratio = fb_width / max(fb_height, 1)

                                if src_ratio > dst_ratio:
                                    new_w = fb_width
                                    new_h = int(fb_width / src_ratio)
                                else:
                                    new_h = fb_height
                                    new_w = int(fb_height * src_ratio)

                                new_img_resized = cv2.resize(new_img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

                                # Create black canvas and center the image
                                new_canvas = np.zeros((fb_height, fb_width, 3), dtype=np.uint8)
                                y_offset = (fb_height - new_h) // 2
                                x_offset = (fb_width - new_w) // 2
                                new_canvas[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = new_img_resized

                                # Convert to framebuffer pixel format
                                if fb_bpp == 32:
                                    new_img_fb = cv2.cvtColor(new_canvas, cv2.COLOR_BGR2BGRA)
                                elif fb_bpp == 16:
                                    new_img_fb = cv2.cvtColor(new_canvas, cv2.COLOR_BGR2BGR565)
                                else:
                                    raise ValueError(f"Unsupported framebuffer bits_per_pixel: {fb_bpp}")

                                # Write to framebuffer (considering line_length)
                                if fb_bpp == 16:
                                    new_img_fb = new_img_fb.reshape(fb_height, fb_width, 1)
                                elif fb_bpp == 32:
                                    new_img_fb = new_img_fb.reshape(fb_height, fb_width, 4)
                                for y in range(fb_height):
                                    row_start = y * line_length
                                    row_data = new_img_fb[y].tobytes()
                                    fb_mmap.seek(row_start)
                                    fb_mmap.write(row_data)
                                fb_mmap.flush()

                                img_w, img_h = new_img.shape[1], new_img.shape[0]
                                image_path = new_path

                                if status_q:
                                    status_q.put({
                                        "type": "switched",
                                        "image_path": image_path,
                                        "width": img_w,
                                        "height": img_h
                                    })
                            except Exception as ex:
                                if status_q:
                                    status_q.put({"type": "error", "error": f"Failed to switch image: {ex}"})
                    except Exception:
                        pass

            if not running:
                break

            time.sleep(0.016)

    except Exception as e:
        if status_q:
            status_q.put({"type": "error", "error": str(e)})
        raise
    finally:
        try:
            fb_mmap.close()
        except Exception:
            pass
        os.close(fb_fd)

        if status_q:
            status_q.put({"type": "stopped"})


def run_signage(
    image_path: str,
    fullscreen: bool = True,
    command_q=None,
    status_q=None,
    use_framebuffer: bool = False,
    fb_device: str = "/dev/fb0"
):
    """Main loop for digital signage display using SDL2/OpenGL or framebuffer."""
    if use_framebuffer:
        return run_signage_framebuffer(
            image_path=image_path,
            command_q=command_q,
            status_q=status_q,
            fb_device=fb_device
        )

    try:
        raw_bytes, img_w, img_h = load_image(image_path)
    except Exception as e:
        if status_q:
            status_q.put({"type": "error", "error": str(e)})
        raise

    if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
        err = f"SDL_Init Error: {sdl2.SDL_GetError()}"
        if status_q:
            status_q.put({"type": "error", "error": err})
        raise RuntimeError(err)

    window = None
    texture_id = None
    gl_context = None

    try:
        flags = sdl2.SDL_WINDOW_OPENGL | sdl2.SDL_WINDOW_RESIZABLE
        if fullscreen:
            flags |= sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP

        sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_MAJOR_VERSION, 2)
        sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_MINOR_VERSION, 1)

        window = sdl2.SDL_CreateWindow(
            b"Digital Signage",
            sdl2.SDL_WINDOWPOS_CENTERED,
            sdl2.SDL_WINDOWPOS_CENTERED,
            1280,
            720,
            flags
        )
        if not window:
            raise RuntimeError(f"SDL_CreateWindow Error: {sdl2.SDL_GetError()}")

        gl_context = sdl2.SDL_GL_CreateContext(window)
        if not gl_context:
            raise RuntimeError(f"SDL_GL_CreateContext Error: {sdl2.SDL_GetError()}")
        if sdl2.SDL_GL_SetSwapInterval(0) != 0:
            sdl2.SDL_GL_SetSwapInterval(1)

        sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE if fullscreen else sdl2.SDL_ENABLE)

        texture_id = create_texture_from_rgba(raw_bytes, img_w, img_h)

        if status_q:
            status_q.put({
                "type": "started",
                "image_path": image_path,
                "width": img_w,
                "height": img_h,
                "fullscreen": fullscreen
            })

        event = sdl2.SDL_Event()
        running = True
        needs_redraw = True

        while running:
            # Handle SDL events
            while sdl2.SDL_PollEvent(event) != 0:
                if event.type == sdl2.SDL_QUIT:
                    running = False
                    break
                elif event.type == sdl2.SDL_KEYDOWN:
                    sym = event.key.keysym.sym
                    if sym in (sdl2.SDLK_ESCAPE, sdl2.SDLK_q):
                        running = False
                        break
                elif event.type == sdl2.SDL_WINDOWEVENT:
                    if event.window.event in (
                        sdl2.SDL_WINDOWEVENT_RESIZED,
                        sdl2.SDL_WINDOWEVENT_SIZE_CHANGED,
                        sdl2.SDL_WINDOWEVENT_EXPOSED
                    ):
                        needs_redraw = True

            # Handle IPC commands
            if command_q is not None:
                while not command_q.empty():
                    try:
                        cmd = command_q.get_nowait()
                        action = cmd.get("action")
                        if action == "stop":
                            running = False
                            break
                        elif action == "switch_image":
                            new_path = cmd.get("image_path")
                            try:
                                new_bytes, new_w, new_h = load_image(new_path)
                                if texture_id is not None:
                                    glDeleteTextures([texture_id])
                                texture_id = create_texture_from_rgba(new_bytes, new_w, new_h)
                                img_w, img_h = new_w, new_h
                                image_path = new_path
                                needs_redraw = True
                                if status_q:
                                    status_q.put({
                                        "type": "switched",
                                        "image_path": image_path,
                                        "width": img_w,
                                        "height": img_h
                                    })
                            except Exception as ex:
                                if status_q:
                                    status_q.put({"type": "error", "error": f"Failed to switch image: {ex}"})
                    except Exception:
                        pass

            if not running:
                break

            if needs_redraw:
                w_ptr, h_ptr = sdl2.c_int(), sdl2.c_int()
                sdl2.SDL_GetWindowSize(window, w_ptr, h_ptr)
                win_w, win_h = w_ptr.value, h_ptr.value

                draw_texture(texture_id, img_w, img_h, win_w, win_h)
                sdl2.SDL_GL_SwapWindow(window)
                needs_redraw = False

            time.sleep(0.016)

    except Exception as e:
        if status_q:
            status_q.put({"type": "error", "error": str(e)})
        raise
    finally:
        if texture_id is not None:
            try:
                glDeleteTextures([texture_id])
            except Exception:
                pass
        if window:
            sdl2.SDL_DestroyWindow(window)
        sdl2.SDL_Quit()

        if status_q:
            status_q.put({"type": "stopped"})


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m core.signage <image_path> [--windowed] [--framebuffer]")
        sys.exit(1)

    path = sys.argv[1]
    is_fullscreen = "--windowed" not in sys.argv
    use_fb = "--framebuffer" in sys.argv
    run_signage(path, fullscreen=is_fullscreen, use_framebuffer=use_fb)
