//! `moip-engine rx`: receive one NDI source and show it fullscreen.
//!
//! Latency path: the capture thread blocks in `NDIlib_recv_capture_v3`; when a
//! frame arrives it wakes this (render) thread, which uploads the SDK buffer
//! straight into a texture, draws and swaps without vsync. No frame queue, no
//! framesync, no copies on the CPU.

use crate::control::{emit, Command, Control};
use crate::gfx::{self, Painter, Rect, VideoTexture, WinEvent, Window};
use crate::recv::{Directory, RecvConfig, RecvWorker};
use anyhow::Result;
use serde::Serialize;
use std::io::{BufRead, Write};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, PartialEq, Eq, Debug, clap::ValueEnum, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum RecvFmt {
    Uyvy,
    Rgb,
    Bgr,
}

impl RecvFmt {
    pub fn color_format(self) -> ndi::ColorFormat {
        match self {
            Self::Uyvy => ndi::ColorFormat::UyvyRgba,
            Self::Rgb => ndi::ColorFormat::RgbxRgba,
            Self::Bgr => ndi::ColorFormat::BgrxBgra,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, clap::ValueEnum, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Bandwidth {
    Highest,
    Lowest,
}

impl Bandwidth {
    pub fn ndi(self) -> ndi::Bandwidth {
        match self {
            Self::Highest => ndi::Bandwidth::Highest,
            Self::Lowest => ndi::Bandwidth::Lowest,
        }
    }
}

#[derive(clap::Args, Debug)]
pub struct Args {
    /// NDI source: full name "HOST (STREAM)" or stream name. Asked interactively if omitted.
    #[arg(short = 's', long = "source")]
    pub source: Option<String>,
    #[arg(long, value_enum, default_value_t = RecvFmt::Uyvy)]
    pub recv_fmt: RecvFmt,
    #[arg(long, value_enum, default_value_t = Bandwidth::Highest)]
    pub recv_bandwidth: Bandwidth,
    #[arg(short, long)]
    pub fullscreen: bool,
    /// SDL video driver: kmsdrm (default; falls back to the desktop if unavailable), auto, wayland or x11.
    #[arg(long, default_value = gfx::DEFAULT_VIDEO_DRIVER, value_parser = clap::builder::PossibleValuesParser::new(gfx::VIDEO_DRIVERS))]
    pub video_driver: String,
    /// Comma-separated IPs to query for sources (networks without mDNS).
    #[arg(long)]
    pub extra_ips: Option<String>,
    /// Seconds to show the IP banner after start.
    #[arg(long, default_value_t = 30.0)]
    pub banner_seconds: f64,
    /// Port of the web UI shown on the waiting screen.
    #[arg(long, default_value_t = 8000)]
    pub web_port: u16,
}

#[derive(Serialize)]
struct Status<'a> {
    r#type: &'static str,
    is_connected: bool,
    current_source: &'a str,
    recv_fmt: RecvFmt,
    recv_bandwidth: Bandwidth,
    fullscreen: bool,
    video_driver: &'a str,
    width: usize,
    height: usize,
    fps: f64,
    fps_real: f64,
    fps_recv: f64,
    latency_ms: Option<f64>,
    dropped_frames: i64,
    skipped_frames: u64,
    audio_level_l: f32,
    audio_level_r: f32,
    audio_peak_l: f32,
    audio_peak_r: f32,
}

/// Ask on the terminal which source to receive.
fn choose_source(extra_ips: Option<&str>) -> Result<String> {
    let finder = ndi::Finder::new(true, extra_ips)?;
    loop {
        println!("Searching for NDI sources...");
        let sources = crate::discover::sorted_items(&crate::discover::list_once(&finder, Duration::from_secs(3)));
        if sources.is_empty() {
            print!("No sources found. Press Enter to search again (q to quit): ");
        } else {
            for (i, s) in sources.iter().enumerate() {
                println!("  [{}] {}", i + 1, s.name);
            }
            print!("Select a source [1-{}] (Enter to rescan, q to quit): ", sources.len());
        }
        std::io::stdout().flush()?;
        let mut line = String::new();
        if std::io::stdin().lock().read_line(&mut line)? == 0 {
            anyhow::bail!("no source selected");
        }
        let line = line.trim();
        if line.eq_ignore_ascii_case("q") {
            anyhow::bail!("no source selected");
        }
        if let Ok(n) = line.parse::<usize>() {
            if (1..=sources.len()).contains(&n) {
                return Ok(sources[n - 1].name.clone());
            }
        }
    }
}

pub fn run(args: Args, control: bool) -> Result<()> {
    ndi::init()?;
    let mut target = match args.source.clone() {
        Some(s) => s,
        None if !control => choose_source(args.extra_ips.as_deref())?,
        None => anyhow::bail!("--source is required with --control"),
    };
    let ctl = Control::new(control);
    let mut win = Window::open("NDI Viewer", 1280, 720, args.fullscreen, Some(&args.video_driver))?;
    let fullscreen = args.fullscreen || win.driver == "kmsdrm";
    let mut painter = Painter::new(&win.gl)?;
    let mut video = VideoTexture::new(&win.gl)?;

    let mut recv_fmt = args.recv_fmt;
    if recv_fmt == RecvFmt::Uyvy && !painter.supports_uyvy() {
        recv_fmt = RecvFmt::Rgb;
    }
    let dir = Directory::start(args.extra_ips.as_deref())?;
    let opts = ndi::RecvOptions {
        color_format: recv_fmt.color_format(),
        bandwidth: args.recv_bandwidth.ndi(),
        allow_video_fields: false,
        name: Some("moip-toolkit RX".into()),
    };
    let worker = RecvWorker::spawn(RecvConfig::new(target.clone(), opts), dir);

    let ip = gfx::local_ip();
    let started = Instant::now();
    let banner = Duration::from_secs_f64(args.banner_seconds.max(0.0));
    let mut was_connected = false;
    let mut last_waiting_draw = Instant::now() - Duration::from_secs(1);
    let mut redraw = false;
    let mut last_status = Instant::now();
    let mut shown = 0u64;
    let mut shown_at_status = 0u64;
    let mut recv_at_status = 0u64;
    let mut latency_ms: Option<f64> = None;
    let mut memstats = crate::memstats::MemStats::new();

    'main: loop {
        for ev in win.poll() {
            match ev {
                WinEvent::Quit => break 'main,
                WinEvent::Resized => redraw = true,
            }
        }
        while let Some(cmd) = ctl.poll() {
            match cmd {
                Command::Switch { source } => {
                    crate::log!("[RX] switch to \"{source}\"");
                    target = source;
                    worker.switch(&target);
                    video.clear();
                    was_connected = false;
                }
                Command::Quit => break 'main,
                _ => {}
            }
        }

        let stats = worker.stats();
        if stats.connected != was_connected {
            was_connected = stats.connected;
            if stats.connected {
                crate::log!("[RX] connected to \"{}\"", stats.source.as_deref().unwrap_or(&target));
            } else {
                crate::log!("[RX] disconnected from \"{target}\"");
                video.clear();
            }
        }

        let frame = worker.take_frame(Duration::from_millis(8));
        let mut draw = redraw && video.has_frame();
        if let Some(f) = frame {
            if video.upload(&win.gl, &f) {
                draw = true;
                latency_ms = f.timestamp().map(|ts| (ndi::now_timestamp() - ts) as f64 / 10_000.0);
            }
            drop(f); // back to the SDK right after the upload copied it
        }

        let win_px = (win.width as f32, win.height as f32);
        if draw {
            redraw = false;
            win.clear(0.0, 0.0, 0.0);
            painter.draw_video(&win.gl, &video, Rect::FULL, win_px);
            if started.elapsed() < banner {
                let text = format!("IP: {ip}  |  NDI: {target}");
                painter.label(&win.gl, &text, 3, 24.0, 24.0, win_px);
            }
            win.swap();
            shown += 1;
        } else if !video.has_frame() && (redraw || last_waiting_draw.elapsed() > Duration::from_millis(33)) {
            // Keep presenting while waiting: KMS/DRM needs a live swap chain.
            redraw = false;
            last_waiting_draw = Instant::now();
            win.clear(0.05, 0.05, 0.08);
            let text = format!("HOST IP: {ip}:{}  -  WAITING FOR NDI SOURCE: {target}", args.web_port);
            let (tw, th) = Painter::label_size(&text, 3);
            painter.label(&win.gl, &text, 3, (win_px.0 - tw) / 2.0, (win_px.1 - th) / 2.0, win_px);
            win.swap();
        }

        memstats.tick();
        if last_status.elapsed() >= Duration::from_millis(500) {
            let dt = last_status.elapsed().as_secs_f64();
            let fps_real = (shown - shown_at_status) as f64 / dt;
            let fps_recv = (stats.frames.saturating_sub(recv_at_status)) as f64 / dt;
            recv_at_status = stats.frames;
            last_status = Instant::now();
            shown_at_status = shown;
            if ctl.enabled() {
                emit(&Status {
                    r#type: "status",
                    is_connected: stats.connected,
                    current_source: &target,
                    recv_fmt,
                    recv_bandwidth: args.recv_bandwidth,
                    fullscreen,
                    video_driver: &win.driver,
                    width: stats.width,
                    height: stats.height,
                    fps: (stats.fps * 100.0).round() / 100.0,
                    fps_real: (fps_real * 10.0).round() / 10.0,
                    fps_recv: (fps_recv * 10.0).round() / 10.0,
                    latency_ms: if stats.connected { latency_ms.map(|l| (l * 10.0).round() / 10.0) } else { None },
                    dropped_frames: stats.dropped,
                    skipped_frames: stats.skipped,
                    audio_level_l: stats.levels.level[0],
                    audio_level_r: stats.levels.level[1],
                    audio_peak_l: stats.levels.peak[0],
                    audio_peak_r: stats.levels.peak[1],
                });
            }
        }
    }

    crate::log!("[RX] shutting down");
    video.delete(&win.gl);
    // Do not wait for receiver teardown (can block on dead sessions).
    std::mem::forget(worker);
    Ok(())
}
