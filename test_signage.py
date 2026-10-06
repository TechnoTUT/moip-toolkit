"""
test_signage.py
Unit tests for digital signage framebuffer rendering.
Uses mocking to test without actual framebuffer hardware.
"""
import unittest
from unittest.mock import patch, MagicMock, mock_open
import numpy as np
import cv2
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.signage import (
    run_signage_framebuffer,
    FramebufferInfo,
    load_image,
    create_texture_from_rgba,
    update_texture_from_rgba,
    calculate_dest_rect,
    draw_texture,
    run_signage,
)


class TestFramebufferInfo(unittest.TestCase):
    """Test FramebufferInfo class with mocked ioctl."""

    @patch("core.signage.fcntl.ioctl")
    def test_framebuffer_info_parsing(self, mock_ioctl):
        """Test that FramebufferInfo correctly parses ioctl responses."""
        # Mock fb_var_screeninfo (160 bytes)
        vinfo = b"\x00" * 160
        # Mock fb_fix_screeninfo (72 bytes)
        finfo = b"\x00" * 72

        mock_ioctl.side_effect = [vinfo, finfo]

        fb_info = FramebufferInfo(0)

        # Verify attributes are set
        self.assertEqual(fb_info.fd, 0)
        self.assertEqual(fb_info.xres, 0)
        self.assertEqual(fb_info.yres, 0)
        self.assertEqual(fb_info.bits_per_pixel, 0)
        self.assertEqual(fb_info.smem_len, 0)
        self.assertEqual(fb_info.line_length, 0)


class TestLoadImage(unittest.TestCase):
    """Test image loading functionality."""

    def test_load_image_file_not_found(self):
        """Test that FileNotFoundError is raised for missing files."""
        with self.assertRaises(FileNotFoundError):
            load_image("/nonexistent/path/image.png")

    @patch("core.signage.cv2.imread")
    def test_load_image_grayscale(self, mock_imread):
        """Test loading grayscale image."""
        mock_img = np.zeros((100, 200), dtype=np.uint8)
        mock_imread.return_value = mock_img

        raw_bytes, w, h = load_image("test.png")

        self.assertEqual(w, 200)
        self.assertEqual(h, 100)
        self.assertIsInstance(raw_bytes, bytes)

    @patch("core.signage.cv2.imread")
    def test_load_image_bgr(self, mock_imread):
        """Test loading BGR image."""
        mock_img = np.zeros((100, 200, 3), dtype=np.uint8)
        mock_imread.return_value = mock_img

        raw_bytes, w, h = load_image("test.png")

        self.assertEqual(w, 200)
        self.assertEqual(h, 100)
        self.assertIsInstance(raw_bytes, bytes)

    @patch("core.signage.cv2.imread")
    def test_load_image_bgra(self, mock_imread):
        """Test loading BGRA image."""
        mock_img = np.zeros((100, 200, 4), dtype=np.uint8)
        mock_imread.return_value = mock_img

        raw_bytes, w, h = load_image("test.png")

        self.assertEqual(w, 200)
        self.assertEqual(h, 100)
        self.assertIsInstance(raw_bytes, bytes)

    @patch("core.signage.cv2.imread")
    def test_load_image_decode_failure(self, mock_imread):
        """Test that ValueError is raised when image decode fails."""
        mock_imread.return_value = None

        with self.assertRaises(ValueError):
            load_image("test.png")


class TestRunSignageFramebuffer(unittest.TestCase):
    """Test run_signage_framebuffer function with mocked framebuffer."""

    @patch("core.signage.os.open")
    @patch("core.signage.FramebufferInfo")
    @patch("core.signage.mmap.mmap")
    @patch("core.signage.cv2.imread")
    @patch("core.signage.load_image")
    def test_run_signage_framebuffer_success(
        self, mock_load_image, mock_imread, mock_mmap, mock_fb_info, mock_open
    ):
        """Test successful framebuffer rendering."""
        # Setup mocks
        mock_load_image.return_value = (b"raw_bytes", 100, 100)
        mock_open.return_value = 0

        mock_info = MagicMock()
        mock_info.xres = 800
        mock_info.yres = 600
        mock_info.bits_per_pixel = 32
        mock_info.smem_len = 800 * 600 * 4
        mock_info.line_length = 800 * 4
        mock_fb_info.return_value = mock_info

        mock_img = np.zeros((100, 100, 4), dtype=np.uint8)
        mock_imread.return_value = mock_img

        mock_mmap_instance = MagicMock()
        mock_mmap.return_value = mock_mmap_instance

        # Mock command queue
        mock_command_q = MagicMock()
        mock_command_q.empty.return_value = True

        # Mock status queue
        mock_status_q = MagicMock()

        # Run with mocked components
        run_signage_framebuffer(
            "test.png",
            command_q=mock_command_q,
            status_q=mock_status_q,
            fb_device="/dev/fb0",
        )

        # Verify status messages were sent
        self.assertTrue(mock_status_q.put.called)

    @patch("core.signage.os.open")
    def test_run_signage_framebuffer_device_not_found(self, mock_open):
        """Test error handling when framebuffer device is not found."""
        mock_open.side_effect = OSError(2, "No such file or directory")

        mock_status_q = MagicMock()

        with self.assertRaises(RuntimeError):
            run_signage_framebuffer(
                "test.png",
                status_q=mock_status_q,
                fb_device="/dev/nonexistent",
            )

        # Verify error was reported
        mock_status_q.put.assert_called_with(
            {
                "type": "error",
                "error": "Failed to open framebuffer device /dev/nonexistent: [Errno 2] No such file or directory",
            }
        )


class TestRunSignage(unittest.TestCase):
    """Test run_signage function dispatch."""

    @patch("core.signage.run_signage_framebuffer")
    def test_run_signage_with_framebuffer(self, mock_run_fb):
        """Test that run_signage dispatches to framebuffer when requested."""
        run_signage("test.png", use_framebuffer=True, fb_device="/dev/fb0")

        mock_run_fb.assert_called_once_with(
            image_path="test.png",
            command_q=None,
            status_q=None,
            fb_device="/dev/fb0",
        )


if __name__ == "__main__":
    unittest.main()
