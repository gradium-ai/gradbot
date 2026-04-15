// Copyright (c) Gradium, all rights reserved.
// LLM API key resolution: api_key parameter → LLM_API_KEY env var → OPENAI_API_KEY env var (backwards compat).
// gpt-5-chat-latest is less prone to use reasoning but more expensive.
use anyhow::{Context, Result};
use async_openai as oai;
use async_openai::types::ChatCompletionRequestUserMessageContentPart;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{Mutex, mpsc};

// ============================================================================
// Text sanitization
// ============================================================================

/// Strip model control tokens (e.g. Gemma's `<|channel>thought<channel|>`)
/// from text before feeding it back into the conversation history.
fn strip_control_tokens(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("<|") {
        out.push_str(&rest[..start]);
        // Find closing |> or >
        if let Some(end) = rest[start..].find("|>") {
            rest = &rest[start + end + 2..];
        } else if let Some(end) = rest[start..].find('>') {
            rest = &rest[start + end + 1..];
        } else {
            break;
        }
    }
    out.push_str(rest);
    // Also strip <word|> patterns (e.g. <channel|>)
    let mut result = String::with_capacity(out.len());
    let mut rest = out.as_str();
    while let Some(start) = rest.find('<') {
        if let Some(end) = rest[start..].find("|>") {
            let inner = &rest[start + 1..start + end];
            if inner.chars().all(|c| c.is_alphanumeric() || c == '_') {
                result.push_str(&rest[..start]);
                rest = &rest[start + end + 2..];
                continue;
            }
        }
        result.push_str(&rest[..start + 1]);
        rest = &rest[start + 1..];
    }
    result.push_str(rest);
    result.trim().to_string()
}

// ============================================================================
// Tool Call Support
// ============================================================================

/// Status of a pending tool call, tracking what has been injected into history.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolCallStatus {
    /// Just created, nothing injected yet
    New,
    /// Injected PENDING result, awaiting actual result
    Pending,
    /// At least one result received and injected
    AnsweredAtLeastOnce,
}

/// A pending tool call being tracked by the session.
/// Result type for tool calls - either success with JSON value or error.
pub type ToolResult = anyhow::Result<Value>;

struct PendingToolCall {
    /// Unique identifier for this tool call
    call_id: String,
    /// Name of the tool/function being called
    tool_name: String,
    /// Arguments passed to the tool
    args: Value,
    /// Receiver for results from the client
    result_rx: mpsc::Receiver<ToolResult>,
    /// Current status
    status: ToolCallStatus,
}

/// Handle given to the client to send tool call results.
///
/// The client can send results via this handle. Multiple sends are allowed;
/// only the last value at injection time is used. If dropped without sending
/// any result, the tool call is marked as LOST.
#[derive(Debug)]
pub struct ToolCallHandle {
    tx: mpsc::Sender<ToolResult>,
    /// Shared counter to signal when results are ready
    results_pending: Arc<std::sync::atomic::AtomicU32>,
}

impl ToolCallHandle {
    /// Send a successful result for this tool call.
    /// Can be called multiple times; the last value wins.
    pub async fn send(&self, result: Value) -> Result<(), mpsc::error::SendError<ToolResult>> {
        let res = self.tx.send(Ok(result)).await;
        if res.is_ok() {
            self.results_pending
                .fetch_add(1, std::sync::atomic::Ordering::Release);
        }
        res
    }

    /// Send an error result for this tool call.
    pub async fn send_error(
        &self,
        error: anyhow::Error,
    ) -> Result<(), mpsc::error::SendError<ToolResult>> {
        let res = self.tx.send(Err(error)).await;
        if res.is_ok() {
            self.results_pending
                .fetch_add(1, std::sync::atomic::Ordering::Release);
        }
        res
    }

    /// Try to send a successful result without waiting.
    pub fn try_send(&self, result: Value) -> Result<(), mpsc::error::TrySendError<ToolResult>> {
        let res = self.tx.try_send(Ok(result));
        if res.is_ok() {
            self.results_pending
                .fetch_add(1, std::sync::atomic::Ordering::Release);
        }
        res
    }

    /// Try to send an error result without waiting.
    pub fn try_send_error(
        &self,
        error: anyhow::Error,
    ) -> Result<(), mpsc::error::TrySendError<ToolResult>> {
        let res = self.tx.try_send(Err(error));
        if res.is_ok() {
            self.results_pending
                .fetch_add(1, std::sync::atomic::Ordering::Release);
        }
        res
    }
}

/// A tool call made by the LLM, sent to the client for handling.
#[derive(Debug, Clone)]
pub struct ToolCall {
    /// Unique identifier for this tool call
    pub call_id: String,
    /// Name of the tool/function being called
    pub tool_name: String,
    /// Arguments as JSON
    pub args: Value,
}

/// Tool definition for the LLM.
#[derive(Debug, Clone)]
pub struct ToolDef {
    /// Name of the tool
    pub name: String,
    /// Description of what the tool does
    pub description: String,
    /// JSON schema for the parameters
    pub parameters: Value,
}

/// Global cache for model listings by base URL.
/// Key is the base URL (or empty string for default OpenAI API).
static MODEL_CACHE: std::sync::OnceLock<tokio::sync::RwLock<HashMap<String, Vec<String>>>> =
    std::sync::OnceLock::new();

fn get_model_cache() -> &'static tokio::sync::RwLock<HashMap<String, Vec<String>>> {
    MODEL_CACHE.get_or_init(|| tokio::sync::RwLock::new(HashMap::new()))
}

/// List model IDs from an OpenAI-compatible API with lenient deserialization.
/// Only requires each model object to have an `id` field, ignoring extras
/// that servers like LM Studio may omit (e.g. `created`).
async fn list_model_ids(client: &oai::Client<oai::config::OpenAIConfig>) -> Result<Vec<String>> {
    use oai::config::Config;
    let url = client.config().url("/models");
    let headers = client.config().headers();
    let resp = reqwest::Client::new()
        .get(&url)
        .headers(headers)
        .send()
        .await
        .context(format!("LLM: HTTP request to {url} failed"))?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!("LLM: GET {url} returned {status}: {body}");
    }
    let body: Value = resp
        .json()
        .await
        .context("LLM: failed to parse JSON from /models")?;
    let ids = body["data"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|m| m["id"].as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();
    Ok(ids)
}

#[derive(Clone)]
pub struct LlmConfig {
    pub user_prompt: String,
    pub language: crate::system_prompt::Lang,
    pub tools: Vec<ToolDef>,
}

impl LlmConfig {
    pub fn new(
        user_prompt: String,
        language: crate::system_prompt::Lang,
        tools: Vec<ToolDef>,
    ) -> Self {
        Self {
            user_prompt,
            language,
            tools,
        }
    }

    /// Returns a new Arc only if the config values differ, otherwise returns the existing Arc.
    /// This allows callers to pass config repeatedly without worrying about Arc management -
    /// ptr_eq checks downstream will still work correctly.
    pub fn maybe_update(
        existing: &Arc<LlmConfig>,
        user_prompt: &str,
        language: crate::system_prompt::Lang,
        tools: Vec<ToolDef>,
    ) -> Arc<LlmConfig> {
        if existing.user_prompt == user_prompt
            && existing.language == language
            && existing.tools.len() == tools.len()
            && existing
                .tools
                .iter()
                .zip(tools.iter())
                .all(|(a, b)| a.name == b.name)
        {
            existing.clone()
        } else {
            Arc::new(LlmConfig::new(user_prompt.to_string(), language, tools))
        }
    }
}

#[derive(Clone)]
pub struct Llm {
    max_completion_tokens: u32,
    model_name: String,
    reqwest_client: reqwest::Client,
    api_key: String,
    completions_url: String,
}

impl Llm {
    /// Create a new LLM client.
    ///
    /// API key resolution order:
    /// 1. `api_key` parameter if provided
    /// 2. `LLM_API_KEY` environment variable if set
    /// 3. `OPENAI_API_KEY` environment variable (backwards compatibility)
    ///
    /// Model resolution order:
    /// 1. `model_name` parameter if provided
    /// 2. `LLM_MODEL` environment variable if set
    /// 3. Auto-detect: lists available models and uses the only one if exactly one exists
    pub async fn new(
        base_url: Option<String>,
        max_completion_tokens: u32,
        model_name: Option<String>,
        api_key: Option<String>,
    ) -> Result<Self> {
        // Resolve API key: parameter > LLM_API_KEY > OPENAI_API_KEY
        let api_key = api_key
            .or_else(|| std::env::var("LLM_API_KEY").ok())
            .or_else(|| std::env::var("OPENAI_API_KEY").ok())
            .unwrap_or_default();

        // Resolve base URL: parameter > LLM_BASE_URL env var > OpenAI default
        let base_url = base_url.or_else(|| std::env::var("LLM_BASE_URL").ok());

        // Cache key for model listing (computed before base_url is moved)
        let cache_key = base_url.as_deref().unwrap_or("").to_string();

        let client = match base_url {
            None => {
                let config = oai::config::OpenAIConfig::default().with_api_key(&api_key);
                oai::Client::with_config(config)
            }
            Some(base_url) => {
                let base_url = base_url.trim_end_matches('/');
                let config = oai::config::OpenAIConfig::default()
                    .with_api_base(base_url)
                    .with_api_key(&api_key);
                oai::Client::with_config(config)
            }
        };

        // Resolve model name: parameter > LLM_MODEL env var > auto-detect (cached)
        let model_name = match model_name.or_else(|| std::env::var("LLM_MODEL").ok()) {
            Some(name) => name,
            None => {
                // Fast path: read lock to check cache
                let cached = get_model_cache().read().await.get(&cache_key).cloned();
                let model_ids = if let Some(ids) = cached {
                    tracing::debug!(base_url = %cache_key, "using cached model list");
                    ids
                } else {
                    // Slow path: write lock, check again, then API call if needed
                    let mut cache = get_model_cache().write().await;
                    if let Some(ids) = cache.get(&cache_key) {
                        ids.clone()
                    } else {
                        tracing::info!("no model name provided, listing available models");
                        let ids = list_model_ids(&client)
                            .await
                            .context("LLM: failed to list models")?;
                        cache.insert(cache_key.clone(), ids.clone());
                        ids
                    }
                };

                if model_ids.len() == 1 {
                    tracing::info!(model = %model_ids[0], "using the only available model");
                    model_ids.into_iter().next().unwrap()
                } else if model_ids.is_empty() {
                    anyhow::bail!("no models available from LLM API");
                } else {
                    anyhow::bail!(
                        "multiple models available ({:?}), please specify model_name or set LLM_MODEL env var",
                        model_ids
                    );
                }
            }
        };

        let completions_url = format!(
            "{}/chat/completions",
            if cache_key.is_empty() {
                "https://api.openai.com/v1"
            } else {
                &cache_key
            }
        );
        tracing::info!(model = %model_name, completions_url = %completions_url, "llm client created");
        Ok(Self {
            max_completion_tokens,
            model_name,
            reqwest_client: reqwest::Client::new(),
            api_key,
            completions_url,
        })
    }

    pub fn session(&self) -> Result<LlmSession> {
        let slf = LlmSession {
            llm: self.clone(),
            max_completion_tokens: self.max_completion_tokens,
            model_name: self.model_name.clone(),
            messages: vec![],
            transmitted: Arc::new(Mutex::new(vec![])),
            last_config: None,
            pending_tool_calls: Arc::new(Mutex::new(vec![])),
            tool_results_pending: Arc::new(std::sync::atomic::AtomicU32::new(0)),
        };
        Ok(slf)
    }
}

pub struct LlmSession {
    llm: Llm,
    max_completion_tokens: u32,
    model_name: String,
    messages: Vec<oai::types::ChatCompletionRequestMessage>,
    transmitted: Arc<Mutex<Vec<String>>>,
    last_config: Option<Arc<LlmConfig>>,
    pending_tool_calls: Arc<Mutex<Vec<PendingToolCall>>>,
    /// Counter incremented when tool results are sent, decremented when processed.
    tool_results_pending: Arc<std::sync::atomic::AtomicU32>,
}

impl LlmSession {
    /// Check if there are any tool call results waiting to be processed.
    /// This allows the caller to trigger an LLM call to process tool results
    /// without waiting for user input.
    pub fn has_pending_tool_results(&self) -> bool {
        self.tool_results_pending
            .load(std::sync::atomic::Ordering::Acquire)
            > 0
    }

    /// Check if there are any NEW tool calls that haven't been injected into history yet.
    /// This is used to trigger LLM calls for tool calls that are still awaiting results.
    pub async fn has_new_tool_calls(&self) -> bool {
        let pending = self.pending_tool_calls.lock().await;
        pending
            .iter()
            .any(|p| matches!(p.status, ToolCallStatus::New))
    }

    /// Clear the pending tool results counter (called after processing).
    fn clear_tool_results_pending(&self) {
        self.tool_results_pending
            .store(0, std::sync::atomic::Ordering::Release);
    }
}

/// Item yielded by the LLM response stream.
#[derive(Debug)]
pub enum LlmResponseItem {
    /// Text content from the LLM
    Text(String),
    /// A tool call from the LLM with a handle to send results
    ToolCall {
        call: ToolCall,
        handle: ToolCallHandle,
    },
    /// An error from the LLM stream
    Error(String),
}

pub struct LlmResponseStream {
    rx: tokio::sync::mpsc::Receiver<LlmResponseItem>,
    _jh: crate::utils::JoinHandleAbortOnDrop,
}

impl LlmResponseStream {
    pub async fn recv(&mut self) -> Option<LlmResponseItem> {
        self.rx.recv().await
    }

    pub fn abort(mut self) {
        self.rx.close();
    }
}

impl LlmSession {
    pub fn transmitted(&self) -> Arc<Mutex<Vec<String>>> {
        self.transmitted.clone()
    }

    pub async fn incorporate_previous_generation(&mut self) -> Result<Option<String>> {
        let previous_generation = {
            let mut transmitted = self.transmitted.lock().await;
            let v: Vec<String> = std::mem::take(&mut *transmitted);
            v
        };
        if !previous_generation.is_empty() {
            let full_response = strip_control_tokens(&previous_generation.join(" "));
            if full_response.is_empty() {
                return Ok(None);
            }
            self.messages.push(
                oai::types::ChatCompletionRequestAssistantMessageArgs::default()
                    .content(full_response.clone())
                    .build()?
                    .into(),
            );
            Ok(Some(full_response))
        } else {
            Ok(None)
        }
    }

    /// Process pending tool calls: drain results and inject into message history.
    async fn process_pending_tool_calls(&mut self) {
        use oai::types::{
            ChatCompletionMessageToolCall, ChatCompletionRequestAssistantMessageArgs,
            ChatCompletionRequestToolMessageArgs, FunctionCall,
        };

        let mut pending_calls = self.pending_tool_calls.lock().await;
        let mut to_remove = vec![];

        for (idx, pending) in pending_calls.iter_mut().enumerate() {
            // Drain all available results, keep only the last one
            let mut last_result: Option<ToolResult> = None;
            while let Ok(result) = pending.result_rx.try_recv() {
                last_result = Some(result);
            }

            // Check if channel is closed
            let is_closed = pending.result_rx.is_closed();

            if let Some(result) = last_result {
                // Got a result - inject tool call + result into history
                let tool_call = ChatCompletionMessageToolCall {
                    id: pending.call_id.clone(),
                    r#type: oai::types::ChatCompletionToolType::Function,
                    function: FunctionCall {
                        name: pending.tool_name.clone(),
                        arguments: pending.args.to_string(),
                    },
                };
                let assistant_msg = ChatCompletionRequestAssistantMessageArgs::default()
                    .tool_calls(vec![tool_call])
                    .build()
                    .expect("building assistant tool call message");
                self.messages.push(assistant_msg.into());

                // Format result - success as JSON, error as ERROR: message
                let result_content = match result {
                    Ok(value) => value.to_string(),
                    Err(e) => format!("ERROR: {}", e),
                };
                let tool_result_msg = ChatCompletionRequestToolMessageArgs::default()
                    .tool_call_id(&pending.call_id)
                    .content(result_content)
                    .build()
                    .expect("building tool result message");
                self.messages.push(tool_result_msg.into());

                pending.status = ToolCallStatus::AnsweredAtLeastOnce;
                if is_closed {
                    to_remove.push(idx);
                }
            } else if is_closed {
                // Channel closed without result - inject LOST if never answered
                if pending.status != ToolCallStatus::AnsweredAtLeastOnce {
                    let tool_call = ChatCompletionMessageToolCall {
                        id: pending.call_id.clone(),
                        r#type: oai::types::ChatCompletionToolType::Function,
                        function: FunctionCall {
                            name: pending.tool_name.clone(),
                            arguments: pending.args.to_string(),
                        },
                    };
                    let assistant_msg = ChatCompletionRequestAssistantMessageArgs::default()
                        .tool_calls(vec![tool_call])
                        .build()
                        .expect("building assistant tool call message");
                    self.messages.push(assistant_msg.into());

                    let tool_result_msg = ChatCompletionRequestToolMessageArgs::default()
                        .tool_call_id(&pending.call_id)
                        .content("LOST")
                        .build()
                        .expect("building tool result message");
                    self.messages.push(tool_result_msg.into());
                }
                to_remove.push(idx);
            } else if pending.status == ToolCallStatus::New {
                // First iteration without result - inject PENDING
                let tool_call = ChatCompletionMessageToolCall {
                    id: pending.call_id.clone(),
                    r#type: oai::types::ChatCompletionToolType::Function,
                    function: FunctionCall {
                        name: pending.tool_name.clone(),
                        arguments: pending.args.to_string(),
                    },
                };
                let assistant_msg = ChatCompletionRequestAssistantMessageArgs::default()
                    .tool_calls(vec![tool_call])
                    .build()
                    .expect("building assistant tool call message");
                self.messages.push(assistant_msg.into());

                let tool_result_msg = ChatCompletionRequestToolMessageArgs::default()
                    .tool_call_id(&pending.call_id)
                    .content("PENDING")
                    .build()
                    .expect("building tool result message");
                self.messages.push(tool_result_msg.into());

                pending.status = ToolCallStatus::Pending;
            }
            // If status == Pending and no result yet, do nothing
        }

        // Remove completed/lost tool calls (in reverse order to preserve indices)
        for idx in to_remove.into_iter().rev() {
            pending_calls.remove(idx);
        }

        // Clear the pending results counter since we've processed them
        self.clear_tool_results_pending();
    }

    pub async fn push(
        &mut self,
        user_msg: &str,
        config: Arc<LlmConfig>,
        extra_config: Option<&str>,
    ) -> Result<LlmResponseStream> {
        use oai::types::ChatCompletionRequestSystemMessageArgs as System;
        use oai::types::ChatCompletionRequestUserMessageArgs as User;

        // Check if config changed using pointer equality (fast path)
        let config_changed = match &self.last_config {
            Some(old) => !Arc::ptr_eq(old, &config),
            None => true,
        };

        if config_changed {
            let prompt =
                crate::system_prompt::system_prompt(config.language, Some(&config.user_prompt));
            let system_msg = System::default()
                .content(prompt)
                .build()
                .expect("building system prompt")
                .into();
            if self.messages.is_empty() {
                self.messages = vec![system_msg];
            } else {
                // Replace system message (always first)
                self.messages[0] = system_msg;
            }
            self.last_config = Some(config.clone());
        }

        // Process any pending tool calls - inject results/PENDING/LOST into history
        self.process_pending_tool_calls().await;

        // TODO(laurent): maybe use previous_response_id to handle the conversation state.
        if let Some(async_openai::types::ChatCompletionRequestMessage::User(user)) =
            self.messages.last_mut()
        {
            match &mut user.content {
                oai::types::ChatCompletionRequestUserMessageContent::Text(text) => {
                    text.push(' ');
                    text.push_str(user_msg);
                }
                oai::types::ChatCompletionRequestUserMessageContent::Array(array) => {
                    array.push(ChatCompletionRequestUserMessageContentPart::Text(
                        user_msg.into(),
                    ));
                }
            }
        } else {
            self.messages
                .push(User::default().content(user_msg).build()?.into());
        }
        tracing::debug!(?self.messages);

        // Build tools from config
        let tools: Vec<oai::types::ChatCompletionTool> = config
            .tools
            .iter()
            .map(|t| {
                oai::types::ChatCompletionToolArgs::default()
                    .r#type(oai::types::ChatCompletionToolType::Function)
                    .function(
                        oai::types::FunctionObjectArgs::default()
                            .name(&t.name)
                            .description(&t.description)
                            .parameters(t.parameters.clone())
                            .build()
                            .expect("building function object"),
                    )
                    .build()
                    .expect("building tool")
            })
            .collect();

        let mut request_builder = oai::types::CreateChatCompletionRequestArgs::default();
        request_builder
            .model(&self.model_name)
            .max_completion_tokens(self.max_completion_tokens)
            .messages(self.messages.clone());
        if !tools.is_empty() {
            request_builder.tools(tools);
        }
        let request = request_builder.build()?;

        // Serialize request to JSON, set stream=true, merge extra_config
        let mut body = serde_json::to_value(&request)?;
        body["stream"] = true.into();
        if let Some(extra) = extra_config {
            let extra: Value =
                serde_json::from_str(extra).context("LLM: failed to parse extra_config JSON")?;
            if let (Some(body_map), Some(extra_map)) = (body.as_object_mut(), extra.as_object()) {
                for (k, v) in extra_map {
                    body_map.insert(k.clone(), v.clone());
                }
            }
        }

        // POST with SSE using reqwest
        let response = self
            .llm
            .reqwest_client
            .post(&self.llm.completions_url)
            .bearer_auth(&self.llm.api_key)
            .header("Content-Type", "application/json")
            .json(&body)
            .send()
            .await
            .context("LLM: failed to send completion request")?;

        let status = response.status();
        if !status.is_success() {
            let err_body = response.text().await.unwrap_or_default();
            anyhow::bail!(
                "LLM: POST {} returned {status}: {err_body}",
                self.llm.completions_url
            );
        }

        let (tx, rx) = tokio::sync::mpsc::channel::<LlmResponseItem>(8);
        let pending_tool_calls = self.pending_tool_calls.clone();
        let tool_results_pending = self.tool_results_pending.clone();

        let jh = crate::utils::spawn_abort_on_drop("llm-loop", async move {
            use futures::StreamExt;
            // Accumulate tool call chunks by index
            let mut tool_call_accum: HashMap<u32, (String, String, String)> = HashMap::new(); // index -> (id, name, args)

            // Parse SSE stream line by line
            let mut byte_stream = response.bytes_stream();
            let mut line_buf = String::new();

            let mut sse_chunk_count: u64 = 0;
            tracing::info!("LLM SSE: entering byte_stream loop");
            while let Some(chunk_result) = byte_stream.next().await {
                if tx.is_closed() {
                    tracing::debug!("LLM SSE: tx closed, stopping");
                    break;
                }
                let chunk = match chunk_result {
                    Ok(c) => c,
                    Err(e) => {
                        tracing::error!(?e, "SSE stream read error");
                        let _ = tx.send(LlmResponseItem::Error(e.to_string())).await;
                        break;
                    }
                };
                sse_chunk_count += 1;
                let text = String::from_utf8_lossy(&chunk);
                if sse_chunk_count <= 3 {
                    tracing::debug!(
                        sse_chunk_count,
                        text_len = text.len(),
                        "LLM SSE chunk received"
                    );
                }
                line_buf.push_str(&text);

                // Process complete lines
                while let Some(newline_pos) = line_buf.find('\n') {
                    let line = line_buf[..newline_pos].trim_end_matches('\r').to_string();
                    line_buf = line_buf[newline_pos + 1..].to_string();

                    if line.is_empty() || line.starts_with(':') {
                        continue;
                    }
                    let data = if let Some(d) = line.strip_prefix("data: ") {
                        d.trim()
                    } else {
                        continue;
                    };
                    if data == "[DONE]" {
                        break;
                    }

                    let chunk: StreamChunk = match serde_json::from_str(data) {
                        Ok(c) => c,
                        Err(e) => {
                            tracing::warn!(?e, data, "failed to parse SSE chunk");
                            continue;
                        }
                    };

                    for choice in chunk.choices {
                        // Handle text content
                        if let Some(c) = choice.delta.content
                            && !c.is_empty()
                        {
                            tx.send(LlmResponseItem::Text(c)).await?;
                        }

                        // Handle tool calls (streamed as chunks)
                        if let Some(tool_calls) = choice.delta.tool_calls {
                            for tc_chunk in tool_calls {
                                let idx = tc_chunk.index;
                                let entry = tool_call_accum.entry(idx).or_insert_with(|| {
                                    (String::new(), String::new(), String::new())
                                });

                                if let Some(id) = tc_chunk.id {
                                    entry.0.push_str(&id);
                                }
                                if let Some(func) = tc_chunk.function {
                                    if let Some(name) = func.name {
                                        entry.1.push_str(&name);
                                    }
                                    if let Some(args) = func.arguments {
                                        entry.2.push_str(&args);
                                    }
                                }
                            }
                        }
                    }
                }
            }

            tracing::debug!(sse_chunk_count, "LLM SSE stream ended");

            // Stream ended - finalize any accumulated tool calls
            for (idx, (call_id, tool_name, args_str)) in tool_call_accum {
                if call_id.is_empty() || tool_name.is_empty() {
                    tracing::warn!(
                        idx,
                        call_id = %call_id,
                        tool_name = %tool_name,
                        args = %args_str,
                        "Dropping incomplete tool call (missing id or name)"
                    );
                    continue;
                }
                let args: Value = match serde_json::from_str(&args_str) {
                    Ok(v) => v,
                    Err(e) => {
                        tracing::warn!(
                            tool_name = %tool_name,
                            args = %args_str,
                            error = %e,
                            "Tool call has malformed JSON args, using empty object"
                        );
                        Value::Object(Default::default())
                    }
                };

                // Create channel for results
                let (result_tx, result_rx) = mpsc::channel::<ToolResult>(10);

                // Create pending tool call
                let pending = PendingToolCall {
                    call_id: call_id.clone(),
                    tool_name: tool_name.clone(),
                    args: args.clone(),
                    result_rx,
                    status: ToolCallStatus::New,
                };
                pending_tool_calls.lock().await.push(pending);

                // Send tool call to client
                let handle = ToolCallHandle {
                    tx: result_tx,
                    results_pending: tool_results_pending.clone(),
                };
                let call = ToolCall {
                    call_id,
                    tool_name,
                    args,
                };
                tx.send(LlmResponseItem::ToolCall { call, handle }).await?;
            }

            Ok::<_, anyhow::Error>(())
        });
        Ok(LlmResponseStream { rx, _jh: jh })
    }
}

// Minimal SSE response types for streaming chat completions
#[derive(serde::Deserialize)]
struct StreamChunk {
    choices: Vec<StreamChoice>,
}

#[derive(serde::Deserialize)]
struct StreamChoice {
    delta: StreamDelta,
}

#[derive(serde::Deserialize)]
struct StreamDelta {
    content: Option<String>,
    tool_calls: Option<Vec<StreamToolCall>>,
}

#[derive(serde::Deserialize)]
struct StreamToolCall {
    index: u32,
    id: Option<String>,
    function: Option<StreamFunction>,
}

#[derive(serde::Deserialize)]
struct StreamFunction {
    name: Option<String>,
    arguments: Option<String>,
}
