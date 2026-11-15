//! `moip-engine discover`: NDI source discovery.

use crate::control::{emit, Command, Control};
use serde::Serialize;
use std::time::{Duration, Instant};

#[derive(Serialize, Clone, PartialEq)]
pub struct SourceItem {
    pub name: String,
    pub stream_name: String,
    pub host_name: String,
    pub url: Option<String>,
}

impl From<&ndi::Source> for SourceItem {
    fn from(s: &ndi::Source) -> Self {
        Self {
            name: s.name.clone(),
            stream_name: s.stream_name().to_string(),
            host_name: s.host_name().to_string(),
            url: s.url.clone(),
        }
    }
}

#[derive(Serialize)]
struct SourcesEvent<'a> {
    r#type: &'static str,
    sources: &'a [SourceItem],
}

pub fn sorted_items(sources: &[ndi::Source]) -> Vec<SourceItem> {
    let mut items: Vec<SourceItem> = sources.iter().map(SourceItem::from).collect();
    items.sort_by(|a, b| a.name.cmp(&b.name));
    items.dedup_by(|a, b| a.name == b.name);
    items
}

/// Wait `timeout` for sources to appear and return them.
pub fn list_once(finder: &ndi::Finder, timeout: Duration) -> Vec<ndi::Source> {
    let end = Instant::now() + timeout;
    while Instant::now() < end {
        let left = end.saturating_duration_since(Instant::now());
        finder.wait(left.as_millis().min(500) as u32);
    }
    finder.sources()
}

pub struct Args {
    pub once: bool,
    pub timeout: f64,
    pub extra_ips: Option<String>,
    pub control: bool,
}

pub fn run(args: Args) -> anyhow::Result<()> {
    let finder = ndi::Finder::new(true, args.extra_ips.as_deref())?;
    if args.once {
        let items = sorted_items(&list_once(&finder, Duration::from_secs_f64(args.timeout)));
        if args.control {
            emit(&SourcesEvent { r#type: "sources", sources: &items });
        } else if items.is_empty() {
            println!("No NDI sources found.");
        } else {
            for s in &items {
                println!("{}", s.name);
            }
        }
        return Ok(());
    }

    // Continuous: emit the list whenever it changes, and at least every 5 s.
    let ctl = Control::new(args.control);
    let mut last: Option<Vec<SourceItem>> = None;
    let mut last_emit = Instant::now();
    loop {
        if let Some(Command::Quit) = ctl.poll() {
            return Ok(());
        }
        finder.wait(500);
        let items = sorted_items(&finder.sources());
        if last.as_ref() != Some(&items) || last_emit.elapsed() > Duration::from_secs(5) {
            if args.control {
                emit(&SourcesEvent { r#type: "sources", sources: &items });
            } else {
                println!("--- {} source(s)", items.len());
                for s in &items {
                    println!("{}", s.name);
                }
            }
            last = Some(items);
            last_emit = Instant::now();
        }
    }
}
