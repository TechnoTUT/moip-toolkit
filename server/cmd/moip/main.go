// Command moip is the moip-toolkit entry point.
//
//	moip web [flags]                 web UI / REST API server
//	moip rx|tx|multiview|signage|discover|devices|version ...
//	                                 run the media engine directly (exec moip-engine)
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/TechnoTUT/moip-toolkit/server/internal/api"
	"github.com/TechnoTUT/moip-toolkit/server/internal/engine"
	"github.com/TechnoTUT/moip-toolkit/server/internal/jobs"
	"github.com/TechnoTUT/moip-toolkit/server/internal/preview"
	"github.com/TechnoTUT/moip-toolkit/server/internal/settings"
	"github.com/TechnoTUT/moip-toolkit/server/internal/sources"
	"github.com/TechnoTUT/moip-toolkit/server/internal/sysmon"
)

const usage = `moip-toolkit

Usage:
  moip web [flags]          Start the web UI and REST API (moip web -h for flags)
  moip rx [flags]           Receive and display an NDI source
  moip tx [flags]           Send a capture device as an NDI source
  moip multiview [flags]    Show several NDI sources in a grid
  moip signage <image>      Show an image fullscreen
  moip discover             List NDI sources
  moip devices              List capture devices
  moip version              Show the NDI runtime version

Engine subcommands accept -h for their flags.
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "web":
		if err := runWeb(os.Args[2:]); err != nil {
			log.Fatal(err)
		}
	case "-h", "--help", "help":
		fmt.Print(usage)
	default:
		execEngine(os.Args[1:])
	}
}

// execEngine replaces this process with moip-engine.
func execEngine(args []string) {
	path, err := engine.Locate("")
	if err != nil {
		log.Fatal(err)
	}
	if err := syscall.Exec(path, append([]string{path}, args...), os.Environ()); err != nil {
		log.Fatalf("exec %s: %v", path, err)
	}
}

func exeDir() string {
	exe, err := os.Executable()
	if err != nil {
		return "."
	}
	if r, err := filepath.EvalSymlinks(exe); err == nil {
		exe = r
	}
	return filepath.Dir(exe)
}

// firstDir returns the first existing directory among candidates.
func firstDir(candidates ...string) string {
	for _, c := range candidates {
		if st, err := os.Stat(c); err == nil && st.IsDir() {
			abs, _ := filepath.Abs(c)
			return abs
		}
	}
	return ""
}

func runWeb(args []string) error {
	fs := flag.NewFlagSet("web", flag.ExitOnError)
	host := fs.String("host", "0.0.0.0", "listen address")
	port := fs.Int("port", 8000, "listen port")
	videoDriver := fs.String("video-driver", "kmsdrm", "SDL video driver for RX / multiview / signage: kmsdrm (falls back to the desktop if unavailable), auto, wayland, x11")
	enginePath := fs.String("engine", "", "path to moip-engine (default: next to this binary, then $PATH)")
	staticDir := fs.String("static", "", "built web UI directory (default: frontend/.output/public)")
	dataDir := fs.String("data", "", "data directory holding signage/ (default: <repo>/data or ~/.local/share/moip-toolkit)")
	settingsPath := fs.String("settings", settings.DefaultPath(), "settings file")
	extraIPs := fs.String("extra-ips", "", "comma-separated IPs to query for NDI sources (networks without mDNS)")
	maxWebRTC := fs.Int("max-webrtc", 4, "maximum concurrent WebRTC previews")
	_ = fs.Parse(args)

	p, err := engine.Locate(*enginePath)
	if err != nil {
		return err
	}
	engine.Path = p
	log.Printf("engine: %s", p)
	if out, err := engine.Run(10*time.Second, "version"); err == nil {
		log.Printf("NDI runtime: %s", bytesTrim(out))
	} else {
		log.Printf("warning: NDI runtime not usable: %v", err)
	}

	dir := exeDir()
	if *staticDir == "" {
		*staticDir = firstDir(
			filepath.Join(dir, "..", "frontend", ".output", "public"),
			filepath.Join(dir, "..", "..", "frontend", ".output", "public"),
			filepath.Join("frontend", ".output", "public"),
		)
	}
	if *staticDir == "" {
		log.Printf("warning: web UI not built (cd frontend && npm run build); serving the API only")
	} else {
		log.Printf("web UI: %s", *staticDir)
	}
	if *dataDir == "" {
		*dataDir = firstDir(filepath.Join(dir, "..", "data"), filepath.Join(dir, "..", "..", "data"), "data")
		if *dataDir == "" {
			home, _ := os.UserHomeDir()
			*dataDir = filepath.Join(home, ".local", "share", "moip-toolkit")
		}
	}
	signageDir := filepath.Join(*dataDir, "signage")
	if err := os.MkdirAll(signageDir, 0o755); err != nil {
		return err
	}
	log.Printf("signage images: %s", signageDir)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	scanner := sources.New(*extraIPs)
	scanner.Start()
	srv := &api.Server{
		Jobs:       jobs.New(*videoDriver, *port),
		Sources:    scanner,
		Preview:    preview.NewHub(*maxWebRTC, *extraIPs),
		Settings:   settings.Open(*settingsPath),
		Sys:        sysmon.New(),
		SignageDir: signageDir,
		StaticDir:  *staticDir,
		Ctx:        ctx,
	}

	addr := net.JoinHostPort(*host, strconv.Itoa(*port))
	httpSrv := &http.Server{Addr: addr, Handler: srv.Handler(), ReadHeaderTimeout: 10 * time.Second}
	errc := make(chan error, 1)
	go func() {
		log.Printf("listening on http://%s", addr)
		errc <- httpSrv.ListenAndServe()
	}()
	go func() {
		time.Sleep(time.Second)
		if ctx.Err() == nil {
			srv.AutoStart()
		}
	}()

	select {
	case <-ctx.Done():
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			srv.Jobs.StopAll()
			return err
		}
	}
	log.Printf("shutting down")
	shutCtx, shutCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutCancel()
	_ = httpSrv.Shutdown(shutCtx)
	srv.Preview.StopAll()
	srv.Jobs.StopAll()
	scanner.Stop()
	return nil
}

func bytesTrim(b []byte) string {
	for len(b) > 0 && (b[len(b)-1] == '\n' || b[len(b)-1] == '\r') {
		b = b[:len(b)-1]
	}
	return string(b)
}
