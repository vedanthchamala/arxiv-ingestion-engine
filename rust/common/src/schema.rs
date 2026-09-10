use anyhow::{Context, Result, anyhow};
use jsonschema::Validator;
use serde::Serialize;
use serde_json::{Value, json};

/// The shared contract, embedded at compile time so binaries never depend on the working directory.
pub const MESSAGES_V1: &str = include_str!("../../../schemas/messages.v1.json");

pub struct Validators {
    pub paper: Validator,
    pub chunked: Validator,
    pub failed: Validator,
}

impl Validators {
    pub fn load() -> Result<Self> {
        let root: Value = serde_json::from_str(MESSAGES_V1).context("parse messages.v1.json")?;
        Ok(Self {
            paper: compile(&root, "paper")?,
            chunked: compile(&root, "chunked")?,
            failed: compile(&root, "failed")?,
        })
    }
}

fn compile(root: &Value, def: &str) -> Result<Validator> {
    let schema = json!({
        "$schema": root["$schema"],
        "$ref": format!("#/$defs/{def}"),
        "$defs": root["$defs"],
    });
    jsonschema::options()
        .should_validate_formats(true)
        .build(&schema)
        .map_err(|e| anyhow!("compile schema `{def}`: {e}"))
}

/// Serialize `value` and check it against `validator`; returns the JSON bytes on success.
pub fn validate_and_serialize<T: Serialize>(validator: &Validator, value: &T) -> Result<Vec<u8>> {
    let instance = serde_json::to_value(value)?;
    if let Err(err) = validator.validate(&instance) {
        return Err(anyhow!("message violates schema at {}: {}", err.instance_path(), err));
    }
    Ok(serde_json::to_vec(&instance)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{ChunkedPaper, Failed, Paper, SCHEMA_VERSION, Stage, TextSource};
    use chrono::Utc;

    /// Written by `arxiv_common.schema.dumps` (pydantic models); see tests/fixtures/README.
    const PYTHON_FAILED: &str = include_str!("../tests/fixtures/python_failed.json");
    const PYTHON_CHUNKED: &str = include_str!("../tests/fixtures/python_chunked.json");

    fn sample() -> Paper {
        Paper {
            schema_version: SCHEMA_VERSION,
            arxiv_id: "2609.01234".into(),
            version: 2,
            title: "A title".into(),
            abstract_text: "An abstract.".into(),
            authors: vec!["Ada Lovelace".into()],
            primary_category: "cs.LG".into(),
            categories: vec!["cs.LG".into(), "cs.CV".into()],
            published_at: Utc::now(),
            updated_at: Utc::now(),
            pdf_url: "https://arxiv.org/pdf/2609.01234v2".into(),
            html_url: Some("https://arxiv.org/html/2609.01234v2".into()),
            doi: None,
            journal_ref: None,
            comment: None,
            polled_at: Utc::now(),
        }
    }

    #[test]
    fn paper_roundtrips_and_validates() {
        let v = Validators::load().unwrap();
        let bytes = validate_and_serialize(&v.paper, &sample()).unwrap();
        let back: Paper = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(back.abstract_text, "An abstract.");
        assert_eq!(back.seen_member(), "2609.01234v2");
    }

    /// The reverse of the Python suite's check on a Rust-produced `paper`: messages the Python
    /// worker produces must parse into the Rust structs and pass the shared schema.
    #[test]
    fn python_produced_failed_and_chunked_are_accepted() {
        let v = Validators::load().unwrap();

        let raw: Value = serde_json::from_str(PYTHON_FAILED).unwrap();
        assert!(v.failed.validate(&raw).is_ok(), "python failed message violates schema");
        let failed: Failed = serde_json::from_str(PYTHON_FAILED).unwrap();
        assert_eq!(failed.stage, Stage::Worker);
        assert_eq!(failed.attempts, 3);
        assert_eq!(failed.arxiv_id, "2609.01234");
        assert_eq!(failed.payload["arxiv_id"], "2609.01234");
        validate_and_serialize(&v.failed, &failed).unwrap();

        let raw: Value = serde_json::from_str(PYTHON_CHUNKED).unwrap();
        assert!(
            v.chunked.validate(&raw).is_ok(),
            "python chunked message violates schema"
        );
        let doc: ChunkedPaper = serde_json::from_str(PYTHON_CHUNKED).unwrap();
        assert_eq!(doc.source, TextSource::Abstract);
        assert_eq!(doc.chunks.len(), 1);
        assert_eq!(doc.chunks[0].section.as_deref(), Some("abstract"));
        assert!(doc.chunks[0].text.starts_with(&doc.paper.title));
        assert_eq!(doc.paper.html_url, None);
        assert_eq!(doc.paper.seen_member(), "2609.01234v1");
        validate_and_serialize(&v.chunked, &doc).unwrap();
    }

    #[test]
    fn schema_rejects_bad_id_and_empty_categories() {
        let v = Validators::load().unwrap();
        let mut p = sample();
        p.arxiv_id = "not-an-id".into();
        assert!(validate_and_serialize(&v.paper, &p).is_err());
        let mut p = sample();
        p.categories.clear();
        assert!(validate_and_serialize(&v.paper, &p).is_err());
    }
}
