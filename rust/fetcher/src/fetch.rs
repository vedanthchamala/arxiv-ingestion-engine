//! All traffic to arxiv.org goes through this one struct, behind the shared request spacing.

use std::time::Duration;

use anyhow::{Context, Result, bail};
use common::ratelimit::Limiter;
use common::telemetry::{record_arxiv_request, record_ratelimit_wait};
use reqwest::StatusCode;
use tracing::{debug, warn};

const PROCESS: &str = "fetcher";

pub struct Fetcher {
    http: reqwest::Client,
    limiter: Limiter,
    max_pdf_bytes: usize,
}

impl Fetcher {
    pub fn new(user_agent: &str, limiter: Limiter, max_pdf_bytes: usize) -> Result<Self> {
        let http = reqwest::Client::builder()
            .user_agent(user_agent)
            .timeout(Duration::from_secs(120))
            .build()?;
        Ok(Self {
            http,
            limiter,
            max_pdf_bytes,
        })
    }

    /// `Ok(None)` when arXiv has no HTML rendering for this version.
    pub async fn html(&self, url: &str) -> Result<Option<String>> {
        let Some(resp) = self.get("html", url).await? else {
            return Ok(None);
        };
        let body = resp.text().await.context("html body")?;
        if !body.contains("ltx_document") {
            warn!(url, "200 but not a LaTeXML page");
            return Ok(None);
        }
        Ok(Some(body))
    }

    /// `Ok(None)` when the PDF is missing or larger than the configured cap.
    pub async fn pdf(&self, url: &str) -> Result<Option<Vec<u8>>> {
        let Some(resp) = self.get("pdf", url).await? else {
            return Ok(None);
        };
        if let Some(len) = resp.content_length()
            && len as usize > self.max_pdf_bytes
        {
            warn!(url, bytes = len, "pdf over size cap; skipping full text");
            return Ok(None);
        }
        let bytes = resp.bytes().await.context("pdf body")?;
        if bytes.len() > self.max_pdf_bytes {
            warn!(url, bytes = bytes.len(), "pdf over size cap; skipping full text");
            return Ok(None);
        }
        if !bytes.starts_with(b"%PDF") {
            bail!("pdf {url}: body is not a PDF");
        }
        Ok(Some(bytes.to_vec()))
    }

    /// Waits for a send slot, issues the GET and records the outcome. `Ok(None)` on 404.
    async fn get(&self, kind: &'static str, url: &str) -> Result<Option<reqwest::Response>> {
        let waited = self.limiter.wait().await;
        record_ratelimit_wait(PROCESS, waited);
        debug!(kind, url, wait_ms = waited.as_millis() as u64, "arxiv request");
        let resp = match self.http.get(url).send().await {
            Ok(r) => r,
            Err(e) => {
                record_arxiv_request(PROCESS, kind, "error");
                return Err(e).with_context(|| format!("{kind} request"));
            }
        };
        match resp.status() {
            StatusCode::OK => {
                record_arxiv_request(PROCESS, kind, "ok");
                Ok(Some(resp))
            }
            StatusCode::NOT_FOUND => {
                record_arxiv_request(PROCESS, kind, "not_found");
                Ok(None)
            }
            s => {
                record_arxiv_request(PROCESS, kind, "error");
                bail!("{kind} {url}: HTTP {s}")
            }
        }
    }
}
