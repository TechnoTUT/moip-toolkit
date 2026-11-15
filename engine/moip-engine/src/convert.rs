//! Pixel format conversions into UYVY (NDI's native format).

/// YUYV (Y0 U Y1 V) -> UYVY (U Y0 V Y1). `src_stride`/`dst_stride` in bytes.
pub fn yuyv_to_uyvy(src: &[u8], src_stride: usize, dst: &mut [u8], dst_stride: usize, width: usize, height: usize) {
    let row = width * 2;
    for y in 0..height {
        let s = &src[y * src_stride..y * src_stride + row];
        let d = &mut dst[y * dst_stride..y * dst_stride + row];
        for (si, di) in s.chunks_exact(4).zip(d.chunks_exact_mut(4)) {
            let x = u32::from_le_bytes([si[0], si[1], si[2], si[3]]);
            let v = ((x >> 8) & 0x00FF_00FF) | ((x << 8) & 0xFF00_FF00);
            di.copy_from_slice(&v.to_le_bytes());
        }
    }
}

/// Copy rows of an already-UYVY image.
pub fn copy_rows(src: &[u8], src_stride: usize, dst: &mut [u8], dst_stride: usize, row_bytes: usize, height: usize) {
    for y in 0..height {
        dst[y * dst_stride..y * dst_stride + row_bytes].copy_from_slice(&src[y * src_stride..y * src_stride + row_bytes]);
    }
}

/// Interleaved full-resolution YCbCr (3 bytes/pixel) -> UYVY, averaging chroma
/// of each horizontal pixel pair.
pub fn ycbcr444_to_uyvy(src: &[u8], dst: &mut [u8], width: usize, height: usize) {
    let w2 = width & !1;
    for y in 0..height {
        let s = &src[y * width * 3..(y + 1) * width * 3];
        let d = &mut dst[y * width * 2..(y + 1) * width * 2];
        for x in (0..w2).step_by(2) {
            let p0 = &s[x * 3..x * 3 + 3];
            let p1 = &s[x * 3 + 3..x * 3 + 6];
            let o = &mut d[x * 2..x * 2 + 4];
            o[0] = ((p0[1] as u16 + p1[1] as u16 + 1) >> 1) as u8;
            o[1] = p0[0];
            o[2] = ((p0[2] as u16 + p1[2] as u16 + 1) >> 1) as u8;
            o[3] = p1[0];
        }
    }
}

/// BT.709 limited-range YCbCr of a full-range RGB colour.
pub fn rgb_to_ycbcr709(r: u8, g: u8, b: u8) -> (u8, u8, u8) {
    let (r, g, b) = (r as f32 / 255.0, g as f32 / 255.0, b as f32 / 255.0);
    let y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    let cb = (b - y) / 1.8556;
    let cr = (r - y) / 1.5748;
    let q = |v: f32| v.round().clamp(0.0, 255.0) as u8;
    (q(16.0 + 219.0 * y), q(128.0 + 224.0 * cb), q(128.0 + 224.0 * cr))
}

/// Downscale UYVY to planar I420 of `dw x dh` (both even) by point sampling.
pub fn uyvy_to_i420_scaled(src: &[u8], stride: usize, sw: usize, sh: usize, dst: &mut [u8], dw: usize, dh: usize) {
    let (yp, rest) = dst.split_at_mut(dw * dh);
    let (up, vp) = rest.split_at_mut(dw * dh / 4);
    for y in 0..dh {
        let sy = y * sh / dh;
        let row = &src[sy * stride..sy * stride + sw * 2];
        for x in 0..dw {
            let sx = x * sw / dw;
            yp[y * dw + x] = row[sx * 2 + 1];
        }
        if y % 2 == 0 {
            for x in 0..dw / 2 {
                let sx = ((x * 2) * sw / dw) & !1;
                up[(y / 2) * (dw / 2) + x] = row[sx * 2];
                vp[(y / 2) * (dw / 2) + x] = row[sx * 2 + 2];
            }
        }
    }
}

/// Downscale UYVY to packed RGB (3 bytes/pixel, BT.709 limited) by point sampling.
pub fn uyvy_to_rgb_scaled(src: &[u8], stride: usize, sw: usize, sh: usize, dst: &mut [u8], dw: usize, dh: usize) {
    for y in 0..dh {
        let sy = y * sh / dh;
        let row = &src[sy * stride..sy * stride + sw * 2];
        for x in 0..dw {
            let sx = x * sw / dw;
            let m = (sx & !1) * 2;
            let yy = row[sx * 2 + 1] as f32;
            let (u, v) = (row[m] as f32 - 128.0, row[m + 2] as f32 - 128.0);
            let c = 1.164_383_6 * (yy - 16.0);
            let o = &mut dst[(y * dw + x) * 3..(y * dw + x) * 3 + 3];
            o[0] = (c + 1.792_741 * v).clamp(0.0, 255.0) as u8;
            o[1] = (c - 0.213_249 * u - 0.532_909 * v).clamp(0.0, 255.0) as u8;
            o[2] = (c + 2.112_402 * u).clamp(0.0, 255.0) as u8;
        }
    }
}

/// Downscale RGBA/BGRA (4 bytes/pixel) to packed RGB by point sampling.
pub fn rgba_to_rgb_scaled(src: &[u8], stride: usize, sw: usize, sh: usize, bgr: bool, dst: &mut [u8], dw: usize, dh: usize) {
    for y in 0..dh {
        let sy = y * sh / dh;
        let row = &src[sy * stride..sy * stride + sw * 4];
        for x in 0..dw {
            let p = &row[(x * sw / dw) * 4..(x * sw / dw) * 4 + 4];
            let o = &mut dst[(y * dw + x) * 3..(y * dw + x) * 3 + 3];
            if bgr {
                o.copy_from_slice(&[p[2], p[1], p[0]]);
            } else {
                o.copy_from_slice(&p[..3]);
            }
        }
    }
}

/// Downscale RGBA/BGRA to I420 by point sampling (BT.709 limited).
pub fn rgba_to_i420_scaled(src: &[u8], stride: usize, sw: usize, sh: usize, bgr: bool, dst: &mut [u8], dw: usize, dh: usize) {
    let (yp, rest) = dst.split_at_mut(dw * dh);
    let (up, vp) = rest.split_at_mut(dw * dh / 4);
    for y in 0..dh {
        let sy = y * sh / dh;
        let row = &src[sy * stride..sy * stride + sw * 4];
        for x in 0..dw {
            let p = &row[(x * sw / dw) * 4..(x * sw / dw) * 4 + 4];
            let (r, g, b) = if bgr { (p[2], p[1], p[0]) } else { (p[0], p[1], p[2]) };
            let (yy, cb, cr) = rgb_to_ycbcr709(r, g, b);
            yp[y * dw + x] = yy;
            if y % 2 == 0 && x % 2 == 0 {
                up[(y / 2) * (dw / 2) + x / 2] = cb;
                vp[(y / 2) * (dw / 2) + x / 2] = cr;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn yuyv_swaps_into_uyvy() {
        let src = [10u8, 20, 30, 40, 50, 60, 70, 80];
        let mut dst = [0u8; 8];
        yuyv_to_uyvy(&src, 8, &mut dst, 8, 4, 1);
        assert_eq!(dst, [20, 10, 40, 30, 60, 50, 80, 70]);
    }

    #[test]
    fn ycbcr_pairs_average_chroma() {
        let src = [100u8, 10, 20, 110, 30, 40];
        let mut dst = [0u8; 4];
        ycbcr444_to_uyvy(&src, &mut dst, 2, 1);
        assert_eq!(dst, [20, 100, 30, 110]);
    }

    #[test]
    fn bt709_white_and_black() {
        assert_eq!(rgb_to_ycbcr709(255, 255, 255), (235, 128, 128));
        assert_eq!(rgb_to_ycbcr709(0, 0, 0), (16, 128, 128));
    }

    #[test]
    fn i420_scaling_samples_luma() {
        // 4x2 UYVY, all luma 200, U=50, V=60
        let src: Vec<u8> = [50u8, 200, 60, 200].repeat(4);
        let mut dst = vec![0u8; 2 * 2 * 3 / 2];
        uyvy_to_i420_scaled(&src, 8, 4, 2, &mut dst, 2, 2);
        assert_eq!(&dst[..4], &[200; 4]);
        assert_eq!(dst[4], 50);
        assert_eq!(dst[5], 60);
    }
}
