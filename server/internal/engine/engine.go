// Package engine starts and talks to moip-engine processes.
//
// Protocol: JSON command lines on stdin, JSON event lines on stdout (each with a
// "type" field), human-readable logs on stderr. The preview subcommand uses a
// binary framing on stdout instead (see package preview).
package engine

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Path of the moip-engine binary, resolved by Locate.
var Path string

// Locate finds moip-engine: $MOIP_ENGINE, next to this executable, the cargo
// build directory of a source checkout, then $PATH.
func Locate(explicit string) (string, error) {
	var candidates []string
	if explicit != "" {
		candidates = append(candidates, explicit)
	}
	if env := os.Getenv("MOIP_ENGINE"); env != "" {
		candidates = append(candidates, env)
	}
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		candidates = append(candidates,
			filepath.Join(dir, "moip-engine"),
			filepath.Join(dir, "..", "engine", "target", "release", "moip-engine"),
			filepath.Join(dir, "..", "..", "engine", "target", "release", "moip-engine"),
		)
	}
	candidates = append(candidates, filepath.Join("engine", "target", "release", "moip-engine"))
	for _, c := range candidates {
		if st, err := os.Stat(c); err == nil && !st.IsDir() && st.Mode()&0o111 != 0 {
			abs, _ := filepath.Abs(c)
			return abs, nil
		}
	}
	if p, err := exec.LookPath("moip-engine"); err == nil {
		return p, nil
	}
	return "", errors.New("moip-engine not found (build it with `make engine`, or set MOIP_ENGINE)")
}

// Event is one decoded stdout line. Raw holds the full JSON object.
type Event struct {
	Type string `json:"type"`
	Raw  json.RawMessage
}

// Process is a running engine subcommand.
type Process struct {
	Name string
	cmd  *exec.Cmd

	mu      sync.Mutex
	stdin   io.WriteCloser
	lastErr string // last stderr line, used when the process dies without an error event

	done    chan struct{}
	exitErr error
}

// Options for Start.
type Options struct {
	// OnEvent is called from a reader goroutine for every JSON line (line mode).
	OnEvent func(Event)
	// Stdout, if set, receives raw stdout instead of line-decoded events.
	Stdout func(r io.Reader)
	// Env is appended to the server's environment.
	Env []string
}

// Start runs `moip-engine --control <args...>`.
func Start(name string, args []string, opt Options) (*Process, error) {
	if Path == "" {
		return nil, errors.New("moip-engine path not set")
	}
	cmd := exec.Command(Path, append([]string{"--control"}, args...)...)
	cmd.Env = append(os.Environ(), opt.Env...)
	// Own process group: a Ctrl-C on the server is handled by us, not delivered twice.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGTERM}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start moip-engine %s: %w", name, err)
	}
	p := &Process{Name: name, cmd: cmd, stdin: stdin, done: make(chan struct{})}
	log.Printf("[%s] started: moip-engine %s (pid %d)", name, strings.Join(args, " "), cmd.Process.Pid)

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		sc := bufio.NewScanner(stderr)
		sc.Buffer(make([]byte, 64*1024), 1024*1024)
		for sc.Scan() {
			line := sc.Text()
			log.Printf("[%s] %s", name, line)
			p.mu.Lock()
			p.lastErr = strings.TrimPrefix(line, "error: ")
			p.mu.Unlock()
		}
	}()
	go func() {
		defer wg.Done()
		if opt.Stdout != nil {
			opt.Stdout(stdout)
			_, _ = io.Copy(io.Discard, stdout)
			return
		}
		sc := bufio.NewScanner(stdout)
		sc.Buffer(make([]byte, 64*1024), 4*1024*1024)
		for sc.Scan() {
			b := append([]byte(nil), sc.Bytes()...)
			var ev Event
			if json.Unmarshal(b, &ev) != nil {
				continue
			}
			ev.Raw = b
			if opt.OnEvent != nil {
				opt.OnEvent(ev)
			}
		}
	}()
	go func() {
		wg.Wait()
		p.exitErr = cmd.Wait()
		log.Printf("[%s] exited: %v", name, exitString(p.exitErr))
		close(p.done)
	}()
	return p, nil
}

func exitString(err error) string {
	if err == nil {
		return "ok"
	}
	return err.Error()
}

// Send writes one JSON command line.
func (p *Process) Send(cmd any) error {
	b, err := json.Marshal(cmd)
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.stdin == nil {
		return errors.New("process stdin closed")
	}
	_, err = p.stdin.Write(append(b, '\n'))
	return err
}

// Done is closed when the process has exited.
func (p *Process) Done() <-chan struct{} { return p.done }

// Exited reports whether the process has exited.
func (p *Process) Exited() bool {
	select {
	case <-p.done:
		return true
	default:
		return false
	}
}

// ExitError describes an abnormal exit ("" for a clean exit or while running).
func (p *Process) ExitError() string {
	if !p.Exited() || p.exitErr == nil {
		return ""
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.lastErr != "" {
		return p.lastErr
	}
	return p.exitErr.Error()
}

// Stop asks the process to quit, then escalates to SIGTERM and SIGKILL.
func (p *Process) Stop(timeout time.Duration) {
	if p.Exited() {
		return
	}
	_ = p.Send(map[string]string{"cmd": "quit"})
	p.mu.Lock()
	if p.stdin != nil {
		_ = p.stdin.Close()
		p.stdin = nil
	}
	p.mu.Unlock()
	select {
	case <-p.done:
		return
	case <-time.After(timeout):
	}
	_ = syscall.Kill(-p.cmd.Process.Pid, syscall.SIGTERM)
	select {
	case <-p.done:
		return
	case <-time.After(2 * time.Second):
	}
	_ = syscall.Kill(-p.cmd.Process.Pid, syscall.SIGKILL)
	<-p.done
}

// Run executes a one-shot engine subcommand (without --control) and returns stdout.
func Run(timeout time.Duration, args ...string) ([]byte, error) {
	if Path == "" {
		return nil, errors.New("moip-engine path not set")
	}
	cmd := exec.Command(Path, args...)
	cmd.Stderr = os.Stderr
	done := make(chan struct{})
	var out []byte
	var err error
	go func() {
		out, err = cmd.Output()
		close(done)
	}()
	select {
	case <-done:
		return out, err
	case <-time.After(timeout):
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		<-done
		return nil, fmt.Errorf("moip-engine %s timed out", strings.Join(args, " "))
	}
}
