//! `moip-engine preview`: low-rate browser preview of one NDI source.
//!
//! Receives the NDI proxy stream (lowest bandwidth) and produces, on demand,
//! H.264 (for WebRTC) and/or JPEG (for MJPEG fallback). Output on stdout is
//! binary framed so the server can forward it without re-encoding:
//!
//! ```text
//! [kind: u8][flags: u8][len: u32 LE][payload: len bytes]
//! kind 0 = JSON status, 1 = H.264 access unit (Annex B), 2 = JPEG
//! flags bit 0 = keyframe (H.264)
//! ```
//! Commands arrive as JSON lines on stdin (`preview`, `keyframe`, `quit`).

use crate::control::{emit_raw, Command, Control};
use crate::convert;
use crate::recv::{Directory, RecvConfig, RecvWorker};
use anyhow::Result;
use openh264::encoder::{
    BitRate, Complexity, Encoder, EncoderConfig, FrameRate, IntraFramePeriod, RateControlMode, UsageType,
};
use openh264::formats::YUVSlices;
use std::time::{Duration, Instant};

pub const KIND_STATUS: u8 = 0;
pub const KIND_H264: u8 = 1;
pub const KIND_JPEG: u8 = 2;

#[derive(clap::Args, Debug)]
pub struct Args {
    #[arg(short = 's', long = "source")]
    pub source: String,
    /// Start with H.264 output enabled.
    #[arg(long)]
    pub h264: bool,
    #[arg(long, default_value_t = 640)]
    pub h264_width: usize,
    #[arg(long, default_value_t = 24)]
    pub h264_fps: u32,
    #[arg(long, default_value_t = 1_200_000)]
    pub h264_bitrate: u32,
    /// Start with JPEG output enabled.
    #[arg(long)]
    pub jpeg: bool,
    #[arg(long, default_value_t = 360)]
    pub jpeg_width: usize,
    #[arg(long, default_value_t = 5)]
    pub jpeg_fps: u32,
    #[arg(long, default_value_t = 70)]
    pub jpeg_quality: u8,
    /// Receive full-bandwidth video instead of the NDI proxy stream.
    #[arg(long)]
    pub full_bandwidth: bool,
    #[arg(long)]
    pub extra_ips: Option<String>,
}

fn send(kind: u8, flags: u8, payload: &[u8]) -> std::io::Result<()> {
    let len = (payload.len() as u32).to_le_bytes();
    emit_raw(&[&[kind, flags], &len, payload])
}

fn even_dims(sw: usize, sh: usize, max_w: usize) -> (usize, usize) {
    let w = sw.min(max_w).max(16) & !1;
    let h = ((sh * w / sw.max(1)).max(16) + 1) & !1;
    (w, h)
}

struct H264 {
    enc: Encoder,
    dims: (usize, usize),
    yuv: Vec<u8>,
}

impl H264 {
    fn new(w: usize, h: usize, fps: u32, bitrate: u32) -> Result<Self> {
        let cfg = EncoderConfig::new()
            .usage_type(UsageType::CameraVideoRealTime)
            .rate_control_mode(RateControlMode::Bitrate)
            .bitrate(BitRate::from_bps(bitrate))
            .max_frame_rate(FrameRate::from_hz(fps as f32))
            .complexity(Complexity::Low)
            .skip_frames(true)
            .intra_frame_period(IntraFramePeriod::from_num_frames(fps * 2));
        let enc = Encoder::with_api_config(openh264::OpenH264API::from_source(), cfg)?;
        Ok(Self { enc, dims: (w, h), yuv: vec![0; w * h * 3 / 2] })
    }

    /// Encode one frame; returns (Annex B bytes, is_keyframe).
    fn encode(&mut self, f: &ndi::VideoFrame) -> Result<Option<(Vec<u8>, bool)>> {
        let (dw, dh) = self.dims;
        let (sw, sh, stride) = (f.width(), f.height(), f.stride());
        match f.fourcc() {
            ndi::FOURCC_UYVY => convert::uyvy_to_i420_scaled(f.data(), stride, sw, sh, &mut self.yuv, dw, dh),
            ndi::FOURCC_RGBA | ndi::FOURCC_RGBX => convert::rgba_to_i420_scaled(f.data(), stride, sw, sh, false, &mut self.yuv, dw, dh),
            ndi::FOURCC_BGRA | ndi::FOURCC_BGRX => convert::rgba_to_i420_scaled(f.data(), stride, sw, sh, true, &mut self.yuv, dw, dh),
            _ => return Ok(None),
        }
        let (y, uv) = self.yuv.split_at(dw * dh);
        let (u, v) = uv.split_at(dw * dh / 4);
        let src = YUVSlices::new((y, u, v), (dw, dh), (dw, dw / 2, dw / 2));
        let bs = self.enc.encode(&src)?;
        let key = matches!(bs.frame_type(), openh264::encoder::FrameType::IDR | openh264::encoder::FrameType::I);
        let data = bs.to_vec();
        Ok((!data.is_empty()).then_some((data, key)))
    }
}

fn encode_jpeg(f: &ndi::VideoFrame, max_w: usize, quality: u8, rgb: &mut Vec<u8>) -> Option<Vec<u8>> {
    let (sw, sh, stride) = (f.width(), f.height(), f.stride());
    let (dw, dh) = even_dims(sw, sh, max_w);
    rgb.resize(dw * dh * 3, 0);
    match f.fourcc() {
        ndi::FOURCC_UYVY => convert::uyvy_to_rgb_scaled(f.data(), stride, sw, sh, rgb, dw, dh),
        ndi::FOURCC_RGBA | ndi::FOURCC_RGBX => convert::rgba_to_rgb_scaled(f.data(), stride, sw, sh, false, rgb, dw, dh),
        ndi::FOURCC_BGRA | ndi::FOURCC_BGRX => convert::rgba_to_rgb_scaled(f.data(), stride, sw, sh, true, rgb, dw, dh),
        _ => return None,
    }
    let mut out = Vec::with_capacity(dw * dh / 4);
    jpeg_encoder::Encoder::new(&mut out, quality)
        .encode(rgb, dw as u16, dh as u16, jpeg_encoder::ColorType::Rgb)
        .ok()?;
    Some(out)
}

#[derive(serde::Serialize)]
struct Status<'a> {
    r#type: &'static str,
    source: &'a str,
    is_connected: bool,
    width: usize,
    height: usize,
    fps: f64,
}

pub fn run(args: Args, control: bool) -> Result<()> {
    ndi::init()?;
    let _ = control; // preview is always driven over stdin
    let ctl = Control::new(true);
    let dir = Directory::start(args.extra_ips.as_deref())?;
    let opts = ndi::RecvOptions {
        color_format: ndi::ColorFormat::UyvyRgba,
        bandwidth: if args.full_bandwidth { ndi::Bandwidth::Highest } else { ndi::Bandwidth::Lowest },
        allow_video_fields: false,
        name: Some("moip-toolkit Preview".into()),
    };
    let mut cfg = RecvConfig::new(args.source.clone(), opts);
    cfg.audio = false;
    let worker = RecvWorker::spawn(cfg, dir);

    let (mut want_h264, mut want_jpeg) = (args.h264, args.jpeg);
    let (mut jpeg_w, mut jpeg_fps) = (args.jpeg_width, args.jpeg_fps.max(1));
    let mut h264: Option<H264> = None;
    let mut force_key = false;
    let mut rgb = Vec::new();
    let mut next_h264 = Instant::now();
    let mut next_jpeg = Instant::now();
    let mut last_status = Instant::now() - Duration::from_secs(10);

    loop {
        while let Some(cmd) = ctl.poll() {
            match cmd {
                Command::Preview { h264: h, jpeg: j, jpeg_width, jpeg_fps: jf } => {
                    want_h264 = h;
                    want_jpeg = j;
                    if let Some(w) = jpeg_width {
                        jpeg_w = (w as usize).clamp(160, 1920);
                    }
                    if let Some(f) = jf {
                        jpeg_fps = f.clamp(1, 30);
                    }
                    if !want_h264 {
                        h264 = None;
                    }
                }
                Command::Keyframe => force_key = true,
                Command::Quit => {
                    std::mem::forget(worker);
                    return Ok(());
                }
                _ => {}
            }
        }

        let frame = worker.take_frame(Duration::from_millis(50));
        let now = Instant::now();
        if let Some(f) = frame.as_ref() {
            if want_h264 && now >= next_h264 {
                next_h264 = now + Duration::from_secs_f64(1.0 / args.h264_fps.max(1) as f64);
                let dims = even_dims(f.width(), f.height(), args.h264_width);
                if h264.as_ref().map(|e| e.dims) != Some(dims) {
                    h264 = Some(H264::new(dims.0, dims.1, args.h264_fps, args.h264_bitrate)?);
                }
                let enc = h264.as_mut().unwrap();
                if std::mem::take(&mut force_key) {
                    enc.enc.force_intra_frame();
                }
                match enc.encode(f) {
                    Ok(Some((data, key))) => send(KIND_H264, key as u8, &data)?,
                    Ok(None) => {}
                    Err(e) => crate::log!("[preview] H.264 encode failed: {e}"),
                }
            }
            if want_jpeg && now >= next_jpeg {
                next_jpeg = now + Duration::from_secs_f64(1.0 / jpeg_fps as f64);
                if let Some(jpg) = encode_jpeg(f, jpeg_w, args.jpeg_quality, &mut rgb) {
                    send(KIND_JPEG, 0, &jpg)?;
                }
            }
        }
        drop(frame);

        if last_status.elapsed() > Duration::from_secs(1) {
            last_status = Instant::now();
            let st = worker.stats();
            let json = serde_json::to_vec(&Status {
                r#type: "status",
                source: &args.source,
                is_connected: st.connected,
                width: st.width,
                height: st.height,
                fps: st.fps,
            })?;
            send(KIND_STATUS, 0, &json)?;
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn even_dims_keep_aspect() {
        assert_eq!(super::even_dims(1920, 1080, 640), (640, 360));
        assert_eq!(super::even_dims(640, 360, 1280), (640, 360));
        assert_eq!(super::even_dims(1366, 768, 641), (640, 360));
    }
}
