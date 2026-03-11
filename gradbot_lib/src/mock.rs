//! Mock implementations for testing gradbot.
//!
//! This module provides mock STT, TTS, and LLM clients for unit testing
//! without requiring network access.
//!
//! ## Mocking Time
//!
//! To control time in tests, use tokio's time mocking:
//!
//! ```ignore
//! #[tokio::test(start_paused = true)]
//! async fn test_with_time_control() {
//!     // Time starts paused at 0
//!
//!     // Advance time by 1 second
//!     tokio::time::advance(std::time::Duration::from_secs(1)).await;
//!
//!     // Or use pause/resume manually:
//!     tokio::time::pause();
//!     tokio::time::advance(std::time::Duration::from_millis(500)).await;
//!     tokio::time::resume();
//! }
//! ```
//!
//! ## Mock Behaviors
//!
//! - **MockSttClient**: Outputs text from a predefined list when silence is detected.
//!   Send silence (all zeros) to trigger end_of_turn and get the next text.
//!
//! - **MockLlm**: Repeats each word in the input twice.
//!   "hello world" -> "hello hello world world"
//!
//! - **MockTtsClient**: Outputs 1 second of blank audio per word, followed by text timing.

use anyhow::Result;
use std::sync::Arc;
use tokio::sync::Mutex;

/// Mock STT client that outputs text from a predefined list.
pub struct MockSttClient {
    texts: Arc<Mutex<Vec<String>>>,
}

impl MockSttClient {
    /// Create a new mock STT client.
    ///
    /// The client will output each text in order when `end_of_turn` is triggered.
    pub fn new(texts: Vec<String>) -> Self {
        Self {
            texts: Arc::new(Mutex::new(texts)),
        }
    }

    pub async fn stt_stream(&self) -> Result<(MockSttStreamSender, MockSttStreamReceiver)> {
        let (audio_tx, audio_rx) = tokio::sync::mpsc::channel::<Vec<f32>>(100);
        let texts = self.texts.clone();
        Ok((
            MockSttStreamSender { tx: audio_tx },
            MockSttStreamReceiver {
                audio_rx,
                texts,
                text_index: 0,
                samples_received: 0,
                pending_text: None,
                end_of_turn_triggered: false,
            },
        ))
    }
}

pub struct MockSttStreamSender {
    tx: tokio::sync::mpsc::Sender<Vec<f32>>,
}

impl MockSttStreamSender {
    pub async fn send_audio(&mut self, audio: &[f32]) -> Result<()> {
        self.tx.send(audio.to_vec()).await?;
        Ok(())
    }
}

pub struct MockSttStreamReceiver {
    audio_rx: tokio::sync::mpsc::Receiver<Vec<f32>>,
    texts: Arc<Mutex<Vec<String>>>,
    text_index: usize,
    samples_received: u64,
    pending_text: Option<String>,
    end_of_turn_triggered: bool,
}

const MOCK_SAMPLE_RATE: u64 = 24000;

impl MockSttStreamReceiver {
    pub async fn next_message(&mut self) -> Result<Option<crate::speech_to_text::Msg>> {
        use crate::speech_to_text::Msg;

        // If we have pending text, emit it first
        if let Some(text) = self.pending_text.take() {
            let start_s = self.samples_received as f64 / MOCK_SAMPLE_RATE as f64;
            return Ok(Some(Msg::Text { text, start_s }));
        }

        // Wait for audio data
        match self.audio_rx.recv().await {
            Some(audio) => {
                self.samples_received += audio.len() as u64;
                let current_s = self.samples_received as f64 / MOCK_SAMPLE_RATE as f64;

                // Check if this is a flush (silence) - triggers end_of_turn
                let is_silence = audio.iter().all(|&s| s.abs() < 0.001);

                if is_silence && !self.end_of_turn_triggered {
                    self.end_of_turn_triggered = true;

                    // Queue up the next text to emit after the Step
                    let texts = self.texts.lock().await;
                    if self.text_index < texts.len() {
                        self.pending_text = Some(texts[self.text_index].clone());
                        self.text_index += 1;
                    }

                    return Ok(Some(Msg::Step {
                        end_of_turn: true,
                        current_s,
                        inactivity_prob: 0.0,
                    }));
                }

                // Reset end_of_turn flag when we get non-silence
                if !is_silence {
                    self.end_of_turn_triggered = false;
                }

                Ok(Some(Msg::Step {
                    end_of_turn: false,
                    current_s,
                    inactivity_prob: 0.0,
                }))
            }
            None => Ok(None),
        }
    }
}

/// Mock LLM that repeats each word twice.
pub struct MockLlm;

impl MockLlm {
    pub fn new() -> Self {
        Self
    }

    pub fn session(&self) -> Result<MockLlmSession> {
        Ok(MockLlmSession {
            transmitted: Arc::new(Mutex::new(vec![])),
        })
    }
}

impl Default for MockLlm {
    fn default() -> Self {
        Self::new()
    }
}

pub struct MockLlmSession {
    transmitted: Arc<Mutex<Vec<String>>>,
}

impl MockLlmSession {
    pub fn transmitted(&self) -> Arc<Mutex<Vec<String>>> {
        self.transmitted.clone()
    }

    pub async fn incorporate_previous_generation(&mut self) -> Result<Option<String>> {
        let mut transmitted = self.transmitted.lock().await;
        let v: Vec<String> = std::mem::take(&mut *transmitted);
        if v.is_empty() {
            Ok(None)
        } else {
            Ok(Some(v.join(" ")))
        }
    }

    pub async fn push(
        &mut self,
        user_msg: &str,
        _config: Arc<crate::llm::LlmConfig>,
    ) -> Result<MockLlmResponseStream> {
        // Repeat each word twice
        let words: Vec<String> = user_msg
            .split_whitespace()
            .flat_map(|w| vec![w.to_string(), w.to_string()])
            .collect();

        let (tx, rx) = tokio::sync::mpsc::channel(8);

        let jh = tokio::spawn(async move {
            for word in words {
                if tx.send(word).await.is_err() {
                    break;
                }
            }
        });

        Ok(MockLlmResponseStream { rx, _jh: jh })
    }
}

pub struct MockLlmResponseStream {
    rx: tokio::sync::mpsc::Receiver<String>,
    _jh: tokio::task::JoinHandle<()>,
}

impl MockLlmResponseStream {
    pub async fn recv(&mut self) -> Option<String> {
        self.rx.recv().await
    }

    pub fn abort(mut self) {
        self.rx.close();
    }
}

/// Mock TTS client that outputs blank audio with 1 second per word.
pub struct MockTtsClient {
    sample_rate: usize,
}

impl MockTtsClient {
    pub fn new() -> Self {
        Self { sample_rate: 24000 }
    }

    pub async fn tts_stream(
        &self,
        _voice_id: Option<String>,
    ) -> Result<(MockTtsStreamSender, MockTtsStreamReceiver)> {
        let (text_tx, text_rx) = tokio::sync::mpsc::channel::<TtsCommand>(100);
        let sample_rate = self.sample_rate;

        Ok((
            MockTtsStreamSender { tx: text_tx },
            MockTtsStreamReceiver {
                text_rx,
                sample_rate,
                current_time_s: 0.0,
                pending: vec![],
            },
        ))
    }
}

impl Default for MockTtsClient {
    fn default() -> Self {
        Self::new()
    }
}

enum TtsCommand {
    Text(String),
    EndOfStream,
}

pub struct MockTtsStreamSender {
    tx: tokio::sync::mpsc::Sender<TtsCommand>,
}

impl MockTtsStreamSender {
    pub async fn send_text(&mut self, text: &str) -> Result<()> {
        self.tx.send(TtsCommand::Text(text.to_string())).await?;
        Ok(())
    }

    pub async fn send_end_of_stream(&mut self) -> Result<()> {
        self.tx.send(TtsCommand::EndOfStream).await?;
        Ok(())
    }
}

enum PendingTtsOut {
    Audio {
        pcm: Vec<f32>,
        start_s: f64,
        stop_s: f64,
    },
    Text {
        text: String,
        start_s: f64,
        stop_s: f64,
    },
}

pub struct MockTtsStreamReceiver {
    text_rx: tokio::sync::mpsc::Receiver<TtsCommand>,
    sample_rate: usize,
    current_time_s: f64,
    pending: Vec<PendingTtsOut>,
}

impl MockTtsStreamReceiver {
    pub async fn next_message(
        &mut self,
        turn_idx: u64,
    ) -> Result<Option<crate::text_to_speech::TtsOut>> {
        use crate::text_to_speech::TtsOut;

        // Process any pending outputs first
        if let Some(out) = self.pending.pop() {
            return Ok(Some(match out {
                PendingTtsOut::Audio {
                    pcm,
                    start_s,
                    stop_s,
                } => TtsOut::Audio {
                    pcm,
                    start_s,
                    stop_s,
                    turn_idx,
                    interrupted: false,
                },
                PendingTtsOut::Text {
                    text,
                    start_s,
                    stop_s,
                } => TtsOut::Text {
                    text,
                    start_s,
                    stop_s,
                    turn_idx,
                },
            }));
        }

        // Get more text
        loop {
            match self.text_rx.recv().await {
                Some(TtsCommand::Text(text)) => {
                    let words: Vec<String> =
                        text.split_whitespace().map(|s| s.to_string()).collect();

                    if words.is_empty() {
                        continue;
                    }

                    // Queue audio and text for each word
                    // Order: Audio first, then Text (matching real TTS behavior)
                    // Calculate timestamps forward, then push in reverse order for popping
                    let mut items = Vec::new();
                    let mut time_s = self.current_time_s;
                    for word in words {
                        let start_s = time_s;
                        let stop_s = start_s + 1.0;
                        time_s = stop_s;

                        let samples = self.sample_rate;
                        let pcm = vec![0.0f32; samples];

                        // Audio comes first, then text
                        items.push(PendingTtsOut::Audio {
                            pcm,
                            start_s,
                            stop_s,
                        });
                        items.push(PendingTtsOut::Text {
                            text: word,
                            start_s,
                            stop_s,
                        });
                    }
                    self.current_time_s = time_s;

                    // Reverse so first items are popped first
                    items.reverse();
                    self.pending = items;

                    // Return the first item
                    if let Some(out) = self.pending.pop() {
                        return Ok(Some(match out {
                            PendingTtsOut::Audio {
                                pcm,
                                start_s,
                                stop_s,
                            } => TtsOut::Audio {
                                pcm,
                                start_s,
                                stop_s,
                                turn_idx,
                                interrupted: false,
                            },
                            PendingTtsOut::Text {
                                text,
                                start_s,
                                stop_s,
                            } => TtsOut::Text {
                                text,
                                start_s,
                                stop_s,
                                turn_idx,
                            },
                        }));
                    }
                }
                Some(TtsCommand::EndOfStream) => {
                    return Ok(None);
                }
                None => {
                    return Ok(None);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Test a simple end-to-end flow:
    /// 1. User speaks "hello" (STT outputs "hello")
    /// 2. LLM repeats each word twice -> "hello hello"
    /// 3. TTS generates audio for "hello hello"
    #[tokio::test]
    async fn test_simple_flow() {
        // Setup mocks
        let stt = MockSttClient::new(vec!["hello".to_string()]);
        let llm = MockLlm::new();
        let tts = MockTtsClient::new();

        let (mut stt_tx, mut stt_rx) = stt.stt_stream().await.unwrap();
        let mut llm_session = llm.session().unwrap();
        let (mut tts_tx, mut tts_rx) = tts.tts_stream(None).await.unwrap();

        // Simulate user speaking (send non-silent audio)
        stt_tx.send_audio(&[0.5; 1920]).await.unwrap();
        let msg = stt_rx.next_message().await.unwrap().unwrap();
        assert!(matches!(
            msg,
            crate::speech_to_text::Msg::Step {
                end_of_turn: false,
                ..
            }
        ));

        // User stops speaking (send silence to trigger end_of_turn)
        stt_tx.send_audio(&[0.0; 1920]).await.unwrap();
        let msg = stt_rx.next_message().await.unwrap().unwrap();
        assert!(matches!(
            msg,
            crate::speech_to_text::Msg::Step {
                end_of_turn: true,
                ..
            }
        ));

        // Get transcribed text
        let msg = stt_rx.next_message().await.unwrap().unwrap();
        let user_text = match msg {
            crate::speech_to_text::Msg::Text { text, .. } => text,
            _ => panic!("expected Text"),
        };
        assert_eq!(user_text, "hello");

        // Send to LLM
        let config = Arc::new(crate::llm::LlmConfig::new(
            "test".to_string(),
            crate::system_prompt::Lang::En,
            vec![],
        ));
        let mut llm_stream = llm_session.push(&user_text, config).await.unwrap();

        // Collect LLM response and send to TTS
        let mut llm_response = String::new();
        while let Some(word) = llm_stream.recv().await {
            if !llm_response.is_empty() {
                llm_response.push(' ');
            }
            llm_response.push_str(&word);
            tts_tx.send_text(&word).await.unwrap();
        }
        tts_tx.send_end_of_stream().await.unwrap();

        assert_eq!(llm_response, "hello hello");

        // Collect TTS output
        // We expect: Audio for "hello", Text for "hello", Audio for "hello", Text for "hello"
        // (LLM output is "hello hello", so 2 words, each with audio + text)
        let mut audio_count = 0;
        let mut text_count = 0;
        let mut texts = vec![];

        while let Some(msg) = tts_rx.next_message(0).await.unwrap() {
            match msg {
                crate::text_to_speech::TtsOut::Audio { .. } => audio_count += 1,
                crate::text_to_speech::TtsOut::Text { text, .. } => {
                    text_count += 1;
                    texts.push(text);
                }
                crate::text_to_speech::TtsOut::TurnComplete { .. } => {}
            }
        }

        assert_eq!(audio_count, 2, "expected 2 audio messages");
        assert_eq!(text_count, 2, "expected 2 text messages");
        assert_eq!(texts, vec!["hello", "hello"]);
    }

    /// Test interruption flow:
    /// 1. User speaks "hello world"
    /// 2. LLM starts responding
    /// 3. User interrupts (new speech detected while TTS is playing)
    /// 4. Check that we can handle the interruption
    #[tokio::test]
    async fn test_interruption_flow() {
        let stt = MockSttClient::new(vec!["hello world".to_string(), "stop".to_string()]);
        let llm = MockLlm::new();
        let tts = MockTtsClient::new();

        let (mut stt_tx, mut stt_rx) = stt.stt_stream().await.unwrap();
        let mut llm_session = llm.session().unwrap();
        let (mut tts_tx, mut tts_rx) = tts.tts_stream(None).await.unwrap();

        // First turn: user says "hello world"
        stt_tx.send_audio(&[0.5; 1920]).await.unwrap();
        let _ = stt_rx.next_message().await.unwrap().unwrap(); // Step

        stt_tx.send_audio(&[0.0; 1920]).await.unwrap();
        let _ = stt_rx.next_message().await.unwrap().unwrap(); // Step with end_of_turn

        let msg = stt_rx.next_message().await.unwrap().unwrap();
        let user_text = match msg {
            crate::speech_to_text::Msg::Text { text, .. } => text,
            _ => panic!("expected Text"),
        };
        assert_eq!(user_text, "hello world");

        // Send to LLM - will generate "hello hello world world"
        let config = Arc::new(crate::llm::LlmConfig::new(
            "test".to_string(),
            crate::system_prompt::Lang::En,
            vec![],
        ));
        let mut llm_stream = llm_session.push(&user_text, config.clone()).await.unwrap();

        // Get first word from LLM and send to TTS
        let first_word = llm_stream.recv().await.unwrap();
        assert_eq!(first_word, "hello");
        tts_tx.send_text(&first_word).await.unwrap();

        // Get first TTS output (audio for "hello")
        let tts_msg = tts_rx.next_message(0).await.unwrap().unwrap();
        match tts_msg {
            crate::text_to_speech::TtsOut::Audio {
                start_s, stop_s, ..
            } => {
                assert!((start_s - 0.0).abs() < 0.001);
                assert!((stop_s - 1.0).abs() < 0.001);
            }
            _ => panic!("expected Audio message"),
        }

        // INTERRUPTION: User starts speaking again while TTS is playing
        // In real system, this would abort the LLM stream
        llm_stream.abort();

        // Simulate user speaking again
        stt_tx.send_audio(&[0.5; 1920]).await.unwrap();
        let msg = stt_rx.next_message().await.unwrap().unwrap();
        // Should not be end_of_turn since it's non-silent audio
        assert!(matches!(
            msg,
            crate::speech_to_text::Msg::Step {
                end_of_turn: false,
                ..
            }
        ));

        // User stops
        stt_tx.send_audio(&[0.0; 1920]).await.unwrap();
        let _ = stt_rx.next_message().await.unwrap().unwrap(); // Step with end_of_turn

        let msg = stt_rx.next_message().await.unwrap().unwrap();
        let user_text = match msg {
            crate::speech_to_text::Msg::Text { text, .. } => text,
            _ => panic!("expected Text"),
        };
        assert_eq!(user_text, "stop");

        // New LLM generation for "stop" -> "stop stop"
        let mut llm_stream = llm_session.push(&user_text, config).await.unwrap();
        let mut response_words = vec![];
        while let Some(word) = llm_stream.recv().await {
            response_words.push(word);
        }
        assert_eq!(response_words, vec!["stop", "stop"]);
    }

    #[tokio::test]
    async fn test_mock_stt() {
        let client = MockSttClient::new(vec!["hello".to_string(), "world".to_string()]);
        let (mut tx, mut rx) = client.stt_stream().await.unwrap();

        // Send some audio
        tx.send_audio(&[0.5; 1920]).await.unwrap();
        let msg = rx.next_message().await.unwrap().unwrap();
        assert!(matches!(
            msg,
            crate::speech_to_text::Msg::Step {
                end_of_turn: false,
                ..
            }
        ));

        // Send silence to trigger end_of_turn
        tx.send_audio(&[0.0; 1920]).await.unwrap();
        let msg = rx.next_message().await.unwrap().unwrap();
        assert!(matches!(
            msg,
            crate::speech_to_text::Msg::Step {
                end_of_turn: true,
                ..
            }
        ));

        // Should get text
        let msg = rx.next_message().await.unwrap().unwrap();
        if let crate::speech_to_text::Msg::Text { text, .. } = msg {
            assert_eq!(text, "hello");
        } else {
            panic!("expected Text message");
        }
    }

    #[tokio::test]
    async fn test_mock_llm() {
        let llm = MockLlm::new();
        let mut session = llm.session().unwrap();

        let config = Arc::new(crate::llm::LlmConfig::new(
            "test".to_string(),
            crate::system_prompt::Lang::En,
            vec![],
        ));

        let mut stream = session.push("hello world", config).await.unwrap();

        let mut words = vec![];
        while let Some(word) = stream.recv().await {
            words.push(word);
        }

        assert_eq!(words, vec!["hello", "hello", "world", "world"]);
    }

    #[tokio::test]
    async fn test_mock_tts() {
        let client = MockTtsClient::new();
        let (mut tx, mut rx) = client.tts_stream(None).await.unwrap();

        tx.send_text("hello world").await.unwrap();
        tx.send_end_of_stream().await.unwrap();

        // Audio and text are emitted for each word (audio first, then text)
        // First word: "hello"
        let msg = rx.next_message(0).await.unwrap().unwrap();
        if let crate::text_to_speech::TtsOut::Audio {
            start_s, stop_s, ..
        } = msg
        {
            assert!((start_s - 0.0).abs() < 0.001);
            assert!((stop_s - 1.0).abs() < 0.001);
        } else {
            panic!("expected Audio message, got {:?}", msg);
        }

        let msg = rx.next_message(0).await.unwrap().unwrap();
        if let crate::text_to_speech::TtsOut::Text {
            text,
            start_s,
            stop_s,
            ..
        } = msg
        {
            assert_eq!(text, "hello");
            assert!((start_s - 0.0).abs() < 0.001);
            assert!((stop_s - 1.0).abs() < 0.001);
        } else {
            panic!("expected Text message, got {:?}", msg);
        }

        // Second word: "world"
        let msg = rx.next_message(0).await.unwrap().unwrap();
        if let crate::text_to_speech::TtsOut::Audio {
            start_s, stop_s, ..
        } = msg
        {
            assert!((start_s - 1.0).abs() < 0.001);
            assert!((stop_s - 2.0).abs() < 0.001);
        } else {
            panic!("expected Audio message, got {:?}", msg);
        }

        let msg = rx.next_message(0).await.unwrap().unwrap();
        if let crate::text_to_speech::TtsOut::Text {
            text,
            start_s,
            stop_s,
            ..
        } = msg
        {
            assert_eq!(text, "world");
            assert!((start_s - 1.0).abs() < 0.001);
            assert!((stop_s - 2.0).abs() < 0.001);
        } else {
            panic!("expected Text message, got {:?}", msg);
        }

        // Should be done
        assert!(rx.next_message(0).await.unwrap().is_none());
    }
}
