//! Full text from PDFs via poppler's `pdftotext`, with reference-list stripping and a light
//! section heuristic.

use std::path::Path;
use std::sync::LazyLock;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use regex::Regex;
use tokio::process::Command;

use crate::html::Section;

static REFERENCES_HEADING: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?im)^\s*(?:\d+\.?\s+)?(references|bibliography|works cited)\s*$").unwrap());
static SECTION_HEADING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?m)^\s*(\d{1,2}(?:\.\d{1,2})?)\.?\s+([A-Z][A-Za-z][A-Za-z ,:&\-]{2,70})\s*$").unwrap()
});
static HYPHEN_BREAK: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(\w)-\n(\w)").unwrap());

pub async fn ensure_pdftotext() -> Result<()> {
    let out = Command::new("pdftotext")
        .arg("-v")
        .output()
        .await
        .context("pdftotext not found on PATH (install poppler)")?;
    if !out.status.success() && out.stderr.is_empty() {
        bail!("pdftotext -v failed");
    }
    Ok(())
}

pub async fn pdftotext(path: &Path) -> Result<String> {
    let run = Command::new("pdftotext")
        .args(["-enc", "UTF-8", "-nopgbrk"])
        .arg(path)
        .arg("-")
        .output();
    let out = tokio::time::timeout(Duration::from_secs(90), run)
        .await
        .map_err(|_| anyhow!("pdftotext timed out"))??;
    if !out.status.success() {
        bail!(
            "pdftotext exited {}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Reflows extracted text: joins hyphenated line breaks, keeps blank lines as paragraph breaks,
/// and drops the reference list when a heading for it appears in the back half of the paper.
pub fn clean(text: &str) -> String {
    let text = text.replace('\r', "");
    let text = HYPHEN_BREAK.replace_all(&text, "$1$2");
    let cut = REFERENCES_HEADING
        .find_iter(&text)
        .map(|m| m.start())
        .filter(|&pos| pos > text.len() / 2)
        .last();
    let body = match cut {
        Some(pos) => &text[..pos],
        None => &text,
    };
    body.split("\n\n")
        .map(|para| para.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|p| !p.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

/// Splits on numbered headings when at least three are found; otherwise one "Body" section.
pub fn into_sections(clean_text: &str) -> Vec<Section> {
    let heads: Vec<(usize, usize, String)> = SECTION_HEADING
        .captures_iter(clean_text)
        .filter_map(|c| {
            let m = c.get(0)?;
            Some((m.start(), m.end(), format!("{} {}", &c[1], c[2].trim())))
        })
        .collect();
    if heads.len() < 3 {
        return if clean_text.len() >= 200 {
            vec![Section {
                title: "Body".into(),
                text: clean_text.to_string(),
            }]
        } else {
            Vec::new()
        };
    }
    let mut out = Vec::new();
    let preamble = clean_text[..heads[0].0].trim();
    if preamble.len() >= 200 {
        out.push(Section {
            title: "Front matter".into(),
            text: preamble.to_string(),
        });
    }
    for (i, (_, end, title)) in heads.iter().enumerate() {
        let stop = heads.get(i + 1).map(|h| h.0).unwrap_or(clean_text.len());
        let text = clean_text[*end..stop].trim();
        if text.len() >= 40 {
            out.push(Section {
                title: title.clone(),
                text: text.to_string(),
            });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_joins_hyphens_and_strips_references() {
        let raw = "Intro para about net-\nworks here.\n\nMore body text that is long.\n\nReferences\n[1] Someone 2020\n[2] Other";
        let c = clean(raw);
        assert!(c.contains("networks here."));
        assert!(!c.contains("Someone 2020"));
    }

    #[test]
    fn references_in_first_half_are_kept() {
        let raw = "References\n\nActually the body comes after and is much much longer than the heading itself.";
        assert!(clean(raw).contains("Actually the body"));
    }

    #[test]
    fn sections_split_on_numbered_headings() {
        let text = "Preamble text ".repeat(20)
            + "\n\n1 Introduction\n\nIntro body that is long enough to keep around for sure.\n\n2 Method\n\nMethod body that is long enough to keep around for sure.\n\n3 Results\n\nResults body that is long enough to keep around.";
        let secs = into_sections(&clean(&text));
        let titles: Vec<_> = secs.iter().map(|s| s.title.as_str()).collect();
        assert_eq!(titles, vec!["Front matter", "1 Introduction", "2 Method", "3 Results"]);
    }

    #[test]
    fn few_headings_means_single_body() {
        let secs = into_sections(&"Just a body. ".repeat(30));
        assert_eq!(secs.len(), 1);
        assert_eq!(secs[0].title, "Body");
    }
}
