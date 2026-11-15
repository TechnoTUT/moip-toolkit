// Package settings persists presets and auto-start flags as JSON.
package settings

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sync"

	"github.com/TechnoTUT/moip-toolkit/server/internal/model"
)

// DefaultPath is the settings file shared with the previous (Python) version.
func DefaultPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "utone-ndi", "settings.json")
}

type Store struct {
	path string
	mu   sync.Mutex
	s    model.AppSettings
}

func Open(path string) *Store {
	st := &Store{path: path, s: model.DefaultSettings()}
	b, err := os.ReadFile(path)
	if err == nil {
		st.s = decode(b)
	} else if !os.IsNotExist(err) {
		log.Printf("[settings] cannot read %s: %v", path, err)
	}
	return st
}

// decode reads settings leniently: unknown fields are ignored and values of
// the wrong type (e.g. the old integer audio_device) fall back to defaults.
func decode(b []byte) model.AppSettings {
	s := model.DefaultSettings()
	var raw map[string]map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		log.Printf("[settings] ignoring malformed settings: %v", err)
		return s
	}
	apply := func(section map[string]json.RawMessage, dst any) {
		fields := map[string]json.RawMessage{}
		for k, v := range section {
			// Validate each field on its own so one bad value keeps the rest.
			probe := map[string]json.RawMessage{k: v}
			pb, _ := json.Marshal(probe)
			scratch := reflect.New(reflect.TypeOf(dst).Elem()).Interface()
			if json.Unmarshal(pb, scratch) == nil {
				fields[k] = v
			}
		}
		fb, _ := json.Marshal(fields)
		_ = json.Unmarshal(fb, dst)
	}
	if rx, ok := raw["rx"]; ok {
		apply(rx, &s.RX)
	}
	if tx, ok := raw["tx"]; ok {
		apply(tx, &s.TX)
	}
	return s
}

func (st *Store) Get() model.AppSettings {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.s
}

func (st *Store) Update(fn func(*model.AppSettings)) model.AppSettings {
	st.mu.Lock()
	defer st.mu.Unlock()
	fn(&st.s)
	st.saveLocked()
	return st.s
}

func (st *Store) saveLocked() {
	if err := os.MkdirAll(filepath.Dir(st.path), 0o755); err != nil {
		log.Printf("[settings] %v", err)
		return
	}
	b, _ := json.MarshalIndent(st.s, "", "  ")
	tmp := st.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		log.Printf("[settings] save: %v", err)
		return
	}
	if err := os.Rename(tmp, st.path); err != nil {
		log.Printf("[settings] save: %v", err)
	}
}
