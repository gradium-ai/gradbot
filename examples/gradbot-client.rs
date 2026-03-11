use anyhow::Result;
use clap::Parser;
use futures_util::StreamExt;
use gradbot_bin::openai_protocol as oai;
use std::sync::{Arc, Mutex};
use tokio_tungstenite::tungstenite as ws;

pub type WebSocket =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;
pub type WebSocketSender = futures_util::stream::SplitSink<WebSocket, ws::Message>;
pub type WebSocketReceiver = futures_util::stream::SplitStream<WebSocket>;

const IN_SAMPLE_RATE: u32 = 24000;
const IN_CHUNK_SIZE: usize = 1920;
const OUT_SAMPLE_RATE: usize = 48000;
const OUT_CHUNK_SIZE: usize = 3840;

struct Connection {
    ws: WebSocket,
}

struct Sender {
    ws_sender: WebSocketSender,
}

struct Receiver {
    ws_receiver: WebSocketReceiver,
}

impl Receiver {
    async fn next(&mut self) -> Option<Result<ws::Message>> {
        let msg = self.ws_receiver.next().await?;
        let msg = match msg {
            Ok(m) => Ok(m),
            Err(e) => Err(e.into()),
        };
        Some(msg)
    }
}

impl Connection {
    pub async fn new(url: &str) -> Result<Self> {
        let (ws, _response) = tokio_tungstenite::connect_async_with_config(url, None, true).await?;
        Ok(Self { ws })
    }

    fn split(self) -> (Sender, Receiver) {
        let (ws_sender, ws_receiver) = self.ws.split();
        (Sender { ws_sender }, Receiver { ws_receiver })
    }
}

#[derive(Parser, Debug)]
struct Args {
    #[clap(long)]
    url: String,

    #[clap(long)]
    infile: String,

    #[clap(long)]
    outfile: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let (pcm, in_sample_rate) = kaudio::pcm_decode(&args.infile)?;
    let mut pcm = if in_sample_rate != IN_SAMPLE_RATE {
        kaudio::resample(&pcm, in_sample_rate as usize, IN_SAMPLE_RATE as usize)?
    } else {
        pcm
    };
    pcm.resize(pcm.len() + IN_SAMPLE_RATE as usize * 5, 0f32);
    let pcm = std::sync::Arc::new(pcm);

    println!("read audio {} samples at {} Hz", pcm.len(), IN_SAMPLE_RATE);
    let connection = Connection::new(&args.url).await?;
    println!("Connected to {}", args.url);
    let (mut sender, mut receiver) = connection.split();

    let samples_sent_so_far = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));

    let send_loop = {
        let samples_sent_so_far = samples_sent_so_far.clone();
        let pcm = pcm.clone();
        async move {
            use futures_util::SinkExt;
            use oai::ClientEvent as CE;
            let mut opus_encoder = kaudio::ogg_opus::Encoder::new(IN_SAMPLE_RATE as usize)?;
            let header = opus_encoder.header_data().to_vec();
            let msg = serde_json::to_string(&CE::input_audio_buffer_append(header))?;
            sender.ws_sender.send(ws::Message::Text(msg)).await?;
            let start_time = std::time::Instant::now();
            for (idx, chunk) in pcm.chunks(IN_CHUNK_SIZE).enumerate() {
                let samples = idx as u64 * IN_CHUNK_SIZE as u64;
                // Simulate real-time by waiting for the appropriate delay.
                let millis = (samples * 1000) / IN_SAMPLE_RATE as u64;
                samples_sent_so_far.store(samples, std::sync::atomic::Ordering::SeqCst);
                let target_time = start_time + std::time::Duration::from_millis(millis);
                tokio::time::sleep_until(target_time.into()).await;
                let chunk = opus_encoder.encode_page(chunk)?;
                let msg = serde_json::to_string(&CE::input_audio_buffer_append(chunk))?;
                sender.ws_sender.send(ws::Message::Text(msg)).await?;
            }
            Ok::<_, anyhow::Error>(())
        }
    };

    let all_pcm_data = Arc::new(Mutex::new(Vec::new()));
    let recv_loop = {
        let mut last_was_input = true;
        let all_pcm_data = all_pcm_data.clone();
        let mut opus_decoder = kaudio::ogg_opus::Decoder::new(OUT_SAMPLE_RATE, OUT_CHUNK_SIZE)?;
        async move {
            while let Some(msg) = receiver.next().await {
                let msg: oai::ServerEvent = match msg? {
                    ws::Message::Text(b) => serde_json::from_str(&b)?,
                    ws::Message::Binary(b) => serde_json::from_slice(&b)?,
                    ws::Message::Close(_) => break,
                    ws::Message::Frame(_) | ws::Message::Ping(_) | ws::Message::Pong(_) => continue,
                };
                match msg {
                    oai::ServerEvent::Error { event_id: _, error } => {
                        anyhow::bail!("Error from server: {error:?}");
                    }
                    oai::ServerEvent::ResponseAudioDelta { event_id: _, delta } => {
                        if let Some(delta) = opus_decoder.decode(&delta)? {
                            let expected_len = samples_sent_so_far
                                .load(std::sync::atomic::Ordering::SeqCst)
                                as usize
                                * OUT_SAMPLE_RATE
                                / IN_SAMPLE_RATE as usize;
                            let mut all_pcm_data = all_pcm_data.lock().unwrap();
                            // If we are lagging behind by more than 200ms, extend with zeros.
                            if all_pcm_data.len() + delta.len() + OUT_SAMPLE_RATE / 5 < expected_len
                            {
                                all_pcm_data.resize(expected_len - delta.len(), 0.0);
                            }
                            all_pcm_data.extend_from_slice(delta);
                        }
                    }
                    oai::ServerEvent::ConversationItemInputAudioTranscriptionDelta {
                        event_id: _,
                        delta,
                        start_time: _,
                    } => {
                        if !last_was_input {
                            println!("\nInput:");
                            last_was_input = true;
                        }
                        print!("{delta} ");
                        use std::io::Write;
                        std::io::stdout().flush()?;
                    }
                    oai::ServerEvent::ResponseTextDelta { event_id: _, delta } => {
                        if last_was_input {
                            println!("\nResponse:");
                            last_was_input = false;
                        }
                        print!("{delta} ");
                        use std::io::Write;
                        std::io::stdout().flush()?;
                    }
                    _ => {
                        eprintln!("Received unhandled message: {:?}", msg);
                    }
                }
            }
            println!("\nReceiver loop ended");
            Ok::<_, anyhow::Error>(())
        }
    };
    tokio::select! {
        res = send_loop => pp_err("send_loop", res),
        res = recv_loop => pp_err("recv_loop", res),
    }
    println!();

    let pcm = kaudio::resample(&pcm, IN_SAMPLE_RATE as usize, OUT_SAMPLE_RATE)?;
    let mut w = std::fs::File::create(&args.outfile)?;
    let all_pcm_data = all_pcm_data.lock().unwrap();
    let min_len = usize::min(all_pcm_data.len(), pcm.len());
    // Interleave all_pcm_data with input pcms.
    let interleaved = all_pcm_data
        .iter()
        .zip(pcm.iter().take(min_len))
        .flat_map(|(o, i)| [*i, *o])
        .collect::<Vec<f32>>();
    kaudio::wav::write_pcm_as_wav(&mut w, &interleaved, OUT_SAMPLE_RATE as u32, 2)?;

    Ok(())
}

fn pp_err(name: &str, res: Result<()>) {
    match res {
        Ok(()) => tracing::info!("{name} ended normally"),
        Err(err) => tracing::error!("{name} error: {err}"),
    }
}
