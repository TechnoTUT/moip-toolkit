// Package api implements the HTTP API used by the web UI.
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/TechnoTUT/moip-toolkit/server/internal/engine"
	"github.com/TechnoTUT/moip-toolkit/server/internal/jobs"
	"github.com/TechnoTUT/moip-toolkit/server/internal/model"
	"github.com/TechnoTUT/moip-toolkit/server/internal/preview"
	"github.com/TechnoTUT/moip-toolkit/server/internal/settings"
	"github.com/TechnoTUT/moip-toolkit/server/internal/sources"
	"github.com/TechnoTUT/moip-toolkit/server/internal/sysmon"
)

var signageExts = map[string]bool{".png": true, ".jpg": true, ".jpeg": true, ".webp": true, ".bmp": true}

const maxUpload = 64 << 20

type Server struct {
	Jobs       *jobs.Manager
	Sources    *sources.Scanner
	Preview    *preview.Hub
	Settings   *settings.Store
	Sys        *sysmon.Monitor
	SignageDir string
	StaticDir  string

	// Ctx is cancelled on shutdown; long-lived streams end with it.
	Ctx context.Context
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/events", s.events)
	mux.HandleFunc("GET /api/system/status", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, s.Sys.Status()) })
	mux.HandleFunc("GET /api/ndi/sources", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, s.sources()) })
	mux.HandleFunc("GET /api/ndi/preview", s.mjpeg)
	mux.HandleFunc("POST /api/webrtc/offer", s.webrtcOffer)
	mux.HandleFunc("GET /api/devices/video", s.videoDevices)
	mux.HandleFunc("GET /api/devices/audio", s.audioDevices)

	mux.HandleFunc("GET /api/rx/status", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, s.Jobs.RxStatus()) })
	mux.HandleFunc("POST /api/rx/start", s.rxStart)
	mux.HandleFunc("POST /api/rx/switch", s.rxSwitch)
	mux.HandleFunc("POST /api/rx/stop", func(w http.ResponseWriter, r *http.Request) {
		s.Jobs.StopRx()
		writeJSON(w, s.Jobs.RxStatus())
	})

	mux.HandleFunc("GET /api/tx/status", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, s.Jobs.TxStatus()) })
	mux.HandleFunc("POST /api/tx/start", s.txStart)
	mux.HandleFunc("POST /api/tx/stop", func(w http.ResponseWriter, r *http.Request) {
		s.Jobs.StopTx()
		writeJSON(w, s.Jobs.TxStatus())
	})

	mux.HandleFunc("GET /api/multiview/status", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, s.Jobs.MultiviewStatus()) })
	mux.HandleFunc("POST /api/multiview/start", s.multiviewStart)
	mux.HandleFunc("POST /api/multiview/stop", func(w http.ResponseWriter, r *http.Request) {
		s.Jobs.StopMultiview()
		writeJSON(w, s.Jobs.MultiviewStatus())
	})

	mux.HandleFunc("GET /api/signage/status", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, s.Jobs.SignageStatus()) })
	mux.HandleFunc("GET /api/signage/images", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, s.signageImages()) })
	mux.HandleFunc("GET /api/signage/image/{filename}", s.signageImage)
	mux.HandleFunc("DELETE /api/signage/image/{filename}", s.signageDelete)
	mux.HandleFunc("POST /api/signage/upload", s.signageUpload)
	mux.HandleFunc("POST /api/signage/start", s.signageStart)
	mux.HandleFunc("POST /api/signage/stop", func(w http.ResponseWriter, r *http.Request) {
		s.Jobs.StopSignage()
		writeJSON(w, s.Jobs.SignageStatus())
	})

	mux.HandleFunc("GET /api/settings", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, s.Settings.Get()) })
	mux.HandleFunc("POST /api/settings", s.settingsUpdate)

	mux.HandleFunc("/api/", func(w http.ResponseWriter, r *http.Request) { httpError(w, http.StatusNotFound, "not found") })
	mux.Handle("/", s.static())
	return withCORS(mux)
}

// ---------------------------------------------------------------- helpers

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func httpError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": msg})
}

func readJSON(r *http.Request, v any) error {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		return err
	}
	if len(bytes.TrimSpace(body)) == 0 {
		return nil
	}
	if err := json.Unmarshal(body, v); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	return nil
}

func withCORS(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		h.ServeHTTP(w, r)
	})
}

func (s *Server) sources() []model.NDISource {
	src := s.Sources.Sources()
	if src == nil {
		src = []model.NDISource{}
	}
	return src
}

// ---------------------------------------------------------------- SSE

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	fl, ok := w.(http.Flusher)
	if !ok {
		httpError(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	var last []byte
	for {
		payload, _ := json.Marshal(map[string]any{
			"sources":   s.sources(),
			"rx":        s.Jobs.RxStatus(),
			"tx":        s.Jobs.TxStatus(),
			"system":    s.Sys.Status(),
			"multiview": s.Jobs.MultiviewStatus(),
			"signage":   s.Jobs.SignageStatus(),
			"settings":  s.Settings.Get(),
		})
		var err error
		if !bytes.Equal(payload, last) {
			last = payload
			_, err = fmt.Fprintf(w, "event: state\ndata: %s\n\n", payload)
		} else {
			_, err = io.WriteString(w, ": keepalive\n\n")
		}
		if err != nil {
			return
		}
		fl.Flush()
		select {
		case <-r.Context().Done():
			return
		case <-s.Ctx.Done():
			return
		case <-tick.C:
		}
	}
}

// ---------------------------------------------------------------- preview

func (s *Server) mjpeg(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	source := q.Get("source")
	if source == "" {
		httpError(w, http.StatusBadRequest, "source is required")
		return
	}
	fps := clampInt(q.Get("fps"), 3, 1, 30)
	width := clampInt(q.Get("width"), 360, 160, 1280)
	ch, cancel := s.Preview.SubscribeMJPEG(source, fps, width)
	defer cancel()

	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")
	w.Header().Set("Cache-Control", "no-cache, no-store, must-revalidate")
	w.Header().Set("Pragma", "no-cache")
	w.Header().Set("Expires", "0")
	w.Header().Set("X-Accel-Buffering", "no")
	fl, _ := w.(http.Flusher)
	for {
		select {
		case <-r.Context().Done():
			return
		case <-s.Ctx.Done():
			return
		case jpg := <-ch:
			if _, err := fmt.Fprintf(w, "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n", len(jpg)); err != nil {
				return
			}
			if _, err := w.Write(append(jpg, '\r', '\n')); err != nil {
				return
			}
			if fl != nil {
				fl.Flush()
			}
		}
	}
}

func clampInt(s string, def, lo, hi int) int {
	v, err := strconv.Atoi(s)
	if err != nil {
		v = def
	}
	return max(lo, min(hi, v))
}

func (s *Server) webrtcOffer(w http.ResponseWriter, r *http.Request) {
	var req model.WebRTCOffer
	if err := readJSON(r, &req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	ans, err := s.Preview.HandleOffer(req.Source, req.SDP, req.Type)
	if err != nil {
		httpError(w, http.StatusBadRequest, "WebRTC offer failed: "+err.Error())
		return
	}
	writeJSON(w, model.WebRTCAnswer{SDP: ans.SDP, Type: ans.Type.String()})
}

// ---------------------------------------------------------------- devices

type devicesOut struct {
	Video []model.VideoDevice `json:"video"`
	Audio []model.AudioDevice `json:"audio"`
}

func listDevices() (devicesOut, error) {
	out, err := engine.Run(10*time.Second, "devices")
	var d devicesOut
	if err != nil {
		return d, err
	}
	err = json.Unmarshal(out, &d)
	return d, err
}

func (s *Server) videoDevices(w http.ResponseWriter, r *http.Request) {
	d, err := listDevices()
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if d.Video == nil {
		d.Video = []model.VideoDevice{}
	}
	writeJSON(w, d.Video)
}

func (s *Server) audioDevices(w http.ResponseWriter, r *http.Request) {
	d, err := listDevices()
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if d.Audio == nil {
		d.Audio = []model.AudioDevice{}
	}
	writeJSON(w, d.Audio)
}

// ---------------------------------------------------------------- RX / TX / multiview

func (s *Server) rxStart(w http.ResponseWriter, r *http.Request) {
	var req model.RxStartRequest
	if err := readJSON(r, &req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.Jobs.StartRx(req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	s.Settings.Update(func(st *model.AppSettings) {
		st.RX.SenderName = model.StrPtr(req.SenderName)
		if req.RecvFmt != "" {
			st.RX.RecvFmt = req.RecvFmt
		}
		if req.RecvBandwidth != "" {
			st.RX.RecvBandwidth = req.RecvBandwidth
		}
		st.RX.Fullscreen = req.Fullscreen
	})
	writeJSON(w, s.Jobs.RxStatus())
}

func (s *Server) rxSwitch(w http.ResponseWriter, r *http.Request) {
	var req model.RxSwitchRequest
	if err := readJSON(r, &req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.Jobs.SwitchRx(req.SenderName); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	s.Settings.Update(func(st *model.AppSettings) { st.RX.SenderName = model.StrPtr(req.SenderName) })
	writeJSON(w, s.Jobs.RxStatus())
}

func (s *Server) txStart(w http.ResponseWriter, r *http.Request) {
	req := model.TxStartRequest{SenderName: "TX", XRes: 1920, YRes: 1080, FPS: "30", SampleRate: 48000, AudioChannels: 2}
	if err := readJSON(r, &req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.Jobs.StartTx(req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	s.Settings.Update(func(st *model.AppSettings) {
		st.TX.SenderName = req.SenderName
		st.TX.VideoDevice = req.VideoDevice
		st.TX.AudioDevice = req.AudioDevice
		st.TX.NoAudio = req.NoAudio
		st.TX.XRes, st.TX.YRes, st.TX.FPS = req.XRes, req.YRes, req.FPS
	})
	writeJSON(w, s.Jobs.TxStatus())
}

func (s *Server) multiviewStart(w http.ResponseWriter, r *http.Request) {
	var req model.MultiviewStartRequest
	if err := readJSON(r, &req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := s.Jobs.StartMultiview(req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, s.Jobs.MultiviewStatus())
}

func (s *Server) settingsUpdate(w http.ResponseWriter, r *http.Request) {
	next := s.Settings.Get()
	if err := readJSON(r, &next); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, s.Settings.Update(func(st *model.AppSettings) { *st = next }))
}

// ---------------------------------------------------------------- signage

// safeName returns the base name if it is a plain, allowed image file name.
func safeName(name string) (string, bool) {
	base := filepath.Base(name)
	if base == "." || base == "/" || base == ".." || strings.HasPrefix(base, ".") {
		return "", false
	}
	return base, signageExts[strings.ToLower(filepath.Ext(base))]
}

func (s *Server) signageImages() []model.SignageImage {
	items := []model.SignageImage{}
	entries, _ := os.ReadDir(s.SignageDir)
	for _, e := range entries {
		name, ok := safeName(e.Name())
		if !ok || e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		items = append(items, model.SignageImage{
			Filename:   name,
			URL:        "/api/signage/image/" + name,
			SizeBytes:  info.Size(),
			ModifiedAt: float64(info.ModTime().UnixNano()) / 1e9,
		})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].ModifiedAt > items[j].ModifiedAt })
	return items
}

func (s *Server) signageImage(w http.ResponseWriter, r *http.Request) {
	name, ok := safeName(r.PathValue("filename"))
	path := filepath.Join(s.SignageDir, name)
	if _, err := os.Stat(path); !ok || err != nil {
		httpError(w, http.StatusNotFound, "Image not found")
		return
	}
	http.ServeFile(w, r, path)
}

func (s *Server) signageDelete(w http.ResponseWriter, r *http.Request) {
	name, ok := safeName(r.PathValue("filename"))
	path := filepath.Join(s.SignageDir, name)
	if _, err := os.Stat(path); !ok || err != nil {
		httpError(w, http.StatusNotFound, "Image not found")
		return
	}
	if st := s.Jobs.SignageStatus(); st.Running && st.Filename != nil && *st.Filename == name {
		s.Jobs.StopSignage()
	}
	if err := os.Remove(path); err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, map[string]string{"status": "deleted", "filename": name})
}

func (s *Server) signageUpload(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxUpload)
	file, hdr, err := r.FormFile("file")
	if err != nil {
		httpError(w, http.StatusBadRequest, "file is required: "+err.Error())
		return
	}
	defer file.Close()
	name, ok := safeName(hdr.Filename)
	if !ok {
		httpError(w, http.StatusBadRequest, "Unsupported file format. Allowed: .bmp, .jpeg, .jpg, .png, .webp")
		return
	}
	if err := os.MkdirAll(s.SignageDir, 0o755); err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	dst := filepath.Join(s.SignageDir, name)
	tmp, err := os.CreateTemp(s.SignageDir, ".upload-*")
	if err != nil {
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	_, err = io.Copy(tmp, file)
	cerr := tmp.Close()
	if err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp.Name(), dst)
	}
	if err != nil {
		os.Remove(tmp.Name())
		httpError(w, http.StatusInternalServerError, err.Error())
		return
	}
	info, _ := os.Stat(dst)
	if s.Jobs.SignageStatus().Running {
		s.Jobs.SwitchSignageImage(dst)
	}
	writeJSON(w, model.SignageImage{
		Filename:   name,
		URL:        "/api/signage/image/" + name,
		SizeBytes:  info.Size(),
		ModifiedAt: float64(info.ModTime().UnixNano()) / 1e9,
	})
}

func (s *Server) signageStart(w http.ResponseWriter, r *http.Request) {
	var req model.SignageStartRequest
	if err := readJSON(r, &req); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	var name string
	if req.Filename != nil && *req.Filename != "" {
		n, ok := safeName(*req.Filename)
		if _, err := os.Stat(filepath.Join(s.SignageDir, n)); !ok || err != nil {
			httpError(w, http.StatusNotFound, fmt.Sprintf("Image %s not found", *req.Filename))
			return
		}
		name = n
	} else {
		imgs := s.signageImages()
		if len(imgs) == 0 {
			httpError(w, http.StatusBadRequest, "No signage images uploaded yet")
			return
		}
		name = imgs[0].Filename
	}
	fullscreen := req.Fullscreen == nil || *req.Fullscreen
	if err := s.Jobs.StartSignage(filepath.Join(s.SignageDir, name), fullscreen, req.UseFramebuffer, req.FBDevice); err != nil {
		httpError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, s.Jobs.SignageStatus())
}

// ---------------------------------------------------------------- static UI

// static serves the built Nuxt SPA, falling back to index.html for client routes.
func (s *Server) static() http.Handler {
	if s.StaticDir == "" {
		return http.NotFoundHandler()
	}
	fs := http.FileServer(http.Dir(s.StaticDir))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := filepath.Join(s.StaticDir, filepath.Clean("/"+r.URL.Path))
		if st, err := os.Stat(p); errors.Is(err, os.ErrNotExist) || (err == nil && st.IsDir() && !fileExists(filepath.Join(p, "index.html"))) {
			http.ServeFile(w, r, filepath.Join(s.StaticDir, "index.html"))
			return
		}
		fs.ServeHTTP(w, r)
	})
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// AutoStart starts TX/RX from saved presets, as configured.
func (s *Server) AutoStart() {
	st := s.Settings.Get()
	if st.TX.AutoStart && !s.Jobs.TxStatus().Running {
		err := s.Jobs.StartTx(model.TxStartRequest{
			VideoDevice: st.TX.VideoDevice, AudioDevice: st.TX.AudioDevice, NoAudio: st.TX.NoAudio,
			SenderName: st.TX.SenderName, XRes: st.TX.XRes, YRes: st.TX.YRes, FPS: st.TX.FPS,
		})
		log.Printf("[autostart] TX %q: %v", st.TX.SenderName, errOrOK(err))
	}
	if st.RX.AutoStart && st.RX.SenderName != nil && *st.RX.SenderName != "" && !s.Jobs.RxStatus().Running {
		err := s.Jobs.StartRx(model.RxStartRequest{
			SenderName: *st.RX.SenderName, RecvFmt: st.RX.RecvFmt, RecvBandwidth: st.RX.RecvBandwidth, Fullscreen: st.RX.Fullscreen,
		})
		log.Printf("[autostart] RX %q: %v", *st.RX.SenderName, errOrOK(err))
	}
}

func errOrOK(err error) string {
	if err == nil {
		return "started"
	}
	return err.Error()
}
