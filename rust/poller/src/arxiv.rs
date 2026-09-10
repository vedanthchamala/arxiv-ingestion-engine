use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use chrono::{DateTime, Utc};
use common::models::{Paper, SCHEMA_VERSION};
use common::ratelimit::MinInterval;
use roxmltree::{Document, Node};
use tracing::{debug, warn};

const ATOM: &str = "http://www.w3.org/2005/Atom";
const ARXIV: &str = "http://arxiv.org/schemas/atom";
const OPENSEARCH: &str = "http://a9.com/-/spec/opensearch/1.1/";

pub struct ArxivClient {
    http: reqwest::Client,
    limiter: MinInterval,
    base_url: String,
}

#[derive(Debug)]
pub struct Page {
    pub total_results: usize,
    pub entries: Vec<Paper>,
}

impl ArxivClient {
    pub fn new(user_agent: &str, min_interval: Duration) -> Result<Self> {
        let http = reqwest::Client::builder()
            .user_agent(user_agent)
            .timeout(Duration::from_secs(60))
            .build()?;
        Ok(Self {
            http,
            limiter: MinInterval::new(min_interval),
            base_url: "https://export.arxiv.org/api/query".to_string(),
        })
    }

    /// One page of a category, newest-updated first. Honors the global request spacing.
    pub async fn category_page(&self, category: &str, start: usize, max_results: usize) -> Result<Page> {
        self.limiter.wait().await;
        let query = format!("cat:{category}");
        debug!(category, start, max_results, "arxiv query");
        let resp = self
            .http
            .get(&self.base_url)
            .query(&[
                ("search_query", query.as_str()),
                ("sortBy", "lastUpdatedDate"),
                ("sortOrder", "descending"),
                ("start", &start.to_string()),
                ("max_results", &max_results.to_string()),
            ])
            .send()
            .await
            .context("arxiv request")?
            .error_for_status()
            .context("arxiv status")?;
        let body = resp.text().await.context("arxiv body")?;
        parse_feed(&body, Utc::now())
    }
}

pub fn parse_feed(xml: &str, polled_at: DateTime<Utc>) -> Result<Page> {
    let doc = Document::parse(xml).context("parse atom feed")?;
    let feed = doc.root_element();
    if feed.tag_name().name() != "feed" {
        bail!("unexpected root element <{}>", feed.tag_name().name());
    }
    let total_results = child_text(feed, OPENSEARCH, "totalResults")
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(0);

    let mut entries = Vec::new();
    for entry in feed.children().filter(|n| is(n, ATOM, "entry")) {
        match parse_entry(entry, polled_at) {
            Ok(Some(p)) => entries.push(p),
            Ok(None) => {}
            Err(e) => warn!(error = %e, "skipping unparseable entry"),
        }
    }
    Ok(Page { total_results, entries })
}

fn parse_entry(entry: Node, polled_at: DateTime<Utc>) -> Result<Option<Paper>> {
    let raw_id = child_text(entry, ATOM, "id").ok_or_else(|| anyhow!("entry without <id>"))?;
    let Some(versioned) = raw_id
        .trim()
        .rsplit("/abs/")
        .next()
        .filter(|_| raw_id.contains("/abs/"))
    else {
        // The API reports query errors as a pseudo-entry with an /api/errors# id.
        warn!(id = raw_id.trim(), "non-paper entry in feed");
        return Ok(None);
    };
    let (arxiv_id, version) = split_version(versioned)?;

    let title = normalize_ws(&child_text(entry, ATOM, "title").unwrap_or_default());
    let abstract_text = normalize_ws(&child_text(entry, ATOM, "summary").unwrap_or_default());
    if title.is_empty() || abstract_text.is_empty() {
        bail!("{versioned}: missing title or abstract");
    }

    let published_at = parse_date(&child_text(entry, ATOM, "published").unwrap_or_default())
        .with_context(|| format!("{versioned}: published"))?;
    let updated_at = parse_date(&child_text(entry, ATOM, "updated").unwrap_or_default())
        .with_context(|| format!("{versioned}: updated"))?;

    let authors: Vec<String> = entry
        .children()
        .filter(|n| is(n, ATOM, "author"))
        .filter_map(|a| child_text(a, ATOM, "name"))
        .map(|s| normalize_ws(&s))
        .filter(|s| !s.is_empty())
        .collect();

    let mut categories: Vec<String> = entry
        .children()
        .filter(|n| is(n, ATOM, "category"))
        .filter_map(|c| c.attribute("term").map(str::to_string))
        .collect();
    let primary_category = entry
        .children()
        .find(|n| is(n, ARXIV, "primary_category"))
        .and_then(|c| c.attribute("term").map(str::to_string))
        .or_else(|| categories.first().cloned())
        .ok_or_else(|| anyhow!("{versioned}: no category"))?;
    if !categories.contains(&primary_category) {
        categories.insert(0, primary_category.clone());
    }

    let mut pdf_url = None;
    let mut doi_link = None;
    for link in entry.children().filter(|n| is(n, ATOM, "link")) {
        match (link.attribute("title"), link.attribute("href")) {
            (Some("pdf"), Some(href)) => pdf_url = Some(href.to_string()),
            (Some("doi"), Some(href)) => doi_link = Some(href.to_string()),
            _ => {}
        }
    }
    let pdf_url = pdf_url
        .map(|u| u.replacen("http://", "https://", 1))
        .unwrap_or_else(|| format!("https://arxiv.org/pdf/{arxiv_id}v{version}"));
    let html_url = Some(format!("https://arxiv.org/html/{arxiv_id}v{version}"));

    let doi = child_text(entry, ARXIV, "doi")
        .map(|s| s.trim().to_string())
        .or_else(|| doi_link.and_then(|u| u.rsplit_once("doi.org/").map(|(_, d)| d.to_string())));
    let journal_ref = child_text(entry, ARXIV, "journal_ref").map(|s| normalize_ws(&s));
    let comment = child_text(entry, ARXIV, "comment").map(|s| normalize_ws(&s));

    Ok(Some(Paper {
        schema_version: SCHEMA_VERSION,
        arxiv_id,
        version,
        title,
        abstract_text,
        authors,
        primary_category,
        categories,
        published_at,
        updated_at,
        pdf_url,
        html_url,
        doi,
        journal_ref,
        comment,
        polled_at,
    }))
}

/// "2609.01234v2" -> ("2609.01234", 2); "hep-th/9901001v1" -> ("hep-th/9901001", 1).
fn split_version(versioned: &str) -> Result<(String, u32)> {
    let Some(pos) = versioned.rfind('v') else {
        bail!("{versioned}: no version suffix");
    };
    let (id, v) = versioned.split_at(pos);
    let version: u32 = v[1..].parse().with_context(|| format!("{versioned}: bad version"))?;
    if id.is_empty() {
        bail!("{versioned}: empty id");
    }
    Ok((id.to_string(), version))
}

fn parse_date(s: &str) -> Result<DateTime<Utc>> {
    Ok(DateTime::parse_from_rfc3339(s.trim())?.with_timezone(&Utc))
}

fn normalize_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn is(n: &Node, ns: &str, name: &str) -> bool {
    n.is_element() && n.tag_name().name() == name && n.tag_name().namespace() == Some(ns)
}

fn child_text(n: Node, ns: &str, name: &str) -> Option<String> {
    n.children()
        .find(|c| is(c, ns, name))
        .and_then(|c| c.text())
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title type="html">ArXiv Query: search_query=cat:cs.RO</title>
  <opensearch:totalResults>12345</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>2</opensearch:itemsPerPage>
  <entry>
    <id>http://arxiv.org/abs/2609.01234v2</id>
    <updated>2026-09-05T17:59:00Z</updated>
    <published>2026-09-01T12:00:00Z</published>
    <title>  Diffusion Policies for
   Dexterous Manipulation </title>
    <summary>We study
  diffusion.  Very much.
</summary>
    <author><name>Ada Lovelace</name><arxiv:affiliation>Analytical Engine</arxiv:affiliation></author>
    <author><name>Grace  Hopper</name></author>
    <arxiv:doi>10.1000/xyz123</arxiv:doi>
    <link title="doi" href="http://dx.doi.org/10.1000/xyz123" rel="related"/>
    <arxiv:comment>12 pages, 3 figures</arxiv:comment>
    <arxiv:journal_ref>Proc. Robotics 2026</arxiv:journal_ref>
    <link href="http://arxiv.org/abs/2609.01234v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2609.01234v2" rel="related" type="application/pdf"/>
    <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.RO" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.RO" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/hep-th/9901001v1</id>
    <updated>1999-01-04T00:00:00Z</updated>
    <published>1999-01-04T00:00:00Z</published>
    <title>Old style</title>
    <summary>Old abstract.</summary>
    <author><name>Someone</name></author>
    <category term="hep-th" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format_for_x</id>
    <title>Error</title>
    <summary>incorrect id format</summary>
  </entry>
</feed>"#;

    #[test]
    fn parses_entries_and_normalizes() {
        let page = parse_feed(SAMPLE, Utc::now()).unwrap();
        assert_eq!(page.total_results, 12345);
        assert_eq!(page.entries.len(), 2);

        let p = &page.entries[0];
        assert_eq!(p.arxiv_id, "2609.01234");
        assert_eq!(p.version, 2);
        assert_eq!(p.title, "Diffusion Policies for Dexterous Manipulation");
        assert_eq!(p.abstract_text, "We study diffusion. Very much.");
        assert_eq!(p.authors, vec!["Ada Lovelace", "Grace Hopper"]);
        assert_eq!(p.primary_category, "cs.RO");
        assert_eq!(p.categories, vec!["cs.RO", "cs.LG"]);
        assert_eq!(p.pdf_url, "https://arxiv.org/pdf/2609.01234v2");
        assert_eq!(p.html_url.as_deref(), Some("https://arxiv.org/html/2609.01234v2"));
        assert_eq!(p.doi.as_deref(), Some("10.1000/xyz123"));
        assert_eq!(p.comment.as_deref(), Some("12 pages, 3 figures"));
        assert_eq!(p.journal_ref.as_deref(), Some("Proc. Robotics 2026"));

        let old = &page.entries[1];
        assert_eq!(old.arxiv_id, "hep-th/9901001");
        assert_eq!(old.version, 1);
        assert_eq!(old.primary_category, "hep-th");
        assert_eq!(old.pdf_url, "https://arxiv.org/pdf/hep-th/9901001v1");
    }

    #[test]
    fn entries_validate_against_shared_schema() {
        let v = common::schema::Validators::load().unwrap();
        let page = parse_feed(SAMPLE, Utc::now()).unwrap();
        for p in &page.entries {
            common::schema::validate_and_serialize(&v.paper, p).unwrap();
        }
    }

    #[test]
    fn split_version_cases() {
        assert_eq!(split_version("2609.01234v12").unwrap(), ("2609.01234".into(), 12));
        assert!(split_version("2609.01234").is_err());
        assert!(split_version("v1").is_err());
    }
}
