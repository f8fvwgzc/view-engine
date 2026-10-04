//! Subprocess runner for CLI providers.
//!
//! Shape follows Paperclip's `runChildProcess` and texc-symphony's transport shutdown:
//! the child gets its own process group, stdin carries the prompt, stdout is capped, and on timeout
//! the whole group is terminated (SIGTERM, grace, SIGKILL) so helper processes never outlive the call.

use std::{collections::HashMap, path::PathBuf, process::Stdio, time::Duration};

use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::Command,
    sync::mpsc::UnboundedSender,
};

const STDOUT_CAP_BYTES: usize = 4 * 1024 * 1024;
const KILL_GRACE: Duration = Duration::from_secs(3);

pub struct ProcessSpec {
    pub program: String,
    pub args: Vec<String>,
    pub stdin: Option<String>,
    pub cwd: PathBuf,
    pub env: HashMap<String, String>,
    pub timeout: Duration,
    /// Receives each stdout line as it is produced (live progress); the full output is still returned.
    pub lines: Option<UnboundedSender<String>>,
}

pub struct ProcessOutput {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: Option<i32>,
    pub timed_out: bool,
}

pub async fn run(spec: ProcessSpec) -> Result<ProcessOutput, String> {
    let mut command = Command::new(&spec.program);
    command
        .args(&spec.args)
        .current_dir(&spec.cwd)
        .envs(&spec.env)
        .stdin(if spec.stdin.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    #[cfg(unix)]
    command.process_group(0);

    let mut child = command
        .spawn()
        .map_err(|error| format!("could not start `{}`: {error} (is it installed and on PATH?)", spec.program))?;
    let pid = child.id();

    if let (Some(input), Some(mut stdin)) = (spec.stdin, child.stdin.take()) {
        tokio::spawn(async move {
            let _ = stdin.write_all(input.as_bytes()).await;
            let _ = stdin.shutdown().await;
        });
    }
    let mut stdout = child.stdout.take().expect("stdout is piped");
    let mut stderr = child.stderr.take().expect("stderr is piped");
    let line_sink = spec.lines.clone();
    let read_stdout = tokio::spawn(async move { read_lines(&mut stdout, line_sink).await });
    let read_stderr = tokio::spawn(async move { read_capped(&mut stderr).await });

    let (exit_code, timed_out) = match tokio::time::timeout(spec.timeout, child.wait()).await {
        Ok(Ok(status)) => (status.code(), false),
        Ok(Err(error)) => return Err(format!("waiting for `{}` failed: {error}", spec.program)),
        Err(_) => {
            terminate_group(pid, &mut child).await;
            (None, true)
        }
    };
    let stdout = read_stdout.await.unwrap_or_default();
    let stderr = read_stderr.await.unwrap_or_default();
    Ok(ProcessOutput { stdout, stderr, exit_code, timed_out })
}

async fn read_lines<R: AsyncReadExt + Unpin>(reader: &mut R, sink: Option<UnboundedSender<String>>) -> String {
    let mut reader = BufReader::new(reader);
    let mut all = String::new();
    let mut line = String::new();
    loop {
        line.clear();
        match reader.read_line(&mut line).await {
            Ok(0) | Err(_) => break,
            Ok(_) => {
                if let Some(sink) = &sink {
                    let _ = sink.send(line.trim_end().to_string());
                }
                if all.len() < STDOUT_CAP_BYTES {
                    all.push_str(&line);
                }
            }
        }
    }
    all
}

async fn read_capped<R: AsyncReadExt + Unpin>(reader: &mut R) -> String {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 16 * 1024];
    loop {
        match reader.read(&mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(read) => {
                if buffer.len() < STDOUT_CAP_BYTES {
                    let room = STDOUT_CAP_BYTES - buffer.len();
                    buffer.extend_from_slice(&chunk[..read.min(room)]);
                }
            }
        }
    }
    String::from_utf8_lossy(&buffer).into_owned()
}

async fn terminate_group(pid: Option<u32>, child: &mut tokio::process::Child) {
    #[cfg(unix)]
    if let Some(pid) = pid {
        let group = format!("-{pid}");
        let _ = Command::new("kill").args(["-TERM", "--", &group]).status().await;
        if tokio::time::timeout(KILL_GRACE, child.wait()).await.is_ok() {
            return;
        }
        let _ = Command::new("kill").args(["-KILL", "--", &group]).status().await;
    }
    let _ = child.kill().await;
}

/// True when `program` resolves on PATH (used for provider availability probes).
pub fn on_path(program: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(program))
        .find(|candidate| candidate.is_file())
}
