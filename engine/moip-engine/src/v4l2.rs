//! Minimal V4L2 video capture (mmap streaming), no external dependencies.
//!
//! Only what capture cards / webcams need: query capabilities, pick a format
//! (YUYV, UYVY or MJPEG), set the frame rate, and dequeue frames.

#![allow(non_camel_case_types)]

use anyhow::{bail, Context, Result};
use std::ffi::CString;
use std::os::raw::{c_int, c_ulong, c_void};
use std::time::Duration;

pub const fn fourcc(a: u8, b: u8, c: u8, d: u8) -> u32 {
    (a as u32) | ((b as u32) << 8) | ((c as u32) << 16) | ((d as u32) << 24)
}
pub const PIX_YUYV: u32 = fourcc(b'Y', b'U', b'Y', b'V');
pub const PIX_UYVY: u32 = fourcc(b'U', b'Y', b'V', b'Y');
pub const PIX_MJPG: u32 = fourcc(b'M', b'J', b'P', b'G');

pub fn fourcc_str(f: u32) -> String {
    f.to_le_bytes().iter().map(|&b| if b.is_ascii_graphic() { b as char } else { '?' }).collect()
}

const BUF_TYPE_VIDEO_CAPTURE: u32 = 1;
const MEMORY_MMAP: u32 = 1;
const FIELD_NONE: u32 = 1;
const CAP_VIDEO_CAPTURE: u32 = 0x0000_0001;
const CAP_STREAMING: u32 = 0x0400_0000;
const CAP_DEVICE_CAPS: u32 = 0x8000_0000;

#[repr(C)]
pub struct v4l2_capability {
    pub driver: [u8; 16],
    pub card: [u8; 32],
    pub bus_info: [u8; 32],
    pub version: u32,
    pub capabilities: u32,
    pub device_caps: u32,
    pub reserved: [u32; 3],
}

#[repr(C)]
struct v4l2_fmtdesc {
    index: u32,
    type_: u32,
    flags: u32,
    description: [u8; 32],
    pixelformat: u32,
    mbus_code: u32,
    reserved: [u32; 3],
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct v4l2_pix_format {
    width: u32,
    height: u32,
    pixelformat: u32,
    field: u32,
    bytesperline: u32,
    sizeimage: u32,
    colorspace: u32,
    priv_: u32,
    flags: u32,
    ycbcr_enc: u32,
    quantization: u32,
    xfer_func: u32,
}

#[repr(C)]
struct v4l2_format {
    type_: u32,
    // The kernel union is 200 bytes and 8-byte aligned (it holds pointers).
    _pad: u32,
    pix: v4l2_pix_format,
    _rest: [u8; 200 - 48],
}

#[repr(C)]
struct v4l2_requestbuffers {
    count: u32,
    type_: u32,
    memory: u32,
    capabilities: u32,
    flags: u8,
    reserved: [u8; 3],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct v4l2_buffer {
    index: u32,
    type_: u32,
    bytesused: u32,
    flags: u32,
    field: u32,
    _pad: u32,
    timestamp: [i64; 2],
    timecode: [u32; 4],
    sequence: u32,
    memory: u32,
    m_offset: u64, // union { offset; userptr; planes; fd }
    length: u32,
    reserved2: u32,
    request_fd: u32,
    _pad2: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct v4l2_fract {
    numerator: u32,
    denominator: u32,
}

#[repr(C)]
struct v4l2_streamparm {
    type_: u32,
    capability: u32,
    capturemode: u32,
    timeperframe: v4l2_fract,
    extendedmode: u32,
    readbuffers: u32,
    _rest: [u8; 200 - 24],
}

const fn ioc(dir: u64, nr: u64, size: usize) -> c_ulong {
    ((dir << 30) | ((size as u64) << 16) | ((b'V' as u64) << 8) | nr) as c_ulong
}
const R: u64 = 2;
const W: u64 = 1;
const RW: u64 = 3;
const VIDIOC_QUERYCAP: c_ulong = ioc(R, 0, std::mem::size_of::<v4l2_capability>());
const VIDIOC_ENUM_FMT: c_ulong = ioc(RW, 2, std::mem::size_of::<v4l2_fmtdesc>());
const VIDIOC_S_FMT: c_ulong = ioc(RW, 5, std::mem::size_of::<v4l2_format>());
const VIDIOC_REQBUFS: c_ulong = ioc(RW, 8, std::mem::size_of::<v4l2_requestbuffers>());
const VIDIOC_QUERYBUF: c_ulong = ioc(RW, 9, std::mem::size_of::<v4l2_buffer>());
const VIDIOC_QBUF: c_ulong = ioc(RW, 15, std::mem::size_of::<v4l2_buffer>());
const VIDIOC_DQBUF: c_ulong = ioc(RW, 17, std::mem::size_of::<v4l2_buffer>());
const VIDIOC_STREAMON: c_ulong = ioc(W, 18, std::mem::size_of::<c_int>());
const VIDIOC_STREAMOFF: c_ulong = ioc(W, 19, std::mem::size_of::<c_int>());
const VIDIOC_G_PARM: c_ulong = ioc(RW, 21, std::mem::size_of::<v4l2_streamparm>());
const VIDIOC_S_PARM: c_ulong = ioc(RW, 22, std::mem::size_of::<v4l2_streamparm>());

unsafe fn xioctl<T>(fd: c_int, req: c_ulong, arg: *mut T) -> std::io::Result<()> {
    loop {
        if libc::ioctl(fd, req as _, arg as *mut c_void) != -1 {
            return Ok(());
        }
        let e = std::io::Error::last_os_error();
        if e.kind() != std::io::ErrorKind::Interrupted {
            return Err(e);
        }
    }
}

fn zeroed<T>() -> T {
    // SAFETY: only used for plain C structs where all-zero is valid.
    unsafe { std::mem::zeroed() }
}

fn cstr(b: &[u8]) -> String {
    let end = b.iter().position(|&c| c == 0).unwrap_or(b.len());
    String::from_utf8_lossy(&b[..end]).trim().to_string()
}

pub struct DeviceInfo {
    pub path: String,
    pub card: String,
    pub driver: String,
    pub bus: String,
    pub formats: Vec<u32>,
}

fn open(path: &str, nonblock: bool) -> Result<c_int> {
    let c = CString::new(path)?;
    let flags = libc::O_RDWR | libc::O_CLOEXEC | if nonblock { libc::O_NONBLOCK } else { 0 };
    // SAFETY: valid C string.
    let fd = unsafe { libc::open(c.as_ptr(), flags) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error()).with_context(|| format!("open {path}"));
    }
    Ok(fd)
}

/// Capabilities of `path` if it is a streaming video capture device.
pub fn probe(path: &str) -> Option<DeviceInfo> {
    let fd = open(path, true).ok()?;
    let mut cap: v4l2_capability = zeroed();
    // SAFETY: valid fd and struct.
    let ok = unsafe { xioctl(fd, VIDIOC_QUERYCAP, &mut cap) }.is_ok();
    let caps = if cap.capabilities & CAP_DEVICE_CAPS != 0 { cap.device_caps } else { cap.capabilities };
    let mut formats = Vec::new();
    if ok && caps & CAP_VIDEO_CAPTURE != 0 && caps & CAP_STREAMING != 0 {
        for index in 0..64 {
            let mut d: v4l2_fmtdesc = zeroed();
            d.index = index;
            d.type_ = BUF_TYPE_VIDEO_CAPTURE;
            // SAFETY: valid fd and struct.
            if unsafe { xioctl(fd, VIDIOC_ENUM_FMT, &mut d) }.is_err() {
                break;
            }
            formats.push(d.pixelformat);
        }
    }
    // SAFETY: fd owned here.
    unsafe { libc::close(fd) };
    if formats.is_empty() {
        return None;
    }
    Some(DeviceInfo {
        path: path.to_string(),
        card: cstr(&cap.card),
        driver: cstr(&cap.driver),
        bus: cstr(&cap.bus_info),
        formats,
    })
}

struct Mapping {
    ptr: *mut c_void,
    len: usize,
}

pub struct Capture {
    fd: c_int,
    bufs: Vec<Mapping>,
    pub width: usize,
    pub height: usize,
    pub pixfmt: u32,
    pub stride: usize,
    pub fps: (u32, u32),
    queued: Option<v4l2_buffer>,
}

// SAFETY: the fd and mappings are owned exclusively by this struct.
unsafe impl Send for Capture {}

pub struct Frame<'a> {
    pub data: &'a [u8],
    pub sequence: u32,
}

impl Capture {
    /// Open `path` and negotiate the closest mode to `width x height @ fps`
    /// using the first pixel format from `prefer` the device offers.
    pub fn open(path: &str, width: u32, height: u32, fps: f64, prefer: &[u32]) -> Result<Self> {
        let info = probe(path).with_context(|| format!("{path} is not a V4L2 capture device"))?;
        let fd = open(path, false)?;
        let mut cap = Capture { fd, bufs: Vec::new(), width: 0, height: 0, pixfmt: 0, stride: 0, fps: (0, 1), queued: None };

        // Take the first preferred format that delivers the requested size at
        // (nearly) the requested rate; otherwise the best size/rate match.
        let mut best: Option<(v4l2_pix_format, (u32, u32), (bool, u64))> = None;
        for &pf in prefer.iter().filter(|p| info.formats.contains(p)) {
            let fmt = cap.set_format(width, height, pf)?;
            let rate = cap.set_fps(fps);
            let got = rate.0 as f64 / rate.1.max(1) as f64;
            let size_ok = fmt.width == width && fmt.height == height;
            if size_ok && (fps <= 0.0 || got >= fps * 0.95) {
                best = Some((fmt, rate, (true, u64::MAX)));
                break;
            }
            let score = (size_ok, (got * 1000.0) as u64 * (fmt.width as u64 * fmt.height as u64));
            if best.as_ref().map_or(true, |b| score > b.2) {
                best = Some((fmt, rate, score));
            }
        }
        let Some((fmt, _, _)) = best else {
            bail!(
                "{path}: none of the supported formats {:?} offered (device has {:?})",
                prefer.iter().map(|f| fourcc_str(*f)).collect::<Vec<_>>(),
                info.formats.iter().map(|f| fourcc_str(*f)).collect::<Vec<_>>()
            );
        };
        // Re-apply the winner (later probes changed the device state).
        let fmt = cap.set_format(fmt.width, fmt.height, fmt.pixelformat)?;
        cap.width = fmt.width as usize;
        cap.height = fmt.height as usize;
        cap.pixfmt = fmt.pixelformat;
        cap.stride = if fmt.bytesperline > 0 { fmt.bytesperline as usize } else { cap.width * 2 };
        cap.fps = cap.set_fps(fps);
        cap.start(4)?;
        Ok(cap)
    }

    fn set_format(&mut self, width: u32, height: u32, pixfmt: u32) -> Result<v4l2_pix_format> {
        let mut f: v4l2_format = zeroed();
        f.type_ = BUF_TYPE_VIDEO_CAPTURE;
        f.pix.width = width;
        f.pix.height = height;
        f.pix.pixelformat = pixfmt;
        f.pix.field = FIELD_NONE;
        // SAFETY: valid fd and struct.
        unsafe { xioctl(self.fd, VIDIOC_S_FMT, &mut f) }.context("VIDIOC_S_FMT")?;
        Ok(f.pix)
    }

    fn set_fps(&mut self, fps: f64) -> (u32, u32) {
        let mut p: v4l2_streamparm = zeroed();
        p.type_ = BUF_TYPE_VIDEO_CAPTURE;
        if fps > 0.0 {
            // e.g. 29.97 -> 1001/30000
            let (num, den) = if (fps.fract() - 0.0).abs() < 1e-6 { (1, fps as u32) } else { (1001, (fps * 1001.0).round() as u32) };
            p.timeperframe = v4l2_fract { numerator: num, denominator: den };
            // SAFETY: valid fd and struct.
            let _ = unsafe { xioctl(self.fd, VIDIOC_S_PARM, &mut p) };
        }
        let mut g: v4l2_streamparm = zeroed();
        g.type_ = BUF_TYPE_VIDEO_CAPTURE;
        // SAFETY: valid fd and struct.
        if unsafe { xioctl(self.fd, VIDIOC_G_PARM, &mut g) }.is_ok() && g.timeperframe.numerator > 0 {
            // frame rate = 1 / timeperframe
            (g.timeperframe.denominator, g.timeperframe.numerator)
        } else {
            ((fps.max(1.0) * 1000.0) as u32, 1000)
        }
    }

    fn start(&mut self, count: u32) -> Result<()> {
        let mut req: v4l2_requestbuffers = zeroed();
        req.count = count;
        req.type_ = BUF_TYPE_VIDEO_CAPTURE;
        req.memory = MEMORY_MMAP;
        // SAFETY: valid fd and struct.
        unsafe { xioctl(self.fd, VIDIOC_REQBUFS, &mut req) }.context("VIDIOC_REQBUFS")?;
        if req.count == 0 {
            bail!("device returned no buffers");
        }
        for i in 0..req.count {
            let mut b = self.empty_buf();
            b.index = i;
            // SAFETY: valid fd and struct.
            unsafe { xioctl(self.fd, VIDIOC_QUERYBUF, &mut b) }.context("VIDIOC_QUERYBUF")?;
            // SAFETY: mapping a buffer the driver just described.
            let ptr = unsafe {
                libc::mmap(ptr::null_mut(), b.length as usize, libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, self.fd, b.m_offset as libc::off_t)
            };
            if ptr == libc::MAP_FAILED {
                return Err(std::io::Error::last_os_error()).context("mmap");
            }
            self.bufs.push(Mapping { ptr, len: b.length as usize });
            // SAFETY: valid fd and struct.
            unsafe { xioctl(self.fd, VIDIOC_QBUF, &mut b) }.context("VIDIOC_QBUF")?;
        }
        let mut t: c_int = BUF_TYPE_VIDEO_CAPTURE as c_int;
        // SAFETY: valid fd.
        unsafe { xioctl(self.fd, VIDIOC_STREAMON, &mut t) }.context("VIDIOC_STREAMON")?;
        Ok(())
    }

    fn empty_buf(&self) -> v4l2_buffer {
        let mut b: v4l2_buffer = zeroed();
        b.type_ = BUF_TYPE_VIDEO_CAPTURE;
        b.memory = MEMORY_MMAP;
        b
    }

    fn requeue(&mut self) {
        if let Some(mut b) = self.queued.take() {
            // SAFETY: returning a buffer we dequeued.
            let _ = unsafe { xioctl(self.fd, VIDIOC_QBUF, &mut b) };
        }
    }

    /// Wait up to `timeout` for the next frame. The previous frame's buffer is
    /// handed back to the driver first, so do not keep a [`Frame`] around.
    pub fn next(&mut self, timeout: Duration) -> Result<Option<Frame<'_>>> {
        self.requeue();
        let mut pfd = libc::pollfd { fd: self.fd, events: libc::POLLIN, revents: 0 };
        // SAFETY: one valid pollfd.
        let n = unsafe { libc::poll(&mut pfd, 1, timeout.as_millis() as c_int) };
        if n < 0 {
            let e = std::io::Error::last_os_error();
            if e.kind() == std::io::ErrorKind::Interrupted {
                return Ok(None);
            }
            return Err(e).context("poll");
        }
        if n == 0 {
            return Ok(None);
        }
        let mut b = self.empty_buf();
        // SAFETY: valid fd and struct.
        unsafe { xioctl(self.fd, VIDIOC_DQBUF, &mut b) }.context("VIDIOC_DQBUF (device unplugged?)")?;
        let m = &self.bufs[b.index as usize];
        let used = (b.bytesused as usize).min(m.len);
        self.queued = Some(b);
        // SAFETY: the mapping stays valid and untouched by the driver until requeued.
        let data = unsafe { std::slice::from_raw_parts(m.ptr as *const u8, used) };
        Ok(Some(Frame { data, sequence: b.sequence }))
    }
}

use std::ptr;

impl Drop for Capture {
    fn drop(&mut self) {
        let mut t: c_int = BUF_TYPE_VIDEO_CAPTURE as c_int;
        // SAFETY: owned fd and mappings.
        unsafe {
            let _ = xioctl(self.fd, VIDIOC_STREAMOFF, &mut t);
            for m in &self.bufs {
                libc::munmap(m.ptr, m.len);
            }
            libc::close(self.fd);
        }
    }
}

/// All V4L2 capture devices, sorted by node number.
pub fn list() -> Vec<(u32, DeviceInfo)> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir("/dev") else { return out };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if let Some(n) = name.strip_prefix("video").and_then(|s| s.parse::<u32>().ok()) {
            if let Some(info) = probe(&format!("/dev/{name}")) {
                out.push((n, info));
            }
        }
    }
    out.sort_by_key(|(n, _)| *n);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn struct_sizes_match_kernel_abi() {
        use std::mem::size_of;
        assert_eq!(size_of::<v4l2_capability>(), 104);
        assert_eq!(size_of::<v4l2_fmtdesc>(), 64);
        assert_eq!(size_of::<v4l2_format>(), 208);
        assert_eq!(size_of::<v4l2_requestbuffers>(), 20);
        assert_eq!(size_of::<v4l2_buffer>(), 88);
        assert_eq!(size_of::<v4l2_streamparm>(), 204);
        assert_eq!(VIDIOC_QUERYCAP, 0x80685600);
        assert_eq!(VIDIOC_S_FMT, 0xc0d05605);
        assert_eq!(VIDIOC_DQBUF, 0xc0585611);
        assert_eq!(fourcc_str(PIX_MJPG), "MJPG");
    }
}
