//! Benchmark fixture manifests.
//!
//! Boundaries are known *by construction* — fixtures are concatenated from
//! utterances whose sample offsets we recorded when building them — never
//! inferred by running a VAD over them, which would make the measurement
//! circular.

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct FixtureTurn {
    /// 24 kHz mono WAV. Resampled by the harness if it is not already 24 kHz.
    pub wav: PathBuf,
    /// Sample offset within `wav` where speech begins.
    pub speech_start_sample: u64,
    /// Sample offset where speech ends. This is the ground truth the headline
    /// metric and `premature_cut_rate` are both measured against.
    pub speech_end_sample: u64,
    /// Silence streamed after this turn before the next one starts.
    pub gap_after_s: f64,
    /// Fixture category, e.g. "short_answer", "mid_sentence_pause".
    pub category: String,
}

impl FixtureTurn {
    pub fn validate(&self) -> Result<()> {
        if self.speech_end_sample <= self.speech_start_sample {
            anyhow::bail!(
                "speech_end_sample ({}) must exceed speech_start_sample ({}) in {}",
                self.speech_end_sample,
                self.speech_start_sample,
                self.wav.display()
            );
        }
        if self.gap_after_s <= 0.0 {
            anyhow::bail!(
                "gap_after_s must be positive in {} — the harness must keep \
                 streaming silence so VAD keeps ticking",
                self.wav.display()
            );
        }
        Ok(())
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Manifest {
    pub name: String,
    pub turns: Vec<FixtureTurn>,
}

impl Manifest {
    pub fn load(path: &Path) -> Result<Self> {
        let body = std::fs::read_to_string(path)
            .with_context(|| format!("reading manifest {}", path.display()))?;
        let manifest: Self = serde_json::from_str(&body)
            .with_context(|| format!("parsing manifest {}", path.display()))?;
        for turn in &manifest.turns {
            turn.validate()?;
        }
        Ok(manifest)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_manifest() {
        let json = r#"{
          "name": "smoke",
          "turns": [
            {"wav": "a.wav", "speech_start_sample": 0, "speech_end_sample": 24000,
             "gap_after_s": 3.0, "category": "short_answer"}
          ]
        }"#;
        let m: Manifest = serde_json::from_str(json).unwrap();
        assert_eq!(m.name, "smoke");
        assert_eq!(m.turns.len(), 1);
        assert_eq!(m.turns[0].speech_end_sample, 24000);
        assert_eq!(m.turns[0].category, "short_answer");
        m.turns[0].validate().unwrap();
    }

    #[test]
    fn rejects_end_before_start() {
        let turn = FixtureTurn {
            wav: PathBuf::from("a.wav"),
            speech_start_sample: 24000,
            speech_end_sample: 100,
            gap_after_s: 3.0,
            category: "bad".to_string(),
        };
        let err = turn.validate().unwrap_err().to_string();
        assert!(err.contains("speech_end_sample"), "got: {err}");
    }

    #[test]
    fn rejects_non_positive_gap() {
        // The harness must keep streaming silence after each utterance: VAD
        // needs frames to keep ticking and the flush mechanism depends on it.
        let turn = FixtureTurn {
            wav: PathBuf::from("a.wav"),
            speech_start_sample: 0,
            speech_end_sample: 100,
            gap_after_s: 0.0,
            category: "bad".to_string(),
        };
        let err = turn.validate().unwrap_err().to_string();
        assert!(err.contains("gap_after_s"), "got: {err}");
    }
}
