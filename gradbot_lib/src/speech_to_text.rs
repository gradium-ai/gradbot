use anyhow::Result;

const VAD_INDEX: usize = 2;

pub struct SttClient(gradium::Client);
pub struct SttStreamReceiver(gradium::stt::SttStreamReceiver);
pub struct SttStreamSender(gradium::stt::SttStreamSender);

#[derive(Debug)]
pub enum Msg {
    Step { end_of_turn: bool, current_s: f64, inactivity_prob: f64 },
    Text { text: String, start_s: f64 },
}

impl SttClient {
    pub fn new(gradium_api_key: Option<&str>, base_url: &str) -> Result<Self> {
        let api_key = match gradium_api_key {
            Some(key) => key.to_string(),
            None => std::env::var("GRADIUM_API_KEY")
                .map_err(|_| anyhow::anyhow!("GRADIUM_API_KEY environment variable not set"))?,
        };
        let client = gradium::Client::new(&api_key).with_base_url(base_url)?;
        Ok(Self(client))
    }

    pub async fn stt_stream(&self) -> Result<(SttStreamSender, SttStreamReceiver)> {
        let setup = gradium::protocol::stt::Setup {
            model_name: "default".to_string(),
            input_format: gradium::protocol::AudioFormat::Pcm,
            json_config: None,
        };
        let stream = self.0.stt_stream(setup).await?;
        let (tx, rx) = stream.split();
        Ok((SttStreamSender(tx), SttStreamReceiver(rx)))
    }
}

impl SttStreamSender {
    pub async fn send_audio(&mut self, audio: &[f32]) -> Result<()> {
        // Convert f32 audio to i16le bytes
        let audio = audio
            .iter()
            .flat_map(|s| {
                let s = (s.clamp(-1.0, 1.0) * 32768.0) as i16;
                s.to_le_bytes()
            })
            .collect::<Vec<u8>>();
        self.0.send_audio(audio).await?;
        Ok(())
    }
}

impl SttStreamReceiver {
    pub async fn next_message(&mut self) -> Result<Option<Msg>> {
        use gradium::protocol::stt::Response;
        while let Some(msg) = self.0.next_message().await? {
            match msg {
                Response::Vad(vad_event) => {
                    // The probability of inactivity at the longest horizon guides us to detect end of turn
                    let inactivity_prob =
                        vad_event.vad.get(VAD_INDEX).map(|v| v.inactivity_prob).unwrap_or(1.0);
                    let end_of_turn = inactivity_prob > 0.8;
                    return Ok(Some(Msg::Step {
                        end_of_turn,
                        current_s: vad_event.total_duration_s,
                        inactivity_prob,
                    }));
                }
                Response::Error { code, message } => {
                    anyhow::bail!("STT Error {code:?}: {message}")
                }
                Response::Ready(_) | Response::EndText(_) => {}
                Response::Text(text) => {
                    return Ok(Some(Msg::Text { text: text.text, start_s: text.start_s }));
                }
                Response::EndOfStream => break,
            }
        }
        Ok(None)
    }
}
