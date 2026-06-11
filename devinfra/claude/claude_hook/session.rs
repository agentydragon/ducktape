//! Per-session daemon state: mailbox + background command output buffers.

use std::collections::HashMap;
use std::sync::Mutex;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum BgStream {
    Stdout,
    Stderr,
}

impl BgStream {
    pub fn as_str(self) -> &'static str {
        match self {
            BgStream::Stdout => "stdout",
            BgStream::Stderr => "stderr",
        }
    }
}

pub struct Session {
    pub session_id: String,
    inner: Mutex<SessionInner>,
}

#[derive(Default)]
struct SessionInner {
    mailbox: Vec<String>,
    bg: HashMap<(String, BgStream), Vec<String>>,
}

impl Session {
    pub fn new(session_id: String) -> Self {
        Self {
            session_id,
            inner: Mutex::new(SessionInner::default()),
        }
    }

    pub fn post_message(&self, msg: String) {
        self.inner.lock().unwrap().mailbox.push(msg);
    }

    pub fn push_bg_line(&self, task: &str, stream: BgStream, line: String) {
        self.inner
            .lock()
            .unwrap()
            .bg
            .entry((task.to_string(), stream))
            .or_default()
            .push(line);
    }

    pub fn drain_messages(&self) -> Vec<String> {
        std::mem::take(&mut self.inner.lock().unwrap().mailbox)
    }

    pub fn drain_bg_output(&self) -> HashMap<(String, BgStream), Vec<String>> {
        std::mem::take(&mut self.inner.lock().unwrap().bg)
    }
}

/// Render drained mailbox + bg output as `HookOutput.system_message`,
/// matching the Python format in `server.py::_apply_mailbox`.
pub fn format_system_message(
    mailbox: Vec<String>,
    bg: HashMap<(String, BgStream), Vec<String>>,
) -> Option<String> {
    if mailbox.is_empty() && bg.is_empty() {
        return None;
    }
    let mut parts: Vec<String> = Vec::new();

    if !bg.is_empty() {
        // Group by task name, preserving per-stream order.
        let mut by_task: Vec<(String, HashMap<BgStream, Vec<String>>)> = Vec::new();
        for ((task, stream), lines) in bg {
            if let Some((_, streams)) = by_task.iter_mut().find(|(t, _)| t == &task) {
                streams.insert(stream, lines);
            } else {
                let mut streams = HashMap::new();
                streams.insert(stream, lines);
                by_task.push((task, streams));
            }
        }
        let task_blocks: Vec<String> = by_task
            .into_iter()
            .map(|(task, streams)| {
                let inner: String = streams
                    .into_iter()
                    .map(|(s, lines)| {
                        let name = s.as_str();
                        format!("<{name}>{}</{name}>", lines.join("\n"))
                    })
                    .collect();
                format!("<task {task}>{inner}</task>")
            })
            .collect();
        parts.push(format!(
            "Background task output:\n{}",
            task_blocks.join("\n")
        ));
    }

    if !mailbox.is_empty() {
        let bullets: Vec<String> = mailbox.into_iter().map(|m| format!("- {m}")).collect();
        parts.push(format!(
            "Messages from hook daemon mailbox:\n{}",
            bullets.join("\n")
        ));
    }

    Some(parts.join("\n\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mailbox_roundtrip() {
        let s = Session::new("s1".into());
        s.post_message("hello".into());
        s.post_message("world".into());
        assert_eq!(s.drain_messages(), vec!["hello", "world"]);
        assert!(s.drain_messages().is_empty());
    }

    #[test]
    fn bg_lines_grouped_by_task_and_stream() {
        let s = Session::new("s1".into());
        s.push_bg_line("t1", BgStream::Stdout, "l1".into());
        s.push_bg_line("t1", BgStream::Stderr, "e1".into());
        s.push_bg_line("t1", BgStream::Stdout, "l2".into());
        s.push_bg_line("t2", BgStream::Stdout, "m1".into());

        let out = s.drain_bg_output();
        assert_eq!(out[&("t1".into(), BgStream::Stdout)], vec!["l1", "l2"]);
        assert_eq!(out[&("t1".into(), BgStream::Stderr)], vec!["e1"]);
        assert_eq!(out[&("t2".into(), BgStream::Stdout)], vec!["m1"]);
        assert!(s.drain_bg_output().is_empty());
    }

    #[test]
    fn format_mailbox_only() {
        let s = format_system_message(vec!["hi".into(), "there".into()], HashMap::new()).unwrap();
        assert!(s.contains("Messages from hook daemon mailbox:"));
        assert!(s.contains("- hi"));
    }

    #[test]
    fn format_bg_only() {
        let mut bg = HashMap::new();
        bg.insert(("kubeconfig".into(), BgStream::Stdout), vec!["done".into()]);
        let s = format_system_message(vec![], bg).unwrap();
        assert!(s.contains("Background task output:"));
        assert!(s.contains("<task kubeconfig><stdout>done</stdout></task>"));
    }

    #[test]
    fn format_empty_is_none() {
        assert!(format_system_message(vec![], HashMap::new()).is_none());
    }

    #[test]
    fn format_both_sections_ordered() {
        let mut bg = HashMap::new();
        bg.insert(("t".into(), BgStream::Stdout), vec!["o".into()]);
        let s = format_system_message(vec!["m".into()], bg).unwrap();
        let a = s.find("Background task output:").unwrap();
        let b = s.find("Messages from hook daemon mailbox:").unwrap();
        assert!(a < b, "bg before mailbox in {s}");
    }
}
