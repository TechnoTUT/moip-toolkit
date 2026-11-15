// Package sources keeps the list of NDI sources from a long-running
// `moip-engine discover` process (restarted if it dies).
package sources

import (
	"encoding/json"
	"log"
	"sort"
	"sync"
	"time"

	"github.com/TechnoTUT/moip-toolkit/server/internal/engine"
	"github.com/TechnoTUT/moip-toolkit/server/internal/model"
)

type Scanner struct {
	ExtraIPs string

	mu      sync.Mutex
	sources []model.NDISource
	proc    *engine.Process
	stop    chan struct{}
}

func New(extraIPs string) *Scanner {
	return &Scanner{ExtraIPs: extraIPs, sources: []model.NDISource{}, stop: make(chan struct{})}
}

type sourcesEvent struct {
	Sources []model.NDISource `json:"sources"`
}

func (s *Scanner) Start() {
	go func() {
		backoff := time.Second
		for {
			args := []string{"discover"}
			if s.ExtraIPs != "" {
				args = append(args, "--extra-ips", s.ExtraIPs)
			}
			p, err := engine.Start("discover", args, engine.Options{OnEvent: func(ev engine.Event) {
				if ev.Type != "sources" {
					return
				}
				var e sourcesEvent
				if json.Unmarshal(ev.Raw, &e) != nil {
					return
				}
				if e.Sources == nil {
					e.Sources = []model.NDISource{}
				}
				sort.Slice(e.Sources, func(i, j int) bool { return e.Sources[i].Name < e.Sources[j].Name })
				s.mu.Lock()
				s.sources = e.Sources
				s.mu.Unlock()
			}})
			if err != nil {
				log.Printf("[discover] %v", err)
			} else {
				s.mu.Lock()
				s.proc = p
				s.mu.Unlock()
				started := time.Now()
				select {
				case <-p.Done():
					if time.Since(started) > time.Minute {
						backoff = time.Second
					}
				case <-s.stop:
					p.Stop(2 * time.Second)
					return
				}
			}
			select {
			case <-time.After(backoff):
			case <-s.stop:
				return
			}
			if backoff < 30*time.Second {
				backoff *= 2
			}
		}
	}()
}

func (s *Scanner) Stop() {
	close(s.stop)
}

func (s *Scanner) Sources() []model.NDISource {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]model.NDISource(nil), s.sources...)
}
