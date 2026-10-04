"""
cli.tx_cmd
CLI command definition for NDI sender (Camera/Audio stream).
"""
from __future__ import annotations
import sys
import click
import cv2
try:
    import sounddevice as sd
except Exception:
    sd = None
from core.tx import PixFmt, Options, capture_and_send, parse_frame_rate


@click.command(name="tx", help="Capture camera/audio and stream over NDI.")
@click.option('--list-devices', is_flag=True, help='List available video and audio devices and exit.')
@click.option('--no-audio', is_flag=True, help='Disable audio and send video only.')
@click.option('--pix-fmt', type=click.Choice([m.name for m in PixFmt], case_sensitive=False), default=PixFmt.BGRX.name, show_default=True, help='Pixel format.')
@click.option('-x', '--x-res', type=int, default=1920, show_default=True, help='Horizontal resolution.')
@click.option('-y', '--y-res', type=int, default=1080, show_default=True, help='Vertical resolution.')
@click.option('--fps', type=str, default='30', show_default=True, help='Frame rate (e.g., 30, 29.97, 60000/1001).')
@click.option('-d', '--video-device', type=int, default=0, show_default=True, help='Video device index (-1 for Color Bars test pattern).')
@click.option('--test-pattern', is_flag=True, help='Shortcut to broadcast SMPTE Color Bars test pattern instead of camera.')
@click.option('--audio-device', type=int, default=None, show_default=False, help='Audio device ID (ignored if --no-audio).')
@click.option('--sample-rate', type=int, default=48000, show_default=True, help='Audio sample rate (ignored if --no-audio).')
@click.option('--audio-channels', type=int, default=2, show_default=True, help='Number of audio channels (ignored if --no-audio).')
@click.option('-n', '--sender-name', type=str, default='TX', show_default=True, help='NDI name for the sender.')
def tx_command(list_devices: bool, no_audio: bool, pix_fmt: str, x_res: int, y_res: int, fps: str, video_device: int, test_pattern: bool, audio_device: int, sample_rate: int, audio_channels: int, sender_name: str):
    if test_pattern:
        video_device = -1
    if list_devices:
        click.echo("--- Available Video Devices (OpenCV) ---")
        for i in range(10):
            cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
            if cap.isOpened():
                click.echo(f"  Device {i}: Available")
                cap.release()
            else:
                break
        click.echo("\n--- Available Audio Devices (sounddevice) ---")
        if sd is not None:
            click.echo(sd.query_devices())
        else:
            click.echo("  sounddevice / PortAudio not available")
        return

    try:
        frame_rate = float(parse_frame_rate(fps))
        opts = Options(
            pix_fmt=PixFmt.from_str(pix_fmt),
            xres=x_res,
            yres=y_res,
            fps=frame_rate,
            video_device=video_device,
            sender_name=sender_name,
            no_audio=no_audio,
            audio_device=audio_device,
            sample_rate=sample_rate,
            audio_channels=audio_channels
        )
        capture_and_send(opts, log_fn=click.echo)
    except (IOError, ValueError, RuntimeError) as e:
        click.echo(f"An error occurred: {type(e).__name__}: {e}", err=True)
    except KeyboardInterrupt:
        click.echo("\nStream stopped by user.")
