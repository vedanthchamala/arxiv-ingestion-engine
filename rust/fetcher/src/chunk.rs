//! Token-window chunking using the embedding model's own tokenizer, so `token_count` is exact
//! for the model that will embed the chunk.

use anyhow::{anyhow, ensure, Result};
use common::models::{Chunk, Paper};
use tokenizers::Tokenizer;

use crate::html::Section;

pub struct Chunker {
    tok: Tokenizer,
    max_tokens: usize,
    overlap: usize,
    max_chunks: usize,
}

impl Chunker {
    pub fn from_pretrained(model: &str, max_tokens: usize, overlap: usize, max_chunks: usize) -> Result<Self> {
        let mut tok = Tokenizer::from_pretrained(model, None).map_err(|e| anyhow!("load tokenizer {model}: {e}"))?;
        tok.with_truncation(None).map_err(|e| anyhow!("{e}"))?;
        Self::new(tok, max_tokens, overlap, max_chunks)
    }

    pub fn new(tok: Tokenizer, max_tokens: usize, overlap: usize, max_chunks: usize) -> Result<Self> {
        ensure!(max_tokens > overlap, "chunk size must exceed overlap");
        ensure!(max_chunks >= 1, "max_chunks must be >= 1");
        Ok(Self { tok, max_tokens, overlap, max_chunks })
    }

    pub fn count(&self, text: &str) -> Result<usize> {
        Ok(self.tok.encode(text, false).map_err(|e| anyhow!("{e}"))?.len())
    }

    /// Chunk 0 is always title + abstract, so abstract-only rows and full-text rows share a shape.
    pub fn chunk(&self, paper: &Paper, sections: &[Section]) -> Result<Vec<Chunk>> {
        let lead = format!("{}\n\n{}", paper.title, paper.abstract_text);
        let mut chunks = vec![Chunk { idx: 0, section: Some("abstract".into()), text: lead.clone(), token_count: self.count(&lead)?.max(1) as u32 }];

        'outer: for sec in sections {
            let enc = self.tok.encode(sec.text.as_str(), false).map_err(|e| anyhow!("{e}"))?;
            let offsets = enc.get_offsets();
            let n = offsets.len();
            if n < 8 {
                continue;
            }
            let mut start = 0usize;
            while start < n {
                if chunks.len() >= self.max_chunks {
                    break 'outer;
                }
                let end = (start + self.max_tokens).min(n);
                let b0 = floor_char_boundary(&sec.text, offsets[start].0);
                let b1 = ceil_char_boundary(&sec.text, offsets[end - 1].1);
                let slice = sec.text[b0..b1].trim();
                if !slice.is_empty() {
                    chunks.push(Chunk {
                        idx: chunks.len() as u32,
                        section: Some(sec.title.clone()),
                        text: format!("{}\n{}", sec.title, slice),
                        token_count: (end - start) as u32,
                    });
                }
                if end == n {
                    break;
                }
                start = end - self.overlap;
            }
        }
        Ok(chunks)
    }
}

fn floor_char_boundary(s: &str, mut i: usize) -> usize {
    i = i.min(s.len());
    while !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

fn ceil_char_boundary(s: &str, mut i: usize) -> usize {
    i = i.min(s.len());
    while !s.is_char_boundary(i) {
        i += 1;
    }
    i
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;
    use common::models::SCHEMA_VERSION;

    /// A tiny whitespace tokenizer so tests need no network or model download.
    fn word_tokenizer() -> Tokenizer {
        let json = r#"{"version":"1.0","pre_tokenizer":{"type":"Whitespace"},
            "model":{"type":"WordLevel","vocab":{"[UNK]":0},"unk_token":"[UNK]"}}"#;
        Tokenizer::from_bytes(json.as_bytes()).unwrap()
    }

    fn paper() -> Paper {
        Paper {
            schema_version: SCHEMA_VERSION,
            arxiv_id: "2609.00001".into(),
            version: 1,
            title: "Title".into(),
            abstract_text: "Abstract text.".into(),
            authors: vec![],
            primary_category: "cs.LG".into(),
            categories: vec!["cs.LG".into()],
            published_at: Utc::now(),
            updated_at: Utc::now(),
            pdf_url: "https://arxiv.org/pdf/2609.00001v1".into(),
            html_url: None,
            doi: None,
            journal_ref: None,
            comment: None,
            polled_at: Utc::now(),
        }
    }

    #[test]
    fn windows_overlap_and_preserve_original_text() {
        let chunker = Chunker::new(word_tokenizer(), 10, 3, 100).unwrap();
        let words: Vec<String> = (1..=25).map(|i| format!("w{i}")).collect();
        let sec = Section { title: "1 Intro".into(), text: words.join(" ") };
        let chunks = chunker.chunk(&paper(), &[sec]).unwrap();
        assert_eq!(chunks[0].idx, 0);
        assert_eq!(chunks[0].section.as_deref(), Some("abstract"));
        let bodies: Vec<&str> = chunks[1..].iter().map(|c| c.text.split_once('\n').unwrap().1).collect();
        assert_eq!(bodies[0], "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10");
        assert_eq!(bodies[1], "w8 w9 w10 w11 w12 w13 w14 w15 w16 w17");
        assert_eq!(bodies.last().unwrap().split(' ').next_back().unwrap(), "w25");
        assert!(chunks[1..].iter().all(|c| c.token_count <= 10));
        assert_eq!(chunks.iter().map(|c| c.idx).collect::<Vec<_>>(), (0..chunks.len() as u32).collect::<Vec<_>>());
    }

    #[test]
    fn respects_max_chunks() {
        let chunker = Chunker::new(word_tokenizer(), 5, 1, 3).unwrap();
        let sec = Section { title: "S".into(), text: "w ".repeat(200) };
        let chunks = chunker.chunk(&paper(), &[sec]).unwrap();
        assert_eq!(chunks.len(), 3);
    }

    #[test]
    fn unicode_boundaries_are_safe() {
        let chunker = Chunker::new(word_tokenizer(), 4, 1, 100).unwrap();
        let sec = Section { title: "S".into(), text: "naïve café résumé façade jalapeño über schön straße groß".into() };
        let chunks = chunker.chunk(&paper(), &[sec]).unwrap();
        assert!(chunks.len() > 2);
    }
}
