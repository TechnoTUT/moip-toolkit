"""
core.signage
Local digital signage display using SDL2 renderer.
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


def create_texture_from_rgba(renderer, raw_bytes: bytes, w: int, h: int):
    """Create and update an SDL2 RGBA texture."""
    texture = sdl2.SDL_CreateTexture(
        renderer,
        sdl2.SDL_PIXELFORMAT_RGBA32,
        sdl2.SDL_TEXTUREACCESS_STATIC,
        w,
        h
    )
    if not texture:
        raise RuntimeError(f"SDL_CreateTexture Error: {sdl2.SDL_GetError()}")

    sdl2.SDL_UpdateTexture(texture, None, raw_bytes, w * 4)
    return texture


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


def run_signage(
    image_path: str,
    fullscreen: bool = True,
    command_q=None,
    status_q=None
):
    """Main loop for digital signage display using SDL2 renderer."""
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
    renderer = None
    texture = None

    try:
        flags = sdl2.SDL_WINDOW_RESIZABLE
        if fullscreen:
            flags |= sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP

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

        renderer = sdl2.SDL_CreateRenderer(
            window,
            -1,
            sdl2.SDL_RENDERER_ACCELERATED | sdl2.SDL_RENDERER_PRESENTVSYNC
        )
        if not renderer:
            # Fallback to software renderer if hardware accelerated fails
            renderer = sdl2.SDL_CreateRenderer(window, -1, sdl2.SDL_RENDERER_SOFTWARE)
            if not renderer:
                raise RuntimeError(f"SDL_CreateRenderer Error: {sdl2.SDL_GetError()}")

        sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE if fullscreen else sdl2.SDL_ENABLE)

        texture = create_texture_from_rgba(renderer, raw_bytes, img_w, img_h)

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
                                if texture:
                                    sdl2.SDL_DestroyTexture(texture)
                                texture = create_texture_from_rgba(renderer, new_bytes, new_w, new_h)
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

                dest_rect = calculate_dest_rect(img_w, img_h, win_w, win_h)

                # Clear background to black
                sdl2.SDL_SetRenderDrawColor(renderer, 0, 0, 0, 255)
                sdl2.SDL_RenderClear(renderer)

                # Render image
                sdl2.SDL_RenderCopy(renderer, texture, None, dest_rect)
                sdl2.SDL_RenderPresent(renderer)
                needs_redraw = False

            time.sleep(0.016)

    except Exception as e:
        if status_q:
            status_q.put({"type": "error", "error": str(e)})
        raise
    finally:
        if texture:
            sdl2.SDL_DestroyTexture(texture)
        if renderer:
            sdl2.SDL_DestroyRenderer(renderer)
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
