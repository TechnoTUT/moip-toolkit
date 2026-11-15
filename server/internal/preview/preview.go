// Package preview serves browser previews of NDI sources.
//
// One `moip-engine preview` process per source is shared by every viewer of
// that source: MJPEG subscribers get JPEG frames, WebRTC viewers get the H.264
// access units forwarded as-is by pion (no transcoding in Go). The process is
// told which outputs are needed and stops 5 s after the last viewer leaves.
package preview

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"sync"
	"time"

	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"

	"github.com/TechnoTUT/moip-toolkit/server/internal/engine"
)

const (
	kindStatus = 0
	kindH264   = 1
	kindJPEG   = 2

	idleStop  = 5 * time.Second
	h264FPS   = 24
	h264Width = 640
)

// Hub owns all preview sessions.
type Hub struct {
	MaxWebRTC int
	ExtraIPs  string

	mu       sync.Mutex
	sessions map[string]*session
	rtcCount int
	api      *webrtc.API
}

func NewHub(maxWebRTC int, extraIPs string) *Hub {
	me := &webrtc.MediaEngine{}
	_ = me.RegisterDefaultCodecs()
	return &Hub{
		MaxWebRTC: maxWebRTC,
		ExtraIPs:  extraIPs,
		sessions:  map[string]*session{},
		api:       webrtc.NewAPI(webrtc.WithMediaEngine(me)),
	}
}

type mjpegSub struct {
	ch    chan []byte
	fps   int
	width int
	next  time.Time
}

type rtcSub struct {
	track   *webrtc.TrackLocalStaticSample
	pc      *webrtc.PeerConnection
	started bool // a keyframe has been sent
}

type session struct {
	hub    *Hub
	source string

	proc      *engine.Process
	mjpeg     map[*mjpegSub]struct{}
	rtc       map[*rtcSub]struct{}
	idleTimer *time.Timer
	lastKeyRq time.Time
	closed    bool
}

// get returns the session for source, creating it (call with h.mu held).
func (h *Hub) get(source string) *session {
	s, ok := h.sessions[source]
	if !ok {
		s = &session{hub: h, source: source, mjpeg: map[*mjpegSub]struct{}{}, rtc: map[*rtcSub]struct{}{}}
		h.sessions[source] = s
	}
	if s.idleTimer != nil {
		s.idleTimer.Stop()
		s.idleTimer = nil
	}
	return s
}

// update starts/stops/reconfigures the engine process (call with h.mu held).
func (s *session) update() {
	if len(s.mjpeg) == 0 && len(s.rtc) == 0 {
		if s.idleTimer == nil {
			s.idleTimer = time.AfterFunc(idleStop, func() {
				s.hub.mu.Lock()
				if len(s.mjpeg) > 0 || len(s.rtc) > 0 || s.closed {
					s.hub.mu.Unlock()
					return
				}
				s.closed = true
				delete(s.hub.sessions, s.source)
				p := s.proc
				s.proc = nil
				s.hub.mu.Unlock()
				if p != nil {
					p.Stop(2 * time.Second)
				}
			})
		}
		return
	}
	width, fps := 0, 0
	for m := range s.mjpeg {
		width = max(width, m.width)
		fps = max(fps, m.fps)
	}
	cmd := map[string]any{"cmd": "preview", "h264": len(s.rtc) > 0, "jpeg": len(s.mjpeg) > 0}
	if width > 0 {
		cmd["jpeg_width"] = width
		cmd["jpeg_fps"] = fps
	}
	if s.proc == nil || s.proc.Exited() {
		if err := s.startProc(); err != nil {
			log.Printf("[preview] %s: %v", s.source, err)
			return
		}
	}
	_ = s.proc.Send(cmd)
}

func (s *session) startProc() error {
	args := []string{"preview", "-s", s.source, "--h264-width", fmt.Sprint(h264Width), "--h264-fps", fmt.Sprint(h264FPS)}
	if s.hub.ExtraIPs != "" {
		args = append(args, "--extra-ips", s.hub.ExtraIPs)
	}
	var p *engine.Process
	p, err := engine.Start("preview "+s.source, args, engine.Options{Stdout: func(r io.Reader) { s.read(r) }})
	if err != nil {
		return err
	}
	s.proc = p
	go func() {
		<-p.Done()
		time.Sleep(2 * time.Second)
		s.hub.mu.Lock()
		defer s.hub.mu.Unlock()
		if s.proc == p && !s.closed {
			s.proc = nil
			s.update() // restart while viewers remain
		}
	}()
	return nil
}

func (s *session) read(r io.Reader) {
	hdr := make([]byte, 6)
	for {
		if _, err := io.ReadFull(r, hdr); err != nil {
			return
		}
		n := binary.LittleEndian.Uint32(hdr[2:])
		if n > 32<<20 {
			log.Printf("[preview] %s: bad frame length %d", s.source, n)
			return
		}
		payload := make([]byte, n)
		if _, err := io.ReadFull(r, payload); err != nil {
			return
		}
		switch hdr[0] {
		case kindH264:
			s.deliverH264(payload, hdr[1]&1 != 0)
		case kindJPEG:
			s.deliverJPEG(payload)
		case kindStatus:
			var st struct {
				IsConnected bool `json:"is_connected"`
			}
			_ = json.Unmarshal(payload, &st)
		}
	}
}

func (s *session) deliverJPEG(jpg []byte) {
	now := time.Now()
	s.hub.mu.Lock()
	defer s.hub.mu.Unlock()
	for m := range s.mjpeg {
		if now.Before(m.next) {
			continue
		}
		m.next = now.Add(time.Second / time.Duration(max(m.fps, 1)))
		select {
		case m.ch <- jpg:
		default: // slow client: drop
		}
	}
}

func (s *session) deliverH264(au []byte, key bool) {
	s.hub.mu.Lock()
	var targets []*rtcSub
	for t := range s.rtc {
		if key {
			t.started = true
		}
		if t.started {
			targets = append(targets, t)
		}
	}
	s.hub.mu.Unlock()
	sample := media.Sample{Data: au, Duration: time.Second / h264FPS}
	for _, t := range targets {
		_ = t.track.WriteSample(sample)
	}
}

// requestKeyframe asks the encoder for an IDR, at most once per second.
func (s *session) requestKeyframe() {
	s.hub.mu.Lock()
	defer s.hub.mu.Unlock()
	if time.Since(s.lastKeyRq) < time.Second || s.proc == nil {
		return
	}
	s.lastKeyRq = time.Now()
	_ = s.proc.Send(map[string]string{"cmd": "keyframe"})
}

// ---------------------------------------------------------------- MJPEG

// SubscribeMJPEG returns a channel of JPEG frames and a cancel function.
func (h *Hub) SubscribeMJPEG(source string, fps, width int) (<-chan []byte, func()) {
	sub := &mjpegSub{ch: make(chan []byte, 2), fps: fps, width: width}
	h.mu.Lock()
	s := h.get(source)
	s.mjpeg[sub] = struct{}{}
	s.update()
	h.mu.Unlock()
	var once sync.Once
	return sub.ch, func() {
		once.Do(func() {
			h.mu.Lock()
			delete(s.mjpeg, sub)
			s.update()
			h.mu.Unlock()
		})
	}
}

// ---------------------------------------------------------------- WebRTC

// HandleOffer answers a browser's recvonly offer with an H.264 track of source.
func (h *Hub) HandleOffer(source, sdp, typ string) (*webrtc.SessionDescription, error) {
	if source == "" {
		return nil, errors.New("source is required")
	}
	h.mu.Lock()
	if h.rtcCount >= h.MaxWebRTC {
		h.mu.Unlock()
		return nil, fmt.Errorf("too many WebRTC connections (max %d)", h.MaxWebRTC)
	}
	h.rtcCount++
	h.mu.Unlock()
	release := sync.OnceFunc(func() {
		h.mu.Lock()
		h.rtcCount--
		h.mu.Unlock()
	})

	pc, err := h.api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		release()
		return nil, err
	}
	fail := func(err error) (*webrtc.SessionDescription, error) {
		_ = pc.Close()
		release()
		return nil, err
	}
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.NewSDPType(typ), SDP: sdp}); err != nil {
		return fail(err)
	}
	track, err := webrtc.NewTrackLocalStaticSample(webrtc.RTPCodecCapability{
		MimeType:    webrtc.MimeTypeH264,
		ClockRate:   90000,
		SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
	}, "video", "ndi-preview")
	if err != nil {
		return fail(err)
	}
	sender, err := pc.AddTrack(track)
	if err != nil {
		return fail(err)
	}

	sub := &rtcSub{track: track, pc: pc}
	h.mu.Lock()
	s := h.get(source)
	s.rtc[sub] = struct{}{}
	s.update()
	h.mu.Unlock()
	s.requestKeyframe()

	remove := sync.OnceFunc(func() {
		h.mu.Lock()
		delete(s.rtc, sub)
		s.update()
		h.mu.Unlock()
		release()
		_ = pc.Close()
	})
	pc.OnConnectionStateChange(func(st webrtc.PeerConnectionState) {
		log.Printf("[webrtc] %s: %s", source, st)
		switch st {
		case webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed, webrtc.PeerConnectionStateDisconnected:
			go remove()
		}
	})
	go func() {
		// Keyframe on picture loss; reading RTCP also keeps interceptors running.
		for {
			pkts, _, err := sender.ReadRTCP()
			if err != nil {
				return
			}
			for _, p := range pkts {
				switch p.(type) {
				case *rtcp.PictureLossIndication, *rtcp.FullIntraRequest:
					s.requestKeyframe()
				}
			}
		}
	}()

	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		remove()
		return nil, err
	}
	gathered := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		remove()
		return nil, err
	}
	select {
	case <-gathered:
	case <-time.After(3 * time.Second):
	}
	// Drop viewers that never connect.
	time.AfterFunc(15*time.Second, func() {
		if pc.ConnectionState() != webrtc.PeerConnectionStateConnected {
			remove()
		}
	})
	return pc.LocalDescription(), nil
}

// StopAll ends every session (server shutdown).
func (h *Hub) StopAll() {
	h.mu.Lock()
	var procs []*engine.Process
	var pcs []*webrtc.PeerConnection
	for _, s := range h.sessions {
		s.closed = true
		if s.proc != nil {
			procs = append(procs, s.proc)
		}
		for r := range s.rtc {
			pcs = append(pcs, r.pc)
		}
	}
	h.sessions = map[string]*session{}
	h.mu.Unlock()
	for _, pc := range pcs {
		_ = pc.Close()
	}
	for _, p := range procs {
		p.Stop(2 * time.Second)
	}
}
