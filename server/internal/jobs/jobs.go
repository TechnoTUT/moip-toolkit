// Package jobs runs the RX, TX, multiview and signage engine processes and
// keeps their latest status.
//
// RX, multiview and signage all own the display (with KMS/DRM only one
// process can), so starting one of them stops the others.
package jobs

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strconv"
	"sync"
	"time"

	"github.com/TechnoTUT/moip-toolkit/server/internal/engine"
	"github.com/TechnoTUT/moip-toolkit/server/internal/model"
)

const stopTimeout = 3 * time.Second

type Manager struct {
	// VideoDriver is passed to display jobs (auto, kmsdrm, wayland, x11).
	VideoDriver string
	// WebPort is shown on the RX waiting screen.
	WebPort int

	mu             sync.Mutex
	rx, tx, mv, sg *engine.Process

	rxStatus model.RxStatus
	txStatus model.TxStatus
	mvStatus model.MultiviewStatus
	sgStatus model.SignageStatus
}

func New(videoDriver string, webPort int) *Manager {
	m := &Manager{VideoDriver: videoDriver, WebPort: webPort}
	m.rxStatus = stoppedRx(model.RxStatus{RecvFmt: "uyvy", RecvBandwidth: "highest"})
	m.txStatus = stoppedTx(model.TxStatus{})
	m.mvStatus = model.MultiviewStatus{Slots: []model.MultiviewSlotStatus{}}
	return m
}

func stoppedRx(s model.RxStatus) model.RxStatus {
	s.Running, s.IsConnected = false, false
	s.Width, s.Height, s.FPS, s.FPSReal, s.FPSRecv, s.LatencyMS = 0, 0, 0, 0, 0, nil
	s.AudioLevelL, s.AudioLevelR, s.AudioPeakL, s.AudioPeakR = model.Silence, model.Silence, model.Silence, model.Silence
	return s
}

func stoppedTx(s model.TxStatus) model.TxStatus {
	s.Running, s.FPSReal, s.Connections = false, 0, 0
	s.AudioLevelL, s.AudioLevelR, s.AudioPeakL, s.AudioPeakR = model.Silence, model.Silence, model.Silence, model.Silence
	return s
}

// errorEvent is the engine's {"type":"error","error":"..."}.
type errorEvent struct {
	Error string `json:"error"`
}

// start launches a process and wires status/error/exit handling.
// apply runs under m.mu for every status event; exit runs under m.mu when the
// process ends while still being the current one for its slot.
func (m *Manager) start(name string, args []string, slot **engine.Process,
	apply func(raw []byte), setErr func(string), exit func(errMsg string)) error {
	var p *engine.Process
	var err error
	p, err = engine.Start(name, args, engine.Options{OnEvent: func(ev engine.Event) {
		m.mu.Lock()
		defer m.mu.Unlock()
		if *slot != p {
			return
		}
		switch ev.Type {
		case "status":
			apply(ev.Raw)
		case "error":
			var e errorEvent
			if json.Unmarshal(ev.Raw, &e) == nil && e.Error != "" {
				setErr(e.Error)
			}
		}
	}})
	if err != nil {
		return err
	}
	*slot = p
	go func() {
		<-p.Done()
		m.mu.Lock()
		defer m.mu.Unlock()
		if *slot == p {
			*slot = nil
			exit(p.ExitError())
		}
	}()
	return nil
}

func stopProc(p *engine.Process) {
	if p != nil {
		p.Stop(stopTimeout)
	}
}

// take removes and returns the process in slot (call with m.mu held).
func take(slot **engine.Process) *engine.Process {
	p := *slot
	*slot = nil
	return p
}

func errPtr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// stopDisplayExcept stops the display jobs other than `keep`.
func (m *Manager) stopDisplayExcept(keep string) {
	m.mu.Lock()
	var procs []*engine.Process
	if keep != "rx" {
		procs = append(procs, take(&m.rx))
		m.rxStatus = stoppedRx(m.rxStatus)
	}
	if keep != "multiview" {
		procs = append(procs, take(&m.mv))
		m.mvStatus = model.MultiviewStatus{Slots: []model.MultiviewSlotStatus{}}
	}
	if keep != "signage" {
		procs = append(procs, take(&m.sg))
		m.sgStatus.Running = false
	}
	m.mu.Unlock()
	var wg sync.WaitGroup
	for _, p := range procs {
		if p != nil {
			wg.Add(1)
			go func(p *engine.Process) { defer wg.Done(); stopProc(p) }(p)
		}
	}
	wg.Wait()
}

func (m *Manager) driverArgs() []string {
	if m.VideoDriver == "" {
		return []string{"--video-driver", "auto"}
	}
	return []string{"--video-driver", m.VideoDriver}
}

// ---------------------------------------------------------------- RX

func (m *Manager) StartRx(req model.RxStartRequest) error {
	if req.SenderName == "" {
		return errors.New("sender_name is required")
	}
	if req.RecvFmt == "" {
		req.RecvFmt = "uyvy"
	}
	if req.RecvBandwidth == "" {
		req.RecvBandwidth = "highest"
	}
	switch req.RecvFmt {
	case "uyvy", "rgb", "bgr":
	default:
		return fmt.Errorf("invalid recv_fmt %q (uyvy, rgb or bgr)", req.RecvFmt)
	}
	switch req.RecvBandwidth {
	case "highest", "lowest":
	default:
		return fmt.Errorf("invalid recv_bandwidth %q (highest or lowest)", req.RecvBandwidth)
	}
	m.StopRx()
	m.stopDisplayExcept("rx")

	args := []string{"rx", "-s", req.SenderName, "--recv-fmt", req.RecvFmt,
		"--recv-bandwidth", req.RecvBandwidth, "--web-port", strconv.Itoa(m.WebPort)}
	args = append(args, m.driverArgs()...)
	if req.Fullscreen {
		args = append(args, "--fullscreen")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.rxStatus = stoppedRx(model.RxStatus{
		CurrentSource: model.StrPtr(req.SenderName),
		RecvFmt:       req.RecvFmt,
		RecvBandwidth: req.RecvBandwidth,
		Fullscreen:    req.Fullscreen,
	})
	m.rxStatus.Running = true
	return m.start("rx", args, &m.rx,
		func(raw []byte) {
			_ = json.Unmarshal(raw, &m.rxStatus)
			m.rxStatus.Running = true
		},
		func(e string) { m.rxStatus.Error = errPtr(e) },
		func(e string) {
			m.rxStatus = stoppedRx(m.rxStatus)
			if e != "" {
				m.rxStatus.Error = errPtr(e)
			}
		})
}

func (m *Manager) SwitchRx(source string) error {
	if source == "" {
		return errors.New("sender_name is required")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.rx == nil {
		return errors.New("RX is not running")
	}
	if err := m.rx.Send(map[string]string{"cmd": "switch", "source": source}); err != nil {
		return err
	}
	m.rxStatus.CurrentSource = model.StrPtr(source)
	m.rxStatus.IsConnected = false
	return nil
}

func (m *Manager) StopRx() {
	m.mu.Lock()
	p := take(&m.rx)
	m.rxStatus = stoppedRx(m.rxStatus)
	m.rxStatus.Error = nil
	m.mu.Unlock()
	stopProc(p)
}

func (m *Manager) RxStatus() model.RxStatus {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.rxStatus
}

// ---------------------------------------------------------------- TX

func (m *Manager) StartTx(req model.TxStartRequest) error {
	if req.SenderName == "" {
		req.SenderName = "TX"
	}
	if req.XRes <= 0 || req.YRes <= 0 {
		req.XRes, req.YRes = 1920, 1080
	}
	if req.FPS == "" {
		req.FPS = "30"
	}
	if req.SampleRate <= 0 {
		req.SampleRate = 48000
	}
	if req.AudioChannels <= 0 {
		req.AudioChannels = 2
	}
	m.StopTx()
	args := []string{"tx", "-n", req.SenderName, "-d", strconv.Itoa(req.VideoDevice),
		"--width", strconv.Itoa(req.XRes), "--height", strconv.Itoa(req.YRes), "--fps", req.FPS,
		"--sample-rate", strconv.Itoa(req.SampleRate), "--audio-channels", strconv.Itoa(req.AudioChannels)}
	if req.NoAudio {
		args = append(args, "--no-audio")
	} else if req.AudioDevice != nil && *req.AudioDevice != "" {
		args = append(args, "-a", *req.AudioDevice)
	}
	vd := req.VideoDevice
	m.mu.Lock()
	defer m.mu.Unlock()
	m.txStatus = stoppedTx(model.TxStatus{
		SenderName:  model.StrPtr(req.SenderName),
		VideoDevice: &vd,
		AudioDevice: req.AudioDevice,
		NoAudio:     req.NoAudio,
	})
	m.txStatus.Running = true
	return m.start("tx", args, &m.tx,
		func(raw []byte) {
			_ = json.Unmarshal(raw, &m.txStatus)
			m.txStatus.Running = true
		},
		func(e string) { m.txStatus.Error = errPtr(e) },
		func(e string) {
			m.txStatus = stoppedTx(m.txStatus)
			if e != "" {
				m.txStatus.Error = errPtr(e)
			}
		})
}

func (m *Manager) StopTx() {
	m.mu.Lock()
	p := take(&m.tx)
	m.txStatus = stoppedTx(m.txStatus)
	m.txStatus.Error = nil
	m.mu.Unlock()
	stopProc(p)
}

func (m *Manager) TxStatus() model.TxStatus {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.txStatus
}

// ---------------------------------------------------------------- Multiview

func (m *Manager) StartMultiview(req model.MultiviewStartRequest) error {
	m.StopMultiview()
	m.stopDisplayExcept("multiview")
	args := []string{"multiview"}
	for _, s := range req.Sources {
		if s != "" {
			args = append(args, "-s", s)
		}
	}
	args = append(args, m.driverArgs()...)
	if req.Fullscreen {
		args = append(args, "--fullscreen")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.mvStatus = model.MultiviewStatus{Running: true, Fullscreen: req.Fullscreen,
		AutoDiscovery: len(req.Sources) == 0, Slots: []model.MultiviewSlotStatus{}}
	return m.start("multiview", args, &m.mv,
		func(raw []byte) {
			_ = json.Unmarshal(raw, &m.mvStatus)
			m.mvStatus.Running = true
			if m.mvStatus.Slots == nil {
				m.mvStatus.Slots = []model.MultiviewSlotStatus{}
			}
		},
		func(e string) { m.mvStatus.Error = errPtr(e) },
		func(e string) {
			m.mvStatus.Running = false
			m.mvStatus.Slots = []model.MultiviewSlotStatus{}
			if e != "" {
				m.mvStatus.Error = errPtr(e)
			}
		})
}

func (m *Manager) StopMultiview() {
	m.mu.Lock()
	p := take(&m.mv)
	m.mvStatus = model.MultiviewStatus{Slots: []model.MultiviewSlotStatus{}}
	m.mu.Unlock()
	stopProc(p)
}

func (m *Manager) MultiviewStatus() model.MultiviewStatus {
	m.mu.Lock()
	defer m.mu.Unlock()
	s := m.mvStatus
	s.Slots = append([]model.MultiviewSlotStatus(nil), s.Slots...)
	if s.Slots == nil {
		s.Slots = []model.MultiviewSlotStatus{}
	}
	return s
}

// ---------------------------------------------------------------- Signage

func (m *Manager) StartSignage(path string, fullscreen, framebuffer bool, fbDevice string) error {
	m.StopSignage()
	m.stopDisplayExcept("signage")
	args := []string{"signage", path}
	if framebuffer {
		if fbDevice == "" {
			fbDevice = "/dev/fb0"
		}
		args = append(args, "--framebuffer", "--fb-device", fbDevice)
	} else {
		args = append(args, m.driverArgs()...)
		if fullscreen {
			args = append(args, "--fullscreen")
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sgStatus = model.SignageStatus{Running: true, CurrentImage: model.StrPtr(path), Fullscreen: fullscreen}
	return m.start("signage", args, &m.sg,
		func(raw []byte) {
			_ = json.Unmarshal(raw, &m.sgStatus)
			m.sgStatus.Running = true
		},
		func(e string) { m.sgStatus.Error = errPtr(e) },
		func(e string) {
			m.sgStatus.Running = false
			if e != "" {
				m.sgStatus.Error = errPtr(e)
			}
		})
}

// SwitchSignageImage shows another image if signage is running.
func (m *Manager) SwitchSignageImage(path string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.sg != nil {
		if err := m.sg.Send(map[string]string{"cmd": "image", "path": path}); err != nil {
			log.Printf("[signage] switch image: %v", err)
		}
	}
}

func (m *Manager) StopSignage() {
	m.mu.Lock()
	p := take(&m.sg)
	m.sgStatus.Running = false
	m.sgStatus.Error = nil
	m.mu.Unlock()
	stopProc(p)
}

func (m *Manager) SignageStatus() model.SignageStatus {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sgStatus
}

// StopAll stops every job (server shutdown).
func (m *Manager) StopAll() {
	var wg sync.WaitGroup
	for _, f := range []func(){m.StopRx, m.StopTx, m.StopMultiview, m.StopSignage} {
		wg.Add(1)
		go func(f func()) { defer wg.Done(); f() }(f)
	}
	wg.Wait()
}
