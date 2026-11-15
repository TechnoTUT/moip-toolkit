//! Receiver churn test: repeatedly create a receiver, capture for a few
//! seconds, destroy it, and print the process RSS after each cycle.
//!
//!   cargo run --release -p ndi --example churn -- "<source>" [cycles] [--trim]
//!
//! With --trim, glibc's malloc_trim(0) is called after each destroy, which
//! tells retained-but-free heap memory apart from a real leak.

use std::time::{Duration, Instant};

extern "C" {
    fn malloc_trim(pad: usize) -> i32;
}

fn rss_kb() -> u64 {
    std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| s.lines().find(|l| l.starts_with("VmRSS:")).map(|l| l.split_whitespace().nth(1).unwrap_or("0").parse().unwrap_or(0)))
        .unwrap_or(0)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let source = args.get(1).expect("usage: churn <source> [cycles] [--trim]").clone();
    let cycles: usize = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(20);
    let trim = args.iter().any(|a| a == "--trim");
    // --bg: destroy receivers on a fresh thread (as moip-engine does).
    let bg = args.iter().any(|a| a == "--bg");
    // --xthread: free frames on another thread than the capturing one (as rx does).
    let xthread = args.iter().any(|a| a == "--xthread");
    // --reuse: keep one receiver and switch sources with recv_connect.
    let reuse = args.iter().any(|a| a == "--reuse");
    // --alt <name>: alternate between the main source and this one.
    let alt = args.iter().position(|a| a == "--alt").and_then(|i| args.get(i + 1)).cloned();
    let (ftx, frx) = std::sync::mpsc::sync_channel::<ndi::VideoFrame>(1);
    if xthread {
        std::thread::spawn(move || for f in frx { drop(f) });
    }

    let finder = ndi::Finder::new(true, None).unwrap();
    let find = |name: &str| loop {
        finder.wait(1000);
        if let Some(s) = finder.sources().into_iter().find(|s| s.matches(name)) {
            break s;
        }
        eprintln!("waiting for {name}...");
    };
    let src_a = find(&source);
    let src_b = alt.as_deref().map(find).unwrap_or_else(|| src_a.clone());
    let mut kept: Option<ndi::Receiver> = None;
    println!("cycle,rss_kb,frames");
    for i in 0..cycles {
        let src = if i % 2 == 0 { &src_a } else { &src_b };
        let rx = if reuse {
            let r = kept.get_or_insert_with(|| ndi::Receiver::new(None, &ndi::RecvOptions::default()).unwrap());
            r.connect(Some(src));
            r.clone()
        } else {
            ndi::Receiver::new(Some(src), &ndi::RecvOptions::default()).unwrap()
        };
        let end = Instant::now() + Duration::from_secs(3);
        let mut frames = 0;
        while Instant::now() < end {
            if let ndi::Capture::Video(f) = rx.capture(100, true, true) {
                frames += 1;
                if xthread {
                    let _ = ftx.send(f);
                }
            }
        }
        if bg {
            rx.destroy_in_background();
            std::thread::sleep(Duration::from_millis(300));
        } else {
            drop(rx);
        }
        if trim {
            // SAFETY: plain libc call.
            unsafe { malloc_trim(0) };
        }
        println!("{i},{},{frames}", rss_kb());
    }
}
