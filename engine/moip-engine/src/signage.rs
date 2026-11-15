//! `moip-engine signage`: show a still image fullscreen (SDL2/OpenGL, or the
//! Linux framebuffer for consoles without DRM/GL).

use crate::control::{emit, Command, Control};
use crate::gfx::{self, Painter, Rect, VideoTexture, WinEvent, Window};
use anyhow::{bail, Context, Result};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

#[derive(clap::Args, Debug)]
pub struct Args {
    /// Image file (png, jpeg, webp, bmp).
    pub image: PathBuf,
    #[arg(short, long)]
    pub fullscreen: bool,
    #[arg(long, default_value = gfx::DEFAULT_VIDEO_DRIVER, value_parser = clap::builder::PossibleValuesParser::new(gfx::VIDEO_DRIVERS))]
    pub video_driver: String,
    /// Draw to a Linux framebuffer device instead of SDL/OpenGL.
    #[arg(long)]
    pub framebuffer: bool,
    #[arg(long, default_value = "/dev/fb0")]
    pub fb_device: String,
}

pub struct Loaded {
    pub path: PathBuf,
    pub width: usize,
    pub height: usize,
    pub rgba: Vec<u8>,
}

pub fn load(path: &Path) -> Result<Loaded> {
    let img = image::open(path).with_context(|| format!("load image {}", path.display()))?.into_rgba8();
    let (w, h) = img.dimensions();
    Ok(Loaded { path: path.to_path_buf(), width: w as usize, height: h as usize, rgba: img.into_raw() })
}

#[derive(Serialize)]
struct Status<'a> {
    r#type: &'static str,
    current_image: String,
    filename: Option<String>,
    fullscreen: bool,
    width: usize,
    height: usize,
    output: &'a str,
}

fn status(img: &Loaded, fullscreen: bool, output: &str) {
    emit(&Status {
        r#type: "status",
        current_image: img.path.display().to_string(),
        filename: img.path.file_name().map(|n| n.to_string_lossy().into_owned()),
        fullscreen,
        width: img.width,
        height: img.height,
        output,
    });
}

pub fn run(args: Args, control: bool) -> Result<()> {
    let ctl = Control::new(control);
    let img = load(&args.image)?;
    if args.framebuffer {
        return run_fb(&args, &ctl, img);
    }
    let mut win = Window::open("Signage", 1280, 720, args.fullscreen, Some(&args.video_driver))?;
    let fullscreen = args.fullscreen || win.driver == "kmsdrm";
    let painter = Painter::new(&win.gl)?;
    let mut tex = VideoTexture::new(&win.gl)?;
    let mut img = img;
    tex.upload_rgba(&win.gl, img.width, img.height, &img.rgba);
    let mut last_status = Instant::now() - Duration::from_secs(10);

    'main: loop {
        for ev in win.poll() {
            if let WinEvent::Quit = ev {
                break 'main;
            }
        }
        while let Some(cmd) = ctl.poll() {
            match cmd {
                Command::Image { path } => match load(Path::new(&path)) {
                    Ok(new) => {
                        crate::log!("[signage] showing {path}");
                        img = new;
                        tex.upload_rgba(&win.gl, img.width, img.height, &img.rgba);
                        last_status = Instant::now() - Duration::from_secs(10);
                    }
                    Err(e) => crate::control::emit_error(&format!("{e:#}")),
                },
                Command::Quit => break 'main,
                _ => {}
            }
        }
        // Redraw continuously at a low rate: KMS/DRM needs a live swap chain.
        win.clear(0.0, 0.0, 0.0);
        painter.draw_video(&win.gl, &tex, Rect::FULL, (win.width as f32, win.height as f32));
        win.swap();
        if ctl.enabled() && last_status.elapsed() > Duration::from_secs(2) {
            last_status = Instant::now();
            status(&img, fullscreen, &win.driver);
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    tex.delete(&win.gl);
    Ok(())
}

// ---------------------------------------------------------------------------
// Linux framebuffer output
// ---------------------------------------------------------------------------

const FBIOGET_VSCREENINFO: libc::c_ulong = 0x4600;
const FBIOGET_FSCREENINFO: libc::c_ulong = 0x4602;

#[repr(C)]
#[derive(Clone, Copy)]
struct FbBitfield {
    offset: u32,
    length: u32,
    msb_right: u32,
}

#[repr(C)]
struct FbVarScreeninfo {
    xres: u32,
    yres: u32,
    xres_virtual: u32,
    yres_virtual: u32,
    xoffset: u32,
    yoffset: u32,
    bits_per_pixel: u32,
    grayscale: u32,
    red: FbBitfield,
    green: FbBitfield,
    blue: FbBitfield,
    transp: FbBitfield,
    _rest: [u32; 16],
}

#[repr(C)]
struct FbFixScreeninfo {
    id: [u8; 16],
    smem_start: libc::c_ulong,
    smem_len: u32,
    type_: u32,
    type_aux: u32,
    visual: u32,
    xpanstep: u16,
    ypanstep: u16,
    ywrapstep: u16,
    line_length: u32,
    mmio_start: libc::c_ulong,
    mmio_len: u32,
    accel: u32,
    capabilities: u16,
    reserved: [u16; 2],
}

struct Framebuffer {
    fd: libc::c_int,
    mem: *mut u8,
    len: usize,
    width: usize,
    height: usize,
    bpp: usize,
    stride: usize,
    shifts: [u32; 3],
    lens: [u32; 3],
}

impl Framebuffer {
    fn open(dev: &str) -> Result<Self> {
        let c = std::ffi::CString::new(dev)?;
        // SAFETY: ioctl/mmap on an fd we own; structs are plain C data.
        unsafe {
            let fd = libc::open(c.as_ptr(), libc::O_RDWR | libc::O_CLOEXEC);
            if fd < 0 {
                return Err(std::io::Error::last_os_error()).with_context(|| format!("open {dev}"));
            }
            let mut v: FbVarScreeninfo = std::mem::zeroed();
            let mut f: FbFixScreeninfo = std::mem::zeroed();
            if libc::ioctl(fd, FBIOGET_VSCREENINFO as _, &mut v) < 0 || libc::ioctl(fd, FBIOGET_FSCREENINFO as _, &mut f) < 0 {
                libc::close(fd);
                bail!("{dev}: not a framebuffer device");
            }
            let bpp = v.bits_per_pixel as usize;
            if bpp != 32 && bpp != 24 && bpp != 16 {
                libc::close(fd);
                bail!("{dev}: unsupported {bpp} bits per pixel");
            }
            let len = f.smem_len as usize;
            let mem = libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ | libc::PROT_WRITE, libc::MAP_SHARED, fd, 0);
            if mem == libc::MAP_FAILED {
                libc::close(fd);
                return Err(std::io::Error::last_os_error()).context("mmap framebuffer");
            }
            Ok(Self {
                fd,
                mem: mem as *mut u8,
                len,
                width: v.xres as usize,
                height: v.yres as usize,
                bpp: bpp / 8,
                stride: f.line_length as usize,
                shifts: [v.red.offset, v.green.offset, v.blue.offset],
                lens: [v.red.length, v.green.length, v.blue.length],
            })
        }
    }

    /// Draw `img` letterboxed (nearest-neighbour scaling) on black.
    fn draw(&mut self, img: &Loaded) {
        let (fw, fh) = (self.width, self.height);
        let r = Rect::FULL.letterbox(img.width as f32, img.height as f32, fw as f32, fh as f32);
        let dw = ((r.x1 - r.x0) / 2.0 * fw as f32) as usize;
        let dh = ((r.y1 - r.y0) / 2.0 * fh as f32) as usize;
        let (ox, oy) = ((fw - dw.min(fw)) / 2, (fh - dh.min(fh)) / 2);
        let mut row = vec![0u8; self.stride];
        for y in 0..fh {
            row.fill(0);
            if y >= oy && y < oy + dh {
                let sy = (y - oy) * img.height / dh.max(1);
                for x in ox..(ox + dw).min(fw) {
                    let sx = (x - ox) * img.width / dw.max(1);
                    let p = &img.rgba[(sy * img.width + sx) * 4..(sy * img.width + sx) * 4 + 4];
                    let a = p[3] as u32;
                    let mut px = 0u32;
                    for c in 0..3 {
                        let v = p[c] as u32 * a / 255;
                        px |= (v >> (8 - self.lens[c].min(8))) << self.shifts[c];
                    }
                    let o = x * self.bpp;
                    row[o..o + self.bpp].copy_from_slice(&px.to_le_bytes()[..self.bpp]);
                }
            }
            let off = y * self.stride;
            if off + self.stride <= self.len {
                // SAFETY: within the mapped framebuffer.
                unsafe { std::ptr::copy_nonoverlapping(row.as_ptr(), self.mem.add(off), self.stride) };
            }
        }
    }
}

impl Drop for Framebuffer {
    fn drop(&mut self) {
        // SAFETY: owned mapping and fd.
        unsafe {
            libc::munmap(self.mem as *mut libc::c_void, self.len);
            libc::close(self.fd);
        }
    }
}

fn run_fb(args: &Args, ctl: &Control, mut img: Loaded) -> Result<()> {
    let mut fb = Framebuffer::open(&args.fb_device)?;
    crate::log!("[signage] framebuffer {}: {}x{} {} bpp", args.fb_device, fb.width, fb.height, fb.bpp * 8);
    fb.draw(&img);
    let mut last_status = Instant::now() - Duration::from_secs(10);
    loop {
        match ctl.wait(Duration::from_millis(500)) {
            Some(Command::Image { path }) => match load(Path::new(&path)) {
                Ok(new) => {
                    img = new;
                    fb.draw(&img);
                    last_status = Instant::now() - Duration::from_secs(10);
                }
                Err(e) => crate::control::emit_error(&format!("{e:#}")),
            },
            Some(Command::Quit) => return Ok(()),
            _ => {}
        }
        if ctl.enabled() && last_status.elapsed() > Duration::from_secs(2) {
            last_status = Instant::now();
            status(&img, true, "framebuffer");
        }
    }
}
