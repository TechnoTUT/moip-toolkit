"""
main.py
Unified CLI entry point for utone-ndi-utils.
Supports `rx`, `tx`, and `web` subcommands.
"""
from __future__ import annotations
import click
import uvicorn

from cli.rx_cmd import rx_command
from cli.tx_cmd import tx_command
from cli.multiview_cmd import multiview_command


@click.group()
@click.version_option(version="1.0.0")
def cli():
    """utone-ndi-utils: NDI Video & Audio Transmission Toolkit for DJ Events."""
    pass


@cli.command(name="web", help="Start the FastAPI backend server and Web UI.")
@click.option("-h", "--host", default="0.0.0.0", show_default=True, help="Host to bind Web server.")
@click.option("-p", "--port", default=8000, show_default=True, help="Port to bind Web server.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload for development.")
def web_command(host: str, port: int, reload: bool):
    import signal
    import sys

    click.echo(f"Starting utone-ndi-utils Web API server on http://{host}:{port}")

    config = uvicorn.Config(
        "backend.main:app",
        host=host,
        port=port,
        reload=reload,
        timeout_graceful_shutdown=0,
    )
    server = uvicorn.Server(config)

    def handle_exit(sig, frame):
        server.should_exit = True
        server.force_exit = True

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    try:
        server.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        sys.exit(0)


cli.add_command(rx_command)
cli.add_command(tx_command)
cli.add_command(multiview_command)


if __name__ == "__main__":
    cli()
