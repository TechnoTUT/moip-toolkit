"""
cli.menu
Interactive terminal selection menu for NDI sources.
"""
from __future__ import annotations
import sys
import time
import click
from cyndilib.finder import Finder


def select_ndi_source_interactive(finder: Finder) -> str:
    """NDIソースを検索してユーザーに選択させる対話型CLIメニュー"""
    click.echo("Searching for NDI sources on the network", nl=False)
    sys.stdout.flush()
    
    sources = []
    try:
        for i in range(100):
            finder.wait_for_sources(0)
            time.sleep(0.1)
            sources = list(finder)
            if sources:
                time.sleep(0.5)
                finder.wait_for_sources(0)
                sources = list(finder)
                break
            if i % 10 == 0:
                click.echo(".", nl=False)
                sys.stdout.flush()
        click.echo()
    except KeyboardInterrupt:
        click.echo("\nSearch cancelled by user.", err=True)
        sys.exit(1)
        
    if not sources:
        click.echo("Error: No NDI sources found. Please ensure the sender is running.", err=True)
        sys.exit(1)
        
    click.echo("\n--- Available NDI Sources ---")
    for i, src in enumerate(sources):
        click.echo(f"  [{i + 1}] {src.name}")
        
    while True:
        try:
            choice = click.prompt("\nSelect a source by number", type=int)
            if 1 <= choice <= len(sources):
                selected_name = sources[choice - 1].name
                click.echo(f"Selected: {selected_name}\n")
                return selected_name
            else:
                click.echo(f"Invalid choice. Please enter a number between 1 and {len(sources)}.")
        except KeyboardInterrupt:
            click.echo("\nSelection cancelled by user.", err=True)
            sys.exit(1)
