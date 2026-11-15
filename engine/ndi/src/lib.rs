//! Safe, minimal wrappers around the NDI SDK.
//!
//! The SDK is not vendored: `libndi.so.6` is loaded at runtime with `dlopen`, so
//! building needs no SDK headers and the NDI runtime from the official SDK
//! installer (or `NDI_RUNTIME_DIR_V6`) is picked up at run time.
//!
//! Every SDK call happens without any interpreter lock, so a slow call (e.g. a
//! receiver tearing down a dead TCP session) only blocks the calling thread.
//! Receivers can therefore be destroyed on a background thread with
//! [`Receiver::destroy_in_background`].

pub mod sys;

use std::ffi::{CStr, CString};
use std::fmt;
use std::ptr;
use std::sync::{Arc, OnceLock};

pub use sys::{
    FOURCC_BGRA, FOURCC_BGRX, FOURCC_I420, FOURCC_NV12, FOURCC_RGBA, FOURCC_RGBX, FOURCC_UYVA,
    FOURCC_UYVY,
};

#[derive(Debug, Clone)]
pub struct Error(String);

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

struct Runtime {
    api: sys::Api,
    _lib: libloading::Library,
}

static RUNTIME: OnceLock<std::result::Result<Runtime, String>> = OnceLock::new();

fn candidates() -> Vec<String> {
    let mut v = Vec::new();
    for var in ["NDI_RUNTIME_DIR_V6", "NDI_RUNTIME_DIR_V5"] {
        if let Ok(dir) = std::env::var(var) {
            v.push(format!("{}/libndi.so.6", dir.trim_end_matches('/')));
            v.push(format!("{}/libndi.so", dir.trim_end_matches('/')));
        }
    }
    v.push("libndi.so.6".into());
    v.push("libndi.so".into());
    v.push("/usr/local/lib/libndi.so.6".into());
    v.push("/usr/lib/libndi.so.6".into());
    v
}

fn load_runtime() -> std::result::Result<Runtime, String> {
    let mut errors = Vec::new();
    for path in candidates() {
        // SAFETY: loading the NDI runtime runs its (trusted) initialisers.
        match unsafe { libloading::Library::new(&path) } {
            Ok(lib) => {
                // SAFETY: the symbols are declared with the SDK's C signatures.
                let api = unsafe { sys::Api::load(&lib) }
                    .map_err(|e| format!("{path}: missing NDI symbol: {e}"))?;
                // SAFETY: plain C call.
                if !unsafe { (api.initialize)() } {
                    return Err("NDIlib_initialize failed (unsupported CPU?)".into());
                }
                return Ok(Runtime { api, _lib: lib });
            }
            Err(e) => errors.push(format!("{path}: {e}")),
        }
    }
    Err(format!(
        "NDI runtime (libndi.so.6) not found. Install the NDI SDK or set NDI_RUNTIME_DIR_V6.\n  {}",
        errors.join("\n  ")
    ))
}

fn api() -> Result<&'static sys::Api> {
    match RUNTIME.get_or_init(load_runtime) {
        Ok(rt) => Ok(&rt.api),
        Err(e) => Err(Error(e.clone())),
    }
}

/// Load and initialise the NDI runtime. Called implicitly by every constructor;
/// call it early to fail fast with a helpful message.
pub fn init() -> Result<()> {
    api().map(|_| ())
}

/// Version string of the loaded NDI runtime.
pub fn version() -> Result<String> {
    let api = api()?;
    // SAFETY: returns a static NUL-terminated string.
    Ok(unsafe { CStr::from_ptr((api.version)()) }.to_string_lossy().into_owned())
}

/// Current time in NDI timestamp units (100 ns since the Unix epoch).
pub fn now_timestamp() -> i64 {
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    (d.as_nanos() / 100) as i64
}

unsafe fn opt_str(p: *const std::os::raw::c_char) -> Option<String> {
    if p.is_null() {
        None
    } else {
        Some(CStr::from_ptr(p).to_string_lossy().into_owned())
    }
}

fn cstring(s: &str) -> CString {
    CString::new(s.replace('\0', "")).expect("NUL removed")
}

// ---------------------------------------------------------------------------
// Sources / finder
// ---------------------------------------------------------------------------

/// An NDI source as announced on the network: `"HOST (STREAM)"`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Source {
    pub name: String,
    pub url: Option<String>,
}

impl Source {
    pub fn new(name: impl Into<String>) -> Self {
        Self { name: name.into(), url: None }
    }

    /// The part between the parentheses, e.g. `"OBS"` for `"PC (OBS)"`.
    pub fn stream_name(&self) -> &str {
        split_name(&self.name).1
    }

    /// The machine part, e.g. `"PC"` for `"PC (OBS)"`.
    pub fn host_name(&self) -> &str {
        split_name(&self.name).0
    }

    /// True when `query` equals the full name or the stream name.
    pub fn matches(&self, query: &str) -> bool {
        self.name == query || self.stream_name() == query
    }
}

fn split_name(name: &str) -> (&str, &str) {
    match (name.find(" ("), name.ends_with(')')) {
        (Some(i), true) => (&name[..i], &name[i + 2..name.len() - 1]),
        _ => (name, name),
    }
}

struct RawSource {
    _name: CString,
    _url: Option<CString>,
    raw: sys::NDIlib_source_t,
}

impl RawSource {
    fn new(src: &Source) -> Self {
        let name = cstring(&src.name);
        let url = src.url.as_deref().map(cstring);
        let raw = sys::NDIlib_source_t {
            p_ndi_name: name.as_ptr(),
            p_url_address: url.as_ref().map_or(ptr::null(), |u| u.as_ptr()),
        };
        Self { _name: name, _url: url, raw }
    }
}

pub struct Finder {
    api: &'static sys::Api,
    ptr: sys::NDIlib_find_instance_t,
}

// SAFETY: a finder instance may be used from any thread (one at a time).
unsafe impl Send for Finder {}

impl Finder {
    pub fn new(show_local_sources: bool, extra_ips: Option<&str>) -> Result<Self> {
        let api = api()?;
        let ips = extra_ips.filter(|s| !s.is_empty()).map(cstring);
        let cfg = sys::NDIlib_find_create_t {
            show_local_sources,
            p_groups: ptr::null(),
            p_extra_ips: ips.as_ref().map_or(ptr::null(), |c| c.as_ptr()),
        };
        // SAFETY: cfg and its strings outlive the call.
        let ptr = unsafe { (api.find_create_v2)(&cfg) };
        if ptr.is_null() {
            return Err(Error("NDIlib_find_create_v2 failed".into()));
        }
        Ok(Self { api, ptr })
    }

    /// Block until the source list changes or `timeout_ms` elapses.
    pub fn wait(&self, timeout_ms: u32) -> bool {
        // SAFETY: valid instance.
        unsafe { (self.api.find_wait_for_sources)(self.ptr, timeout_ms) }
    }

    pub fn sources(&self) -> Vec<Source> {
        let mut n = 0u32;
        // SAFETY: the returned array stays valid until the next call on this finder.
        unsafe {
            let p = (self.api.find_get_current_sources)(self.ptr, &mut n);
            if p.is_null() {
                return Vec::new();
            }
            std::slice::from_raw_parts(p, n as usize)
                .iter()
                .filter_map(|s| {
                    Some(Source { name: opt_str(s.p_ndi_name)?, url: opt_str(s.p_url_address) })
                })
                .collect()
        }
    }
}

impl Drop for Finder {
    fn drop(&mut self) {
        // SAFETY: valid instance, destroyed once.
        unsafe { (self.api.find_destroy)(self.ptr) }
    }
}

// ---------------------------------------------------------------------------
// Receiver
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ColorFormat {
    /// UYVY for opaque video, RGBA when the source has alpha. Cheapest.
    UyvyRgba,
    UyvyBgra,
    RgbxRgba,
    BgrxBgra,
    Fastest,
}

impl ColorFormat {
    fn raw(self) -> i32 {
        match self {
            Self::UyvyRgba => sys::RECV_COLOR_UYVY_RGBA,
            Self::UyvyBgra => sys::RECV_COLOR_UYVY_BGRA,
            Self::RgbxRgba => sys::RECV_COLOR_RGBX_RGBA,
            Self::BgrxBgra => sys::RECV_COLOR_BGRX_BGRA,
            Self::Fastest => sys::RECV_COLOR_FASTEST,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Bandwidth {
    Highest,
    Lowest,
    AudioOnly,
    MetadataOnly,
}

impl Bandwidth {
    fn raw(self) -> i32 {
        match self {
            Self::Highest => sys::RECV_BANDWIDTH_HIGHEST,
            Self::Lowest => sys::RECV_BANDWIDTH_LOWEST,
            Self::AudioOnly => sys::RECV_BANDWIDTH_AUDIO_ONLY,
            Self::MetadataOnly => sys::RECV_BANDWIDTH_METADATA_ONLY,
        }
    }
}

#[derive(Debug, Clone)]
pub struct RecvOptions {
    pub color_format: ColorFormat,
    pub bandwidth: Bandwidth,
    pub allow_video_fields: bool,
    /// Name shown to the sender (e.g. in NDI Studio Monitor).
    pub name: Option<String>,
}

impl Default for RecvOptions {
    fn default() -> Self {
        Self {
            color_format: ColorFormat::UyvyRgba,
            bandwidth: Bandwidth::Highest,
            allow_video_fields: false,
            name: None,
        }
    }
}

struct RecvInner {
    api: &'static sys::Api,
    ptr: sys::NDIlib_recv_instance_t,
}

// SAFETY: NDI receivers are thread-safe: capture, free and query calls may come
// from different threads.
unsafe impl Send for RecvInner {}
unsafe impl Sync for RecvInner {}

impl Drop for RecvInner {
    fn drop(&mut self) {
        // SAFETY: last reference; every frame holding an Arc has been freed.
        unsafe { (self.api.recv_destroy)(self.ptr) }
    }
}

/// An NDI receiver. Cheap to clone (shared handle); destroyed when the last
/// clone and the last frame captured from it are dropped.
#[derive(Clone)]
pub struct Receiver {
    inner: Arc<RecvInner>,
}

/// Result of [`Receiver::capture`].
pub enum Capture {
    None,
    Video(VideoFrame),
    Audio(AudioFrame),
    Metadata,
    StatusChange,
    SourceChange,
    Error,
}

#[derive(Debug, Default, Clone, Copy)]
pub struct Performance {
    pub video_total: i64,
    pub video_dropped: i64,
    pub audio_total: i64,
    pub audio_dropped: i64,
}

impl Receiver {
    pub fn new(source: Option<&Source>, opts: &RecvOptions) -> Result<Self> {
        let api = api()?;
        let src = source.map(RawSource::new);
        let name = opts.name.as_deref().map(cstring);
        let cfg = sys::NDIlib_recv_create_v3_t {
            source_to_connect_to: src.as_ref().map_or(
                sys::NDIlib_source_t { p_ndi_name: ptr::null(), p_url_address: ptr::null() },
                |s| s.raw,
            ),
            color_format: opts.color_format.raw(),
            bandwidth: opts.bandwidth.raw(),
            allow_video_fields: opts.allow_video_fields,
            p_ndi_recv_name: name.as_ref().map_or(ptr::null(), |c| c.as_ptr()),
        };
        // SAFETY: cfg and its strings outlive the call.
        let ptr = unsafe { (api.recv_create_v3)(&cfg) };
        if ptr.is_null() {
            return Err(Error("NDIlib_recv_create_v3 failed".into()));
        }
        Ok(Self { inner: Arc::new(RecvInner { api, ptr }) })
    }

    /// Switch to another source, or disconnect with `None`.
    ///
    /// Disconnecting a session whose peer vanished can block for many seconds;
    /// prefer creating a new receiver and [`destroy_in_background`](Self::destroy_in_background).
    pub fn connect(&self, source: Option<&Source>) {
        let src = source.map(RawSource::new);
        // SAFETY: valid instance; src outlives the call.
        unsafe {
            (self.inner.api.recv_connect)(
                self.inner.ptr,
                src.as_ref().map_or(ptr::null(), |s| &s.raw as *const _),
            )
        }
    }

    /// Wait up to `timeout_ms` for the next frame. Frame kinds that are not
    /// requested are discarded by the SDK.
    pub fn capture(&self, timeout_ms: u32, video: bool, audio: bool) -> Capture {
        let api = self.inner.api;
        let mut v = sys::NDIlib_video_frame_v2_t::default();
        let mut a = sys::NDIlib_audio_frame_v3_t::default();
        let mut m = sys::NDIlib_metadata_frame_t::default();
        // SAFETY: valid instance and out-pointers.
        let t = unsafe {
            (api.recv_capture_v3)(
                self.inner.ptr,
                if video { &mut v } else { ptr::null_mut() },
                if audio { &mut a } else { ptr::null_mut() },
                &mut m,
                timeout_ms,
            )
        };
        match t {
            sys::FRAME_TYPE_VIDEO => Capture::Video(VideoFrame { recv: self.inner.clone(), raw: v }),
            sys::FRAME_TYPE_AUDIO => Capture::Audio(AudioFrame { recv: self.inner.clone(), raw: a }),
            sys::FRAME_TYPE_METADATA => {
                // SAFETY: m was filled by this capture call.
                unsafe { (api.recv_free_metadata)(self.inner.ptr, &m) };
                Capture::Metadata
            }
            sys::FRAME_TYPE_STATUS_CHANGE => Capture::StatusChange,
            sys::FRAME_TYPE_SOURCE_CHANGE => Capture::SourceChange,
            sys::FRAME_TYPE_ERROR => Capture::Error,
            _ => Capture::None,
        }
    }

    /// Number of senders this receiver is connected to (0 or 1).
    pub fn connections(&self) -> i32 {
        // SAFETY: valid instance.
        unsafe { (self.inner.api.recv_get_no_connections)(self.inner.ptr) }
    }

    pub fn is_connected(&self) -> bool {
        self.connections() > 0
    }

    pub fn performance(&self) -> Performance {
        let mut total = sys::NDIlib_recv_performance_t::default();
        let mut dropped = sys::NDIlib_recv_performance_t::default();
        // SAFETY: valid instance and out-pointers.
        unsafe { (self.inner.api.recv_get_performance)(self.inner.ptr, &mut total, &mut dropped) };
        Performance {
            video_total: total.video_frames,
            video_dropped: dropped.video_frames,
            audio_total: total.audio_frames,
            audio_dropped: dropped.audio_frames,
        }
    }

    /// Frames waiting in the receive queue (video, audio).
    pub fn queue(&self) -> (i32, i32) {
        let mut q = sys::NDIlib_recv_queue_t::default();
        // SAFETY: valid instance and out-pointer.
        unsafe { (self.inner.api.recv_get_queue)(self.inner.ptr, &mut q) };
        (q.video_frames, q.audio_frames)
    }

    /// Drop this handle on a detached thread, so a slow teardown of a dead
    /// session never stalls the caller.
    pub fn destroy_in_background(self) {
        let _ = std::thread::Builder::new()
            .name("ndi-recv-destroy".into())
            .spawn(move || drop(self));
    }
}

/// A video frame owned by the SDK; returned to it on drop.
pub struct VideoFrame {
    recv: Arc<RecvInner>,
    raw: sys::NDIlib_video_frame_v2_t,
}

impl VideoFrame {
    pub fn width(&self) -> usize {
        self.raw.xres.max(0) as usize
    }
    pub fn height(&self) -> usize {
        self.raw.yres.max(0) as usize
    }
    pub fn fourcc(&self) -> u32 {
        self.raw.fourcc
    }
    pub fn stride(&self) -> usize {
        self.raw.line_stride_in_bytes.max(0) as usize
    }
    pub fn frame_rate(&self) -> (i32, i32) {
        (self.raw.frame_rate_n, self.raw.frame_rate_d)
    }
    pub fn fps(&self) -> f64 {
        if self.raw.frame_rate_d > 0 {
            self.raw.frame_rate_n as f64 / self.raw.frame_rate_d as f64
        } else {
            0.0
        }
    }
    /// Sender-side timestamp (100 ns since epoch), if the sender set one.
    pub fn timestamp(&self) -> Option<i64> {
        let ts = self.raw.timestamp;
        (ts != sys::RECV_TIMESTAMP_UNDEFINED && ts > 0).then_some(ts)
    }
    pub fn timecode(&self) -> i64 {
        self.raw.timecode
    }

    /// Size in bytes of the pixel data for the frame's FourCC.
    pub fn data_len(&self) -> usize {
        let (h, s) = (self.height(), self.stride());
        match self.raw.fourcc {
            sys::FOURCC_UYVA => s * h + self.width() * h,
            sys::FOURCC_NV12 => s * h * 3 / 2,
            sys::FOURCC_I420 | sys::FOURCC_YV12 => s * h * 3 / 2,
            _ => s * h,
        }
    }

    pub fn data(&self) -> &[u8] {
        if self.raw.p_data.is_null() {
            return &[];
        }
        // SAFETY: the SDK guarantees data_len() bytes until the frame is freed.
        unsafe { std::slice::from_raw_parts(self.raw.p_data, self.data_len()) }
    }
}

impl Drop for VideoFrame {
    fn drop(&mut self) {
        // SAFETY: raw came from capture on this receiver and is freed once.
        unsafe { (self.recv.api.recv_free_video_v2)(self.recv.ptr, &self.raw) }
    }
}

// SAFETY: frames may be freed from any thread.
unsafe impl Send for VideoFrame {}

/// A planar 32-bit float audio frame owned by the SDK.
pub struct AudioFrame {
    recv: Arc<RecvInner>,
    raw: sys::NDIlib_audio_frame_v3_t,
}

impl AudioFrame {
    pub fn sample_rate(&self) -> i32 {
        self.raw.sample_rate
    }
    pub fn channels(&self) -> usize {
        self.raw.no_channels.max(0) as usize
    }
    pub fn samples(&self) -> usize {
        self.raw.no_samples.max(0) as usize
    }
    /// Samples of channel `ch` (FLTP layout).
    pub fn channel(&self, ch: usize) -> &[f32] {
        if self.raw.p_data.is_null() || ch >= self.channels() || self.raw.fourcc != sys::FOURCC_FLTP {
            return &[];
        }
        let stride = self.raw.channel_stride_in_bytes.max(0) as usize;
        // SAFETY: FLTP data holds `channels` planes of `samples` floats, `stride` bytes apart.
        unsafe {
            std::slice::from_raw_parts(self.raw.p_data.add(ch * stride) as *const f32, self.samples())
        }
    }
}

impl Drop for AudioFrame {
    fn drop(&mut self) {
        // SAFETY: raw came from capture on this receiver and is freed once.
        unsafe { (self.recv.api.recv_free_audio_v3)(self.recv.ptr, &self.raw) }
    }
}

// SAFETY: frames may be freed from any thread.
unsafe impl Send for AudioFrame {}

// ---------------------------------------------------------------------------
// Sender
// ---------------------------------------------------------------------------

/// A borrowed video frame to send.
pub struct VideoSend<'a> {
    pub width: usize,
    pub height: usize,
    pub fourcc: u32,
    pub stride: usize,
    pub frame_rate: (i32, i32),
    pub data: &'a [u8],
}

pub struct Sender {
    api: &'static sys::Api,
    ptr: sys::NDIlib_send_instance_t,
}

// SAFETY: NDI senders are thread-safe (video and audio may be sent from different threads).
unsafe impl Send for Sender {}
unsafe impl Sync for Sender {}

impl Sender {
    /// `clock_video`: let the SDK pace video submissions to the frame rate.
    /// Leave it off when the capture device already paces frames.
    pub fn new(name: &str, clock_video: bool, clock_audio: bool) -> Result<Self> {
        let api = api()?;
        let cname = cstring(name);
        let cfg = sys::NDIlib_send_create_t {
            p_ndi_name: cname.as_ptr(),
            p_groups: ptr::null(),
            clock_video,
            clock_audio,
        };
        // SAFETY: cfg and its strings outlive the call.
        let ptr = unsafe { (api.send_create)(&cfg) };
        if ptr.is_null() {
            return Err(Error(format!("NDIlib_send_create failed for \"{name}\"")));
        }
        Ok(Self { api, ptr })
    }

    /// Send a video frame synchronously (the data is no longer referenced on return).
    pub fn send_video(&self, f: &VideoSend<'_>) {
        let needed = match f.fourcc {
            sys::FOURCC_NV12 | sys::FOURCC_I420 => f.stride * f.height * 3 / 2,
            _ => f.stride * f.height,
        };
        assert!(f.data.len() >= needed, "video buffer too small");
        let raw = sys::NDIlib_video_frame_v2_t {
            xres: f.width as i32,
            yres: f.height as i32,
            fourcc: f.fourcc,
            frame_rate_n: f.frame_rate.0,
            frame_rate_d: f.frame_rate.1,
            picture_aspect_ratio: 0.0,
            frame_format_type: sys::FRAME_FORMAT_PROGRESSIVE,
            timecode: sys::SEND_TIMECODE_SYNTHESIZE,
            p_data: f.data.as_ptr() as *mut u8,
            line_stride_in_bytes: f.stride as i32,
            p_metadata: ptr::null(),
            timestamp: 0,
        };
        // SAFETY: valid instance; data checked above and borrowed for the call.
        unsafe { (self.api.send_send_video_v2)(self.ptr, &raw) }
    }

    /// Send planar float audio: `planar` holds `channels` planes of `samples` floats.
    pub fn send_audio(&self, sample_rate: i32, channels: usize, samples: usize, planar: &[f32]) {
        assert!(planar.len() >= channels * samples, "audio buffer too small");
        let raw = sys::NDIlib_audio_frame_v3_t {
            sample_rate,
            no_channels: channels as i32,
            no_samples: samples as i32,
            timecode: sys::SEND_TIMECODE_SYNTHESIZE,
            fourcc: sys::FOURCC_FLTP,
            p_data: planar.as_ptr() as *mut u8,
            channel_stride_in_bytes: (samples * 4) as i32,
            p_metadata: ptr::null(),
            timestamp: 0,
        };
        // SAFETY: valid instance; data checked above and borrowed for the call.
        unsafe { (self.api.send_send_audio_v3)(self.ptr, &raw) }
    }

    pub fn connections(&self, timeout_ms: u32) -> i32 {
        // SAFETY: valid instance.
        unsafe { (self.api.send_get_no_connections)(self.ptr, timeout_ms) }
    }
}

impl Drop for Sender {
    fn drop(&mut self) {
        // SAFETY: valid instance, destroyed once.
        unsafe { (self.api.send_destroy)(self.ptr) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_name_parts() {
        let s = Source::new("STUDIO-PC (OBS Main)");
        assert_eq!(s.host_name(), "STUDIO-PC");
        assert_eq!(s.stream_name(), "OBS Main");
        assert!(s.matches("OBS Main"));
        assert!(s.matches("STUDIO-PC (OBS Main)"));
        let plain = Source::new("weird");
        assert_eq!(plain.stream_name(), "weird");
    }

    #[test]
    fn struct_layout_matches_c() {
        use std::mem::size_of;
        assert_eq!(size_of::<sys::NDIlib_source_t>(), 16);
        assert_eq!(size_of::<sys::NDIlib_video_frame_v2_t>(), 72);
        assert_eq!(size_of::<sys::NDIlib_audio_frame_v3_t>(), 64);
        assert_eq!(size_of::<sys::NDIlib_recv_create_v3_t>(), 40);
        assert_eq!(size_of::<sys::NDIlib_find_create_t>(), 24);
        assert_eq!(size_of::<sys::NDIlib_send_create_t>(), 24);
        assert_eq!(std::mem::offset_of!(sys::NDIlib_video_frame_v2_t, p_data), 40);
        assert_eq!(std::mem::offset_of!(sys::NDIlib_video_frame_v2_t, timestamp), 64);
        assert_eq!(std::mem::offset_of!(sys::NDIlib_audio_frame_v3_t, p_data), 32);
        assert_eq!(std::mem::offset_of!(sys::NDIlib_recv_create_v3_t, p_ndi_recv_name), 32);
    }
}
