"""
cli.multiview_cmd
CLI command definition for NDI Multi-Viewer.
"""
from __future__ import annotations
from typing import List, Optional
import click
from core.multiview import play_multiview


@click.command(name="multiview", help="Start NDI Multi-Viewer to monitor multiple sources in an auto-grid layout via SDL2.")
@click.option('-s', '--sources', multiple=True, type=str, help='Specific NDI source names to monitor (e.g. -s CAM1 -s CAM2). If omitted, all discovered sources will be shown.')
@click.option('--fullscreen', is_flag=True, help='Start in fullscreen mode')
def multiview_command(sources: tuple[str, ...], fullscreen: bool):
    try:
        source_list = list(sources) if sources else None
        click.echo(f"Starting Multi-Viewer (fullscreen={fullscreen}, sources={'auto' if not source_list else source_list})...")
        play_multiview(
            source_names=source_list,
            fullscreen=fullscreen,
            auto_discover=True,
            log_fn=click.echo
        )
    except KeyboardInterrupt:
        click.echo("\nMulti-Viewer terminated by user.", err=True)
    except Exception as e:
        click.echo(f"A fatal error occurred: {e}", err=True)
