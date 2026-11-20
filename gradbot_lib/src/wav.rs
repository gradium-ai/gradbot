// WAV encoder/decoder utilities

use anyhow::{Result, bail};
use rubato::Resampler;
use std::io::prelude::*;

pub trait Sample {
    fn to_i16(&self) -> i16;
}

impl Sample for f32 {
    fn to_i16(&self) -> i16 {
        (self.clamp(-1.0, 1.0) * 32767.0) as i16
    }
}

impl Sample for f64 {
    fn to_i16(&self) -> i16 {
        (self.clamp(-1.0, 1.0) * 32767.0) as i16
    }
}

impl Sample for i16 {
    fn to_i16(&self) -> i16 {
        *self
    }
}

pub fn write_wav_header<W: Write>(
    w: &mut W,
    sample_rate: u32,
    chunk_size: u32,
    data_size: u32,
) -> std::io::Result<()> {
    let n_channels = 1u16;
    let bits_per_sample = 16u16;
    let byte_rate = sample_rate * n_channels as u32 * (bits_per_sample / 8) as u32;
    let block_align = n_channels * (bits_per_sample / 8);

    w.write_all(b"RIFF")?;
    w.write_all(&chunk_size.to_le_bytes())?;
    w.write_all(b"WAVE")?;

    w.write_all(b"fmt ")?;
    w.write_all(&16u32.to_le_bytes())?;
    w.write_all(&1u16.to_le_bytes())?; // PCM format
    w.write_all(&n_channels.to_le_bytes())?;
    w.write_all(&sample_rate.to_le_bytes())?;
    w.write_all(&byte_rate.to_le_bytes())?;
    w.write_all(&block_align.to_le_bytes())?;
    w.write_all(&bits_per_sample.to_le_bytes())?;

    w.write_all(b"data")?;
    w.write_all(&data_size.to_le_bytes())?;

    Ok(())
}

pub fn write_pcm_in_wav<W: Write, S: Sample>(w: &mut W, samples: &[S]) -> std::io::Result<usize> {
    for sample in samples {
        w.write_all(&sample.to_i16().to_le_bytes())?
    }
    Ok(samples.len() * std::mem::size_of::<i16>())
}

enum Parsed<'a, T> {
    Incomplete,
    Complete(T, &'a [u8]),
}

struct MasterChunk;

impl MasterChunk {
    fn parse(data: &[u8]) -> Result<Parsed<'_, Self>> {
        if data.len() < 12 {
            return Ok(Parsed::Incomplete);
        }
        if &data[0..4] != b"RIFF" {
            bail!("wav-decoder: invalid RIFF header");
        }
        if &data[8..12] != b"WAVE" {
            bail!("wav-decoder: invalid WAVE format");
        }

        Ok(Parsed::Complete(MasterChunk, &data[12..]))
    }
}

#[derive(Debug, Clone)]
struct FmtChunk {
    audio_format: u16,
    channels: u16,
    sample_rate: u32,
    bits_per_sample: u16,
}

impl FmtChunk {
    fn parse(data: &[u8]) -> Result<Parsed<'_, Self>> {
        if data.len() < 24 {
            return Ok(Parsed::Incomplete);
        }
        if &data[0..4] != b"fmt " {
            bail!("wav-decoder: invalid fmt chunk");
        }
        let audio_format = u16::from_le_bytes([data[8], data[9]]);
        let channels = u16::from_le_bytes([data[10], data[11]]);
        let sample_rate = u32::from_le_bytes([data[12], data[13], data[14], data[15]]);
        let bits_per_sample = u16::from_le_bytes([data[22], data[23]]);

        let fmt = Self {
            audio_format,
            channels,
            sample_rate,
            bits_per_sample,
        };
        Ok(Parsed::Complete(fmt, &data[24..]))
    }
}

fn extend_from_vec<T: Clone>(v: &mut Vec<T>, other: Vec<T>) {
    if v.is_empty() {
        *v = other
    } else {
        v.extend_from_slice(&other)
    }
}

/// A resampler that handles buffering internally.
struct BufResampler {
    resampler: Option<Box<rubato::SincFixedOut<f32>>>,
    input_buffer: Vec<f32>,
    buffer_samples_by: usize,
}

impl BufResampler {
    fn new(in_sr: usize, out_sr: usize, buffer_samples_by: usize) -> Result<Self> {
        let resampler = if in_sr == out_sr {
            None
        } else {
            let params = rubato::SincInterpolationParameters {
                sinc_len: 256,
                f_cutoff: 0.95,
                interpolation: rubato::SincInterpolationType::Linear,
                oversampling_factor: 256,
                window: rubato::WindowFunction::BlackmanHarris2,
            };
            let resampler = rubato::SincFixedOut::<f32>::new(
                out_sr as f64 / in_sr as f64,
                5.0,
                params,
                buffer_samples_by,
                1,
            )?;
            Some(Box::new(resampler))
        };
        Ok(Self {
            resampler,
            input_buffer: vec![],
            buffer_samples_by,
        })
    }

    fn push(&mut self, sample: f32) {
        self.input_buffer.push(sample);
    }

    fn flush(&mut self) -> Result<Vec<f32>> {
        match &mut self.resampler {
            Some(resampler) => {
                let mut current_index = 0;
                let mut resampled = Vec::new();
                loop {
                    let samples_required = resampler.input_frames_next();
                    if self.input_buffer.len() < current_index + samples_required {
                        break;
                    }
                    let mut resampler_out = resampler.process(
                        &[&self.input_buffer[current_index..current_index + samples_required]],
                        None,
                    )?;
                    extend_from_vec(&mut resampled, resampler_out.swap_remove(0));
                    current_index += samples_required;
                }
                self.input_buffer = self.input_buffer[current_index..].to_vec();
                Ok(resampled)
            }
            None => {
                let num_frames = self.input_buffer.len() / self.buffer_samples_by;
                let output = self.input_buffer[..num_frames * self.buffer_samples_by].to_vec();
                self.input_buffer =
                    self.input_buffer[num_frames * self.buffer_samples_by..].to_vec();
                Ok(output)
            }
        }
    }
}

enum DecoderState {
    WaitingForMasterChunk,
    ReadingChunks,
    ReadingData {
        fmt: FmtChunk,
        resampler: BufResampler,
        remaining_data: usize,
    },
}

pub struct Decoder {
    target_sample_rate: usize,
    state: DecoderState,
    buffer: Vec<u8>,
    fmt: Option<FmtChunk>,
    buffer_samples_by: usize,
}

impl Decoder {
    pub fn new(target_sample_rate: usize, buffer_samples_by: usize) -> Result<Self> {
        Ok(Self {
            target_sample_rate,
            state: DecoderState::WaitingForMasterChunk,
            buffer: vec![],
            fmt: None,
            buffer_samples_by,
        })
    }

    /// Return data resampled from the header sample rate to the target sample rate.
    pub fn decode(&mut self, input: &[u8]) -> Result<Vec<f32>> {
        self.buffer.extend_from_slice(input);
        let mut output = Vec::new();
        loop {
            let b = &self.buffer;
            match &mut self.state {
                DecoderState::WaitingForMasterChunk => {
                    let remaining = match MasterChunk::parse(b)? {
                        Parsed::Incomplete => return Ok(output),
                        Parsed::Complete(MasterChunk, remaining) => remaining,
                    };
                    self.state = DecoderState::ReadingChunks;
                    self.buffer = remaining.to_vec();
                }
                DecoderState::ReadingChunks => {
                    if b.len() < 8 {
                        return Ok(output);
                    }
                    let chunk_size = u32::from_le_bytes([b[4], b[5], b[6], b[7]]) as usize;
                    if &b[0..4] == b"data" {
                        let fmt = match &self.fmt {
                            Some(fmt) => fmt.clone(),
                            None => bail!("wav-decoder: data chunk before fmt chunk"),
                        };
                        let resampler = BufResampler::new(
                            fmt.sample_rate as usize,
                            self.target_sample_rate,
                            self.buffer_samples_by,
                        )?;
                        self.state = DecoderState::ReadingData {
                            fmt,
                            resampler,
                            remaining_data: chunk_size,
                        };
                        self.buffer = b[8..].to_vec();
                    } else {
                        if b.len() < 8 + chunk_size {
                            return Ok(output);
                        }
                        if &b[0..4] == b"fmt " {
                            let fmt = match FmtChunk::parse(&b[..8 + chunk_size])? {
                                Parsed::Incomplete => {
                                    anyhow::bail!("wav-decoder: unexpected incomplete fmt chunk")
                                }
                                Parsed::Complete(fmt, _) => fmt,
                            };
                            if fmt.audio_format != 1 {
                                bail!("wav-decoder: only WAV/PCM format is supported")
                            }
                            if fmt.bits_per_sample != 16
                                && fmt.bits_per_sample != 24
                                && fmt.bits_per_sample != 32
                            {
                                bail!("wav-decoder: only 16/24/32-bit samples supported")
                            }
                            self.fmt = Some(fmt);
                        }
                        self.buffer = b[8 + chunk_size..].to_vec();
                    }
                }
                DecoderState::ReadingData {
                    fmt,
                    resampler,
                    remaining_data,
                } => {
                    if *remaining_data == 0 {
                        self.state = DecoderState::ReadingChunks;
                        continue;
                    }
                    let bytes_per_sample = (fmt.bits_per_sample / 8) as usize;
                    let frame_size = fmt.channels as usize * bytes_per_sample;
                    let complete_frames = usize::min(*remaining_data, b.len()) / frame_size;
                    let samples_to_process = complete_frames * frame_size;
                    if samples_to_process == 0 {
                        return Ok(output);
                    }
                    for frame_start in (0..samples_to_process).step_by(frame_size) {
                        let mut sum = 0.0;
                        for channel in 0..fmt.channels as usize {
                            let sample_start = frame_start + channel * bytes_per_sample;
                            let sample = match fmt.bits_per_sample {
                                16 => {
                                    let raw =
                                        i16::from_le_bytes([b[sample_start], b[sample_start + 1]]);
                                    raw as f32 / 32768.0
                                }
                                24 => {
                                    let raw = i32::from_le_bytes([
                                        b[sample_start],
                                        b[sample_start + 1],
                                        b[sample_start + 2],
                                        if (b[sample_start + 2] & 0x80) != 0 {
                                            0xff
                                        } else {
                                            0x00
                                        },
                                    ]);
                                    raw as f32 / 8388608.0
                                }
                                32 => {
                                    let raw = i32::from_le_bytes([
                                        b[sample_start],
                                        b[sample_start + 1],
                                        b[sample_start + 2],
                                        b[sample_start + 3],
                                    ]);
                                    raw as f32 / 2147483648.0
                                }
                                bps => bail!("wav-decoder: unsupported bits per sample {bps}"),
                            };
                            sum += sample;
                        }
                        resampler.push(sum / fmt.channels as f32);
                    }
                    let resampled = resampler.flush()?;
                    extend_from_vec(&mut output, resampled);
                    self.buffer = self.buffer[complete_frames * frame_size..].to_vec();
                    *remaining_data = remaining_data.saturating_sub(complete_frames * frame_size);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn to_i16_passes_through_unchanged() {
        assert_eq!(Sample::to_i16(&1234i16), 1234);
        assert_eq!(Sample::to_i16(&-1234i16), -1234);
    }

    #[test]
    fn to_i16_scales_in_range_floats() {
        assert_eq!(Sample::to_i16(&0.0f32), 0);
        assert_eq!(Sample::to_i16(&0.5f32), 16383);
        assert_eq!(Sample::to_i16(&-0.5f64), -16383);
    }

    #[test]
    fn to_i16_clamps_out_of_range_floats() {
        assert_eq!(Sample::to_i16(&2.0f32), 32767);
        assert_eq!(Sample::to_i16(&-2.0f32), -32767);
        assert_eq!(Sample::to_i16(&1.0f64), 32767);
        assert_eq!(Sample::to_i16(&-1.0f64), -32767);
    }

    #[test]
    fn write_wav_header_produces_correct_byte_layout() {
        let mut buf = Vec::new();
        write_wav_header(&mut buf, 16000, 100, 84).unwrap();

        assert_eq!(buf.len(), 44);
        assert_eq!(&buf[0..4], b"RIFF");
        assert_eq!(u32::from_le_bytes(buf[4..8].try_into().unwrap()), 100);
        assert_eq!(&buf[8..12], b"WAVE");
        assert_eq!(&buf[12..16], b"fmt ");
        assert_eq!(u32::from_le_bytes(buf[16..20].try_into().unwrap()), 16);
        assert_eq!(u16::from_le_bytes(buf[20..22].try_into().unwrap()), 1); // PCM
        assert_eq!(u16::from_le_bytes(buf[22..24].try_into().unwrap()), 1); // mono
        assert_eq!(u32::from_le_bytes(buf[24..28].try_into().unwrap()), 16000);
        // byte_rate = sample_rate * channels * bytes_per_sample
        assert_eq!(u32::from_le_bytes(buf[28..32].try_into().unwrap()), 32000);
        // block_align = channels * bytes_per_sample
        assert_eq!(u16::from_le_bytes(buf[32..34].try_into().unwrap()), 2);
        assert_eq!(u16::from_le_bytes(buf[34..36].try_into().unwrap()), 16);
        assert_eq!(&buf[36..40], b"data");
        assert_eq!(u32::from_le_bytes(buf[40..44].try_into().unwrap()), 84);
    }

    #[test]
    fn write_pcm_in_wav_writes_little_endian_i16_samples() {
        let mut buf = Vec::new();
        let n = write_pcm_in_wav(&mut buf, &[0i16, 1i16, -1i16]).unwrap();

        assert_eq!(n, 6);
        assert_eq!(buf, vec![0, 0, 1, 0, 0xff, 0xff]);
    }

    #[test]
    fn extend_from_vec_replaces_when_target_is_empty() {
        let mut target: Vec<i32> = Vec::new();
        extend_from_vec(&mut target, vec![1, 2, 3]);
        assert_eq!(target, vec![1, 2, 3]);
    }

    #[test]
    fn extend_from_vec_appends_when_target_is_non_empty() {
        let mut target = vec![1, 2];
        extend_from_vec(&mut target, vec![3, 4]);
        assert_eq!(target, vec![1, 2, 3, 4]);
    }

    #[test]
    fn master_chunk_parse_rejects_short_buffer() {
        let data = b"RIFF";
        assert!(matches!(
            MasterChunk::parse(data).unwrap(),
            Parsed::Incomplete
        ));
    }

    #[test]
    fn master_chunk_parse_rejects_invalid_riff_magic() {
        let mut data = vec![0u8; 12];
        data[8..12].copy_from_slice(b"WAVE");
        assert!(MasterChunk::parse(&data).is_err());
    }

    #[test]
    fn master_chunk_parse_rejects_invalid_wave_magic() {
        let mut data = vec![0u8; 12];
        data[0..4].copy_from_slice(b"RIFF");
        assert!(MasterChunk::parse(&data).is_err());
    }

    #[test]
    fn master_chunk_parse_returns_remaining_bytes() {
        let mut data = vec![0u8; 14];
        data[0..4].copy_from_slice(b"RIFF");
        data[8..12].copy_from_slice(b"WAVE");
        data[12..14].copy_from_slice(&[7, 8]);

        match MasterChunk::parse(&data).unwrap() {
            Parsed::Complete(MasterChunk, remaining) => assert_eq!(remaining, &[7, 8]),
            Parsed::Incomplete => panic!("expected Complete"),
        }
    }

    fn fmt_chunk_bytes(audio_format: u16, channels: u16, sample_rate: u32, bits: u16) -> Vec<u8> {
        let mut b = vec![0u8; 24];
        b[0..4].copy_from_slice(b"fmt ");
        b[4..8].copy_from_slice(&16u32.to_le_bytes());
        b[8..10].copy_from_slice(&audio_format.to_le_bytes());
        b[10..12].copy_from_slice(&channels.to_le_bytes());
        b[12..16].copy_from_slice(&sample_rate.to_le_bytes());
        b[22..24].copy_from_slice(&bits.to_le_bytes());
        b
    }

    #[test]
    fn fmt_chunk_parse_rejects_short_buffer() {
        assert!(matches!(
            FmtChunk::parse(&[0u8; 8]).unwrap(),
            Parsed::Incomplete
        ));
    }

    #[test]
    fn fmt_chunk_parse_reads_expected_fields() {
        let bytes = fmt_chunk_bytes(1, 2, 44100, 24);
        match FmtChunk::parse(&bytes).unwrap() {
            Parsed::Complete(fmt, remaining) => {
                assert_eq!(fmt.audio_format, 1);
                assert_eq!(fmt.channels, 2);
                assert_eq!(fmt.sample_rate, 44100);
                assert_eq!(fmt.bits_per_sample, 24);
                assert!(remaining.is_empty());
            }
            Parsed::Incomplete => panic!("expected Complete"),
        }
    }

    /// Build a minimal mono 16-bit PCM WAV file containing the given samples.
    fn build_wav(sample_rate: u32, samples: &[i16]) -> Vec<u8> {
        let data_size = (samples.len() * 2) as u32;
        let mut buf = Vec::new();
        write_wav_header(&mut buf, sample_rate, 36 + data_size, data_size).unwrap();
        write_pcm_in_wav(&mut buf, samples).unwrap();
        buf
    }

    #[test]
    fn decoder_round_trips_pcm_samples_at_matching_sample_rate() {
        let samples: Vec<i16> = vec![0, 1000, -1000, 32767, -32767, 42];
        let wav = build_wav(16000, &samples);

        // buffer_samples_by = 1 and a matching target rate means every sample is
        // flushed immediately with no resampling.
        let mut decoder = Decoder::new(16000, 1).unwrap();
        let decoded = decoder.decode(&wav).unwrap();

        assert_eq!(decoded.len(), samples.len());
        for (got, raw) in decoded.iter().zip(samples.iter()) {
            let want = *raw as f32 / 32768.0;
            assert!((got - want).abs() < 1e-6, "got {got}, want {want}");
        }
    }

    #[test]
    fn decoder_handles_input_split_across_multiple_decode_calls() {
        let samples: Vec<i16> = vec![100, -100, 200, -200];
        let wav = build_wav(8000, &samples);
        let (head, tail) = wav.split_at(wav.len() / 2);

        let mut decoder = Decoder::new(8000, 1).unwrap();
        let mut decoded = decoder.decode(head).unwrap();
        decoded.extend(decoder.decode(tail).unwrap());

        assert_eq!(decoded.len(), samples.len());
    }

    #[test]
    fn decoder_rejects_non_pcm_audio_format() {
        let mut buf = Vec::new();
        buf.extend_from_slice(b"RIFF");
        buf.extend_from_slice(&36u32.to_le_bytes());
        buf.extend_from_slice(b"WAVE");
        buf.extend_from_slice(&fmt_chunk_bytes(3, 1, 16000, 16)); // 3 = IEEE float, unsupported

        let mut decoder = Decoder::new(16000, 1).unwrap();
        assert!(decoder.decode(&buf).is_err());
    }

    #[test]
    fn decoder_rejects_unsupported_bits_per_sample() {
        let mut buf = Vec::new();
        buf.extend_from_slice(b"RIFF");
        buf.extend_from_slice(&36u32.to_le_bytes());
        buf.extend_from_slice(b"WAVE");
        buf.extend_from_slice(&fmt_chunk_bytes(1, 1, 16000, 8)); // 8-bit unsupported

        let mut decoder = Decoder::new(16000, 1).unwrap();
        assert!(decoder.decode(&buf).is_err());
    }
}
