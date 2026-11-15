//! Control channel between the Go server and an engine process.
//!
//! Protocol (newline-delimited JSON):
//! * stdin  -> engine: commands, e.g. `{"cmd":"switch","source":"PC (OBS)"}`.
//!   EOF on stdin (parent gone) is delivered as [`Command::Quit`].
//! * engine -> stdout: events, each with a `"type"` field
//!   (`"status"`, `"sources"`, `"error"`, ...).
//! * stderr: human-readable log lines.
//!
//! Binary payloads (preview frames) use their own framing on stdout and do not
//! go through [`emit`].

use serde::{Deserialize, Serialize};
use std::io::{BufRead, Write};
use std::sync::mpsc::{channel, Receiver, TryRecvError};
use std::sync::Mutex;

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Command {
    /// Connect to another source (rx).
    Switch { source: String },
    /// Replace the source list (multiview); empty = auto-discover.
    SetSources { sources: Vec<String> },
    /// Show another image (signage).
    Image { path: String },
    /// Preview output configuration.
    Preview {
        #[serde(default)]
        h264: bool,
        #[serde(default)]
        jpeg: bool,
        #[serde(default)]
        jpeg_width: Option<u32>,
        #[serde(default)]
        jpeg_fps: Option<u32>,
    },
    /// Ask the H.264 encoder for an IDR frame (new WebRTC viewer).
    Keyframe,
    Quit,
}

pub struct Control {
    rx: Option<Receiver<Command>>,
}

impl Control {
    /// With `enabled`, read commands from stdin on a background thread.
    pub fn new(enabled: bool) -> Self {
        if !enabled {
            return Self { rx: None };
        }
        let (tx, rx) = channel();
        std::thread::Builder::new()
            .name("control-stdin".into())
            .spawn(move || {
                let stdin = std::io::stdin();
                for line in stdin.lock().lines() {
                    let Ok(line) = line else { break };
                    let line = line.trim();
                    if line.is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<Command>(line) {
                        Ok(cmd) => {
                            if tx.send(cmd).is_err() {
                                return;
                            }
                        }
                        Err(e) => log(&format!("ignoring bad command {line:?}: {e}")),
                    }
                }
                let _ = tx.send(Command::Quit);
            })
            .expect("spawn control thread");
        Self { rx: Some(rx) }
    }

    pub fn enabled(&self) -> bool {
        self.rx.is_some()
    }

    /// Next pending command, if any.
    pub fn poll(&self) -> Option<Command> {
        let rx = self.rx.as_ref()?;
        match rx.try_recv() {
            Ok(c) => Some(c),
            Err(TryRecvError::Empty) => None,
            Err(TryRecvError::Disconnected) => Some(Command::Quit),
        }
    }

    /// Block up to `timeout` for a command.
    pub fn wait(&self, timeout: std::time::Duration) -> Option<Command> {
        match &self.rx {
            Some(rx) => rx.recv_timeout(timeout).ok(),
            None => {
                std::thread::sleep(timeout);
                None
            }
        }
    }
}

static STDOUT: Mutex<()> = Mutex::new(());

/// Write one JSON event line to stdout.
pub fn emit<T: Serialize>(event: &T) {
    let Ok(mut line) = serde_json::to_vec(event) else { return };
    line.push(b'\n');
    let _g = STDOUT.lock().unwrap_or_else(|e| e.into_inner());
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(&line);
    let _ = out.flush();
}

/// Write raw bytes to stdout (binary framing for preview data).
pub fn emit_raw(parts: &[&[u8]]) -> std::io::Result<()> {
    let _g = STDOUT.lock().unwrap_or_else(|e| e.into_inner());
    let mut out = std::io::stdout().lock();
    for p in parts {
        out.write_all(p)?;
    }
    out.flush()
}

#[derive(Serialize)]
struct ErrorEvent<'a> {
    r#type: &'static str,
    error: &'a str,
}

pub fn emit_error(msg: &str) {
    emit(&ErrorEvent { r#type: "error", error: msg });
}

pub fn log(msg: &str) {
    eprintln!("{msg}");
}

#[macro_export]
macro_rules! log {
    ($($arg:tt)*) => { $crate::control::log(&format!($($arg)*)) };
}
