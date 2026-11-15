//! `moip-engine devices`: list capture devices as JSON.

use crate::{audio, v4l2};
use serde::Serialize;

#[derive(Serialize)]
pub struct VideoDevice {
    /// /dev/videoN index; -1 is the built-in colour bar generator.
    pub index: i32,
    pub path: String,
    pub name: String,
    pub formats: Vec<String>,
    pub is_available: bool,
}

#[derive(Serialize)]
struct Devices {
    r#type: &'static str,
    video: Vec<VideoDevice>,
    audio: Vec<audio::AudioDevice>,
}

pub fn video() -> Vec<VideoDevice> {
    let mut out = vec![VideoDevice {
        index: -1,
        path: String::new(),
        name: "Color Bars (Test Pattern)".into(),
        formats: vec!["UYVY".into()],
        is_available: true,
    }];
    for (n, info) in v4l2::list() {
        let formats: Vec<String> = info.formats.iter().map(|f| v4l2::fourcc_str(*f)).collect();
        let usable = info.formats.iter().any(|f| [v4l2::PIX_YUYV, v4l2::PIX_UYVY, v4l2::PIX_MJPG].contains(f));
        out.push(VideoDevice {
            index: n as i32,
            path: info.path,
            name: format!("{} (video{n})", info.card),
            formats,
            is_available: usable,
        });
    }
    out
}

pub fn run() -> anyhow::Result<()> {
    let d = Devices { r#type: "devices", video: video(), audio: audio::list() };
    println!("{}", serde_json::to_string_pretty(&d)?);
    Ok(())
}
