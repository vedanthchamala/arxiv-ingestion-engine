//! All traffic to arxiv.org goes through this one struct, behind the shared request spacing.

use std::time::Duration;

use anyhow::{Context, Result, bail};
use common::ratelimit::MinInterval;
use reqwest::StatusCode;
use tracing::{debug, warn};

pub struct Fetcher {
    http: reqwest::Client,
    limiter: MinInterval,
    max_pdf_bytes: usize,
}

impl Fetcher {
    pub fn new(user_agent: &str, min_interval: Duration, max_pdf_bytes: usize) -> Result<Self> {
        let http = reqwest::Client::builder()
            .user_agent(user_agent)
            .timeout(Duration::from_secs(120))
            .build()?;
        Ok(Self {
            http,
            limiter: MinInterval::new(min_interval),
            max_pdf_bytes,
        })
    }

    /// `Ok(None)` when arXiv has no HTML rendering for this version.
    pub async fn html(&self, url: &str) -> Result<Option<String>> {
        self.limiter.wait().await;
        debug!(url, "GET html");
        let resp = self.http.get(url).send().await.context("html request")?;
        match resp.status() {
            StatusCode::OK => {}
            StatusCode::NOT_FOUND => return Ok(None),
            s => bail!("html {url}: HTTP {s}"),
        }
        let body = resp.text().await.context("html body")?;
        if !body.contains("ltx_document") {
            warn!(url, "200 but not a LaTeXML page");
            return Ok(None);
        }
        Ok(Some(body))
    }

    /// `Ok(None)` when the PDF is missing or larger than the configured cap.
    pub async fn pdf(&self, url: &str) -> Result<Option<Vec<u8>>> {
        self.limiter.wait().await;
        debug!(url, "GET pdf");
        let resp = self.http.get(url).send().await.context("pdf request")?;
        match resp.status() {
            StatusCode::OK => {}
            StatusCode::NOT_FOUND => return Ok(None),
            s => bail!("pdf {url}: HTTP {s}"),
        }
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
}
