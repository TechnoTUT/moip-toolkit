// Package model holds the JSON types of the HTTP API.
package model

// NDISource is a source found on the network.
type NDISource struct {
	Name       string  `json:"name"`
	StreamName string  `json:"stream_name"`
	HostName   string  `json:"host_name"`
	URL        *string `json:"url"`
}

type VideoDevice struct {
	Index       int      `json:"index"`
	Path        string   `json:"path"`
	Name        string   `json:"name"`
	Formats     []string `json:"formats"`
	IsAvailable bool     `json:"is_available"`
}

type AudioDevice struct {
	ID                string  `json:"id"`
	Name              string  `json:"name"`
	MaxInputChannels  int     `json:"max_input_channels"`
	DefaultSamplerate float64 `json:"default_samplerate"`
}

// NodeInfo identifies this node to a controller (GET /api/node/info).
type NodeInfo struct {
	// ID is stable across reboots and address changes; it defaults to the hostname.
	ID       string `json:"id"`
	Hostname string `json:"hostname"`
	Version  string `json:"version"`
	// Roles lists what the node can do: rx, tx, multiview, signage.
	Roles []string `json:"roles"`
}

type SystemStatus struct {
	CPUPercent float64    `json:"cpu_percent"`
	MemPercent float64    `json:"mem_percent"`
	MemUsedMB  float64    `json:"mem_used_mb"`
	MemTotalMB float64    `json:"mem_total_mb"`
	LoadAvg    [3]float64 `json:"load_avg"`
}

// ---- RX ----

type RxStartRequest struct {
	SenderName    string `json:"sender_name"`
	RecvFmt       string `json:"recv_fmt"`
	RecvBandwidth string `json:"recv_bandwidth"`
	Fullscreen    bool   `json:"fullscreen"`
}

type RxSwitchRequest struct {
	SenderName string `json:"sender_name"`
}

type RxStatus struct {
	Running       bool     `json:"running"`
	IsConnected   bool     `json:"is_connected"`
	CurrentSource *string  `json:"current_source"`
	RecvFmt       string   `json:"recv_fmt"`
	RecvBandwidth string   `json:"recv_bandwidth"`
	Fullscreen    bool     `json:"fullscreen"`
	VideoDriver   string   `json:"video_driver"`
	Width         int      `json:"width"`
	Height        int      `json:"height"`
	FPS           float64  `json:"fps"`
	FPSReal       float64  `json:"fps_real"`
	FPSRecv       float64  `json:"fps_recv"`
	LatencyMS     *float64 `json:"latency_ms"`
	DroppedFrames int64    `json:"dropped_frames"`
	SkippedFrames int64    `json:"skipped_frames"`
	AudioLevelL   float64  `json:"audio_level_l"`
	AudioLevelR   float64  `json:"audio_level_r"`
	AudioPeakL    float64  `json:"audio_peak_l"`
	AudioPeakR    float64  `json:"audio_peak_r"`
	Error         *string  `json:"error"`
}

// ---- TX ----

type TxStartRequest struct {
	VideoDevice   int     `json:"video_device"`
	AudioDevice   *string `json:"audio_device"`
	NoAudio       bool    `json:"no_audio"`
	SenderName    string  `json:"sender_name"`
	XRes          int     `json:"x_res"`
	YRes          int     `json:"y_res"`
	FPS           string  `json:"fps"`
	InputFormat   string  `json:"input_format"`
	SampleRate    int     `json:"sample_rate"`
	AudioChannels int     `json:"audio_channels"`
}

type TxStatus struct {
	Running       bool    `json:"running"`
	SenderName    *string `json:"sender_name"`
	VideoDevice   *int    `json:"video_device"`
	AudioDevice   *string `json:"audio_device"`
	NoAudio       bool    `json:"no_audio"`
	InputFormat   string  `json:"input_format"`
	ActualWidth   int     `json:"actual_width"`
	ActualHeight  int     `json:"actual_height"`
	ActualFPS     float64 `json:"actual_fps"`
	FPSReal       float64 `json:"fps_real"`
	DroppedFrames int64   `json:"dropped_frames"`
	Connections   int     `json:"connections"`
	SampleRate    int     `json:"sample_rate"`
	AudioChannels int     `json:"audio_channels"`
	AudioLevelL   float64 `json:"audio_level_l"`
	AudioLevelR   float64 `json:"audio_level_r"`
	AudioPeakL    float64 `json:"audio_peak_l"`
	AudioPeakR    float64 `json:"audio_peak_r"`
	Error         *string `json:"error"`
}

// ---- Multiview ----

type MultiviewSlotStatus struct {
	SourceName  string  `json:"source_name"`
	IsConnected bool    `json:"is_connected"`
	Width       int     `json:"width"`
	Height      int     `json:"height"`
	AudioLevelL float64 `json:"audio_level_l"`
	AudioLevelR float64 `json:"audio_level_r"`
}

type MultiviewStartRequest struct {
	Sources    []string `json:"sources"`
	Fullscreen bool     `json:"fullscreen"`
}

type MultiviewStatus struct {
	Running       bool                  `json:"running"`
	Fullscreen    bool                  `json:"fullscreen"`
	AutoDiscovery bool                  `json:"auto_discovery"`
	Slots         []MultiviewSlotStatus `json:"slots"`
	Error         *string               `json:"error"`
}

// ---- Signage ----

type SignageImage struct {
	Filename   string  `json:"filename"`
	URL        string  `json:"url"`
	SizeBytes  int64   `json:"size_bytes"`
	ModifiedAt float64 `json:"modified_at"`
}

type SignageStartRequest struct {
	Filename       *string `json:"filename"`
	Fullscreen     *bool   `json:"fullscreen"`
	UseFramebuffer bool    `json:"use_framebuffer"`
	FBDevice       string  `json:"fb_device"`
}

type SignageStatus struct {
	Running      bool    `json:"running"`
	CurrentImage *string `json:"current_image"`
	Filename     *string `json:"filename"`
	Fullscreen   bool    `json:"fullscreen"`
	Width        int     `json:"width"`
	Height       int     `json:"height"`
	Output       string  `json:"output"`
	Error        *string `json:"error"`
}

// ---- WebRTC ----

type WebRTCOffer struct {
	Source string `json:"source"`
	SDP    string `json:"sdp"`
	Type   string `json:"type"`
}

type WebRTCAnswer struct {
	SDP  string `json:"sdp"`
	Type string `json:"type"`
}

// ---- Settings ----

type RxPreset struct {
	AutoStart     bool    `json:"auto_start"`
	SenderName    *string `json:"sender_name"`
	RecvFmt       string  `json:"recv_fmt"`
	RecvBandwidth string  `json:"recv_bandwidth"`
	Fullscreen    bool    `json:"fullscreen"`
}

type TxPreset struct {
	AutoStart   bool    `json:"auto_start"`
	SenderName  string  `json:"sender_name"`
	VideoDevice int     `json:"video_device"`
	AudioDevice *string `json:"audio_device"`
	NoAudio     bool    `json:"no_audio"`
	XRes        int     `json:"x_res"`
	YRes        int     `json:"y_res"`
	FPS         string  `json:"fps"`
	InputFormat string  `json:"input_format"`
}

type AppSettings struct {
	RX RxPreset `json:"rx"`
	TX TxPreset `json:"tx"`
}

func DefaultSettings() AppSettings {
	return AppSettings{
		RX: RxPreset{RecvFmt: "uyvy", RecvBandwidth: "highest"},
		TX: TxPreset{SenderName: "TX", XRes: 1920, YRes: 1080, FPS: "30", InputFormat: "auto"},
	}
}

// Silence is the audio level reported when there is no signal (dBFS).
const Silence = -60.0

func StrPtr(s string) *string { return &s }
