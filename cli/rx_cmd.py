"""
cli.rx_cmd
CLI command definition for NDI receiver (Viewer).
"""
from __future__ import annotations
import click
from cyndilib.finder import Finder
from core.rx import RecvFmt, Bandwidth, Options, play_sdl
from cli.menu import select_ndi_source_interactive


@click.command(name="rx", help="Start NDI Viewer to receive and display video via SDL2.")
@click.option('-s', '--sender-name', type=str, default=None, help='NDI source name to connect to. If omitted, an interactive menu will be shown.')
@click.option('-f', '--recv-fmt', type=click.Choice(choices=[m.name for m in RecvFmt]), default='rgb', show_default=True, help='Pixel format for receiving')
@click.option('-b', '--recv-bandwidth', type=click.Choice(choices=[m.name for m in Bandwidth]), default='highest', show_default=True, help='Receiving bandwidth')
@click.option('--fullscreen', is_flag=True, help='Start in fullscreen mode')
def rx_command(sender_name: str | None, recv_fmt: str, recv_bandwidth: str, fullscreen: bool):
    finder = Finder()
    try:
        if not sender_name:
            sender_name = select_ndi_source_interactive(finder)

        options = Options(
            sender_name=sender_name,
            recv_fmt=RecvFmt.from_str(recv_fmt),
            recv_bandwidth=Bandwidth.from_str(recv_bandwidth),
            fullscreen=fullscreen,
        )
        play_sdl(options, finder, log_fn=click.echo)
    except KeyboardInterrupt:
        click.echo("\nProgram terminated by user.", err=True)
    except Exception as e:
        click.echo(f"A fatal error occurred: {e}", err=True)
    finally:
        if hasattr(finder, 'destroy'):
            finder.destroy()
