"""
cli.signage_cmd
CLI command definition for digital signage viewer.
"""
from __future__ import annotations
import os
import glob
import click
from core.signage import run_signage


@click.command(name="signage", help="Display a static image in fullscreen as digital signage.")
@click.option("-i", "--image", "image_path", type=click.Path(exists=True, dir_okay=False), default=None, help="Path to the image file to display.")
@click.option("--fullscreen/--windowed", default=True, show_default=True, help="Display in fullscreen or windowed mode.")
def signage_command(image_path: str | None, fullscreen: bool):
    if not image_path:
        # Search in data/signage
        candidates = sorted(
            glob.glob("data/signage/*.*"),
            key=os.path.getmtime,
            reverse=True
        )
        valid_candidates = [
            c for c in candidates
            if c.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
        ]
        if valid_candidates:
            image_path = valid_candidates[0]
            click.echo(f"Using latest uploaded image: {image_path}")
        else:
            click.echo("Error: No image specified and no images found in data/signage/.", err=True)
            click.echo("Please provide an image with -i / --image <path>", err=True)
            return

    try:
        click.echo(f"Starting digital signage with image: {image_path} (fullscreen={fullscreen})")
        click.echo("Press ESC or 'q' to quit.")
        run_signage(image_path, fullscreen=fullscreen)
    except KeyboardInterrupt:
        click.echo("\nDigital signage stopped by user.")
    except Exception as e:
        click.echo(f"Error running digital signage: {e}", err=True)
