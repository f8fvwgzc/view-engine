//! Provider adapters: one request/response shape over several LLM back ends.
//!
//! Claude Code CLI is the integrated, tested provider. Codex CLI, GitHub Copilot CLI, Ollama and
//! OpenAI-compatible HTTP servers are implemented against the same interface but are not exercised by
//! this project yet (argument shapes follow Paperclip's adapters and each tool's `--help`).

use std::{collections::HashMap, path::PathBuf, time::{Duration, Instant}};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::{
    compress::estimate_tokens,
    process::{self, ProcessSpec},
    retry::{classify, ErrorClass},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ProviderKind {
    Claude,
    Codex,
    Copilot,
    Ollama,
    Openai,
    Simulated,
}

impl ProviderKind {
    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "claude" | "claude-code" | "claudecode" => Some(Self::Claude),
            "codex" => Some(Self::Codex),
            "copilot" => Some(Self::Copilot),
            "ollama" => Some(Self::Ollama),
            "openai" | "openai-compatible" | "lmstudio" | "vllm" => Some(Self::Openai),
            "simulated" | "sim" | "offline" => Some(Self::Simulated),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
            Self::Copilot => "copilot",
            Self::Ollama => "ollama",
            Self::Openai => "openai",
            Self::Simulated => "simulated",
        }
    }

    /// Providers that browse the web themselves. Everything else gets search/fetch done by the harness.
    pub fn native_web(self) -> bool {
        matches!(self, Self::Claude | Self::Codex | Self::Copilot)
    }

    pub fn all() -> [Self; 6] {
        [Self::Claude, Self::Codex, Self::Copilot, Self::Ollama, Self::Openai, Self::Simulated]
    }
}

/// Connection settings for every provider; values come from the settings table with env fallbacks.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderConfig {
    pub claude_bin: String,
    pub claude_model: Option<String>,
    pub codex_bin: String,
    pub codex_model: Option<String>,
    pub copilot_bin: String,
    pub copilot_model: Option<String>,
    pub ollama_url: String,
    pub ollama_model: String,
    pub openai_base_url: String,
    pub openai_model: String,
    #[serde(skip_serializing)]
    pub openai_api_key: Option<String>,
    pub workdir: PathBuf,
}

impl ProviderConfig {
    pub fn from_env() -> Self {
        let env = |key: &str| std::env::var(key).ok().filter(|value| !value.trim().is_empty());
        let workdir = env("GODVIEW_WORKDIR").map(PathBuf::from).unwrap_or_else(|| std::env::temp_dir().join("godview-agents"));
        Self {
            claude_bin: env("CLAUDE_BIN").unwrap_or_else(|| "claude".into()),
            claude_model: env("CLAUDE_MODEL").or(Some("sonnet".into())),
            codex_bin: env("CODEX_BIN").unwrap_or_else(|| "codex".into()),
            codex_model: env("CODEX_MODEL"),
            copilot_bin: env("COPILOT_BIN").unwrap_or_else(|| "copilot".into()),
            copilot_model: env("COPILOT_MODEL"),
            ollama_url: env("OLLAMA_URL").unwrap_or_else(|| "http://127.0.0.1:11434".into()).trim_end_matches('/').into(),
            ollama_model: env("OLLAMA_MODEL").unwrap_or_else(|| "llama3.1".into()),
            openai_base_url: env("OPENAI_BASE_URL").unwrap_or_else(|| "http://127.0.0.1:1234/v1".into()).trim_end_matches('/').into(),
            openai_model: env("OPENAI_MODEL").unwrap_or_else(|| "local-model".into()),
            openai_api_key: env("OPENAI_API_KEY"),
            workdir,
        }
    }

    pub fn model_for(&self, kind: ProviderKind) -> Option<String> {
        match kind {
            ProviderKind::Claude => self.claude_model.clone(),
            ProviderKind::Codex => self.codex_model.clone(),
            ProviderKind::Copilot => self.copilot_model.clone(),
            ProviderKind::Ollama => Some(self.ollama_model.clone()),
            ProviderKind::Openai => Some(self.openai_model.clone()),
            ProviderKind::Simulated => Some("simulated".into()),
        }
    }
}

#[derive(Debug, Clone)]
pub struct LlmRequest {
    pub provider: ProviderKind,
    pub model: Option<String>,
    /// Stable, byte-identical across agents in a run so the provider's prompt cache can reuse it.
    pub system: String,
    pub prompt: String,
    /// Let the provider use its own web search/fetch tools.
    pub web: bool,
    pub timeout: Duration,
    /// When set, the provider is asked for JSON that matches this schema (Claude `--json-schema`).
    pub json_schema: Option<Value>,
    /// Live progress (web searches, page reads, reasoning) while the call runs.
    pub progress: Option<ProgressSink>,
    /// Images (chart screenshots) the model should look at.
    pub attachments: Vec<PathBuf>,
}

/// What an agent is doing right now, parsed from the provider's event stream.
#[derive(Debug, Clone)]
pub enum Progress {
    Search(String),
    Fetch(String),
    Result(String),
    Thinking(String),
    Writing(String),
    Tool(String),
    /// Provider subscription window status (Claude `rate_limit_event`).
    RateLimit(Value),
}

pub type ProgressSink = tokio::sync::mpsc::UnboundedSender<Progress>;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Usage {
    pub input: i64,
    pub output: i64,
    pub cache_read: i64,
    pub cache_write: i64,
    pub web_searches: i64,
    /// True when the numbers are estimated from text length (provider reported nothing).
    pub estimated: bool,
}

impl Usage {
    pub fn add(&mut self, other: &Usage) {
        self.input += other.input;
        self.output += other.output;
        self.cache_read += other.cache_read;
        self.cache_write += other.cache_write;
        self.web_searches += other.web_searches;
        self.estimated |= other.estimated;
    }

    fn estimate(prompt: &str, output: &str) -> Self {
        Self { input: estimate_tokens(prompt) as i64, output: estimate_tokens(output) as i64, estimated: true, ..Default::default() }
    }
}

#[derive(Debug, Clone)]
pub struct LlmResponse {
    pub text: String,
    /// Parsed JSON when the provider returned structured output.
    pub structured: Option<Value>,
    pub usage: Usage,
    pub model: Option<String>,
    pub duration_ms: u64,
    pub cache_hit: bool,
    /// Subscription window reported by the provider, e.g. Claude's five-hour window status and reset time.
    pub rate_limit: Option<Value>,
}

#[derive(Debug, Clone)]
pub struct LlmError {
    pub class: ErrorClass,
    pub message: String,
}

impl LlmError {
    fn new(message: impl Into<String>) -> Self {
        let message = message.into();
        Self { class: classify(&message), message }
    }
}

impl std::fmt::Display for LlmError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{:?}: {}", self.class, self.message)
    }
}

pub async fn complete(config: &ProviderConfig, http: &reqwest::Client, request: &LlmRequest) -> Result<LlmResponse, LlmError> {
    let started = Instant::now();
    let mut response = match request.provider {
        ProviderKind::Claude => claude(config, request).await,
        ProviderKind::Codex => codex(config, request).await,
        ProviderKind::Copilot => copilot(config, request).await,
        ProviderKind::Ollama => ollama(config, http, request).await,
        ProviderKind::Openai => openai_compatible(config, http, request).await,
        ProviderKind::Simulated => Ok(simulated(request)),
    }?;
    response.duration_ms = started.elapsed().as_millis() as u64;
    Ok(response)
}

/// Availability probe for the settings screen.
pub async fn probe(config: &ProviderConfig, http: &reqwest::Client, kind: ProviderKind) -> Value {
    let (available, detail) = match kind {
        ProviderKind::Claude => cli_probe(&config.claude_bin),
        ProviderKind::Codex => cli_probe(&config.codex_bin),
        ProviderKind::Copilot => cli_probe(&config.copilot_bin),
        ProviderKind::Ollama => match http.get(format!("{}/api/tags", config.ollama_url)).timeout(Duration::from_secs(2)).send().await {
            Ok(response) if response.status().is_success() => (true, config.ollama_url.clone()),
            Ok(response) => (false, format!("{} answered {}", config.ollama_url, response.status())),
            Err(_) => (false, format!("no Ollama server at {}", config.ollama_url)),
        },
        ProviderKind::Openai => match http.get(format!("{}/models", config.openai_base_url)).timeout(Duration::from_secs(2)).send().await {
            Ok(response) if response.status().is_success() => (true, config.openai_base_url.clone()),
            _ => (false, format!("no OpenAI-compatible server at {}", config.openai_base_url)),
        },
        ProviderKind::Simulated => (true, "offline demo provider".into()),
    };
    json!({
        "id": kind.as_str(),
        "available": available,
        "detail": detail,
        "model": config.model_for(kind),
        "native_web": kind.native_web(),
        "tested": matches!(kind, ProviderKind::Claude | ProviderKind::Simulated),
    })
}

fn cli_probe(binary: &str) -> (bool, String) {
    match process::on_path(binary) {
        Some(path) => (true, path.display().to_string()),
        None if std::path::Path::new(binary).is_file() => (true, binary.into()),
        None => (false, format!("`{binary}` not found on PATH")),
    }
}

async fn write_prompt_file(config: &ProviderConfig, name: &str, contents: &str) -> Result<PathBuf, LlmError> {
    let dir = config.workdir.join("prompts");
    tokio::fs::create_dir_all(&dir).await.map_err(|error| LlmError::new(format!("prompt dir: {error}")))?;
    // Content-addressed name: identical system prompts map to the same file across agents.
    let path = dir.join(format!("{name}-{}.md", &super::ledger::sha256_hex(contents)[..16]));
    if !path.exists() {
        tokio::fs::write(&path, contents).await.map_err(|error| LlmError::new(format!("prompt file: {error}")))?;
    }
    Ok(path)
}

/// Claude Code: `claude -p --output-format stream-json` in a lean, cache-friendly configuration. Runs on the user's
/// own Claude subscription (OAuth login of the installed CLI); the `total_cost_usd` the CLI prints is an API-price
/// estimate, not a charge, so it is ignored. The stream is parsed live so every web search, page fetch and
/// reasoning step is reported as progress while the agent works.
/// `--strict-mcp-config`, `--setting-sources ""` and a custom system prompt keep each call's fixed context to a few
/// hundred tokens instead of ~33k (measured on this machine). Only WebSearch/WebFetch are enabled, never shell or file tools.
async fn claude(config: &ProviderConfig, request: &LlmRequest) -> Result<LlmResponse, LlmError> {
    tokio::fs::create_dir_all(&config.workdir).await.map_err(|error| LlmError::new(format!("workdir: {error}")))?;
    let system_file = write_prompt_file(config, "system", &request.system).await?;
    let mut args: Vec<String> = vec![
        "-p".into(), "--output-format".into(), "stream-json".into(), "--verbose".into(),
        "--no-session-persistence".into(),
        "--strict-mcp-config".into(),
        "--setting-sources".into(), "".into(),
        "--disable-slash-commands".into(),
        "--system-prompt-file".into(), system_file.display().to_string(),
    ];
    // Only read-only tools: web for research, Read (scoped to the upload folder) when charts are attached.
    let mut tools: Vec<&str> = Vec::new();
    if request.web { tools.extend(["WebSearch", "WebFetch"]) }
    if !request.attachments.is_empty() { tools.push("Read") }
    args.extend(["--tools".into(), tools.join(","), "--allowedTools".into(), tools.join(" ")]);
    let mut directories: Vec<String> = request.attachments.iter().filter_map(|path| path.parent()).map(|dir| dir.display().to_string()).collect();
    directories.sort();
    directories.dedup();
    for directory in directories {
        args.extend(["--add-dir".into(), directory]);
    }
    if let Some(model) = request.model.as_ref().or(config.claude_model.as_ref()) {
        args.extend(["--model".into(), model.clone()]);
    }
    if let Some(schema) = &request.json_schema {
        args.extend(["--json-schema".into(), schema.to_string()]);
    }
    let lines = spawn_line_parser(request.progress.clone(), claude_progress);
    let output = process::run(ProcessSpec {
        program: config.claude_bin.clone(),
        args,
        stdin: Some(with_attachment_note(&request.prompt, &request.attachments)),
        cwd: config.workdir.clone(),
        env: HashMap::new(),
        timeout: request.timeout,
        lines,
    })
    .await
    .map_err(LlmError::new)?;
    if output.timed_out {
        return Err(LlmError { class: ErrorClass::Transient, message: format!("claude timed out after {}s", request.timeout.as_secs()) });
    }
    let mut parsed = Value::Null;
    let mut rate_limit = None;
    for line in output.stdout.lines() {
        let Ok(event) = serde_json::from_str::<Value>(line) else { continue };
        match event["type"].as_str() {
            Some("result") => parsed = event,
            Some("rate_limit_event") => rate_limit = Some(event["rate_limit_info"].clone()),
            _ => {}
        }
    }
    if parsed.is_null() {
        return Err(LlmError::new(format!("claude returned no result (exit {:?}): {}", output.exit_code, tail(&format!("{}\n{}", output.stdout, output.stderr), 600))));
    }
    let usage_json = &parsed["usage"];
    let usage = Usage {
        input: usage_json["input_tokens"].as_i64().unwrap_or(0),
        output: usage_json["output_tokens"].as_i64().unwrap_or(0),
        cache_read: usage_json["cache_read_input_tokens"].as_i64().unwrap_or(0),
        cache_write: usage_json["cache_creation_input_tokens"].as_i64().unwrap_or(0),
        web_searches: usage_json["server_tool_use"]["web_search_requests"].as_i64().unwrap_or(0),
        estimated: false,
    };
    let text = parsed["result"].as_str().unwrap_or_default().to_string();
    if parsed["is_error"].as_bool().unwrap_or(false) || parsed["subtype"].as_str().is_some_and(|subtype| subtype != "success") {
        let reason = parsed["subtype"].as_str().unwrap_or("error");
        return Err(LlmError::new(format!("claude {reason}: {}", if text.is_empty() { tail(&output.stderr, 400) } else { tail(&text, 400) })));
    }
    let structured = parsed.get("structured_output").filter(|value| !value.is_null()).cloned();
    let model = parsed["modelUsage"].as_object().and_then(|models| models.keys().next().cloned());
    Ok(LlmResponse { text, structured, usage, model, duration_ms: 0, cache_hit: false, rate_limit })
}

/// Live progress from one CLI line of Claude's stream-json output.
fn claude_progress(line: &str) -> Vec<Progress> {
    let Ok(event) = serde_json::from_str::<Value>(line) else { return vec![] };
    let mut out = Vec::new();
    match event["type"].as_str() {
        Some("assistant") => {
            for block in event["message"]["content"].as_array().into_iter().flatten() {
                match block["type"].as_str() {
                    Some("tool_use") => match block["name"].as_str() {
                        Some("WebSearch") => out.push(Progress::Search(block["input"]["query"].as_str().unwrap_or_default().to_string())),
                        Some("WebFetch") => out.push(Progress::Fetch(block["input"]["url"].as_str().unwrap_or_default().to_string())),
                        Some(name) => out.push(Progress::Tool(name.to_string())),
                        None => {}
                    },
                    Some("thinking") => {
                        if let Some(text) = block["thinking"].as_str().filter(|text| !text.trim().is_empty()) {
                            out.push(Progress::Thinking(snippet(text, 220)));
                        }
                    }
                    Some("text") => {
                        if let Some(text) = block["text"].as_str().filter(|text| !text.trim().is_empty()) {
                            out.push(Progress::Writing(snippet(text, 220)));
                        }
                    }
                    _ => {}
                }
            }
        }
        Some("user") => {
            for block in event["message"]["content"].as_array().into_iter().flatten() {
                if block["type"] == "tool_result" {
                    let content = match &block["content"] { Value::String(text) => text.clone(), other => other.to_string() };
                    let links = content.matches("\"url\"").count();
                    out.push(Progress::Result(if links > 0 { format!("{links} results") } else { format!("{} chars read", content.len()) }));
                }
            }
        }
        Some("rate_limit_event") => out.push(Progress::RateLimit(event["rate_limit_info"].clone())),
        _ => {}
    }
    out
}

fn with_attachment_note(prompt: &str, attachments: &[PathBuf]) -> String {
    if attachments.is_empty() {
        return prompt.to_string();
    }
    let list = attachments.iter().map(|path| format!("- {}", path.display())).collect::<Vec<_>>().join("\n");
    format!("{prompt}\n\n# Attached chart screenshots\nOpen every file below with the Read tool and analyse what you see before writing:\n{list}")
}

fn snippet(text: &str, max: usize) -> String {
    let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= max { flat } else { format!("{}…", flat.chars().take(max).collect::<String>()) }
}

/// Feeds stdout lines through `parse` and forwards the resulting progress to the caller, if it asked for progress.
fn spawn_line_parser(progress: Option<ProgressSink>, parse: fn(&str) -> Vec<Progress>) -> Option<tokio::sync::mpsc::UnboundedSender<String>> {
    let progress = progress?;
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel::<String>();
    tokio::spawn(async move {
        while let Some(line) = receiver.recv().await {
            for item in parse(&line) {
                let _ = progress.send(item);
            }
        }
    });
    Some(sender)
}

/// Codex CLI (untested here): `codex exec --json`, prompt on stdin, JSONL events on stdout.
async fn codex(config: &ProviderConfig, request: &LlmRequest) -> Result<LlmResponse, LlmError> {
    tokio::fs::create_dir_all(&config.workdir).await.map_err(|error| LlmError::new(format!("workdir: {error}")))?;
    let mut args: Vec<String> = vec!["exec".into(), "--json".into(), "--skip-git-repo-check".into(), "--ephemeral".into(), "-s".into(), "read-only".into()];
    if request.web {
        args.insert(0, "--search".into());
    }
    if let Some(model) = request.model.as_ref().or(config.codex_model.as_ref()) {
        args.extend(["--model".into(), model.clone()]);
    }
    for image in &request.attachments {
        args.extend(["-i".into(), image.display().to_string()]);
    }
    args.push("-".into());
    let lines = spawn_line_parser(request.progress.clone(), codex_progress);
    let output = process::run(ProcessSpec {
        program: config.codex_bin.clone(),
        args,
        stdin: Some(format!("{}\n\n{}", request.system, request.prompt)),
        cwd: config.workdir.clone(),
        env: HashMap::new(),
        timeout: request.timeout,
        lines,
    })
    .await
    .map_err(LlmError::new)?;
    if output.timed_out {
        return Err(LlmError { class: ErrorClass::Transient, message: "codex timed out".into() });
    }
    let mut text = String::new();
    let mut usage = Usage::default();
    let mut failure = None;
    let mut saw_terminal = false;
    for line in output.stdout.lines() {
        let Ok(event) = serde_json::from_str::<Value>(line) else { continue };
        match event["type"].as_str() {
            Some("item.completed") if event["item"]["type"] == "agent_message" => {
                text = event["item"]["text"].as_str().unwrap_or_default().to_string();
            }
            Some("turn.completed") => {
                saw_terminal = true;
                let reported = &event["usage"];
                let cached = reported["cached_input_tokens"].as_i64().unwrap_or(0);
                usage.input += reported["input_tokens"].as_i64().unwrap_or(0) - cached;
                usage.cache_read += cached;
                usage.output += reported["output_tokens"].as_i64().unwrap_or(0);
            }
            Some("turn.failed") => {
                saw_terminal = true;
                failure = event["error"]["message"].as_str().map(String::from);
            }
            Some("error") => failure = event["message"].as_str().map(String::from),
            _ => {}
        }
    }
    if let Some(message) = failure {
        return Err(LlmError::new(format!("codex: {message}")));
    }
    if !saw_terminal {
        // Paperclip treats a CLI exit without a terminal event as an infrastructure crash, not an agent failure.
        return Err(LlmError { class: ErrorClass::Transient, message: format!("codex harness crash (exit {:?}): {}", output.exit_code, tail(&output.stderr, 300)) });
    }
    Ok(LlmResponse { text, structured: None, usage, model: request.model.clone(), duration_ms: 0, cache_hit: false, rate_limit: None })
}

fn codex_progress(line: &str) -> Vec<Progress> {
    let Ok(event) = serde_json::from_str::<Value>(line) else { return vec![] };
    let item = &event["item"];
    match (event["type"].as_str(), item["type"].as_str()) {
        (Some("item.started"), Some("web_search")) => vec![Progress::Search(item["query"].as_str().unwrap_or_default().to_string())],
        (Some("item.completed"), Some("reasoning")) => item["text"].as_str().map(|text| vec![Progress::Thinking(snippet(text, 220))]).unwrap_or_default(),
        (Some("item.completed"), Some("agent_message")) => item["text"].as_str().map(|text| vec![Progress::Writing(snippet(text, 220))]).unwrap_or_default(),
        _ => vec![],
    }
}

/// GitHub Copilot CLI (untested here): non-interactive `copilot -p`, plain-text output, usage estimated.
async fn copilot(config: &ProviderConfig, request: &LlmRequest) -> Result<LlmResponse, LlmError> {
    tokio::fs::create_dir_all(&config.workdir).await.map_err(|error| LlmError::new(format!("workdir: {error}")))?;
    let prompt = format!("{}\n\n{}", request.system, request.prompt);
    let mut args: Vec<String> = vec!["-p".into(), prompt.clone(), "--allow-all-tools".into()];
    if let Some(model) = request.model.as_ref().or(config.copilot_model.as_ref()) {
        args.extend(["--model".into(), model.clone()]);
    }
    let output = process::run(ProcessSpec {
        program: config.copilot_bin.clone(),
        args,
        stdin: None,
        cwd: config.workdir.clone(),
        env: HashMap::new(),
        timeout: request.timeout,
        lines: None,
    })
    .await
    .map_err(LlmError::new)?;
    if output.timed_out {
        return Err(LlmError { class: ErrorClass::Transient, message: "copilot timed out".into() });
    }
    if output.exit_code != Some(0) {
        return Err(LlmError::new(format!("copilot exited {:?}: {}", output.exit_code, tail(&output.stderr, 400))));
    }
    let text = output.stdout.trim().to_string();
    Ok(LlmResponse { usage: Usage::estimate(&prompt, &text), text, structured: None, model: request.model.clone(), duration_ms: 0, cache_hit: false, rate_limit: None })
}

/// Ollama HTTP API (untested here). No native browsing: the harness feeds it search results.
async fn ollama(config: &ProviderConfig, http: &reqwest::Client, request: &LlmRequest) -> Result<LlmResponse, LlmError> {
    let model = request.model.clone().unwrap_or_else(|| config.ollama_model.clone());
    let images: Vec<String> = request.attachments.iter().filter_map(|path| std::fs::read(path).ok()).map(|bytes| { use base64::Engine; base64::engine::general_purpose::STANDARD.encode(bytes) }).collect();
    let mut body = json!({
        "model": model,
        "stream": false,
        "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.prompt, "images": images}],
    });
    if let Some(schema) = &request.json_schema {
        body["format"] = schema.clone();
    }
    let response = http
        .post(format!("{}/api/chat", config.ollama_url))
        .timeout(request.timeout)
        .json(&body)
        .send()
        .await
        .map_err(|error| LlmError::new(format!("ollama request failed: {error}")))?;
    let status = response.status();
    let value: Value = response.json().await.map_err(|error| LlmError::new(format!("ollama returned invalid JSON: {error}")))?;
    if !status.is_success() {
        return Err(LlmError::new(format!("ollama {status}: {}", value["error"].as_str().unwrap_or_default())));
    }
    let text = value["message"]["content"].as_str().unwrap_or_default().to_string();
    let usage = Usage {
        input: value["prompt_eval_count"].as_i64().unwrap_or(0),
        output: value["eval_count"].as_i64().unwrap_or(0),
        ..Default::default()
    };
    Ok(LlmResponse { structured: serde_json::from_str(&text).ok().filter(|_| request.json_schema.is_some()), text, usage, model: Some(model), duration_ms: 0, cache_hit: false, rate_limit: None })
}

/// Any OpenAI-compatible chat endpoint: LM Studio, vLLM, llama.cpp server, OpenRouter, etc. (untested here).
async fn openai_compatible(config: &ProviderConfig, http: &reqwest::Client, request: &LlmRequest) -> Result<LlmResponse, LlmError> {
    let model = request.model.clone().unwrap_or_else(|| config.openai_model.clone());
    let mut builder = http.post(format!("{}/chat/completions", config.openai_base_url)).timeout(request.timeout).json(&json!({
        "model": model,
        "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.prompt}],
    }));
    if let Some(key) = &config.openai_api_key {
        builder = builder.bearer_auth(key);
    }
    let response = builder.send().await.map_err(|error| LlmError::new(format!("openai-compatible request failed: {error}")))?;
    let status = response.status();
    let value: Value = response.json().await.map_err(|error| LlmError::new(format!("invalid JSON: {error}")))?;
    if !status.is_success() {
        return Err(LlmError::new(format!("openai-compatible {status}: {}", value["error"]["message"].as_str().unwrap_or_default())));
    }
    let text = value["choices"][0]["message"]["content"].as_str().unwrap_or_default().to_string();
    let reported = &value["usage"];
    let cached = reported["prompt_tokens_details"]["cached_tokens"].as_i64().unwrap_or(0);
    let usage = Usage {
        input: reported["prompt_tokens"].as_i64().unwrap_or(0) - cached,
        output: reported["completion_tokens"].as_i64().unwrap_or(0),
        cache_read: cached,
        ..Default::default()
    };
    Ok(LlmResponse { text, structured: None, usage, model: Some(model), duration_ms: 0, cache_hit: false, rate_limit: None })
}

/// Offline provider so the whole pipeline can be exercised without any model installed.
fn simulated(request: &LlmRequest) -> LlmResponse {
    let text = if let Some(schema) = &request.json_schema {
        super::simulation::structured_reply(schema, &request.prompt).to_string()
    } else {
        super::simulation::text_reply(&request.prompt)
    };
    LlmResponse {
        structured: request.json_schema.as_ref().and_then(|_| serde_json::from_str(&text).ok()),
        usage: Usage::estimate(&format!("{}{}", request.system, request.prompt), &text),
        text,
        model: Some("simulated".into()),
        duration_ms: 0,
        cache_hit: false,
        rate_limit: None,
    }
}

fn tail(text: &str, max: usize) -> String {
    let trimmed = text.trim();
    let start = trimmed.char_indices().rev().nth(max).map(|(index, _)| index).unwrap_or(0);
    trimmed[start..].to_string()
}
