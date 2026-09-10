//! Section extraction from arXiv's LaTeXML HTML (`arxiv.org/html/<id>`).

use scraper::{ElementRef, Html, Node, Selector};

#[derive(Debug, Clone, PartialEq)]
pub struct Section {
    pub title: String,
    pub text: String,
}

/// Classes whose subtree is noise for embeddings: math markup, tables, bibliography, footnotes.
const SKIP_CLASSES: &[&str] = &[
    "ltx_equation",
    "ltx_equationgroup",
    "ltx_tabular",
    "ltx_bibliography",
    "ltx_note",
    "ltx_pagination",
    "ltx_authors",
    "ltx_abstract",
    "ltx_title_document",
];

/// Returns an empty vector when the document is not an arXiv LaTeXML page.
pub fn extract_sections(html: &str) -> Vec<Section> {
    let doc = Html::parse_document(html);
    let article = Selector::parse("article.ltx_document").unwrap();
    let Some(article) = doc.select(&article).next() else {
        return Vec::new();
    };
    let section_sel = Selector::parse("section.ltx_section, section.ltx_appendix").unwrap();
    let title_sel = Selector::parse("h2.ltx_title, h3.ltx_title").unwrap();

    let mut out = Vec::new();
    for sec in article.select(&section_sel) {
        if has_class(&sec, "ltx_bibliography") {
            continue;
        }
        let title = sec
            .select(&title_sel)
            .next()
            .map(|h| normalize(&collect_text(h)))
            .filter(|t| !t.is_empty())
            .unwrap_or_else(|| "Section".to_string());
        let mut paragraphs = Vec::new();
        collect_paragraphs(sec, &mut paragraphs);
        let text = paragraphs.join("\n\n");
        if text.len() >= 40 {
            out.push(Section { title, text });
        }
    }

    if out.is_empty() {
        // Some documents have no <section> markup; fall back to every paragraph in the article.
        let mut paragraphs = Vec::new();
        collect_paragraphs(article, &mut paragraphs);
        let text = paragraphs.join("\n\n");
        if text.len() >= 200 {
            out.push(Section {
                title: "Body".to_string(),
                text,
            });
        }
    }
    out
}

fn has_class(el: &ElementRef, class: &str) -> bool {
    el.value().classes().any(|c| c == class)
}

fn is_skipped(el: &ElementRef) -> bool {
    let name = el.value().name();
    name == "math"
        || name == "table"
        || name == "script"
        || name == "style"
        || SKIP_CLASSES.iter().any(|c| has_class(el, c))
}

/// Paragraph-level text (`p.ltx_p`), plus figure captions, with skipped subtrees removed.
fn collect_paragraphs(root: ElementRef, out: &mut Vec<String>) {
    for child in root.children() {
        let Some(el) = ElementRef::wrap(child) else { continue };
        if is_skipped(&el) {
            continue;
        }
        let is_para = el.value().name() == "p" && has_class(&el, "ltx_p");
        let is_caption = el.value().name() == "figcaption";
        if is_para || is_caption {
            let t = normalize(&collect_text(el));
            if t.len() >= 20 {
                out.push(t);
            }
        } else {
            collect_paragraphs(el, out);
        }
    }
}

fn collect_text(el: ElementRef) -> String {
    let mut s = String::new();
    push_text(el, &mut s);
    s
}

fn push_text(el: ElementRef, s: &mut String) {
    for child in el.children() {
        match child.value() {
            Node::Text(t) => s.push_str(t),
            Node::Element(_) => {
                let child_el = ElementRef::wrap(child).unwrap();
                if !is_skipped(&child_el) {
                    push_text(child_el, s);
                }
            }
            _ => {}
        }
    }
}

pub fn normalize(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    const DOC: &str = r#"<html><body><article class="ltx_document">
      <h1 class="ltx_title ltx_title_document">Big Title</h1>
      <div class="ltx_abstract"><p class="ltx_p">The abstract that we already have.</p></div>
      <section class="ltx_section" id="S1">
        <h2 class="ltx_title ltx_title_section"><span class="ltx_tag">1 </span>Introduction</h2>
        <div class="ltx_para"><p class="ltx_p">Robots   are cool and this paragraph is long enough to count.</p></div>
        <section class="ltx_subsection"><h3 class="ltx_title">1.1 Sub</h3>
          <div class="ltx_para"><p class="ltx_p">Nested paragraph text with <math alttext="x^2"><mi>x</mi></math> inline math removed here.</p></div>
        </section>
        <figure class="ltx_figure"><figcaption class="ltx_caption">Figure 1: A caption that is long enough.</figcaption></figure>
        <table class="ltx_equation"><tr><td>E = mc^2 should be skipped entirely</td></tr></table>
      </section>
      <section class="ltx_bibliography" id="bib"><h2 class="ltx_title">References</h2>
        <p class="ltx_p">Some reference entry that must not appear in output at all.</p></section>
    </article></body></html>"#;

    #[test]
    fn extracts_sections_skips_math_and_bibliography() {
        let secs = extract_sections(DOC);
        assert_eq!(secs.len(), 1);
        assert_eq!(secs[0].title, "1 Introduction");
        assert!(secs[0].text.contains("Robots are cool"));
        assert!(
            secs[0]
                .text
                .contains("Nested paragraph text with inline math removed here.")
        );
        assert!(secs[0].text.contains("Figure 1: A caption"));
        assert!(!secs[0].text.contains("mc^2"));
        assert!(!secs[0].text.contains("reference entry"));
        assert!(!secs[0].text.contains("abstract that we already have"));
    }

    #[test]
    fn non_latexml_page_yields_nothing() {
        assert!(extract_sections("<html><body><p>No HTML for this paper</p></body></html>").is_empty());
    }
}
