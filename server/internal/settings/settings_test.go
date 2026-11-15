package settings

import (
	"path/filepath"
	"testing"

	"github.com/TechnoTUT/moip-toolkit/server/internal/model"
)

func TestDecodeLegacyPythonSettings(t *testing.T) {
	legacy := `{
	  "rx": {"auto_start": true, "sender_name": "PC (OBS)", "recv_fmt": "uyvy", "recv_bandwidth": "lowest", "fullscreen": true},
	  "tx": {"auto_start": false, "sender_name": "CAM", "video_device": 2, "audio_device": 3,
	         "no_audio": false, "x_res": 1280, "y_res": 720, "fps": "60", "pix_fmt": "BGRX"}
	}`
	s := decode([]byte(legacy))
	if !s.RX.AutoStart || s.RX.SenderName == nil || *s.RX.SenderName != "PC (OBS)" || s.RX.RecvBandwidth != "lowest" {
		t.Fatalf("rx not decoded: %+v", s.RX)
	}
	if s.TX.SenderName != "CAM" || s.TX.VideoDevice != 2 || s.TX.XRes != 1280 || s.TX.FPS != "60" {
		t.Fatalf("tx not decoded: %+v", s.TX)
	}
	if s.TX.AudioDevice != nil {
		t.Fatalf("legacy integer audio_device should be dropped, got %q", *s.TX.AudioDevice)
	}
}

func TestDecodeGarbageGivesDefaults(t *testing.T) {
	if s := decode([]byte("not json")); s != model.DefaultSettings() {
		t.Fatalf("expected defaults, got %+v", s)
	}
}

func TestRoundTrip(t *testing.T) {
	p := filepath.Join(t.TempDir(), "s.json")
	st := Open(p)
	st.Update(func(s *model.AppSettings) { s.TX.AudioDevice = model.StrPtr("plughw:CARD=PCH,DEV=0") })
	got := Open(p).Get()
	if got.TX.AudioDevice == nil || *got.TX.AudioDevice != "plughw:CARD=PCH,DEV=0" {
		t.Fatalf("round trip failed: %+v", got.TX)
	}
}
