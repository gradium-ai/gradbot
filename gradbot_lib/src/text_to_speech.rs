use anyhow::Result;

pub struct TtsClient(gradium::Client);
pub struct TtsStreamSender(gradium::tts::TtsStreamSender);
pub struct TtsStreamReceiver(gradium::tts::TtsStreamReceiver);

/// TTS output message with turn index.
/// The turn_idx is used to skip stale audio after an interruption:
/// when the user interrupts, the turn_idx is incremented, and any
/// TtsOut messages with an older turn_idx are discarded.
#[derive(Debug)]
pub enum TtsOut {
    Audio {
        pcm: Vec<f32>,
        start_s: f64,
        stop_s: f64,
        turn_idx: u64,
        /// Set by multiplex when this is the last audio before an interruption.
        /// The TTS receiver always sets this to false.
        interrupted: bool,
    },
    Text {
        text: String,
        start_s: f64,
        stop_s: f64,
        turn_idx: u64,
    },
    /// Signals that all output for this turn has been sent.
    /// The stop_s is the timestamp of the last audio sent, used to
    /// set the listening start time for silence detection.
    TurnComplete {
        turn_idx: u64,
        stop_s: f64,
    },
}

impl TtsClient {
    pub fn new(gradium_api_key: Option<&str>, base_url: &str) -> Result<Self> {
        let api_key = match gradium_api_key {
            Some(key) => key.to_string(),
            None => std::env::var("GRADIUM_API_KEY")
                .map_err(|_| anyhow::anyhow!("GRADIUM_API_KEY environment variable not set"))?,
        };
        let client = gradium::Client::new(&api_key).with_base_url(base_url)?;
        Ok(Self(client))
    }

    pub async fn tts_stream(
        &self,
        voice_id: Option<String>,
    ) -> Result<(TtsStreamSender, TtsStreamReceiver)> {
        let setup = gradium::protocol::tts::Setup {
            model_name: "default".to_string(),
            voice_id,
            voice: None,
            output_format: gradium::protocol::AudioFormat::Pcm,
            json_config: None,
        };
        let stream = self.0.tts_stream(setup).await?;
        let (tx, rx) = stream.split();
        Ok((TtsStreamSender(tx), TtsStreamReceiver(rx)))
    }
}

impl TtsStreamSender {
    pub async fn send_text(&mut self, text: &str) -> Result<()> {
        self.0.send_text(text).await?;
        Ok(())
    }

    pub async fn send_end_of_stream(&mut self) -> Result<()> {
        self.0.send_eos().await?;
        Ok(())
    }
}

impl TtsStreamReceiver {
    pub async fn next_message(&mut self, turn_idx: u64) -> Result<Option<TtsOut>> {
        use gradium::protocol::tts::Response;
        while let Some(msg) = self.0.next_message().await? {
            match msg {
                Response::Audio(a) => {
                    let pcm = a.raw_audio()?;
                    let pcm = pcm
                        .chunks_exact(2)
                        .map(|b| i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0)
                        .collect::<Vec<f32>>();
                    return Ok(Some(TtsOut::Audio {
                        pcm,
                        start_s: a.start_s,
                        stop_s: a.stop_s,
                        turn_idx,
                        interrupted: false,
                    }));
                }
                Response::Text(text) => {
                    return Ok(Some(TtsOut::Text {
                        text: text.text,
                        start_s: text.start_s,
                        stop_s: text.stop_s,
                        turn_idx,
                    }));
                }
                Response::Error { code, message } => {
                    tracing::error!(?code, ?message, "TTS API returned error");
                    anyhow::bail!("TTS Error {code:?}: {message}")
                }
                Response::Ready(_) => {
                    tracing::debug!("TTS Ready received");
                }
                Response::EndOfStream => {
                    tracing::debug!("TTS EndOfStream received");
                }
            }
        }
        tracing::debug!("TTS stream ended (no more messages)");
        Ok(None)
    }
}
