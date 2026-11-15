//! ALSA audio capture and device enumeration.

use alsa::pcm::{Access, Format, HwParams, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};
use serde::Serialize;

#[derive(Serialize, Clone, Debug)]
pub struct AudioDevice {
    /// ALSA device string to open, e.g. `"plughw:CARD=PCH,DEV=0"`.
    pub id: String,
    pub name: String,
    pub max_input_channels: u32,
    pub default_samplerate: f64,
}

/// Capture-capable PCM devices of all sound cards, plus ALSA's `default`.
pub fn list() -> Vec<AudioDevice> {
    let mut out = vec![AudioDevice {
        id: "default".into(),
        name: "System default".into(),
        max_input_channels: 2,
        default_samplerate: 48000.0,
    }];
    for card in alsa::card::Iter::new().flatten() {
        let idx = card.get_index();
        let Ok(ctl) = alsa::Ctl::new(&format!("hw:{idx}"), false) else { continue };
        let Ok(ci) = ctl.card_info() else { continue };
        let card_id = ci.get_id().unwrap_or("").to_string();
        let card_name = ci.get_name().unwrap_or("").to_string();
        for dev in alsa::ctl::DeviceIter::new(&ctl) {
            let Ok(info) = ctl.pcm_info(dev as u32, 0, Direction::Capture) else { continue };
            let pcm_name = info.get_name().unwrap_or("").to_string();
            let id = format!("plughw:CARD={card_id},DEV={dev}");
            let (ch, rate) = probe(&format!("hw:CARD={card_id},DEV={dev}")).unwrap_or((2, 48000.0));
            out.push(AudioDevice {
                id,
                name: format!("{card_name}: {pcm_name} (hw:{idx},{dev})"),
                max_input_channels: ch,
                default_samplerate: rate,
            });
        }
    }
    out
}

fn probe(dev: &str) -> Option<(u32, f64)> {
    let pcm = PCM::new(dev, Direction::Capture, true).ok()?;
    let hw = HwParams::any(&pcm).ok()?;
    let ch = hw.get_channels_max().ok()?.min(64);
    let rate = if hw.test_rate(48000).is_ok() { 48000.0 } else { hw.get_rate_max().ok()? as f64 };
    Some((ch, rate))
}

pub struct Capture {
    pcm: PCM,
    pub channels: usize,
    pub rate: u32,
    pub period: usize,
    buf: Vec<i16>,
}

impl Capture {
    /// Open `device` for interleaved S16 capture with ~10 ms periods.
    pub fn open(device: &str, rate: u32, channels: u32) -> Result<Self> {
        let pcm = PCM::new(device, Direction::Capture, false).with_context(|| format!("open ALSA device {device}"))?;
        let period;
        let (rate, channels) = {
            let hw = HwParams::any(&pcm)?;
            hw.set_access(Access::RWInterleaved)?;
            hw.set_format(Format::s16())?;
            hw.set_channels_near(channels.max(1))?;
            hw.set_rate_near(rate, ValueOr::Nearest)?;
            let p = hw.set_period_size_near((rate / 100) as alsa::pcm::Frames, ValueOr::Nearest)?;
            hw.set_buffer_size_near(p * 8)?;
            pcm.hw_params(&hw).context("ALSA hw_params")?;
            let cur = pcm.hw_params_current()?;
            period = cur.get_period_size()? as usize;
            (cur.get_rate()?, cur.get_channels()?)
        };
        pcm.prepare()?;
        pcm.start().ok();
        Ok(Self { pcm, channels: channels as usize, rate, period, buf: vec![0; period * channels as usize] })
    }

    /// Read one period and append it de-interleaved as f32 planes to `planar`
    /// (`channels` planes of `samples` floats). Returns the sample count.
    pub fn read_planar(&mut self, planar: &mut Vec<f32>) -> Result<usize> {
        let io = self.pcm.io_i16()?;
        let n = match io.readi(&mut self.buf) {
            Ok(n) => n,
            Err(e) => {
                // Overrun etc.: recover and drop this period.
                self.pcm.try_recover(e, true).context("ALSA read")?;
                return Ok(0);
            }
        };
        let ch = self.channels;
        planar.clear();
        planar.resize(ch * n, 0.0);
        for (i, frame) in self.buf[..n * ch].chunks_exact(ch).enumerate() {
            for (c, &s) in frame.iter().enumerate() {
                planar[c * n + i] = s as f32 / 32768.0;
            }
        }
        Ok(n)
    }
}
