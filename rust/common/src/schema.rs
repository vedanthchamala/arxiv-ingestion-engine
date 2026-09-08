use anyhow::{anyhow, Context, Result};
use jsonschema::Validator;
use serde::Serialize;
use serde_json::{json, Value};

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
    use crate::models::{Paper, SCHEMA_VERSION};
    use chrono::Utc;

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
