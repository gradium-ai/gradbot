// Audio decoder supporting OggOpus, PCM, and WAV formats

pub use crate::encoder::Format;
pub use crate::encoder::PcmFormat;
use anyhow::Result;

pub enum Decoder {
    OggOpus(kaudio::ogg_opus::Decoder),
    Pcm {
        fft: Option<(Vec<f32>, Box<rubato::FftFixedOut<f32>>)>,
        format: PcmFormat,
    },
    Wav(crate::wav::Decoder),
}

impl Decoder {
    pub fn new(format: Format, out_sample_rate: usize, frame_size: usize) -> Result<Self> {
        match format {
            Format::OggOpus => Self::ogg_opus(out_sample_rate, frame_size),
            Format::Pcm {
                sample_rate,
                format,
            } => {
                let sample_rate = sample_rate.unwrap_or(out_sample_rate);
                let fft = if sample_rate == out_sample_rate {
                    None
                } else {
                    use rubato::Resampler;
                    let fft = rubato::FftFixedOut::<f32>::new(
                        sample_rate,
                        out_sample_rate,
                        frame_size,
                        1,
                        1,
                    )?;
                    let buf: Vec<f32> = Vec::with_capacity(fft.input_frames_next());
                    Some((buf, Box::new(fft)))
                };
                Ok(Self::Pcm { fft, format })
            }
            Format::Wav => Ok(Self::wav(out_sample_rate, frame_size)?),
        }
    }

    fn ogg_opus(sample_rate: usize, frame_size: usize) -> Result<Self> {
        Ok(Self::OggOpus(kaudio::ogg_opus::Decoder::new(
            sample_rate,
            frame_size,
        )?))
    }

    fn wav(sample_rate: usize, frame_size: usize) -> Result<Self> {
        let decoder = crate::wav::Decoder::new(sample_rate, frame_size)?;
        Ok(Self::Wav(decoder))
    }

    pub fn decode(&mut self, data: &[u8]) -> Result<Vec<f32>> {
        let pcm = match self {
            Self::OggOpus(oo) => match oo.decode(data)? {
                None => vec![],
                Some(pcm) => pcm.to_vec(),
            },
            Self::Wav(decoder) => decoder.decode(data)?,
            Self::Pcm { fft, format } => {
                use byteorder::ByteOrder;
                if !data.len().is_multiple_of(2) {
                    anyhow::bail!("pcm data length is not a multiple of 2 {}", data.len());
                }
                let pcm: Vec<f32> = match format {
                    PcmFormat::Raw => data
                        .chunks_exact(2)
                        .map(|b| {
                            let v = byteorder::LittleEndian::read_i16(b);
                            v as f32 / i16::MAX as f32
                        })
                        .collect(),
                    PcmFormat::Alaw => data
                        .iter()
                        .map(|&s| law_decoder::alaw_decode_sample(s) as f32 / i16::MAX as f32)
                        .collect(),
                    PcmFormat::Ulaw => data
                        .iter()
                        .map(|&s| law_decoder::ulaw_decode_sample(s) as f32 / i16::MAX as f32)
                        .collect(),
                };
                match fft {
                    Some((buf, fft)) => {
                        use rubato::Resampler;
                        let mut pcm_out = vec![];
                        buf.extend_from_slice(&pcm);
                        while buf.len() >= fft.input_frames_next() {
                            let input: Vec<f32> = buf.drain(..fft.input_frames_next()).collect();
                            let pcm_resampled = fft.process(&[&input], None)?;
                            match pcm_resampled.into_iter().next() {
                                None => anyhow::bail!("resampling produced no output"),
                                Some(pcm_resampled) => pcm_out.extend_from_slice(&pcm_resampled),
                            }
                        }
                        pcm_out
                    }
                    None => pcm,
                }
            }
        };
        Ok(pcm)
    }
}

// A-law and mu-law decoding
mod law_decoder {
    pub fn alaw_decode_sample(a_val: u8) -> i16 {
        let a_val = a_val ^ 0x55;
        let t = a_val as i16 & 0x0F;
        let seg = (a_val & 0x70) >> 4;
        let t = if seg != 0 {
            (t + t + 1 + 32) << (seg + 2)
        } else {
            (t + t + 1) << 3
        };
        if a_val & 0x80 != 0 { t } else { -t }
    }

    pub fn ulaw_decode_sample(input: u8) -> i16 {
        let u_val = !input;
        let t = ((u_val as i16 & 0x0f) << 3) + 0x84;
        let t = t << ((u_val as i16 & 0x70) >> 4);
        if u_val & 0x80 != 0 {
            0x84 - t
        } else {
            t - 0x84
        }
    }
}
