"""
backend.main
FastAPI application entry point.
Exposes REST endpoints for NDI source discovery, device listing, and RX/TX control.
"""
from __future__ import annotations
import os
from contextlib import asynccontextmanager
from typing import List
import json
import asyncio
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.models import (
    NDISourceItem,
    VideoDeviceItem,
    AudioDeviceItem,
    RxStartRequest,
    RxSwitchRequest,
    RxStatus,
    TxStartRequest,
    TxStatus,
    SystemStatus,
    AppSettings,
    MultiviewStartRequest,
    MultiviewStatus,
    WebRTCOfferRequest,
    WebRTCAnswerResponse,
)
from backend.ndi_scanner import scanner
from backend.devices import list_video_devices, list_audio_devices
from backend.rx_runner import rx_runner
from backend.tx_runner import tx_runner
from backend.multiview_runner import multiview_runner
from backend.preview_manager import preview_manager
from backend.webrtc_manager import webrtc_manager
from backend.system_monitor import system_monitor
from backend.settings import settings_manager

is_shutting_down = False


async def auto_start_worker():
    """Background task on startup to auto-start RX / TX if configured."""
    await asyncio.sleep(1.0)
    if is_shutting_down:
        return
    settings = settings_manager.get_settings()
    
    # Auto-start TX if configured
    if settings.tx.auto_start and not tx_runner.get_status().running:
        try:
            req = TxStartRequest(
                video_device=settings.tx.video_device,
                audio_device=settings.tx.audio_device,
                no_audio=settings.tx.no_audio,
                sender_name=settings.tx.sender_name,
                x_res=settings.tx.x_res,
                y_res=settings.tx.y_res,
                fps=settings.tx.fps,
                pix_fmt=settings.tx.pix_fmt
            )
            tx_runner.start(req)
            print(f"[AutoStart] TX started with sender name: {settings.tx.sender_name}")
        except Exception as e:
            print(f"[AutoStart] Failed to auto-start TX: {e}")

    # Auto-start RX if configured
    if settings.rx.auto_start and settings.rx.sender_name and not rx_runner.get_status().running:
        try:
            rx_runner.start(
                sender_name=settings.rx.sender_name,
                recv_fmt=settings.rx.recv_fmt,
                recv_bandwidth=settings.rx.recv_bandwidth,
                fullscreen=settings.rx.fullscreen
            )
            print(f"[AutoStart] RX started with target source: {settings.rx.sender_name}")
        except Exception as e:
            print(f"[AutoStart] Failed to auto-start RX: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global is_shutting_down
    is_shutting_down = False
    # Start background NDI finder scanner on startup
    scanner.start()
    auto_task = asyncio.create_task(auto_start_worker())
    yield
    # Cleanup on shutdown
    is_shutting_down = True
    auto_task.cancel()
    scanner.stop()
    preview_manager.stop_all()
    rx_runner.stop()
    tx_runner.stop()
    multiview_runner.stop()


app = FastAPI(
    title="utone-ndi-utils API",
    description="REST API to control NDI RX (Viewer) and TX (Sender)",
    version="1.0.0",
    lifespan=lifespan,
)

from starlette.middleware.base import BaseHTTPMiddleware

# Enable CORS for local Nuxt dev server (typically port 3000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SuppressShutdownCancellationMiddleware(BaseHTTPMiddleware):
    """
    Suppresses asyncio.CancelledError on shutdown for persistent streaming endpoints
    (like SSE or MJPEG) so uvicorn does not dump tracebacks when cancelled by graceful shutdown.
    """
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except asyncio.CancelledError:
            # Re-raise as EmptyResponse or simply suppress if shutting down
            if is_shutting_down:
                return Response(status_code=204)
            raise


app.add_middleware(SuppressShutdownCancellationMiddleware)


# --- Server-Sent Events (SSE) Stream ---
@app.get("/api/events", tags=["SSE"])
async def events_stream(request: Request):
    """
    Server-Sent Events endpoint pushing NDI sources and RX/TX statuses.
    Sends full state periodically (or immediately on change) without frontend polling.
    """
    async def event_generator():
        last_payload_str = ""
        while not is_shutting_down:
            if await request.is_disconnected():
                break

            sources = sorted(
                [s.model_dump() for s in scanner.get_sources()],
                key=lambda x: x["name"]
            )
            rx_stat = rx_runner.get_status().model_dump()
            tx_stat = tx_runner.get_status().model_dump()
            sys_stat = system_monitor.get_status().model_dump()

            current_payload = {
                "sources": sources,
                "rx": rx_stat,
                "tx": tx_stat,
                "system": sys_stat,
                "multiview": multiview_runner.get_status().model_dump(),
                "settings": settings_manager.get_settings().model_dump(),
            }
            payload_str = json.dumps(current_payload, sort_keys=True)

            # Send update immediately if changed, or heartbeat ping periodically
            if payload_str != last_payload_str:
                last_payload_str = payload_str
                yield f"event: state\ndata: {payload_str}\n\n"
            else:
                yield ": keepalive\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


# --- System Monitor Endpoints ---
@app.get("/api/system/status", response_model=SystemStatus, tags=["System"])
def get_system_status():
    """Get current system CPU, memory, and load average."""
    return system_monitor.get_status()


# --- NDI Discovery Endpoints ---
@app.get("/api/ndi/sources", response_model=List[NDISourceItem], tags=["NDI"])
def get_ndi_sources():
    """Get list of currently detected NDI sources on the local network."""
    return scanner.get_sources()


@app.get("/api/ndi/preview", tags=["NDI"])
async def get_ndi_preview(
    request: Request,
    source: str,
    fps: int = 3,
    width: int = 360
):
    """
    Stream live MJPEG preview for a given NDI source name.
    fps: Target frames per second (e.g. 2-3 for thumbnail multi-view, 20 for modal).
    width: Target width to scale to (e.g. 360 for thumbnail, 720 for modal).
    """
    # Clamp parameters to reasonable bounds
    target_fps = max(1, min(fps, 30))
    target_width = max(160, min(width, 1280))

    session = preview_manager.get_or_create_session(source)
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    session.add_subscriber(queue, fps=target_fps, max_width=target_width)

    async def mjpeg_generator():
        try:
            while not is_shutting_down:
                if await request.is_disconnected():
                    break
                try:
                    # Wait up to 0.5 second for a new frame
                    jpeg_bytes = await asyncio.wait_for(queue.get(), timeout=0.5)
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + jpeg_bytes + b"\r\n"
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            session.remove_subscriber(queue)
            preview_manager.cleanup_idle_sessions()

    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/api/webrtc/offer", response_model=WebRTCAnswerResponse, tags=["WebRTC"])
async def webrtc_offer(req: WebRTCOfferRequest):
    """
    WebRTC signaling endpoint. Receives SDP offer from browser,
    attaches an NDI video track, and returns the SDP answer.
    """
    try:
        answer = await webrtc_manager.handle_offer(
            source_name=req.source,
            sdp=req.sdp,
            sdp_type=req.type
        )
        return answer
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"WebRTC offer failed: {e}")



# --- Device Discovery Endpoints ---
@app.get("/api/devices/video", response_model=List[VideoDeviceItem], tags=["Devices"])
def get_video_devices():
    """List available video capture devices."""
    return list_video_devices()


@app.get("/api/devices/audio", response_model=List[AudioDeviceItem], tags=["Devices"])
def get_audio_devices():
    """List available audio capture devices."""
    return list_audio_devices()


# --- RX (Receiver / Viewer) Endpoints ---
@app.get("/api/rx/status", response_model=RxStatus, tags=["RX"])
def get_rx_status():
    """Get current status of NDI RX viewer."""
    return rx_runner.get_status()


@app.post("/api/rx/start", response_model=RxStatus, tags=["RX"])
def start_rx(req: RxStartRequest):
    """Start NDI RX viewer window with specified source."""
    try:
        rx_runner.start(
            sender_name=req.sender_name,
            recv_fmt=req.recv_fmt,
            recv_bandwidth=req.recv_bandwidth,
            fullscreen=req.fullscreen,
        )
        settings_manager.on_rx_started(req)
        return rx_runner.get_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/rx/switch", response_model=RxStatus, tags=["RX"])
def switch_rx(req: RxSwitchRequest):
    """Switch active NDI source without closing the viewer window."""
    try:
        rx_runner.switch_source(req.sender_name)
        curr_settings = settings_manager.get_settings()
        curr_settings.rx.sender_name = req.sender_name
        settings_manager.update_settings(curr_settings)
        return rx_runner.get_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/rx/stop", response_model=RxStatus, tags=["RX"])
def stop_rx():
    """Stop NDI RX viewer window."""
    rx_runner.stop()
    return rx_runner.get_status()


# --- TX (Sender) Endpoints ---
@app.get("/api/tx/status", response_model=TxStatus, tags=["TX"])
def get_tx_status():
    """Get current status of NDI TX sender."""
    return tx_runner.get_status()


@app.post("/api/tx/start", response_model=TxStatus, tags=["TX"])
def start_tx(req: TxStartRequest):
    """Start NDI TX sender."""
    try:
        tx_runner.start(req)
        settings_manager.on_tx_started(req)
        return tx_runner.get_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/tx/stop", response_model=TxStatus, tags=["TX"])
def stop_tx():
    """Stop NDI TX sender."""
    tx_runner.stop()
    return tx_runner.get_status()


# --- Multi-Viewer Endpoints ---
@app.get("/api/multiview/status", response_model=MultiviewStatus, tags=["Multiview"])
def get_multiview_status():
    """Get status of SDL2 multi-viewer window."""
    return multiview_runner.get_status()


@app.post("/api/multiview/start", response_model=MultiviewStatus, tags=["Multiview"])
def start_multiview(req: MultiviewStartRequest):
    """Start SDL2 multi-viewer grid window with auto-discovery or specific sources."""
    try:
        multiview_runner.start(req)
        return multiview_runner.get_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/multiview/stop", response_model=MultiviewStatus, tags=["Multiview"])
def stop_multiview():
    """Stop SDL2 multi-viewer window."""
    multiview_runner.stop()
    return multiview_runner.get_status()


# --- Persistent Settings Endpoints ---
@app.get("/api/settings", response_model=AppSettings, tags=["Settings"])
def get_settings():
    """Get saved preset settings and auto-start preferences."""
    return settings_manager.get_settings()


@app.post("/api/settings", response_model=AppSettings, tags=["Settings"])
def update_settings(settings: AppSettings):
    """Update preset settings and auto-start preferences."""
    settings_manager.update_settings(settings)
    return settings_manager.get_settings()


# --- Serve Nuxt static build if exists ---
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", ".output", "public")
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
