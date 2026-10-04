//! Token compression for everything that is about to enter a prompt.
//!
//! Techniques ported from headroom (headroomlabs-ai/headroom, Apache-2.0) and rtk (rtk-ai/rtk, Apache-2.0):
//! - chars-per-token estimation (headroom `tokenizers/estimator.py`)
//! - dense-line elision for machine-generated blobs (headroom `dense_line_elider.py`: 300/0.06/2000/160/80)
//! - collapse of identical consecutive lines (headroom `lossless_compaction.py::collapse_runs`, rtk counts)
//! - query-relevance extractive selection: BM25 with real IDF (k1=1.2, b=0.75), paragraph segmentation
//!   (≤8 lines / ≤1200 chars), 3-word-shingle near-dup filter at 0.85 (headroom `text_crusher`, `relevance_split`)
//! - `never_worse`: the compressed text is only used when it is actually smaller (rtk `core/guard.rs`)
//! Compression is deterministic (no clocks, stable ordering) so identical inputs produce identical prompt bytes,
//! which keeps provider prompt caches warm (headroom invariant I4).

use std::collections::{BTreeSet, HashMap, HashSet};

use scraper::{ElementRef, Html, Node};

pub fn estimate_tokens(text: &str) -> usize {
    let mut cjk = 0usize;
    let mut other = 0usize;
    for character in text.chars() {
        if is_cjk(character) { cjk += 1 } else { other += 1 }
    }
    let trimmed = text.trim_start();
    let ratio = if (trimmed.starts_with('{') || trimmed.starts_with('[')) && serde_json::from_str::<serde_json::Value>(trimmed).is_ok() { 3.2 } else { 4.0 };
    ((other as f64) / ratio + (cjk as f64) / 1.5).ceil() as usize
}

fn is_cjk(character: char) -> bool {
    matches!(character as u32, 0x3040..=0x30FF | 0x3400..=0x4DBF | 0x4E00..=0x9FFF | 0xAC00..=0xD7AF | 0xF900..=0xFAFF)
}

/// rtk `never_worse`: keep the original when "compression" did not help.
pub fn never_worse(raw: String, compressed: String) -> String {
    if estimate_tokens(&compressed) < estimate_tokens(&raw) { compressed } else { raw }
}

const SKIP_TAGS: &[&str] = &["script", "style", "noscript", "svg", "iframe", "nav", "footer", "header", "aside", "form", "button", "template", "canvas", "select", "option"];
const BLOCK_TAGS: &[&str] = &["p", "div", "section", "article", "main", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table", "ul", "ol", "dd", "dt", "figcaption"];

/// HTML → markdown-ish plain text: main content only, headings marked with `#`, lists with `-`, table cells with `|`.
pub fn html_to_text(html: &str) -> String {
    let document = Html::parse_document(html);
    let root = ["article", "main", "[role=main]", "body"]
        .iter()
        .filter_map(|selector| scraper::Selector::parse(selector).ok())
        .find_map(|selector| document.select(&selector).max_by_key(|element| element.text().map(str::len).sum::<usize>()));
    let mut out = String::new();
    match root {
        Some(element) => walk(element, &mut out),
        None => out.push_str(&document.root_element().text().collect::<Vec<_>>().join(" ")),
    }
    normalize_whitespace(&elide_dense_lines(&out))
}

fn walk(element: ElementRef, out: &mut String) {
    for child in element.children() {
        match child.value() {
            Node::Text(text) => {
                let piece = text.trim();
                if !piece.is_empty() {
                    if !out.ends_with(['\n', ' ']) && !out.is_empty() { out.push(' ') }
                    out.push_str(piece);
                }
            }
            Node::Element(tag) => {
                let name = tag.name();
                if SKIP_TAGS.contains(&name) || tag.attr("aria-hidden") == Some("true") || tag.attr("hidden").is_some() {
                    continue;
                }
                let Some(child_element) = ElementRef::wrap(child) else { continue };
                let block = BLOCK_TAGS.contains(&name);
                if block { out.push('\n') }
                match name {
                    "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => {
                        let level = name[1..].parse::<usize>().unwrap_or(2);
                        out.push_str(&"#".repeat(level));
                        out.push(' ');
                    }
                    "li" => out.push_str("- "),
                    "td" | "th" => out.push_str(" | "),
                    _ => {}
                }
                walk(child_element, out);
                if block { out.push('\n') }
            }
            _ => {}
        }
    }
}

fn normalize_whitespace(text: &str) -> String {
    let mut out = Vec::new();
    let mut blank = false;
    for line in text.lines() {
        let collapsed = line.split_whitespace().collect::<Vec<_>>().join(" ");
        if collapsed.is_empty() {
            if !blank && !out.is_empty() { out.push(String::new()) }
            blank = true;
        } else {
            out.push(collapsed);
            blank = false;
        }
    }
    collapse_runs(&out.join("\n"))
}

/// headroom dense-line elider: long, space-poor lines (base64, minified JS, tracking blobs) keep only head and tail.
pub fn elide_dense_lines(text: &str) -> String {
    let is_dense = |line: &str| {
        let trimmed = line.trim();
        trimmed.len() >= 300
            && !trimmed.contains('\t')
            && (trimmed.matches(' ').count() as f64 / trimmed.len() as f64) < 0.06
            && !(trimmed.starts_with('{') && trimmed.ends_with('}'))
            && !(trimmed.starts_with('[') && trimmed.ends_with(']'))
    };
    let dense_total: usize = text.lines().filter(|line| is_dense(line)).map(str::len).sum();
    if dense_total < 2000 {
        return text.to_string();
    }
    text.lines()
        .map(|line| {
            if !is_dense(line) { return line.to_string() }
            let chars: Vec<char> = line.trim().chars().collect();
            let head: String = chars[..160.min(chars.len())].iter().collect();
            let tail: String = chars[chars.len().saturating_sub(80)..].iter().collect();
            format!("{head} ...[{} chars of dense machine-generated content elided]... {tail}", chars.len().saturating_sub(240))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Identical consecutive lines become one line with a count (headroom collapse_runs / rtk `[×N]`).
pub fn collapse_runs(text: &str) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let mut out = Vec::with_capacity(lines.len());
    let mut index = 0;
    while index < lines.len() {
        let mut end = index;
        while end + 1 < lines.len() && lines[end + 1] == lines[index] && !lines[index].is_empty() {
            end += 1;
        }
        if end > index { out.push(format!("{} [×{}]", lines[index], end - index + 1)) } else { out.push(lines[index].to_string()) }
        index = end + 1;
    }
    out.join("\n")
}

pub fn tokenize(text: &str) -> Vec<String> {
    text.split(|character: char| !character.is_alphanumeric() && character != '_')
        .filter(|word| word.chars().count() > 1)
        .map(str::to_lowercase)
        .collect()
}

struct Segment {
    text: String,
    heading: Option<usize>,
    is_heading: bool,
}

/// headroom `segment()`: blank-line separated blocks, windowed to ≤8 lines / ≤1200 chars; headings tracked.
fn segment(text: &str) -> Vec<Segment> {
    let mut segments: Vec<Segment> = Vec::new();
    let mut current: Vec<&str> = Vec::new();
    let mut current_heading: Option<usize> = None;
    let flush = |current: &mut Vec<&str>, segments: &mut Vec<Segment>, heading: Option<usize>| {
        if !current.is_empty() {
            segments.push(Segment { text: current.join("\n"), heading, is_heading: false });
            current.clear();
        }
    };
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('#') {
            flush(&mut current, &mut segments, current_heading);
            segments.push(Segment { text: trimmed.to_string(), heading: None, is_heading: true });
            current_heading = Some(segments.len() - 1);
            continue;
        }
        if trimmed.is_empty() {
            flush(&mut current, &mut segments, current_heading);
            continue;
        }
        let length: usize = current.iter().map(|line| line.len()).sum();
        if current.len() >= 8 || length + trimmed.len() > 1200 {
            flush(&mut current, &mut segments, current_heading);
        }
        current.push(trimmed);
    }
    flush(&mut current, &mut segments, current_heading);
    segments
}

/// BM25 with real IDF over the segments of one document, max-normalised to [0, 1].
pub fn bm25_scores(documents: &[Vec<String>], query: &[String]) -> Vec<f64> {
    const K1: f64 = 1.2;
    const B: f64 = 0.75;
    let count = documents.len() as f64;
    if documents.is_empty() || query.is_empty() {
        return vec![0.0; documents.len()];
    }
    let average = documents.iter().map(Vec::len).sum::<usize>() as f64 / count;
    let unique_query: BTreeSet<&String> = query.iter().collect();
    let mut document_frequency: HashMap<&str, usize> = HashMap::new();
    for document in documents {
        let terms: HashSet<&str> = document.iter().map(String::as_str).collect();
        for term in &unique_query {
            if terms.contains(term.as_str()) { *document_frequency.entry(term.as_str()).or_default() += 1 }
        }
    }
    let scores: Vec<f64> = documents
        .iter()
        .map(|document| {
            let mut frequency: HashMap<&str, usize> = HashMap::new();
            for term in document { *frequency.entry(term.as_str()).or_default() += 1 }
            unique_query.iter().map(|term| {
                let tf = *frequency.get(term.as_str()).unwrap_or(&0) as f64;
                if tf == 0.0 { return 0.0 }
                let df = *document_frequency.get(term.as_str()).unwrap_or(&0) as f64;
                let idf = ((count - df + 0.5) / (df + 0.5) + 1.0).ln();
                idf * tf * (K1 + 1.0) / (tf + K1 * (1.0 - B + B * document.len() as f64 / average.max(1.0)))
            }).sum()
        })
        .collect();
    let max = scores.iter().cloned().fold(0.0, f64::max);
    if max > 0.0 { scores.iter().map(|score| score / max).collect() } else { scores }
}

fn salience(text: &str) -> f64 {
    let words: Vec<&str> = text.split_whitespace().collect();
    if words.is_empty() { return 0.0 }
    let salient = words.iter().filter(|word| word.chars().any(|c| c.is_ascii_digit()) || (word.len() > 1 && word.chars().all(|c| c.is_uppercase())) || word.contains('%') || word.contains('$')).count();
    salient as f64 / (words.len() as f64 + 1.0)
}

fn shingles(text: &str) -> HashSet<String> {
    let words = tokenize(text);
    words.windows(3).map(|window| window.join(" ")).collect()
}

/// Query-aware extractive compression of a page or report down to `budget_tokens`.
/// Scores each segment by 2·relevance + 0.5·lead position + 1·salience, keeps the best in original order,
/// drops near-duplicates, and marks gaps so the model knows content was omitted.
pub fn compress_text(text: &str, query: &str, budget_tokens: usize) -> String {
    if estimate_tokens(text) <= budget_tokens {
        return text.to_string();
    }
    let segments = segment(text);
    if segments.is_empty() {
        return text.to_string();
    }
    let tokens: Vec<Vec<String>> = segments.iter().map(|segment| tokenize(&segment.text)).collect();
    let relevance = bm25_scores(&tokens, &tokenize(query));
    let total = segments.len() as f64;
    let scores: Vec<f64> = segments
        .iter()
        .enumerate()
        .map(|(index, segment)| {
            let mut score = 2.0 * relevance[index] + 0.5 * (1.0 - index as f64 / total) + salience(&segment.text);
            if segment.text.len() < 12 { score *= 0.25 }
            if segment.is_heading { score *= 0.5 }
            score
        })
        .collect();
    let mut order: Vec<usize> = (0..segments.len()).collect();
    order.sort_by(|&a, &b| scores[b].partial_cmp(&scores[a]).unwrap_or(std::cmp::Ordering::Equal).then(a.cmp(&b)));

    let mut keep = BTreeSet::new();
    let mut used = 0usize;
    let mut covered: HashSet<String> = HashSet::new();
    for index in order {
        let segment = &segments[index];
        let own = shingles(&segment.text);
        if !own.is_empty() && own.iter().filter(|shingle| covered.contains(*shingle)).count() as f64 / own.len() as f64 >= 0.85 {
            continue;
        }
        let heading_cost = segment.heading.filter(|heading| !keep.contains(heading)).map(|heading| estimate_tokens(&segments[heading].text)).unwrap_or(0);
        let cost = estimate_tokens(&segment.text) + heading_cost;
        if used + cost > budget_tokens {
            continue;
        }
        keep.insert(index);
        if let Some(heading) = segment.heading { keep.insert(heading); }
        covered.extend(own);
        used += cost;
    }

    let mut out = Vec::new();
    let mut last: Option<usize> = None;
    for index in &keep {
        if let Some(previous) = last {
            let gap = index - previous - 1;
            if gap > 0 { out.push(format!("[… {gap} sections omitted]")) }
        } else if *index > 0 {
            out.push(format!("[… {index} sections omitted]"));
        }
        out.push(segments[*index].text.clone());
        last = Some(*index);
    }
    never_worse(text.to_string(), out.join("\n\n"))
}

/// Hard cap on characters at a char boundary, with an explicit marker.
pub fn truncate_chars(text: &str, max_chars: usize) -> String {
    if text.chars().count() <= max_chars {
        return text.to_string();
    }
    let cut: String = text.chars().take(max_chars).collect();
    format!("{cut}\n[… truncated {} chars]", text.chars().count() - max_chars)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn html_keeps_main_text_and_drops_chrome() {
        let html = "<html><body><nav>Menu Home About</nav><article><h2>Prices</h2><p>Gold rose 3% in 2025.</p><script>var x=1</script></article><footer>© site</footer></body></html>";
        let text = html_to_text(html);
        assert!(text.contains("## Prices"));
        assert!(text.contains("Gold rose 3% in 2025."));
        assert!(!text.contains("Menu"));
        assert!(!text.contains("var x"));
    }

    #[test]
    fn compress_prefers_relevant_segments() {
        let filler = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor.\n\n".repeat(40);
        let text = format!("{filler}Lithium battery prices fell 20% in 2024 according to BNEF.\n\n{filler}");
        let compressed = compress_text(&text, "lithium battery prices", 120);
        assert!(compressed.contains("Lithium battery prices fell 20%"));
        assert!(estimate_tokens(&compressed) < estimate_tokens(&text));
    }

    #[test]
    fn collapses_repeated_lines() {
        assert_eq!(collapse_runs("a\na\na\nb"), "a [×3]\nb");
    }
}
