use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

pub const SCHEMA_VERSION: u8 = 1;

/// Topic `papers.new`. One arXiv paper version, exactly as the arXiv API returned it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Paper {
    pub schema_version: u8,
    pub arxiv_id: String,
    pub version: u32,
    pub title: String,
    #[serde(rename = "abstract")]
    pub abstract_text: String,
    pub authors: Vec<String>,
    pub primary_category: String,
    pub categories: Vec<String>,
    pub published_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub pdf_url: String,
    #[serde(default)]
    pub html_url: Option<String>,
    #[serde(default)]
    pub doi: Option<String>,
    #[serde(default)]
    pub journal_ref: Option<String>,
    #[serde(default)]
    pub comment: Option<String>,
    pub polled_at: DateTime<Utc>,
}

impl Paper {
    /// Kafka partition key: all versions of a paper land on the same partition.
    pub fn key(&self) -> &str {
        &self.arxiv_id
    }

    /// Redis seen-set member: a new version of a known paper is treated as new.
    pub fn seen_member(&self) -> String {
        format!("{}v{}", self.arxiv_id, self.version)
    }

    pub fn versioned_id(&self) -> String {
        format!("{}v{}", self.arxiv_id, self.version)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Chunk {
    pub idx: u32,
    pub section: Option<String>,
    pub text: String,
    pub token_count: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TextSource {
    Html,
    Pdf,
    Abstract,
}

/// Topic `papers.chunked`. The original message plus extracted, chunked full text.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChunkedPaper {
    pub schema_version: u8,
    pub paper: Paper,
    pub source: TextSource,
    pub chunks: Vec<Chunk>,
    pub fetched_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Stage {
    Fetcher,
    Worker,
}

/// Topic `papers.failed` (dead letter).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Failed {
    pub schema_version: u8,
    pub arxiv_id: String,
    pub stage: Stage,
    pub error: String,
    pub attempts: u32,
    pub failed_at: DateTime<Utc>,
    pub payload: serde_json::Value,
}
