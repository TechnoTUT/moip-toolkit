// Package sysmon reads CPU, memory and load from /proc.
package sysmon

import (
	"bufio"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"

	"github.com/TechnoTUT/moip-toolkit/server/internal/model"
)

type Monitor struct {
	mu                  sync.Mutex
	lastTotal, lastIdle float64
}

func New() *Monitor {
	m := &Monitor{}
	m.lastTotal, m.lastIdle = readCPU()
	return m
}

func readCPU() (total, idle float64) {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return 0, 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	if !sc.Scan() {
		return 0, 0
	}
	fields := strings.Fields(sc.Text())
	for i, s := range fields[1:] {
		v, _ := strconv.ParseFloat(s, 64)
		total += v
		if i == 3 || i == 4 { // idle + iowait
			idle += v
		}
	}
	return total, idle
}

func round(v float64, digits int) float64 {
	p := math.Pow(10, float64(digits))
	return math.Round(v*p) / p
}

// Status returns usage since the previous call.
func (m *Monitor) Status() model.SystemStatus {
	var s model.SystemStatus
	total, idle := readCPU()
	m.mu.Lock()
	dt, di := total-m.lastTotal, idle-m.lastIdle
	if dt > 0 {
		s.CPUPercent = round(math.Max(0, math.Min(100, (1-di/dt)*100)), 1)
	}
	m.lastTotal, m.lastIdle = total, idle
	m.mu.Unlock()

	if f, err := os.Open("/proc/meminfo"); err == nil {
		info := map[string]float64{}
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			k, v, ok := strings.Cut(sc.Text(), ":")
			if !ok {
				continue
			}
			if fs := strings.Fields(v); len(fs) > 0 {
				info[k], _ = strconv.ParseFloat(fs[0], 64)
			}
		}
		f.Close()
		totalKB := info["MemTotal"]
		avail, ok := info["MemAvailable"]
		if !ok {
			avail = info["MemFree"]
		}
		if totalKB > 0 {
			used := math.Max(0, totalKB-avail)
			s.MemPercent = round(used/totalKB*100, 1)
			s.MemUsedMB = round(used/1024, 1)
			s.MemTotalMB = round(totalKB/1024, 1)
		}
	}
	if b, err := os.ReadFile("/proc/loadavg"); err == nil {
		fs := strings.Fields(string(b))
		for i := 0; i < 3 && i < len(fs); i++ {
			v, _ := strconv.ParseFloat(fs[i], 64)
			s.LoadAvg[i] = round(v, 2)
		}
	}
	return s
}
