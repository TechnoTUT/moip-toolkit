//! Raw C ABI of the NDI SDK (subset), transcribed from the SDK headers
//! (`Processing.NDI.structs.h`, `Find.h`, `Recv.h`, `Send.h`). Only the
//! functions used by moip-toolkit are declared. The layout has been stable
//! since NDI 4 and matches NDI 6.

#![allow(non_camel_case_types, dead_code)]

use std::os::raw::{c_char, c_float, c_int};

pub const fn fourcc(a: u8, b: u8, c: u8, d: u8) -> u32 {
    (a as u32) | ((b as u32) << 8) | ((c as u32) << 16) | ((d as u32) << 24)
}

pub const FOURCC_UYVY: u32 = fourcc(b'U', b'Y', b'V', b'Y');
pub const FOURCC_UYVA: u32 = fourcc(b'U', b'Y', b'V', b'A');
pub const FOURCC_NV12: u32 = fourcc(b'N', b'V', b'1', b'2');
pub const FOURCC_I420: u32 = fourcc(b'I', b'4', b'2', b'0');
pub const FOURCC_BGRA: u32 = fourcc(b'B', b'G', b'R', b'A');
pub const FOURCC_BGRX: u32 = fourcc(b'B', b'G', b'R', b'X');
pub const FOURCC_RGBA: u32 = fourcc(b'R', b'G', b'B', b'A');
pub const FOURCC_RGBX: u32 = fourcc(b'R', b'G', b'B', b'X');
pub const FOURCC_FLTP: u32 = fourcc(b'F', b'L', b'T', b'p');
pub const FOURCC_YV12: u32 = fourcc(b'Y', b'V', b'1', b'2');

pub const FRAME_TYPE_NONE: c_int = 0;
pub const FRAME_TYPE_VIDEO: c_int = 1;
pub const FRAME_TYPE_AUDIO: c_int = 2;
pub const FRAME_TYPE_METADATA: c_int = 3;
pub const FRAME_TYPE_ERROR: c_int = 4;
pub const FRAME_TYPE_STATUS_CHANGE: c_int = 100;
pub const FRAME_TYPE_SOURCE_CHANGE: c_int = 101;

pub const FRAME_FORMAT_PROGRESSIVE: c_int = 1;

pub const RECV_BANDWIDTH_METADATA_ONLY: c_int = -10;
pub const RECV_BANDWIDTH_AUDIO_ONLY: c_int = 10;
pub const RECV_BANDWIDTH_LOWEST: c_int = 0;
pub const RECV_BANDWIDTH_HIGHEST: c_int = 100;

pub const RECV_COLOR_BGRX_BGRA: c_int = 0;
pub const RECV_COLOR_UYVY_BGRA: c_int = 1;
pub const RECV_COLOR_RGBX_RGBA: c_int = 2;
pub const RECV_COLOR_UYVY_RGBA: c_int = 3;
pub const RECV_COLOR_FASTEST: c_int = 100;
pub const RECV_COLOR_BEST: c_int = 101;

pub const SEND_TIMECODE_SYNTHESIZE: i64 = i64::MAX;
pub const RECV_TIMESTAMP_UNDEFINED: i64 = i64::MAX;

#[repr(C)]
pub struct NDIlib_find_instance_type {
    _p: [u8; 0],
}
#[repr(C)]
pub struct NDIlib_recv_instance_type {
    _p: [u8; 0],
}
#[repr(C)]
pub struct NDIlib_send_instance_type {
    _p: [u8; 0],
}
pub type NDIlib_find_instance_t = *mut NDIlib_find_instance_type;
pub type NDIlib_recv_instance_t = *mut NDIlib_recv_instance_type;
pub type NDIlib_send_instance_t = *mut NDIlib_send_instance_type;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct NDIlib_source_t {
    pub p_ndi_name: *const c_char,
    pub p_url_address: *const c_char,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct NDIlib_video_frame_v2_t {
    pub xres: c_int,
    pub yres: c_int,
    pub fourcc: u32,
    pub frame_rate_n: c_int,
    pub frame_rate_d: c_int,
    pub picture_aspect_ratio: c_float,
    pub frame_format_type: c_int,
    pub timecode: i64,
    pub p_data: *mut u8,
    /// line_stride_in_bytes (uncompressed) / data_size_in_bytes (compressed)
    pub line_stride_in_bytes: c_int,
    pub p_metadata: *const c_char,
    pub timestamp: i64,
}

impl Default for NDIlib_video_frame_v2_t {
    fn default() -> Self {
        Self {
            xres: 0,
            yres: 0,
            fourcc: FOURCC_UYVY,
            frame_rate_n: 30000,
            frame_rate_d: 1001,
            picture_aspect_ratio: 0.0,
            frame_format_type: FRAME_FORMAT_PROGRESSIVE,
            timecode: SEND_TIMECODE_SYNTHESIZE,
            p_data: std::ptr::null_mut(),
            line_stride_in_bytes: 0,
            p_metadata: std::ptr::null(),
            timestamp: 0,
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct NDIlib_audio_frame_v3_t {
    pub sample_rate: c_int,
    pub no_channels: c_int,
    pub no_samples: c_int,
    pub timecode: i64,
    pub fourcc: u32,
    pub p_data: *mut u8,
    /// channel_stride_in_bytes (FLTP) / data_size_in_bytes (compressed)
    pub channel_stride_in_bytes: c_int,
    pub p_metadata: *const c_char,
    pub timestamp: i64,
}

impl Default for NDIlib_audio_frame_v3_t {
    fn default() -> Self {
        Self {
            sample_rate: 48000,
            no_channels: 2,
            no_samples: 0,
            timecode: SEND_TIMECODE_SYNTHESIZE,
            fourcc: FOURCC_FLTP,
            p_data: std::ptr::null_mut(),
            channel_stride_in_bytes: 0,
            p_metadata: std::ptr::null(),
            timestamp: 0,
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct NDIlib_metadata_frame_t {
    pub length: c_int,
    pub timecode: i64,
    pub p_data: *mut c_char,
}

impl Default for NDIlib_metadata_frame_t {
    fn default() -> Self {
        Self { length: 0, timecode: 0, p_data: std::ptr::null_mut() }
    }
}

#[repr(C)]
pub struct NDIlib_find_create_t {
    pub show_local_sources: bool,
    pub p_groups: *const c_char,
    pub p_extra_ips: *const c_char,
}

#[repr(C)]
pub struct NDIlib_recv_create_v3_t {
    pub source_to_connect_to: NDIlib_source_t,
    pub color_format: c_int,
    pub bandwidth: c_int,
    pub allow_video_fields: bool,
    pub p_ndi_recv_name: *const c_char,
}

#[repr(C)]
#[derive(Default, Clone, Copy)]
pub struct NDIlib_recv_performance_t {
    pub video_frames: i64,
    pub audio_frames: i64,
    pub metadata_frames: i64,
}

#[repr(C)]
#[derive(Default, Clone, Copy)]
pub struct NDIlib_recv_queue_t {
    pub video_frames: c_int,
    pub audio_frames: c_int,
    pub metadata_frames: c_int,
}

#[repr(C)]
pub struct NDIlib_send_create_t {
    pub p_ndi_name: *const c_char,
    pub p_groups: *const c_char,
    pub clock_video: bool,
    pub clock_audio: bool,
}

/// Function table resolved from libndi at runtime.
pub struct Api {
    pub initialize: unsafe extern "C" fn() -> bool,
    pub destroy: unsafe extern "C" fn(),
    pub version: unsafe extern "C" fn() -> *const c_char,

    pub find_create_v2: unsafe extern "C" fn(*const NDIlib_find_create_t) -> NDIlib_find_instance_t,
    pub find_destroy: unsafe extern "C" fn(NDIlib_find_instance_t),
    pub find_get_current_sources:
        unsafe extern "C" fn(NDIlib_find_instance_t, *mut u32) -> *const NDIlib_source_t,
    pub find_wait_for_sources: unsafe extern "C" fn(NDIlib_find_instance_t, u32) -> bool,

    pub recv_create_v3: unsafe extern "C" fn(*const NDIlib_recv_create_v3_t) -> NDIlib_recv_instance_t,
    pub recv_destroy: unsafe extern "C" fn(NDIlib_recv_instance_t),
    pub recv_connect: unsafe extern "C" fn(NDIlib_recv_instance_t, *const NDIlib_source_t),
    pub recv_capture_v3: unsafe extern "C" fn(
        NDIlib_recv_instance_t,
        *mut NDIlib_video_frame_v2_t,
        *mut NDIlib_audio_frame_v3_t,
        *mut NDIlib_metadata_frame_t,
        u32,
    ) -> c_int,
    pub recv_free_video_v2: unsafe extern "C" fn(NDIlib_recv_instance_t, *const NDIlib_video_frame_v2_t),
    pub recv_free_audio_v3: unsafe extern "C" fn(NDIlib_recv_instance_t, *const NDIlib_audio_frame_v3_t),
    pub recv_free_metadata: unsafe extern "C" fn(NDIlib_recv_instance_t, *const NDIlib_metadata_frame_t),
    pub recv_get_performance: unsafe extern "C" fn(
        NDIlib_recv_instance_t,
        *mut NDIlib_recv_performance_t,
        *mut NDIlib_recv_performance_t,
    ),
    pub recv_get_queue: unsafe extern "C" fn(NDIlib_recv_instance_t, *mut NDIlib_recv_queue_t),
    pub recv_get_no_connections: unsafe extern "C" fn(NDIlib_recv_instance_t) -> c_int,

    pub send_create: unsafe extern "C" fn(*const NDIlib_send_create_t) -> NDIlib_send_instance_t,
    pub send_destroy: unsafe extern "C" fn(NDIlib_send_instance_t),
    pub send_send_video_v2: unsafe extern "C" fn(NDIlib_send_instance_t, *const NDIlib_video_frame_v2_t),
    pub send_send_audio_v3: unsafe extern "C" fn(NDIlib_send_instance_t, *const NDIlib_audio_frame_v3_t),
    pub send_get_no_connections: unsafe extern "C" fn(NDIlib_send_instance_t, u32) -> c_int,
}

impl Api {
    /// # Safety
    /// `lib` must be the NDI runtime; the declared signatures must match it.
    pub unsafe fn load(lib: &libloading::Library) -> Result<Self, libloading::Error> {
        macro_rules! sym {
            ($name:literal) => {
                *lib.get(concat!($name, "\0").as_bytes())?
            };
        }
        Ok(Self {
            initialize: sym!("NDIlib_initialize"),
            destroy: sym!("NDIlib_destroy"),
            version: sym!("NDIlib_version"),
            find_create_v2: sym!("NDIlib_find_create_v2"),
            find_destroy: sym!("NDIlib_find_destroy"),
            find_get_current_sources: sym!("NDIlib_find_get_current_sources"),
            find_wait_for_sources: sym!("NDIlib_find_wait_for_sources"),
            recv_create_v3: sym!("NDIlib_recv_create_v3"),
            recv_destroy: sym!("NDIlib_recv_destroy"),
            recv_connect: sym!("NDIlib_recv_connect"),
            recv_capture_v3: sym!("NDIlib_recv_capture_v3"),
            recv_free_video_v2: sym!("NDIlib_recv_free_video_v2"),
            recv_free_audio_v3: sym!("NDIlib_recv_free_audio_v3"),
            recv_free_metadata: sym!("NDIlib_recv_free_metadata"),
            recv_get_performance: sym!("NDIlib_recv_get_performance"),
            recv_get_queue: sym!("NDIlib_recv_get_queue"),
            recv_get_no_connections: sym!("NDIlib_recv_get_no_connections"),
            send_create: sym!("NDIlib_send_create"),
            send_destroy: sym!("NDIlib_send_destroy"),
            send_send_video_v2: sym!("NDIlib_send_send_video_v2"),
            send_send_audio_v3: sym!("NDIlib_send_send_audio_v3"),
            send_get_no_connections: sym!("NDIlib_send_get_no_connections"),
        })
    }
}
