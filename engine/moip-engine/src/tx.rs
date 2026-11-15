//! `moip-engine tx`: send a V4L2 capture device (or a test pattern) and an
//! ALSA input as an NDI source.
//!
//! Threads: capture (V4L2 -> UYVY buffer) -> send (NDI, 1-slot hand-off so a
//! slow send drops frames instead of building latency) and audio (ALSA ->
//! NDI). Video is always sent as UYVY, NDI's native format.

use crate::control::{emit, Command, Control};
use crate::recv::{Levels, Meter};
use crate::{audio, convert, font, v4l2};
use anyhow::{bail, Context, Result};
use serde::Serialize;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, PartialEq, Eq, Debug, clap::ValueEnum)]
pub enum InputFormat {
    /// Uncompressed when the device delivers the requested size, else MJPEG.
    Auto,
    Yuyv,
    Uyvy,
    Mjpg,
}

#[derive(clap::Args, Debug)]
pub struct Args {
    /// NDI source name to publish.
    #[arg(short = 'n', long = "name", default_value = "TX")]
    pub name: String,
    /// Video device: index (/dev/videoN), path, or -1 for colour bars.
    #[arg(short = 'd', long = "video-device", default_value = "0", allow_hyphen_values = true)]
    pub video_device: String,
    #[arg(long, default_value_t = 1920)]
    pub width: u32,
    #[arg(long, default_value_t = 1080)]
    pub height: u32,
    /// Frame rate: 30, 29.97, 59.94, 60 or N/D.
    #[arg(long, default_value = "30")]
    pub fps: String,
    #[arg(long, value_enum, default_value_t = InputFormat::Auto)]
    pub input_format: InputFormat,
    /// ALSA capture device (see `moip-engine devices`).
    #[arg(short = 'a', long = "audio-device", default_value = "default")]
    pub audio_device: String,
    #[arg(long)]
    pub no_audio: bool,
    #[arg(long, default_value_t = 48000)]
    pub sample_rate: u32,
    #[arg(long, default_value_t = 2)]
    pub audio_channels: u32,
}

/// Parse "30", "29.97" or "30000/1001" into an NDI frame rate N/D.
pub fn parse_fps(s: &str) -> Result<(i32, i32)> {
    let s = s.trim();
    if let Some((n, d)) = s.split_once('/') {
        let (n, d) = (n.trim().parse::<i32>()?, d.trim().parse::<i32>()?);
        if n <= 0 || d <= 0 {
            bail!("invalid frame rate {s}");
        }
        return Ok((n, d));
    }
    let f: f64 = s.parse().with_context(|| format!("invalid frame rate {s:?}"))?;
    if !(1.0..=240.0).contains(&f) {
        bail!("invalid frame rate {s}");
    }
    for base in [24.0, 30.0, 48.0, 60.0, 120.0] {
        if (f - base * 1000.0 / 1001.0).abs() < 0.01 {
            return Ok(((base * 1000.0) as i32, 1001));
        }
    }
    if f.fract().abs() < 1e-6 {
        Ok((f as i32, 1))
    } else {
        Ok(((f * 1000.0).round() as i32, 1000))
    }
}

struct Shared {
    sent: AtomicU64,
    dropped: AtomicU64,
    stop: AtomicBool,
    levels: Mutex<Levels>,
    error: Mutex<Option<String>>,
}

struct VideoBuf {
    data: Vec<u8>,
    width: usize,
    height: usize,
}

#[derive(Serialize)]
struct Status<'a> {
    r#type: &'static str,
    sender_name: &'a str,
    video_device: &'a str,
    audio_device: Option<&'a str>,
    no_audio: bool,
    input_format: &'a str,
    actual_width: usize,
    actual_height: usize,
    actual_fps: f64,
    fps_real: f64,
    dropped_frames: u64,
    connections: i32,
    sample_rate: u32,
    audio_channels: usize,
    audio_level_l: f32,
    audio_level_r: f32,
    audio_peak_l: f32,
    audio_peak_r: f32,
    error: Option<String>,
}

enum Source {
    Device(v4l2::Capture),
    Bars,
}

pub fn run(args: Args, control: bool) -> Result<()> {
    ndi::init()?;
    let ctl = Control::new(control);
    let rate = parse_fps(&args.fps)?;
    let req_fps = rate.0 as f64 / rate.1 as f64;

    let (source, width, height, frame_rate, input_name) = if args.video_device.trim() == "-1" {
        (Source::Bars, args.width as usize & !1, args.height as usize, rate, "TEST".to_string())
    } else {
        let path = match args.video_device.parse::<u32>() {
            Ok(n) => format!("/dev/video{n}"),
            Err(_) => args.video_device.clone(),
        };
        let prefer: &[u32] = match args.input_format {
            InputFormat::Auto => &[v4l2::PIX_UYVY, v4l2::PIX_YUYV, v4l2::PIX_MJPG],
            InputFormat::Yuyv => &[v4l2::PIX_YUYV],
            InputFormat::Uyvy => &[v4l2::PIX_UYVY],
            InputFormat::Mjpg => &[v4l2::PIX_MJPG],
        };
        let cap = v4l2::Capture::open(&path, args.width, args.height, req_fps, prefer)?;
        let fr = (cap.fps.0 as i32, cap.fps.1.max(1) as i32);
        let (w, h, name) = (cap.width & !1, cap.height, v4l2::fourcc_str(cap.pixfmt));
        crate::log!("[TX] {path}: {w}x{h} {name} @ {:.2} fps", fr.0 as f64 / fr.1 as f64);
        (Source::Device(cap), w, h, fr, name)
    };
    let actual_fps = frame_rate.0 as f64 / frame_rate.1 as f64;

    // The capture device paces frames; the test pattern is paced by its own loop.
    let sender = Arc::new(ndi::Sender::new(&args.name, false, false)?);
    crate::log!("[TX] publishing NDI source \"{}\"", args.name);
    let shared = Arc::new(Shared {
        sent: AtomicU64::new(0),
        dropped: AtomicU64::new(0),
        stop: AtomicBool::new(false),
        levels: Mutex::new(Levels::default()),
        error: Mutex::new(None),
    });

    // Buffer pool: 3 buffers circulate between capture and send.
    let (to_send, send_rx) = sync_channel::<VideoBuf>(1);
    let (to_pool, pool_rx) = sync_channel::<VideoBuf>(3);
    for _ in 0..3 {
        to_pool.send(VideoBuf { data: vec![0; width * 2 * height], width, height }).ok();
    }
    let to_pool_capture = to_pool.clone();

    let send_thread = {
        let (sender, shared) = (sender.clone(), shared.clone());
        std::thread::Builder::new().name("ndi-send".into()).spawn(move || {
            send_loop(send_rx, to_pool, &sender, frame_rate, &shared)
        })?
    };

    let capture_thread = {
        let shared = shared.clone();
        let to_pool = to_pool_capture;
        std::thread::Builder::new().name("capture".into()).spawn(move || {
            let r = capture_loop(source, width, height, actual_fps, (pool_rx, to_pool), to_send, &shared);
            if let Err(e) = r {
                crate::log!("[TX] capture stopped: {e:#}");
                *shared.error.lock().unwrap() = Some(format!("{e:#}"));
                shared.stop.store(true, Ordering::Relaxed);
            }
        })?
    };

    let mut audio_info = (0u32, 0usize);
    let audio_thread = if args.no_audio {
        None
    } else {
        match audio::Capture::open(&args.audio_device, args.sample_rate, args.audio_channels) {
            Ok(cap) => {
                crate::log!("[TX] audio {}: {} ch @ {} Hz", args.audio_device, cap.channels, cap.rate);
                audio_info = (cap.rate, cap.channels);
                let (sender, shared) = (sender.clone(), shared.clone());
                Some(std::thread::Builder::new().name("audio".into()).spawn(move || audio_loop(cap, &sender, &shared))?)
            }
            Err(e) => {
                let msg = format!("audio disabled: {e:#}");
                crate::log!("[TX] {msg}");
                *shared.error.lock().unwrap() = Some(msg);
                None
            }
        }
    };

    let mut last_status = Instant::now();
    let mut sent_at_status = 0u64;
    while !shared.stop.load(Ordering::Relaxed) {
        if let Some(Command::Quit) = ctl.wait(Duration::from_millis(100)) {
            break;
        }
        if last_status.elapsed() >= Duration::from_millis(500) {
            let dt = last_status.elapsed().as_secs_f64();
            last_status = Instant::now();
            let sent = shared.sent.load(Ordering::Relaxed);
            let fps_real = (sent - sent_at_status) as f64 / dt;
            sent_at_status = sent;
            let levels = *shared.levels.lock().unwrap();
            let status = Status {
                r#type: "status",
                sender_name: &args.name,
                video_device: &args.video_device,
                audio_device: (!args.no_audio).then_some(args.audio_device.as_str()),
                no_audio: args.no_audio,
                input_format: &input_name,
                actual_width: width,
                actual_height: height,
                actual_fps: (actual_fps * 100.0).round() / 100.0,
                fps_real: (fps_real * 10.0).round() / 10.0,
                dropped_frames: shared.dropped.load(Ordering::Relaxed),
                connections: sender.connections(0),
                sample_rate: audio_info.0,
                audio_channels: audio_info.1,
                audio_level_l: levels.level[0],
                audio_level_r: levels.level[1],
                audio_peak_l: levels.peak[0],
                audio_peak_r: levels.peak[1],
                error: shared.error.lock().unwrap().clone(),
            };
            if ctl.enabled() {
                emit(&status);
            }
        }
    }
    shared.stop.store(true, Ordering::Relaxed);
    let _ = capture_thread.join();
    let _ = send_thread.join();
    if let Some(t) = audio_thread {
        let _ = t.join();
    }
    let err = shared.error.lock().unwrap().clone();
    match err {
        Some(e) if !e.starts_with("audio disabled") => bail!(e),
        _ => Ok(()),
    }
}

fn send_loop(rx: Receiver<VideoBuf>, pool: SyncSender<VideoBuf>, sender: &ndi::Sender, rate: (i32, i32), shared: &Shared) {
    while let Ok(buf) = rx.recv() {
        sender.send_video(&ndi::VideoSend {
            width: buf.width,
            height: buf.height,
            fourcc: ndi::FOURCC_UYVY,
            stride: buf.width * 2,
            frame_rate: rate,
            data: &buf.data,
        });
        shared.sent.fetch_add(1, Ordering::Relaxed);
        let _ = pool.try_send(buf);
    }
}

fn capture_loop(
    mut source: Source,
    width: usize,
    height: usize,
    fps: f64,
    (pool, pool_tx): (Receiver<VideoBuf>, SyncSender<VideoBuf>),
    out: SyncSender<VideoBuf>,
    shared: &Shared,
) -> Result<()> {
    let bars = matches!(source, Source::Bars).then(|| color_bars(width, height));
    let mut ycbcr = Vec::new();
    let mut label_sec = u64::MAX;
    let mut label: Option<(font::Image, usize, usize)> = None;
    let period = Duration::from_secs_f64(1.0 / fps.max(1.0));
    let mut next = Instant::now();
    let mut last_frame = Instant::now();

    while !shared.stop.load(Ordering::Relaxed) {
        let Ok(mut buf) = pool.recv_timeout(Duration::from_millis(200)) else { continue };
        let filled = match &mut source {
            Source::Bars => {
                let now = Instant::now();
                if next > now {
                    std::thread::sleep(next - now);
                }
                next += period;
                if next + period < Instant::now() {
                    next = Instant::now() + period; // fell behind: resync
                }
                buf.data.copy_from_slice(bars.as_ref().unwrap());
                let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs();
                if secs != label_sec {
                    label_sec = secs;
                    let (h, m, s) = local_hms(secs as i64);
                    let img = font::render(&format!("TEST PATTERN  {h:02}:{m:02}:{s:02}"), (height / 270).max(2), [255; 4], [0, 0, 0, 255]);
                    let x = (width.saturating_sub(img.width) / 2) & !1;
                    let y = height * 3 / 4 + (height / 4).saturating_sub(img.height) / 2;
                    label = Some((img, x, y));
                }
                if let Some((img, x, y)) = &label {
                    overlay_label(&mut buf.data, width, height, img, *x, *y);
                }
                true
            }
            Source::Device(cap) => {
                let (pixfmt, stride) = (cap.pixfmt, cap.stride);
                match cap.next(Duration::from_millis(500))? {
                None => {
                    if last_frame.elapsed() > Duration::from_secs(5) {
                        bail!("no frames from capture device for 5 s");
                    }
                    false
                }
                Some(frame) => {
                    last_frame = Instant::now();
                    let (w, h) = (width, height);
                    match pixfmt {
                        v4l2::PIX_YUYV if frame.data.len() >= stride * (h - 1) + w * 2 => {
                            convert::yuyv_to_uyvy(frame.data, stride, &mut buf.data, w * 2, w, h);
                            true
                        }
                        v4l2::PIX_UYVY if frame.data.len() >= stride * (h - 1) + w * 2 => {
                            convert::copy_rows(frame.data, stride, &mut buf.data, w * 2, w * 2, h);
                            true
                        }
                        v4l2::PIX_MJPG => decode_mjpeg(frame.data, &mut ycbcr, &mut buf.data, w, h),
                        _ => false,
                    }
                }
            }},
        };
        let result = if filled { out.try_send(buf) } else { Err(TrySendError::Full(buf)) };
        if let Err(TrySendError::Full(b) | TrySendError::Disconnected(b)) = result {
            if filled {
                shared.dropped.fetch_add(1, Ordering::Relaxed);
            }
            let _ = pool_tx.try_send(b);
        }
    }
    Ok(())
}

fn decode_mjpeg(jpeg: &[u8], ycbcr: &mut Vec<u8>, out: &mut [u8], w: usize, h: usize) -> bool {
    use zune_jpeg::zune_core::colorspace::ColorSpace;
    use zune_jpeg::zune_core::options::DecoderOptions;
    let opts = DecoderOptions::default().jpeg_set_out_colorspace(ColorSpace::YCbCr);
    let mut dec = zune_jpeg::JpegDecoder::new_with_options(jpeg, opts);
    if dec.decode_headers().is_err() {
        return false;
    }
    match dec.dimensions() {
        Some((dw, dh)) if dw as usize >= w && dh as usize == h => {
            let need = dw as usize * dh as usize * 3;
            ycbcr.resize(need, 0);
            if dec.decode_into(ycbcr).is_err() {
                return false;
            }
            if dw as usize == w {
                convert::ycbcr444_to_uyvy(ycbcr, out, w, h);
            } else {
                // Odd-width JPEG: drop the last column.
                for y in 0..h {
                    let row = &ycbcr[y * dw as usize * 3..y * dw as usize * 3 + w * 3];
                    convert::ycbcr444_to_uyvy(row, &mut out[y * w * 2..(y + 1) * w * 2], w, 1);
                }
            }
            true
        }
        _ => false,
    }
}

fn audio_loop(mut cap: audio::Capture, sender: &ndi::Sender, shared: &Shared) {
    let mut planar = Vec::new();
    let mut meter = Meter::default();
    let mut last_meter = Instant::now();
    while !shared.stop.load(Ordering::Relaxed) {
        match cap.read_planar(&mut planar) {
            Ok(0) => continue,
            Ok(n) => {
                sender.send_audio(cap.rate as i32, cap.channels, n, &planar);
                let chans: Vec<&[f32]> = (0..cap.channels.min(2)).map(|c| &planar[c * n..(c + 1) * n]).collect();
                meter.feed(&chans);
                if last_meter.elapsed() > Duration::from_millis(100) {
                    last_meter = Instant::now();
                    *shared.levels.lock().unwrap() = meter.levels();
                }
            }
            Err(e) => {
                crate::log!("[TX] audio stopped: {e:#}");
                *shared.error.lock().unwrap() = Some(format!("audio disabled: {e:#}"));
                *shared.levels.lock().unwrap() = Levels::default();
                return;
            }
        }
    }
}

/// 75% SMPTE-style colour bars over a black lower quarter, as UYVY.
fn color_bars(width: usize, height: usize) -> Vec<u8> {
    const BARS: [(u8, u8, u8); 7] = [
        (191, 191, 191),
        (191, 191, 0),
        (0, 191, 191),
        (0, 191, 0),
        (191, 0, 191),
        (191, 0, 0),
        (0, 0, 191),
    ];
    let mut out = vec![0u8; width * height * 2];
    let split = height * 3 / 4;
    for y in 0..height {
        for x in (0..width).step_by(2) {
            let (yy, cb, cr) = if y < split {
                let (r, g, b) = BARS[(x * 7 / width).min(6)];
                convert::rgb_to_ycbcr709(r, g, b)
            } else {
                (16, 128, 128)
            };
            let o = (y * width + x) * 2;
            out[o..o + 4].copy_from_slice(&[cb, yy, cr, yy]);
        }
    }
    out
}

fn local_hms(secs: i64) -> (i32, i32, i32) {
    let t: libc::time_t = secs as libc::time_t;
    // SAFETY: localtime_r writes into the provided struct only.
    unsafe {
        let mut tm: libc::tm = std::mem::zeroed();
        libc::localtime_r(&t, &mut tm);
        (tm.tm_hour, tm.tm_min, tm.tm_sec)
    }
}

fn overlay_label(dst: &mut [u8], width: usize, height: usize, img: &font::Image, x0: usize, y0: usize) {
    for y in 0..img.height.min(height.saturating_sub(y0)) {
        for x in 0..img.width.min(width.saturating_sub(x0)) {
            let lit = img.rgba[(y * img.width + x) * 4] > 127;
            let o = ((y0 + y) * width + x0 + x) * 2;
            dst[o + 1] = if lit { 235 } else { 16 };
            if (x0 + x) % 2 == 0 {
                dst[o] = 128;
                dst[o + 2] = 128;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::parse_fps;
    #[test]
    fn fps_parsing() {
        assert_eq!(parse_fps("30").unwrap(), (30, 1));
        assert_eq!(parse_fps("29.97").unwrap(), (30000, 1001));
        assert_eq!(parse_fps("59.94").unwrap(), (60000, 1001));
        assert_eq!(parse_fps("30000/1001").unwrap(), (30000, 1001));
        assert_eq!(parse_fps("12.5").unwrap(), (12500, 1000));
        assert!(parse_fps("0").is_err());
        assert!(parse_fps("abc").is_err());
    }
}
