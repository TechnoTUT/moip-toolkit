//! `moip-engine multiview`: several NDI sources in a grid with labels and
//! audio meters.

use crate::control::{emit, Command, Control};
use crate::gfx::{self, Painter, Rect, VideoTexture, WinEvent, Window};
use crate::recv::{Directory, RecvConfig, RecvWorker, SILENCE_DB};
use anyhow::Result;
use serde::Serialize;
use std::sync::Arc;
use std::time::{Duration, Instant};

const MAX_SLOTS: usize = 16;

#[derive(Clone, Copy, PartialEq, Eq, Debug, clap::ValueEnum)]
pub enum MvBandwidth {
    /// Full quality for up to 2 sources, NDI proxy streams for more.
    Auto,
    Highest,
    Lowest,
}

#[derive(clap::Args, Debug)]
pub struct Args {
    /// Sources to show (full or stream names). Omit to show every source found.
    #[arg(short = 's', long = "source")]
    pub sources: Vec<String>,
    #[arg(long, value_enum, default_value_t = MvBandwidth::Auto)]
    pub recv_bandwidth: MvBandwidth,
    #[arg(short, long)]
    pub fullscreen: bool,
    #[arg(long, default_value = gfx::DEFAULT_VIDEO_DRIVER, value_parser = clap::builder::PossibleValuesParser::new(gfx::VIDEO_DRIVERS))]
    pub video_driver: String,
    #[arg(long)]
    pub extra_ips: Option<String>,
}

/// (cols, rows) for `n` tiles.
pub fn grid(n: usize) -> (usize, usize) {
    match n {
        0 | 1 => (1, 1),
        2 => (2, 1),
        3 | 4 => (2, 2),
        5 | 6 => (3, 2),
        7..=9 => (3, 3),
        _ => {
            let cols = (n as f64).sqrt().ceil() as usize;
            (cols, n.div_ceil(cols))
        }
    }
}

struct Slot {
    name: String,
    worker: RecvWorker,
    video: VideoTexture,
}

#[derive(Serialize)]
struct SlotStatus<'a> {
    source_name: &'a str,
    is_connected: bool,
    width: usize,
    height: usize,
    audio_level_l: f32,
    audio_level_r: f32,
}

#[derive(Serialize)]
struct Status<'a> {
    r#type: &'static str,
    fullscreen: bool,
    auto_discovery: bool,
    slots: Vec<SlotStatus<'a>>,
}

struct Wall {
    dir: Arc<Directory>,
    bandwidth: MvBandwidth,
    slots: Vec<Slot>,
}

impl Wall {
    fn sync(&mut self, gl: &glow::Context, wanted: &[String]) -> Result<()> {
        let wanted: Vec<String> = wanted.iter().take(MAX_SLOTS).cloned().collect();
        let (keep, gone): (Vec<Slot>, Vec<Slot>) =
            std::mem::take(&mut self.slots).into_iter().partition(|s| wanted.contains(&s.name));
        for s in gone {
            crate::log!("[MV] removing \"{}\"", s.name);
            s.video.delete(gl);
            drop(s.worker);
        }
        let mut keep = keep;
        let bw = match self.bandwidth {
            MvBandwidth::Highest => ndi::Bandwidth::Highest,
            MvBandwidth::Lowest => ndi::Bandwidth::Lowest,
            MvBandwidth::Auto if wanted.len() <= 2 => ndi::Bandwidth::Highest,
            MvBandwidth::Auto => ndi::Bandwidth::Lowest,
        };
        for name in &wanted {
            if keep.iter().any(|s| &s.name == name) {
                continue;
            }
            crate::log!("[MV] adding \"{name}\"");
            let opts = ndi::RecvOptions {
                color_format: ndi::ColorFormat::UyvyRgba,
                bandwidth: bw,
                allow_video_fields: false,
                name: Some("moip-toolkit Multiview".into()),
            };
            let worker = RecvWorker::spawn(RecvConfig::new(name.clone(), opts), self.dir.clone());
            keep.push(Slot { name: name.clone(), worker, video: VideoTexture::new(gl)? });
        }
        keep.sort_by_key(|s| wanted.iter().position(|w| w == &s.name));
        self.slots = keep;
        Ok(())
    }
}

fn meter_color(db: f32) -> [f32; 4] {
    if db > -6.0 {
        [0.95, 0.25, 0.2, 1.0]
    } else if db > -18.0 {
        [0.95, 0.8, 0.2, 1.0]
    } else {
        [0.25, 0.85, 0.4, 1.0]
    }
}

pub fn run(args: Args, control: bool) -> Result<()> {
    ndi::init()?;
    let ctl = Control::new(control);
    let mut win = Window::open("NDI Multiview", 1280, 720, args.fullscreen, Some(&args.video_driver))?;
    let fullscreen = args.fullscreen || win.driver == "kmsdrm";
    let mut painter = Painter::new(&win.gl)?;
    let dir = Directory::start(args.extra_ips.as_deref())?;
    let mut wall = Wall { dir: dir.clone(), bandwidth: args.recv_bandwidth, slots: Vec::new() };
    let mut fixed: Vec<String> = args.sources.clone();
    wall.sync(&win.gl, &fixed)?;

    let ip = gfx::local_ip();
    let mut last_discovery = Instant::now() - Duration::from_secs(10);
    let mut last_draw = Instant::now() - Duration::from_secs(1);
    let mut last_status = Instant::now();
    let mut redraw = true;

    'main: loop {
        for ev in win.poll() {
            match ev {
                WinEvent::Quit => break 'main,
                WinEvent::Resized => redraw = true,
            }
        }
        while let Some(cmd) = ctl.poll() {
            match cmd {
                Command::SetSources { sources } => {
                    fixed = sources;
                    wall.sync(&win.gl, &fixed)?;
                    last_discovery = Instant::now() - Duration::from_secs(10);
                    redraw = true;
                }
                Command::Quit => break 'main,
                _ => {}
            }
        }
        if fixed.is_empty() && last_discovery.elapsed() > Duration::from_secs(1) {
            last_discovery = Instant::now();
            let mut names: Vec<String> = dir.sources().into_iter().map(|s| s.name).collect();
            names.sort();
            names.dedup();
            let current: Vec<&String> = wall.slots.iter().map(|s| &s.name).collect();
            if names.iter().collect::<Vec<_>>() != current {
                wall.sync(&win.gl, &names)?;
                redraw = true;
            }
        }

        // Upload every new frame; redraw when anything changed (meters ~30 Hz).
        for s in wall.slots.iter_mut() {
            if let Some(f) = s.worker.take_frame(Duration::ZERO) {
                if s.video.upload(&win.gl, &f) {
                    redraw = true;
                }
            }
        }
        if redraw || last_draw.elapsed() > Duration::from_millis(33) {
            redraw = false;
            last_draw = Instant::now();
            draw(&win, &mut painter, &mut wall, &ip);
            win.swap();
        } else {
            std::thread::sleep(Duration::from_millis(2));
        }

        if ctl.enabled() && last_status.elapsed() >= Duration::from_millis(500) {
            last_status = Instant::now();
            let stats: Vec<_> = wall.slots.iter().map(|s| s.worker.stats()).collect();
            emit(&Status {
                r#type: "status",
                fullscreen,
                auto_discovery: fixed.is_empty(),
                slots: wall
                    .slots
                    .iter()
                    .zip(stats.iter())
                    .map(|(s, st)| SlotStatus {
                        source_name: &s.name,
                        is_connected: st.connected,
                        width: st.width,
                        height: st.height,
                        audio_level_l: st.levels.level[0],
                        audio_level_r: st.levels.level[1],
                    })
                    .collect(),
            });
        }
    }
    for s in &wall.slots {
        s.video.delete(&win.gl);
    }
    std::mem::forget(wall);
    Ok(())
}

fn draw(win: &Window, painter: &mut Painter, wall: &mut Wall, ip: &str) {
    let gl = &win.gl;
    let (ww, wh) = (win.width as f32, win.height as f32);
    win.clear(0.03, 0.035, 0.05);
    if wall.slots.is_empty() {
        let text = format!("HOST IP: {ip}  -  WAITING FOR NDI SOURCES");
        let (tw, th) = Painter::label_size(&text, 3);
        painter.label(gl, &text, 3, (ww - tw) / 2.0, (wh - th) / 2.0, (ww, wh));
        return;
    }
    let (cols, rows) = grid(wall.slots.len());
    let (cw, ch) = (ww / cols as f32, wh / rows as f32);
    let label_scale = if ch < 300.0 { 1 } else { 2 };
    for (i, s) in wall.slots.iter().enumerate() {
        let (c, r) = ((i % cols) as f32, (i / cols) as f32);
        let (px, py) = (c * cw, r * ch);
        let cell = Rect::from_pixels(px + 2.0, py + 2.0, cw - 4.0, ch - 4.0, ww, wh);
        painter.fill(gl, cell, [0.0, 0.0, 0.0, 1.0]);
        let stats = s.worker.stats();
        if stats.connected && s.video.has_frame() {
            painter.draw_video(gl, &s.video, cell, (cw - 4.0, ch - 4.0));
        } else {
            let msg = if stats.source.is_some() { "CONNECTING..." } else { "NO SIGNAL" };
            let (tw, th) = Painter::label_size(msg, label_scale);
            painter.label(gl, msg, label_scale, px + (cw - tw) / 2.0, py + (ch - th) / 2.0, (ww, wh));
        }
        // Border
        let border = [0.2, 0.25, 0.35, 0.9];
        painter.fill(gl, Rect::from_pixels(px, py, cw, 2.0, ww, wh), border);
        painter.fill(gl, Rect::from_pixels(px, py + ch - 2.0, cw, 2.0, ww, wh), border);
        painter.fill(gl, Rect::from_pixels(px, py, 2.0, ch, ww, wh), border);
        painter.fill(gl, Rect::from_pixels(px + cw - 2.0, py, 2.0, ch, ww, wh), border);
        // Label
        let name: String = s.name.chars().take(40).collect();
        painter.label(gl, &name, label_scale, px + 10.0, py + 10.0, (ww, wh));
        // Audio meters (L above R) along the bottom.
        let mh = (ch * 0.025).clamp(4.0, 12.0);
        let (mx, mw) = (px + 10.0, cw - 20.0);
        let my = py + ch - 10.0 - mh * 2.0 - 2.0;
        painter.fill(gl, Rect::from_pixels(mx - 2.0, my - 2.0, mw + 4.0, mh * 2.0 + 6.0, ww, wh), [0.08, 0.1, 0.14, 0.85]);
        for (k, db) in stats.levels.level.iter().enumerate() {
            let frac = ((db.clamp(SILENCE_DB, 0.0) - SILENCE_DB) / -SILENCE_DB).clamp(0.0, 1.0);
            if frac > 0.0 {
                let y = my + k as f32 * (mh + 2.0);
                painter.fill(gl, Rect::from_pixels(mx, y, mw * frac, mh, ww, wh), meter_color(*db));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn grid_sizes() {
        assert_eq!(super::grid(1), (1, 1));
        assert_eq!(super::grid(2), (2, 1));
        assert_eq!(super::grid(4), (2, 2));
        assert_eq!(super::grid(5), (3, 2));
        assert_eq!(super::grid(9), (3, 3));
        assert_eq!(super::grid(10), (4, 3));
        assert_eq!(super::grid(16), (4, 4));
    }
}
