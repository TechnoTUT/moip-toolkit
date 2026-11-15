//! SDL2 window + OpenGL 2.1 helpers shared by rx, multiview and signage.
//!
//! Video frames are drawn as textured quads. UYVY frames are uploaded as-is
//! (half-width RGBA texture, one texel per macropixel) and converted to RGB in a
//! fragment shader, which halves the upload size and skips any CPU colour
//! conversion.

use anyhow::{anyhow, Context as _, Result};
use glow::HasContext;
use sdl2::event::{Event, WindowEvent};
use sdl2::keyboard::Keycode;
use std::collections::HashMap;

use crate::font;

pub const VIDEO_DRIVERS: [&str; 4] = ["kmsdrm", "auto", "wayland", "x11"];

/// Default SDL video driver: render straight to the display (lowest latency).
pub const DEFAULT_VIDEO_DRIVER: &str = "kmsdrm";

pub enum WinEvent {
    Quit,
    Resized,
}

pub struct Window {
    pub gl: glow::Context,
    _ctx: sdl2::video::GLContext,
    window: sdl2::video::Window,
    _video: sdl2::VideoSubsystem,
    events: sdl2::EventPump,
    _sdl: sdl2::Sdl,
    pub width: u32,
    pub height: u32,
    pub driver: String,
}

impl Window {
    /// Open an OpenGL window. `video_driver` selects the SDL backend; "kmsdrm"
    /// renders straight to the display without a compositor (always fullscreen).
    ///
    /// If KMS/DRM is unavailable (e.g. a desktop session owns the display),
    /// falls back to SDL's automatic choice (Wayland / X11) with a warning.
    pub fn open(title: &str, width: u32, height: u32, fullscreen: bool, video_driver: Option<&str>) -> Result<Self> {
        let driver = video_driver.filter(|d| !d.is_empty() && *d != "auto");
        let win = match Self::open_with(title, width, height, fullscreen, driver) {
            Err(e) if driver == Some("kmsdrm") => {
                crate::log!("[SDL] kmsdrm unavailable ({e:#}); falling back to the desktop (use --video-driver auto to skip kmsdrm)");
                Self::open_with(title, width, height, fullscreen, None)?
            }
            r => r?,
        };
        // SDL's automatic choice ends at an invisible driver when there is no
        // display at all; treat that as an error instead of showing nothing.
        if driver != Some(win.driver.as_str()) && matches!(win.driver.as_str(), "offscreen" | "dummy" | "evdev") {
            anyhow::bail!(
                "no display available (SDL picked \"{}\"): the display is in use by another process, or no desktop session is reachable (set WAYLAND_DISPLAY / DISPLAY)",
                win.driver
            );
        }
        Ok(win)
    }

    fn open_with(title: &str, width: u32, height: u32, fullscreen: bool, driver: Option<&str>) -> Result<Self> {
        match driver {
            Some(d) => std::env::set_var("SDL_VIDEODRIVER", d),
            None => std::env::remove_var("SDL_VIDEODRIVER"),
        }
        // One queued frame fewer between swap and scan-out where supported (KMSDRM).
        sdl2::hint::set("SDL_VIDEO_DOUBLE_BUFFER", "1");
        sdl2::hint::set("SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR", "1");

        let sdl = sdl2::init().map_err(|e| anyhow!("SDL_Init: {e}"))?;
        let video = sdl.video().map_err(|e| anyhow!("SDL video: {e}"))?;
        let driver = video.current_video_driver().to_lowercase();
        crate::log!("[SDL] video driver: {driver}");
        let fullscreen = fullscreen || driver == "kmsdrm";

        let attr = video.gl_attr();
        attr.set_context_version(2, 1);
        attr.set_double_buffer(true);

        let mut wb = video.window(title, width, height);
        wb.opengl().resizable().position_centered();
        if fullscreen {
            wb.fullscreen_desktop();
        }
        let window = wb.build().context("SDL_CreateWindow")?;
        let ctx = window.gl_create_context().map_err(|e| anyhow!("GL context: {e}"))?;
        window.gl_make_current(&ctx).map_err(|e| anyhow!("GL make current: {e}"))?;
        // No vsync wait: present as soon as a frame is drawn (lowest latency).
        if video.gl_set_swap_interval(sdl2::video::SwapInterval::Immediate).is_err() {
            let _ = video.gl_set_swap_interval(sdl2::video::SwapInterval::VSync);
        }
        sdl.mouse().show_cursor(false);

        // SAFETY: the context is current on this thread.
        let gl = unsafe {
            glow::Context::from_loader_function(|s| video.gl_get_proc_address(s) as *const _)
        };
        let events = sdl.event_pump().map_err(|e| anyhow!("event pump: {e}"))?;
        let (w, h) = window.drawable_size();
        let win = Self { gl, _ctx: ctx, window, _video: video, events, _sdl: sdl, width: w, height: h, driver };
        win.viewport();
        Ok(win)
    }

    fn viewport(&self) {
        // SAFETY: context current.
        unsafe { self.gl.viewport(0, 0, self.width as i32, self.height as i32) }
    }

    pub fn poll(&mut self) -> Vec<WinEvent> {
        let mut out = Vec::new();
        for ev in self.events.poll_iter() {
            match ev {
                Event::Quit { .. } | Event::KeyDown { keycode: Some(Keycode::Escape), .. } => {
                    out.push(WinEvent::Quit)
                }
                Event::Window { win_event: WindowEvent::SizeChanged(..) | WindowEvent::Resized(..), .. } => {
                    out.push(WinEvent::Resized)
                }
                _ => {}
            }
        }
        if out.iter().any(|e| matches!(e, WinEvent::Resized)) {
            let (w, h) = self.window.drawable_size();
            self.width = w;
            self.height = h;
            self.viewport();
        }
        out
    }

    pub fn swap(&self) {
        self.window.gl_swap_window();
    }

    pub fn clear(&self, r: f32, g: f32, b: f32) {
        // SAFETY: context current.
        unsafe {
            self.gl.clear_color(r, g, b, 1.0);
            self.gl.clear(glow::COLOR_BUFFER_BIT);
        }
    }
}

/// Rectangle in normalised device coordinates: (x0, y0) bottom-left, (x1, y1) top-right.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rect {
    pub x0: f32,
    pub y0: f32,
    pub x1: f32,
    pub y1: f32,
}

impl Rect {
    pub const FULL: Rect = Rect { x0: -1.0, y0: -1.0, x1: 1.0, y1: 1.0 };

    /// Largest rect with aspect `src_w:src_h` centred inside `self`
    /// (`self` spans `area_w x area_h` pixels).
    pub fn letterbox(self, src_w: f32, src_h: f32, area_w: f32, area_h: f32) -> Rect {
        let (cx, cy) = ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0);
        let (hw, hh) = ((self.x1 - self.x0) / 2.0, (self.y1 - self.y0) / 2.0);
        let src = src_w / src_h.max(1.0);
        let dst = area_w / area_h.max(1.0);
        let (sx, sy) = if src > dst { (1.0, dst / src) } else { (src / dst, 1.0) };
        Rect { x0: cx - hw * sx, y0: cy - hh * sy, x1: cx + hw * sx, y1: cy + hh * sy }
    }

    /// Rect of `w x h` pixels whose top-left corner is `(px, py)` pixels from
    /// the window's top-left.
    pub fn from_pixels(px: f32, py: f32, w: f32, h: f32, win_w: f32, win_h: f32) -> Rect {
        let x0 = -1.0 + px / win_w * 2.0;
        let y1 = 1.0 - py / win_h * 2.0;
        Rect { x0, y0: y1 - h / win_h * 2.0, x1: x0 + w / win_w * 2.0, y1 }
    }
}

const VERT: &str = r#"#version 110
attribute vec2 a_pos;
uniform vec4 u_rect;
varying vec2 v_tc;
void main() {
    v_tc = vec2(a_pos.x, 1.0 - a_pos.y);
    gl_Position = vec4(mix(u_rect.xy, u_rect.zw, a_pos), 0.0, 1.0);
}
"#;

// UYVY 4:2:2 uploaded as an RGBA texture of half width: each texel is one
// macropixel (r=U, g=Y0, b=V, a=Y1). Sampled with GL_LINEAR exactly at
// macropixel centres horizontally, so the hardware only filters vertically;
// horizontal interpolation is done per luma pixel here.
const FRAG_UYVY: &str = r#"#version 110
uniform sampler2D tex;
uniform vec2 size;   // frame size in luma pixels
uniform vec4 coef;   // (Rv, Gu, Gv, Bu) of the YCbCr -> RGB matrix
varying vec2 v_tc;
void main() {
    float half_w = size.x * 0.5;
    float p = v_tc.x * size.x - 0.5;
    float i = floor(p);
    float f = p - i;
    float x0 = clamp(i, 0.0, size.x - 1.0);
    float x1 = clamp(i + 1.0, 0.0, size.x - 1.0);
    float m0 = floor(x0 * 0.5);
    float m1 = floor(x1 * 0.5);
    vec4 t0 = texture2D(tex, vec2((m0 + 0.5) / half_w, v_tc.y));
    vec4 t1 = texture2D(tex, vec2((m1 + 0.5) / half_w, v_tc.y));
    float y0 = mix(t0.g, t0.a, x0 - m0 * 2.0);
    float y1 = mix(t1.g, t1.a, x1 - m1 * 2.0);
    float y = (mix(y0, y1, f) - 0.0627451) * 1.1643836;
    vec2 uv = mix(t0.rb, t1.rb, f) - 0.5;
    gl_FragColor = vec4(y + coef.x * uv.y, y - coef.y * uv.x - coef.z * uv.y, y + coef.w * uv.x, 1.0);
}
"#;

const FRAG_TEX: &str = r#"#version 110
uniform sampler2D tex;
uniform float opaque;
varying vec2 v_tc;
void main() {
    vec4 c = texture2D(tex, v_tc);
    gl_FragColor = vec4(c.rgb, max(c.a, opaque));
}
"#;

const FRAG_SOLID: &str = r#"#version 110
uniform vec4 color;
void main() { gl_FragColor = color; }
"#;

// NDI uses BT.709 for HD and BT.601 for SD (< 720 lines).
const COEF_BT709: [f32; 4] = [1.792_741_1, 0.213_248_6, 0.532_909_3, 2.112_401_8];
const COEF_BT601: [f32; 4] = [1.596_026_8, 0.391_762_3, 0.812_967_6, 2.017_232_1];

struct Program {
    id: glow::Program,
    rect: Option<glow::UniformLocation>,
    u: HashMap<&'static str, glow::UniformLocation>,
}

unsafe fn compile(gl: &glow::Context, frag: &str, uniforms: &[&'static str]) -> Result<Program> {
    let prog = gl.create_program().map_err(|e| anyhow!(e))?;
    let mut shaders = Vec::new();
    for (ty, src) in [(glow::VERTEX_SHADER, VERT), (glow::FRAGMENT_SHADER, frag)] {
        let s = gl.create_shader(ty).map_err(|e| anyhow!(e))?;
        gl.shader_source(s, src);
        gl.compile_shader(s);
        if !gl.get_shader_compile_status(s) {
            return Err(anyhow!("shader compile: {}", gl.get_shader_info_log(s)));
        }
        gl.attach_shader(prog, s);
        shaders.push(s);
    }
    gl.bind_attrib_location(prog, 0, "a_pos");
    gl.link_program(prog);
    if !gl.get_program_link_status(prog) {
        return Err(anyhow!("program link: {}", gl.get_program_info_log(prog)));
    }
    for s in shaders {
        gl.detach_shader(prog, s);
        gl.delete_shader(s);
    }
    gl.use_program(Some(prog));
    if let Some(t) = gl.get_uniform_location(prog, "tex") {
        gl.uniform_1_i32(Some(&t), 0);
    }
    let mut u = HashMap::new();
    for name in uniforms {
        if let Some(loc) = gl.get_uniform_location(prog, name) {
            u.insert(*name, loc);
        }
    }
    let rect = gl.get_uniform_location(prog, "u_rect");
    gl.use_program(None);
    Ok(Program { id: prog, rect, u })
}

/// A GL texture that is re-allocated only when its size or format changes.
pub struct Texture {
    id: glow::Texture,
    key: (i32, i32, u32),
    pub width: usize,
    pub height: usize,
}

impl Texture {
    pub fn new(gl: &glow::Context, filter: u32) -> Result<Self> {
        // SAFETY: context current.
        unsafe {
            let id = gl.create_texture().map_err(|e| anyhow!(e))?;
            gl.bind_texture(glow::TEXTURE_2D, Some(id));
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, filter as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, filter as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
            Ok(Self { id, key: (0, 0, 0), width: 0, height: 0 })
        }
    }

    /// Upload 4-byte texels; `row_texels` is the source row length in texels.
    pub fn upload(&mut self, gl: &glow::Context, w: usize, h: usize, format: u32, row_texels: usize, data: &[u8]) -> bool {
        if w == 0 || h == 0 || data.len() < row_texels * 4 * (h - 1) + w * 4 {
            return false;
        }
        let key = (w as i32, h as i32, format);
        // SAFETY: context current; data length checked above.
        unsafe {
            gl.bind_texture(glow::TEXTURE_2D, Some(self.id));
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            gl.pixel_store_i32(glow::UNPACK_ROW_LENGTH, if row_texels != w { row_texels as i32 } else { 0 });
            if key != self.key {
                gl.tex_image_2d(
                    glow::TEXTURE_2D, 0, glow::RGBA8 as i32, key.0, key.1, 0,
                    format, glow::UNSIGNED_BYTE, glow::PixelUnpackData::Slice(Some(data)),
                );
                self.key = key;
            } else {
                gl.tex_sub_image_2d(
                    glow::TEXTURE_2D, 0, 0, 0, key.0, key.1,
                    format, glow::UNSIGNED_BYTE, glow::PixelUnpackData::Slice(Some(data)),
                );
            }
            gl.pixel_store_i32(glow::UNPACK_ROW_LENGTH, 0);
        }
        self.width = w;
        self.height = h;
        true
    }

    pub fn delete(&self, gl: &glow::Context) {
        // SAFETY: context current.
        unsafe { gl.delete_texture(self.id) }
    }
}

/// What a [`VideoTexture`] currently holds.
#[derive(Clone, Copy, PartialEq)]
enum Content {
    None,
    Uyvy,
    Rgb,
}

/// A video frame texture plus the frame's pixel size and layout.
pub struct VideoTexture {
    tex: Texture,
    content: Content,
    pub frame_w: usize,
    pub frame_h: usize,
}

impl VideoTexture {
    pub fn new(gl: &glow::Context) -> Result<Self> {
        Ok(Self { tex: Texture::new(gl, glow::LINEAR)?, content: Content::None, frame_w: 0, frame_h: 0 })
    }

    pub fn has_frame(&self) -> bool {
        self.content != Content::None
    }

    pub fn clear(&mut self) {
        self.content = Content::None;
    }

    /// Upload an NDI frame (UYVY, RGBA/RGBX or BGRA/BGRX). Returns false for
    /// unsupported or malformed frames.
    pub fn upload(&mut self, gl: &glow::Context, f: &ndi::VideoFrame) -> bool {
        let (w, h, stride) = (f.width(), f.height(), f.stride());
        let (tex_w, format, content) = match f.fourcc() {
            ndi::FOURCC_UYVY => (w / 2, glow::RGBA, Content::Uyvy),
            ndi::FOURCC_RGBA | ndi::FOURCC_RGBX => (w, glow::RGBA, Content::Rgb),
            ndi::FOURCC_BGRA | ndi::FOURCC_BGRX => (w, glow::BGRA, Content::Rgb),
            _ => return false,
        };
        let row = if stride > 0 { stride / 4 } else { tex_w };
        if !self.tex.upload(gl, tex_w, h, format, row, f.data()) {
            return false;
        }
        self.content = content;
        self.frame_w = w;
        self.frame_h = h;
        true
    }

    /// Upload a tightly packed RGBA image (signage).
    pub fn upload_rgba(&mut self, gl: &glow::Context, w: usize, h: usize, rgba: &[u8]) -> bool {
        if !self.tex.upload(gl, w, h, glow::RGBA, w, rgba) {
            return false;
        }
        self.content = Content::Rgb;
        self.frame_w = w;
        self.frame_h = h;
        true
    }

    pub fn delete(&self, gl: &glow::Context) {
        self.tex.delete(gl);
    }
}

/// Shaders, a unit quad and cached text labels.
pub struct Painter {
    uyvy: Option<Program>,
    tex: Program,
    solid: Program,
    _vbo: glow::Buffer,
    labels: HashMap<String, Texture>,
}

impl Painter {
    pub fn new(gl: &glow::Context) -> Result<Self> {
        // SAFETY: context current.
        unsafe {
            let uyvy = match compile(gl, FRAG_UYVY, &["size", "coef"]) {
                Ok(p) => Some(p),
                Err(e) => {
                    crate::log!("[GL] UYVY shader unavailable, receiving RGB instead: {e}");
                    None
                }
            };
            let tex = compile(gl, FRAG_TEX, &["opaque"])?;
            let solid = compile(gl, FRAG_SOLID, &["color"])?;
            let vbo = gl.create_buffer().map_err(|e| anyhow!(e))?;
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            let quad: [f32; 8] = [0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 1.0];
            let bytes: Vec<u8> = quad.iter().flat_map(|v| v.to_ne_bytes()).collect();
            gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, &bytes, glow::STATIC_DRAW);
            gl.enable_vertex_attrib_array(0);
            gl.vertex_attrib_pointer_f32(0, 2, glow::FLOAT, false, 8, 0);
            gl.blend_func(glow::SRC_ALPHA, glow::ONE_MINUS_SRC_ALPHA);
            Ok(Self { uyvy, tex, solid, _vbo: vbo, labels: HashMap::new() })
        }
    }

    pub fn supports_uyvy(&self) -> bool {
        self.uyvy.is_some()
    }

    unsafe fn quad(gl: &glow::Context, p: &Program, r: Rect) {
        gl.uniform_4_f32(p.rect.as_ref(), r.x0, r.y0, r.x1, r.y1);
        gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    }

    /// Draw a video texture into `area` (NDC), letterboxed; `area_px` is its size in pixels.
    pub fn draw_video(&self, gl: &glow::Context, v: &VideoTexture, area: Rect, area_px: (f32, f32)) {
        if !v.has_frame() {
            return;
        }
        let r = area.letterbox(v.frame_w as f32, v.frame_h as f32, area_px.0, area_px.1);
        // SAFETY: context current.
        unsafe {
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(v.tex.id));
            match (v.content, &self.uyvy) {
                (Content::Uyvy, Some(p)) => {
                    gl.use_program(Some(p.id));
                    gl.uniform_2_f32(p.u.get("size"), v.frame_w as f32, v.frame_h as f32);
                    let c = if v.frame_h < 720 { COEF_BT601 } else { COEF_BT709 };
                    gl.uniform_4_f32(p.u.get("coef"), c[0], c[1], c[2], c[3]);
                    Self::quad(gl, p, r);
                }
                (Content::Rgb, _) => {
                    gl.use_program(Some(self.tex.id));
                    gl.uniform_1_f32(self.tex.u.get("opaque"), 1.0);
                    Self::quad(gl, &self.tex, r);
                }
                _ => {}
            }
            gl.use_program(None);
        }
    }

    pub fn fill(&self, gl: &glow::Context, r: Rect, color: [f32; 4]) {
        // SAFETY: context current.
        unsafe {
            gl.enable(glow::BLEND);
            gl.use_program(Some(self.solid.id));
            gl.uniform_4_f32(self.solid.u.get("color"), color[0], color[1], color[2], color[3]);
            Self::quad(gl, &self.solid, r);
            gl.use_program(None);
            gl.disable(glow::BLEND);
        }
    }

    /// Size in pixels of a label rendered at `scale`.
    pub fn label_size(text: &str, scale: usize) -> (f32, f32) {
        let n = text.chars().count();
        (((n * 6 + 6) * scale) as f32, (18 * scale) as f32)
    }

    /// Draw a text label with its top-left corner at pixel `(px, py)`.
    pub fn label(&mut self, gl: &glow::Context, text: &str, scale: usize, px: f32, py: f32, win: (f32, f32)) {
        let key = format!("{scale}\u{1}{text}");
        if !self.labels.contains_key(&key) {
            if self.labels.len() > 64 {
                for t in self.labels.values() {
                    t.delete(gl);
                }
                self.labels.clear();
            }
            let img = font::render(text, scale, [255, 255, 255, 255], [20, 24, 33, 220]);
            let Ok(mut t) = Texture::new(gl, glow::NEAREST) else { return };
            t.upload(gl, img.width, img.height, glow::RGBA, img.width, &img.rgba);
            self.labels.insert(key.clone(), t);
        }
        let t = &self.labels[&key];
        let r = Rect::from_pixels(px, py, t.width as f32, t.height as f32, win.0, win.1);
        // SAFETY: context current.
        unsafe {
            gl.enable(glow::BLEND);
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(t.id));
            gl.use_program(Some(self.tex.id));
            gl.uniform_1_f32(self.tex.u.get("opaque"), 0.0);
            Self::quad(gl, &self.tex, r);
            gl.use_program(None);
            gl.disable(glow::BLEND);
        }
    }
}

/// Primary local IPv4 address (the one used for the default route).
pub fn local_ip() -> String {
    std::net::UdpSocket::bind("0.0.0.0:0")
        .and_then(|s| {
            s.connect("8.8.8.8:80")?;
            s.local_addr()
        })
        .map(|a| a.ip().to_string())
        .unwrap_or_else(|_| "127.0.0.1".into())
}

#[cfg(test)]
mod tests {
    use super::Rect;

    #[test]
    fn letterbox_wide_source_in_square_area() {
        let r = Rect::FULL.letterbox(1920.0, 1080.0, 1000.0, 1000.0);
        assert!((r.x0 + 1.0).abs() < 1e-6 && (r.x1 - 1.0).abs() < 1e-6);
        assert!((r.y1 - 0.5625).abs() < 1e-4);
    }

    #[test]
    fn pixel_rect_top_left() {
        let r = Rect::from_pixels(0.0, 0.0, 100.0, 50.0, 1000.0, 500.0);
        assert_eq!((r.x0, r.y1), (-1.0, 1.0));
        assert!((r.x1 - -0.8).abs() < 1e-6 && (r.y0 - 0.8).abs() < 1e-6);
    }
}
