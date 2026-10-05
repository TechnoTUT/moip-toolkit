"""
core.signage
Local digital signage display using SDL2/OpenGL.
Renders static images in fullscreen or windowed mode with aspect ratio preservation.
"""
from __future__ import annotations

import os
import sys
import time
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


def run_signage(
    image_path: str,
    fullscreen: bool = True,
    command_q=None,
    status_q=None
):
    """Main loop for digital signage display using SDL2/OpenGL."""
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
        print("Usage: python -m core.signage <image_path> [--windowed]")
        sys.exit(1)

    path = sys.argv[1]
    is_fullscreen = "--windowed" not in sys.argv
    run_signage(path, fullscreen=is_fullscreen)
