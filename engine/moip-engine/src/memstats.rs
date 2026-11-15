//! Optional memory diagnostics: with MOIP_MEMSTATS=1, log RSS and glibc heap
//! statistics periodically, to tell leaks, fragmentation and non-heap growth apart.

use std::time::{Duration, Instant};

pub struct MemStats {
    enabled: bool,
    last: Instant,
}

impl MemStats {
    pub fn new() -> Self {
        Self { enabled: std::env::var_os("MOIP_MEMSTATS").is_some(), last: Instant::now() }
    }

    pub fn tick(&mut self) {
        if !self.enabled || self.last.elapsed() < Duration::from_secs(30) {
            return;
        }
        self.last = Instant::now();
        let rss = std::fs::read_to_string("/proc/self/status")
            .ok()
            .and_then(|s| s.lines().find(|l| l.starts_with("VmRSS:")).map(|l| l.split_whitespace().nth(1).unwrap_or("0").to_string()))
            .unwrap_or_default();
        // SAFETY: plain glibc call returning a struct by value.
        let m = unsafe { libc::mallinfo2() };
        crate::log!(
            "[mem] rss_kb={rss} heap_kb={} mmap_kb={} inuse_kb={} free_kb={} keepcost_kb={}",
            m.arena / 1024,
            m.hblkhd / 1024,
            m.uordblks / 1024,
            m.fordblks / 1024,
            m.keepcost / 1024
        );
    }
}
