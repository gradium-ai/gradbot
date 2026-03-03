// Audio encoder supporting OggOpus, PCM, and WAV formats

use anyhow::Result;

#[derive(serde::Deserialize, serde::Serialize, Debug, Clone, Copy, PartialEq, Eq)]
pub enum PcmFormat {
    Raw,
    Alaw,
    Ulaw,
}

#[derive(serde::Deserialize, serde::Serialize, Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Pcm {
        sample_rate: Option<usize>,
        format: PcmFormat,
    },
    Wav,
    OggOpus,
}

impl std::str::FromStr for Format {
    type Err = anyhow::Error;

    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        let format = match s.to_lowercase().as_str() {
            "" | "pcm" => Default::default(),
            "pcm_8000" => Self::pcm(8000),
            "pcm_16000" => Self::pcm(16000),
            "pcm_22050" => Self::pcm(22050),
            "pcm_24000" => Self::pcm(24000),
            "pcm_44100" => Self::pcm(44100),
            "pcm_48000" => Self::pcm(48000),
            "ulaw_8000" => Self::ulaw(8000),
            "mulaw_8000" => Self::ulaw(8000),
            "alaw_8000" => Self::alaw(8000),
            "wav" => Self::Wav,
            "opus" => Self::OggOpus,
            s => anyhow::bail!(
                "unsupported output format '{s}', supported formats: 'pcm', 'pcm_24000', 'ulaw_8000', 'alaw_8000', 'wav', 'opus'"
            ),
        };
        Ok(format)
    }
}

impl Format {
    pub fn pcm(sample_rate: usize) -> Self {
        Self::Pcm {
            sample_rate: Some(sample_rate),
            format: PcmFormat::Raw,
        }
    }

    pub fn ulaw(sample_rate: usize) -> Self {
        Self::Pcm {
            sample_rate: Some(sample_rate),
            format: PcmFormat::Ulaw,
        }
    }

    pub fn alaw(sample_rate: usize) -> Self {
        Self::Pcm {
            sample_rate: Some(sample_rate),
            format: PcmFormat::Alaw,
        }
    }
}

fn pcm_f32_to_s16(v: f32) -> i16 {
    (v * i16::MAX as f32) as i16
}

impl PcmFormat {
    fn pcm_to_bytes(&self, pcm: &[f32]) -> Vec<u8> {
        use byteorder::ByteOrder;
        match self {
            Self::Raw => {
                let pcm: Vec<i16> = pcm.iter().map(|&v| pcm_f32_to_s16(v)).collect();
                let mut buf = vec![0u8; std::mem::size_of_val(pcm.as_slice())];
                byteorder::LittleEndian::write_i16_into(&pcm, &mut buf);
                buf
            }
            Self::Alaw => pcm
                .iter()
                .map(|&s| law_encoder::alaw_encode_sample(pcm_f32_to_s16(s)))
                .collect(),
            Self::Ulaw => pcm
                .iter()
                .map(|&s| law_encoder::mulaw_encode_sample(pcm_f32_to_s16(s)))
                .collect(),
        }
    }
}

impl Default for Format {
    fn default() -> Self {
        Self::Pcm {
            sample_rate: None,
            format: PcmFormat::Raw,
        }
    }
}

enum Encoder_ {
    OggOpus(kaudio::ogg_opus::Encoder),
    Pcm {
        fft: Option<rubato::FftFixedInOut<f32>>,
        format: PcmFormat,
    },
    Wav {
        header: Vec<u8>,
    },
}

pub struct Encoder {
    inner: Encoder_,
    samples_encoded: usize,
    in_sample_rate: usize,
}

pub struct EncodedAudio {
    pub data: Vec<u8>,
    pub start_s: f64,
    pub stop_s: f64,
}

impl Encoder {
    pub fn new(format: Format, frame_size: usize, in_sample_rate: usize) -> Result<Self> {
        let inner = match format {
            Format::OggOpus => Self::ogg_opus(in_sample_rate),
            Format::Pcm {
                sample_rate,
                format,
            } => {
                let sample_rate = sample_rate.unwrap_or(in_sample_rate);
                let fft = if sample_rate == in_sample_rate {
                    None
                } else {
                    let fft = rubato::FftFixedInOut::<f32>::new(
                        in_sample_rate,
                        sample_rate,
                        frame_size,
                        1,
                    )?;
                    Some(fft)
                };
                Ok(Encoder_::Pcm { fft, format })
            }
            Format::Wav => Ok(Self::wav(in_sample_rate)?),
        };
        Ok(Self {
            inner: inner?,
            samples_encoded: 0,
            in_sample_rate,
        })
    }

    fn ogg_opus(sample_rate: usize) -> Result<Encoder_> {
        Ok(Encoder_::OggOpus(kaudio::ogg_opus::Encoder::new(
            sample_rate,
        )?))
    }

    fn wav(sample_rate: usize) -> Result<Encoder_> {
        let mut header = vec![];
        crate::wav::write_wav_header(
            &mut header,
            sample_rate as u32,
            0xFFFF_FFFFu32,
            0xFFFF_FFFFu32,
        )?;
        Ok(Encoder_::Wav { header })
    }

    pub fn header(&self) -> Option<&[u8]> {
        match &self.inner {
            Encoder_::OggOpus(oo) => Some(oo.header_data()),
            Encoder_::Wav { header } => Some(header.as_slice()),
            Encoder_::Pcm { fft: _, format: _ } => None,
        }
    }

    pub fn encode(&mut self, pcm: &[f32]) -> Result<EncodedAudio> {
        let buf = match &mut self.inner {
            Encoder_::OggOpus(oo) => oo.encode_page(pcm)?,
            Encoder_::Wav { .. } => {
                let mut buf = vec![];
                crate::wav::write_pcm_in_wav(&mut buf, pcm)?;
                buf
            }
            Encoder_::Pcm { fft: None, format } => format.pcm_to_bytes(pcm),
            Encoder_::Pcm {
                fft: Some(fft),
                format,
            } => {
                use rubato::Resampler;
                let pcm = fft.process(&[&pcm], None)?;
                if pcm.is_empty() {
                    anyhow::bail!("resampling produced no output");
                }
                format.pcm_to_bytes(&pcm[0])
            }
        };
        let start_s = self.samples_encoded as f64 / self.in_sample_rate as f64;
        self.samples_encoded += pcm.len();
        let stop_s = self.samples_encoded as f64 / self.in_sample_rate as f64;
        Ok(EncodedAudio {
            data: buf,
            start_s,
            stop_s,
        })
    }
}

// A-law and mu-law encoding
mod law_encoder {
    const CLIP: i16 = 0x1FFF;
    const BIAS: i16 = 33;

    pub fn alaw_encode_sample(input: i16) -> u8 {
        let mut sample = if input < 0 { !input >> 4 } else { input >> 4 };

        if sample > 15 {
            let mut exp_pos = 1;
            while sample > 31 {
                sample >>= 1;
                exp_pos += 1;
            }
            sample -= 16;
            sample += exp_pos << 4;
        }

        if input >= 0 {
            sample |= 0x0080;
        }

        (sample ^ 0x0055) as u8
    }

    pub fn mulaw_encode_sample(input: i16) -> u8 {
        let mut sample = if input < 0 {
            (!input >> 2) + BIAS
        } else {
            (input >> 2) + BIAS
        };

        if sample > CLIP {
            sample = CLIP;
        }

        let mut i = sample >> 6;
        let mut segno = 1;
        while i != 0 {
            segno += 1;
            i >>= 1;
        }

        let high_nibble = (0x0008) - segno;
        let low_nibble = (0x000F) - ((sample >> segno) & (0x000F));

        if input >= 0 {
            ((high_nibble << 4) | low_nibble | (0x0080)) as u8
        } else {
            ((high_nibble << 4) | low_nibble) as u8
        }
    }
}
