//! moip-engine: the media side of moip-toolkit.
//!
//! Each subcommand runs one job (receive & display, send, ...) as its own
//! process. It works standalone from a terminal, or under the Go server with
//! `--control`, which reads JSON commands on stdin and writes JSON events on
//! stdout (see `control.rs`).

mod audio;
mod control;
mod discover;
mod font;
mod gfx;
mod recv;
mod rx;
mod v4l2;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "moip-engine", version, about = "NDI media engine for moip-toolkit")]
struct Cli {
    /// Machine-readable mode for the server: JSON commands on stdin, JSON events on stdout.
    #[arg(long, global = true)]
    control: bool,

    #[command(subcommand)]
    command: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Print the NDI runtime version.
    Version,
    /// List NDI sources on the network.
    Discover {
        /// Print the list once and exit instead of watching for changes.
        #[arg(long)]
        once: bool,
        /// Seconds to wait for sources with --once.
        #[arg(long, default_value_t = 3.0)]
        timeout: f64,
        /// Comma-separated IPs to query directly (for networks without mDNS).
        #[arg(long)]
        extra_ips: Option<String>,
    },
    /// Receive an NDI source and display it (fullscreen viewer).
    Rx(rx::Args),
}

fn main() {
    let cli = Cli::parse();
    let result = match cli.command {
        Cmd::Version => ndi::version().map(|v| println!("{v}")).map_err(Into::into),
        Cmd::Discover { once, timeout, extra_ips } => {
            discover::run(discover::Args { once, timeout, extra_ips, control: cli.control })
        }
        Cmd::Rx(args) => rx::run(args, cli.control),
    };
    if let Err(e) = result {
        if cli.control {
            control::emit_error(&format!("{e:#}"));
        }
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}
