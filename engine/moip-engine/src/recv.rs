//! Receiving building blocks shared by rx, multiview and preview.
//!
//! * [`Directory`]: one NDI finder per process, polled on its own thread.
//! * [`RecvWorker`]: a capture thread per source that keeps only the newest
//!   video frame (older ones are returned to the SDK immediately), measures
//!   audio levels, and replaces the receiver when the stream stalls.
//!
//! The SDK frame buffer is handed to the consumer as-is (no copy); the
//! consumer drops it as soon as it has been uploaded/encoded.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Sender, TryRecvError};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

// ---------------------------------------------------------------------------
// Directory
// ---------------------------------------------------------------------------

pub struct Directory {
    sources: Mutex<Vec<ndi::Source>>,
    stop: AtomicBool,
}

impl Directory {
    pub fn start(extra_ips: Option<&str>) -> anyhow::Result<Arc<Self>> {
        let finder = ndi::Finder::new(true, extra_ips)?;
        let dir = Arc::new(Self { sources: Mutex::new(Vec::new()), stop: AtomicBool::new(false) });
        let d = dir.clone();
        std::thread::Builder::new().name("ndi-find".into()).spawn(move || {
            while !d.stop.load(Ordering::Relaxed) {
                finder.wait(500);
                *d.sources.lock().unwrap() = finder.sources();
            }
        })?;
        Ok(dir)
    }

    pub fn sources(&self) -> Vec<ndi::Source> {
        self.sources.lock().unwrap().clone()
    }

    /// Find a source by full name (`"HOST (STREAM)"`) or stream name.
    pub fn find(&self, query: &str) -> Option<ndi::Source> {
        let list = self.sources.lock().unwrap();
        list.iter().find(|s| s.name == query).or_else(|| list.iter().find(|s| s.matches(query))).cloned()
    }
}

impl Drop for Directory {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

// ---------------------------------------------------------------------------
// Audio meter
// ---------------------------------------------------------------------------

pub const SILENCE_DB: f32 = -60.0;

#[derive(Clone, Copy, Debug)]
pub struct Levels {
    pub level: [f32; 2],
    pub peak: [f32; 2],
}

impl Default for Levels {
    fn default() -> Self {
        Self { level: [SILENCE_DB; 2], peak: [SILENCE_DB; 2] }
    }
}

fn to_db(v: f32) -> f32 {
    if v <= 1e-6 {
        SILENCE_DB
    } else {
        (20.0 * v.log10()).max(SILENCE_DB)
    }
}

/// RMS level and decaying peak hold per channel (L/R; mono is duplicated).
#[derive(Default)]
pub struct Meter {
    levels: Levels,
    last: Option<Instant>,
}

impl Meter {
    /// Peak hold falls by this many dB per second.
    const PEAK_DECAY_DB_S: f32 = 20.0;

    pub fn feed(&mut self, channels: &[&[f32]]) {
        let now = Instant::now();
        let dt = self.last.map_or(0.0, |t| now.duration_since(t).as_secs_f32());
        self.last = Some(now);
        for i in 0..2 {
            let Some(ch) = channels.get(i).or_else(|| channels.first()) else { continue };
            if ch.is_empty() {
                continue;
            }
            let (mut sum, mut peak) = (0.0f32, 0.0f32);
            for &s in ch.iter() {
                sum += s * s;
                peak = peak.max(s.abs());
            }
            self.levels.level[i] = to_db((sum / ch.len() as f32).sqrt());
            let held = (self.levels.peak[i] - Self::PEAK_DECAY_DB_S * dt).max(SILENCE_DB);
            self.levels.peak[i] = held.max(to_db(peak));
        }
    }

    pub fn levels(&self) -> Levels {
        match self.last {
            // Fall back to silence when audio stopped arriving.
            Some(t) if t.elapsed() < Duration::from_millis(500) => self.levels,
            _ => Levels::default(),
        }
    }
}

// ---------------------------------------------------------------------------
// RecvWorker
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct RecvConfig {
    /// Source to receive: full name or stream name.
    pub target: String,
    pub opts: ndi::RecvOptions,
    pub audio: bool,
    /// Replace a connected receiver that delivers no video for this long.
    pub stall_timeout: Duration,
    /// Re-resolve and recreate a receiver that does not connect within this time.
    pub connect_timeout: Duration,
}

impl RecvConfig {
    pub fn new(target: impl Into<String>, opts: ndi::RecvOptions) -> Self {
        Self {
            target: target.into(),
            opts,
            audio: true,
            stall_timeout: Duration::from_secs(2),
            connect_timeout: Duration::from_secs(5),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct RecvStats {
    pub target: String,
    /// Resolved full source name while attached.
    pub source: Option<String>,
    pub connected: bool,
    pub width: usize,
    pub height: usize,
    pub fps: f64,
    pub frames: u64,
    /// Frames replaced before the consumer took them (consumer too slow).
    pub skipped: u64,
    /// Frames the SDK dropped.
    pub dropped: i64,
    pub levels: Levels,
}

struct Slot {
    frame: Option<ndi::VideoFrame>,
}

struct Shared {
    slot: Mutex<Slot>,
    cv: Condvar,
    stats: Mutex<RecvStats>,
    stop: AtomicBool,
}

enum Ctl {
    Switch(String),
}

pub struct RecvWorker {
    shared: Arc<Shared>,
    ctl: Sender<Ctl>,
    handle: Option<JoinHandle<()>>,
}

impl RecvWorker {
    pub fn spawn(cfg: RecvConfig, dir: Arc<Directory>) -> Self {
        let shared = Arc::new(Shared {
            slot: Mutex::new(Slot { frame: None }),
            cv: Condvar::new(),
            stats: Mutex::new(RecvStats { target: cfg.target.clone(), ..Default::default() }),
            stop: AtomicBool::new(false),
        });
        let (ctl, ctl_rx) = channel();
        let sh = shared.clone();
        let handle = std::thread::Builder::new()
            .name("ndi-recv".into())
            .spawn(move || capture_loop(cfg, dir, sh, ctl_rx))
            .expect("spawn capture thread");
        Self { shared, ctl, handle: Some(handle) }
    }

    /// Take the newest frame, waiting up to `timeout` for one.
    pub fn take_frame(&self, timeout: Duration) -> Option<ndi::VideoFrame> {
        let mut slot = self.shared.slot.lock().unwrap();
        if slot.frame.is_none() && !timeout.is_zero() {
            slot = self.shared.cv.wait_timeout_while(slot, timeout, |s| s.frame.is_none()).unwrap().0;
        }
        slot.frame.take()
    }

    pub fn switch(&self, target: &str) {
        self.shared.slot.lock().unwrap().frame = None;
        let _ = self.ctl.send(Ctl::Switch(target.to_string()));
    }

    pub fn stats(&self) -> RecvStats {
        self.shared.stats.lock().unwrap().clone()
    }
}

impl Drop for RecvWorker {
    fn drop(&mut self) {
        self.shared.stop.store(true, Ordering::Relaxed);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

fn capture_loop(mut cfg: RecvConfig, dir: Arc<Directory>, sh: Arc<Shared>, ctl: std::sync::mpsc::Receiver<Ctl>) {
    let mut rx: Option<ndi::Receiver> = None;
    let mut attached_at = Instant::now();
    let mut last_video = Instant::now();
    let mut got_video = false;
    let mut stall_after = cfg.stall_timeout;
    let mut meter = Meter::default();
    let mut last_poll = Instant::now() - Duration::from_secs(1);
    let mut last_wait_log = Instant::now() - Duration::from_secs(60);
    let mut base_dropped = 0i64;

    let detach = |rx: &mut Option<ndi::Receiver>, sh: &Shared| {
        if let Some(r) = rx.take() {
            // A dead session can take seconds to tear down: never on this thread.
            r.destroy_in_background();
        }
        let mut st = sh.stats.lock().unwrap();
        st.connected = false;
        st.source = None;
    };

    while !sh.stop.load(Ordering::Relaxed) {
        loop {
            match ctl.try_recv() {
                Ok(Ctl::Switch(t)) => {
                    crate::log!("[recv] switching to \"{t}\"");
                    detach(&mut rx, &sh);
                    cfg.target = t.clone();
                    got_video = false;
                    stall_after = cfg.stall_timeout;
                    let mut st = sh.stats.lock().unwrap();
                    *st = RecvStats { target: t, ..Default::default() };
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => return,
            }
        }

        let Some(r) = rx.as_ref() else {
            match dir.find(&cfg.target) {
                Some(src) => match ndi::Receiver::new(Some(&src), &cfg.opts) {
                    Ok(r) => {
                        crate::log!("[recv] attaching to \"{}\"", src.name);
                        base_dropped = 0;
                        sh.stats.lock().unwrap().source = Some(src.name.clone());
                        attached_at = Instant::now();
                        last_video = attached_at;
                        rx = Some(r);
                    }
                    Err(e) => {
                        crate::log!("[recv] {e}");
                        std::thread::sleep(Duration::from_secs(1));
                    }
                },
                None => {
                    if last_wait_log.elapsed() > Duration::from_secs(30) {
                        crate::log!("[recv] waiting for source \"{}\"", cfg.target);
                        last_wait_log = Instant::now();
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
            }
            continue;
        };

        match r.capture(50, true, cfg.audio) {
            ndi::Capture::Video(f) => {
                let now = Instant::now();
                last_video = now;
                got_video = true;
                stall_after = cfg.stall_timeout;
                {
                    let mut st = sh.stats.lock().unwrap();
                    st.width = f.width();
                    st.height = f.height();
                    st.fps = f.fps();
                    st.frames += 1;
                    st.connected = true;
                }
                let replaced = sh.slot.lock().unwrap().frame.replace(f);
                if replaced.is_some() {
                    sh.stats.lock().unwrap().skipped += 1;
                }
                drop(replaced);
                sh.cv.notify_all();
            }
            ndi::Capture::Audio(a) => {
                let chans: Vec<&[f32]> = (0..a.channels().min(2)).map(|c| a.channel(c)).collect();
                meter.feed(&chans);
            }
            _ => {}
        }

        let now = Instant::now();
        if now.duration_since(last_poll) >= Duration::from_millis(250) {
            last_poll = now;
            let connected = r.is_connected();
            let perf = r.performance();
            let mut st = sh.stats.lock().unwrap();
            st.connected = connected;
            st.dropped = base_dropped + perf.video_dropped;
            st.levels = meter.levels();
            drop(st);

            let silent = now.duration_since(last_video);
            let reason = if connected && silent > stall_after {
                Some(format!("no video for {:.1}s", silent.as_secs_f32()))
            } else if !connected && now.duration_since(attached_at) > cfg.connect_timeout {
                Some("not connected".to_string())
            } else {
                None
            };
            if let Some(reason) = reason {
                crate::log!("[recv] \"{}\": {reason}, reconnecting with a fresh receiver", cfg.target);
                detach(&mut rx, &sh);
                if got_video || connected {
                    // Back off when the sender is legitimately paused.
                    stall_after = (stall_after * 2).min(Duration::from_secs(30));
                }
                got_video = false;
            }
        }
    }
    detach(&mut rx, &sh);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn meter_full_scale_sine_is_about_minus_3db() {
        let s: Vec<f32> = (0..4800).map(|i| (i as f32 * 0.05).sin()).collect();
        let mut m = Meter::default();
        m.feed(&[&s, &s]);
        let l = m.levels();
        assert!((l.level[0] + 3.0).abs() < 0.2, "{}", l.level[0]);
        assert!(l.peak[0] > -0.1);
    }

    #[test]
    fn meter_silence_and_mono() {
        let z = vec![0.0f32; 480];
        let mut m = Meter::default();
        m.feed(&[&z]);
        assert_eq!(m.levels().level, [SILENCE_DB; 2]);
    }
}
