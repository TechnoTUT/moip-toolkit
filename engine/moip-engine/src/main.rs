//! moip-engine: the media side of moip-toolkit.
//!
//! Each subcommand runs one job (receive & display, send, ...) as its own
//! process. It works standalone from a terminal, or under the Go server with
//! `--control`, which reads JSON commands on stdin and writes JSON events on
//! stdout (see `control.rs`).

mod audio;
mod control;
mod convert;
mod devices;
mod discover;
mod font;
mod gfx;
mod memstats;
mod multiview;
mod preview;
mod recv;
mod rx;
mod signage;
mod tx;
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
    /// Show several NDI sources in a grid.
    Multiview(multiview::Args),
    /// Encode a low-rate browser preview of a source (binary output, for the server).
    Preview(preview::Args),
    /// List video (V4L2) and audio (ALSA) capture devices as JSON.
    Devices,
    /// Show a still image fullscreen (digital signage).
    Signage(signage::Args),
    /// Send a capture device (or colour bars) as an NDI source.
    Tx(tx::Args),
}

/// Make large allocations (video frames, a few MB each) always use mmap so
/// they go back to the OS when freed.
///
/// glibc raises its mmap threshold dynamically after an mmapped block is
/// freed; from then on frame-sized buffers come from the heap arenas, and
/// every receiver re-creation leaves fragmented free memory behind that is
/// never returned (RSS grew ~3 MB/min while switching sources, with live heap
/// usage flat). A fixed threshold disables that adjustment.
fn tune_malloc() {
    if std::env::var_os("MOIP_NO_MALLOPT").is_some() {
        return;
    }
    // SAFETY: plain glibc calls, made before any other thread exists.
    unsafe {
        libc::mallopt(libc::M_MMAP_THRESHOLD, 1 << 20);
        libc::mallopt(libc::M_TRIM_THRESHOLD, 4 << 20);
    }
}

fn main() {
    tune_malloc();
    let cli = Cli::parse();
    let result = match cli.command {
        Cmd::Version => ndi::version().map(|v| println!("{v}")).map_err(Into::into),
        Cmd::Discover { once, timeout, extra_ips } => {
            discover::run(discover::Args { once, timeout, extra_ips, control: cli.control })
        }
        Cmd::Rx(args) => rx::run(args, cli.control),
        Cmd::Devices => devices::run(),
        Cmd::Preview(args) => preview::run(args, cli.control),
        Cmd::Multiview(args) => multiview::run(args, cli.control),
        Cmd::Signage(args) => signage::run(args, cli.control),
        Cmd::Tx(args) => tx::run(args, cli.control),
    };
    if let Err(e) = result {
        if cli.control {
            control::emit_error(&format!("{e:#}"));
        }
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}
