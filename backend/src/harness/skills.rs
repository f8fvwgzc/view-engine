//! SKILL.md library — user data, not shipped with the engine.
//!
//! Layout (same as Claude Code / Paperclip skills): `<skills dir>/<slug>/SKILL.md` with frontmatter
//! (`name`, `description`, optional `domain`, `tags`) and a markdown body. Users add their own skills, edit them
//! in the UI, or import SKILL.md packs from open-source GitHub repositories.
//!
//! Selection must stay fast and cheap however large the library grows, so no model ever reads the whole library:
//! - an in-memory index (inverted index for BM25 + hashed-feature vectors for similarity, fused by reciprocal rank)
//!   answers "which skills fit this text?" in well under a millisecond per query for thousands of skills;
//! - the orchestrator only sees a shortlist retrieved for the task (progressive disclosure, level 1);
//! - each hired agent gets its skills resolved against the index from its role and objective, and only those
//!   bodies are injected into its prompt (level 2);
//! - per-skill outcome statistics (see `skill_stats`) nudge the ranking toward skills that have actually helped.

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::RwLock;

use super::{compress::{estimate_tokens, tokenize}, memory::hash_embed};

const BM25_K1: f64 = 1.2;
const BM25_B: f64 = 0.75;
const RRF_K: f64 = 60.0;
const MAX_SKILL_BYTES: usize = 64 * 1024;
const MAX_IMPORT: usize = 300;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Skill {
    pub name: String,
    pub description: String,
    pub domain: String,
    pub tags: Vec<String>,
    #[serde(skip_serializing_if = "String::is_empty", default)]
    pub body: String,
    pub tokens: usize,
    /// Where the skill came from: "local" or e.g. "github:owner/repo/path".
    #[serde(default)]
    pub source: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SkillMatch {
    pub name: String,
    pub description: String,
    pub domain: String,
    pub score: f64,
}

/// Search structure over skill names, descriptions and tags.
#[derive(Default)]
struct Index {
    names: Vec<String>,
    /// term → postings (skill position, term frequency)
    postings: HashMap<String, Vec<(usize, f64)>>,
    lengths: Vec<f64>,
    average_length: f64,
    vectors: Vec<Vec<f32>>,
}

impl Index {
    fn build(skills: &BTreeMap<String, Skill>) -> Self {
        let mut index = Index::default();
        for (position, skill) in skills.values().enumerate() {
            // Name and tags are repeated so they weigh more than description words.
            let text = format!("{0} {0} {1} {1} {2} {3}", skill.name.replace('-', " "), skill.tags.join(" "), skill.domain, skill.description);
            let terms = tokenize(&text);
            let mut frequency: HashMap<&str, f64> = HashMap::new();
            for term in &terms {
                *frequency.entry(term.as_str()).or_default() += 1.0;
            }
            for (term, count) in frequency {
                index.postings.entry(term.to_string()).or_default().push((position, count));
            }
            index.lengths.push(terms.len() as f64);
            index.vectors.push(hash_embed(&text));
            index.names.push(skill.name.clone());
        }
        index.average_length = if index.lengths.is_empty() { 0.0 } else { index.lengths.iter().sum::<f64>() / index.lengths.len() as f64 };
        index
    }

    /// Ranked skill positions: BM25 over the inverted index (touches only skills sharing a query term) and
    /// cosine similarity over the vectors, fused by reciprocal rank.
    fn search(&self, query: &str, limit: usize) -> Vec<(usize, f64)> {
        let count = self.names.len();
        if count == 0 || query.trim().is_empty() {
            return vec![];
        }
        let terms: HashSet<String> = tokenize(query).into_iter().collect();
        let mut lexical: HashMap<usize, f64> = HashMap::new();
        for term in &terms {
            let Some(postings) = self.postings.get(term) else { continue };
            let document_frequency = postings.len() as f64;
            let idf = ((count as f64 - document_frequency + 0.5) / (document_frequency + 0.5) + 1.0).ln();
            for (position, frequency) in postings {
                let norm = 1.0 - BM25_B + BM25_B * self.lengths[*position] / self.average_length.max(1.0);
                *lexical.entry(*position).or_default() += idf * frequency * (BM25_K1 + 1.0) / (frequency + BM25_K1 * norm);
            }
        }
        let mut lexical_ranked: Vec<(usize, f64)> = lexical.into_iter().collect();
        lexical_ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal).then(a.0.cmp(&b.0)));

        let query_vector = hash_embed(query);
        let mut semantic_ranked: Vec<(usize, f64)> = self
            .vectors
            .iter()
            .enumerate()
            .map(|(position, vector)| (position, vector.iter().zip(&query_vector).map(|(a, b)| (a * b) as f64).sum::<f64>()))
            .filter(|(_, similarity)| *similarity > 0.05)
            .collect();
        semantic_ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal).then(a.0.cmp(&b.0)));
        semantic_ranked.truncate(limit.max(20) * 3);

        let mut fused: HashMap<usize, f64> = HashMap::new();
        for ranked in [&lexical_ranked, &semantic_ranked] {
            for (rank, (position, _)) in ranked.iter().enumerate() {
                *fused.entry(*position).or_default() += 1.0 / (RRF_K + rank as f64 + 1.0);
            }
        }
        let mut out: Vec<(usize, f64)> = fused.into_iter().collect();
        out.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal).then(a.0.cmp(&b.0)));
        out.truncate(limit);
        out
    }
}

struct Library {
    skills: BTreeMap<String, Skill>,
    index: Index,
    /// Outcome prior per skill in [0.9, 1.1], from `skill_stats`.
    prior: HashMap<String, f64>,
}

pub struct SkillLibrary {
    root: PathBuf,
    library: RwLock<Library>,
}

impl SkillLibrary {
    pub async fn load(root: PathBuf) -> Self {
        let _ = tokio::fs::create_dir_all(&root).await;
        let library = Self { root, library: RwLock::new(Library { skills: BTreeMap::new(), index: Index::default(), prior: HashMap::new() }) };
        library.reload().await;
        library
    }

    pub async fn reload(&self) -> usize {
        let mut loaded = BTreeMap::new();
        if let Ok(mut entries) = tokio::fs::read_dir(&self.root).await {
            while let Ok(Some(entry)) = entries.next_entry().await {
                let directory = entry.path();
                let Ok(text) = tokio::fs::read_to_string(directory.join("SKILL.md")).await else { continue };
                if let Some(mut skill) = parse_skill(&text, &entry.file_name().to_string_lossy()) {
                    skill.source = tokio::fs::read_to_string(directory.join(".source")).await.map(|source| source.trim().to_string()).unwrap_or_else(|_| "local".into());
                    loaded.insert(skill.name.clone(), skill);
                }
            }
        }
        let count = loaded.len();
        let index = Index::build(&loaded);
        let mut library = self.library.write().await;
        library.skills = loaded;
        library.index = index;
        count
    }

    pub async fn set_priors(&self, prior: HashMap<String, f64>) {
        self.library.write().await.prior = prior;
    }

    pub async fn len(&self) -> usize {
        self.library.read().await.skills.len()
    }

    pub async fn list(&self) -> Vec<Skill> {
        self.library.read().await.skills.values().map(|skill| Skill { body: String::new(), ..skill.clone() }).collect()
    }

    pub async fn get(&self, name: &str) -> Option<Skill> {
        self.library.read().await.skills.get(name).cloned()
    }

    pub async fn known(&self, name: &str) -> bool {
        self.library.read().await.skills.contains_key(name)
    }

    /// Best-matching skills for free text, with the outcome prior applied.
    pub async fn search(&self, query: &str, limit: usize) -> Vec<SkillMatch> {
        let library = self.library.read().await;
        let mut matches: Vec<SkillMatch> = library
            .index
            .search(query, limit * 2)
            .into_iter()
            .filter_map(|(position, score)| {
                let skill = library.skills.get(&library.index.names[position])?;
                let prior = library.prior.get(&skill.name).copied().unwrap_or(1.0);
                Some(SkillMatch { name: skill.name.clone(), description: skill.description.clone(), domain: skill.domain.clone(), score: score * prior })
            })
            .collect();
        matches.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal).then(a.name.cmp(&b.name)));
        matches.truncate(limit);
        matches
    }

    /// Shortlist for the orchestrator's hiring prompt: one line per skill, sorted by name so the text is stable.
    /// Small libraries are shown whole; large ones are narrowed to the best matches for the task.
    pub async fn catalog_for(&self, task: &str, limit: usize) -> String {
        let total = self.len().await;
        if total == 0 {
            return "(the skill library is empty — hire agents without skills)".into();
        }
        let mut lines: Vec<String> = if total <= limit {
            self.library.read().await.skills.values().map(|skill| format!("- {} [{}]: {}", skill.name, skill.domain, skill.description)).collect()
        } else {
            self.search(task, limit).await.into_iter().map(|skill| format!("- {} [{}]: {}", skill.name, skill.domain, skill.description)).collect()
        };
        lines.sort();
        if total > limit {
            lines.push(format!("({} of {total} skills shown: the best matches for this task. Leave `skills` empty for an agent and the engine will match skills to its role automatically.)", lines.len()));
        }
        lines.join("\n")
    }

    /// Skills for one agent: the orchestrator's valid picks first, then index matches for the agent's own role
    /// and objective until `limit` — a deterministic lookup, no model call.
    pub async fn resolve(&self, picked: &[String], role: &str, objective: &str, limit: usize) -> Vec<String> {
        let mut resolved: Vec<String> = Vec::new();
        for name in picked {
            let slug = slugify(name);
            if self.known(&slug).await && !resolved.contains(&slug) && resolved.len() < limit {
                resolved.push(slug);
            }
        }
        if resolved.len() < limit {
            let matches = self.search(&format!("{role} {objective}"), limit + 2).await;
            let best = matches.first().map(|skill| skill.score).unwrap_or(0.0);
            for candidate in matches {
                // Only clearly relevant matches: a single-list hit scores ~1/61; require agreement or a top result.
                if resolved.len() >= limit || candidate.score < best * 0.6 || candidate.score < 0.02 {
                    break;
                }
                if !resolved.contains(&candidate.name) {
                    resolved.push(candidate.name);
                }
            }
        }
        resolved
    }

    /// Full bodies for the skills an agent was hired with, in a fixed order, unknown names ignored.
    pub async fn render(&self, names: &[String]) -> (String, Vec<String>) {
        let library = self.library.read().await;
        let mut used = Vec::new();
        let mut out = Vec::new();
        let mut sorted: Vec<&String> = names.iter().collect();
        sorted.sort();
        sorted.dedup();
        for name in sorted {
            if let Some(skill) = library.skills.get(name.as_str()) {
                out.push(format!("<skill name=\"{}\">\n{}\n</skill>", skill.name, skill.body.trim()));
                used.push(skill.name.clone());
            }
        }
        (out.join("\n\n"), used)
    }

    /// Writes (or replaces) a skill on disk and rebuilds the index.
    pub async fn save(&self, name: &str, markdown: &str, source: &str) -> Result<Skill, String> {
        let slug = slugify(name);
        if slug.is_empty() {
            return Err("skill name must contain letters or digits".into());
        }
        if markdown.len() > MAX_SKILL_BYTES {
            return Err(format!("SKILL.md is larger than {} KB", MAX_SKILL_BYTES / 1024));
        }
        let mut skill = parse_skill(markdown, &slug).ok_or("SKILL.md needs frontmatter with at least `description`")?;
        skill.source = source.to_string();
        let directory = self.root.join(&slug);
        tokio::fs::create_dir_all(&directory).await.map_err(|error| error.to_string())?;
        tokio::fs::write(directory.join("SKILL.md"), markdown).await.map_err(|error| error.to_string())?;
        if source != "local" {
            tokio::fs::write(directory.join(".source"), source).await.map_err(|error| error.to_string())?;
        }
        self.reload().await;
        Ok(skill)
    }

    pub async fn delete(&self, name: &str) -> Result<(), String> {
        let slug = slugify(name);
        let directory = self.root.join(&slug);
        if slug.is_empty() || !directory.starts_with(&self.root) || !directory.join("SKILL.md").is_file() {
            return Err("skill not found".into());
        }
        tokio::fs::remove_dir_all(&directory).await.map_err(|error| error.to_string())?;
        self.reload().await;
        Ok(())
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Imports every SKILL.md found in a public GitHub repository (optionally under a sub-path).
    /// `source` forms: `owner/repo`, `owner/repo/sub/path`, or a github.com URL (with or without `/tree/<ref>/path`).
    /// Only the SKILL.md text is imported; bundled scripts are not executed or copied.
    pub async fn import_github(&self, http: &reqwest::Client, source: &str, overwrite: bool) -> Result<Value, String> {
        let (owner, repo, reference, prefix) = parse_github_source(source)?;
        let api = |path: &str| http.get(format!("https://api.github.com/repos/{owner}/{repo}{path}")).header("Accept", "application/vnd.github+json").header("User-Agent", "view-engine");
        let reference = match reference {
            Some(reference) => reference,
            None => {
                let repository: Value = api("").send().await.map_err(|error| error.to_string())?.json().await.map_err(|error| error.to_string())?;
                repository["default_branch"].as_str().ok_or_else(|| format!("repository {owner}/{repo} not found or GitHub rate limit reached: {}", repository["message"].as_str().unwrap_or("")))?.to_string()
            }
        };
        let tree: Value = api(&format!("/git/trees/{reference}?recursive=1")).send().await.map_err(|error| error.to_string())?.json().await.map_err(|error| error.to_string())?;
        let paths: Vec<String> = tree["tree"]
            .as_array()
            .ok_or_else(|| format!("could not list {owner}/{repo}@{reference}: {}", tree["message"].as_str().unwrap_or("no tree")))?
            .iter()
            .filter_map(|entry| entry["path"].as_str())
            .filter(|path| path.ends_with("SKILL.md") && (prefix.is_empty() || path.starts_with(&format!("{prefix}/")) || *path == format!("{prefix}/SKILL.md")))
            .take(MAX_IMPORT)
            .map(String::from)
            .collect();
        let (mut imported, mut skipped, mut failed) = (Vec::new(), Vec::new(), Vec::new());
        for path in &paths {
            let raw = format!("https://raw.githubusercontent.com/{owner}/{repo}/{reference}/{path}");
            let text = match http.get(&raw).send().await {
                Ok(response) if response.status().is_success() => response.text().await.unwrap_or_default(),
                _ => {
                    failed.push(json!({"path": path, "reason": "download failed"}));
                    continue;
                }
            };
            let folder = Path::new(path).parent().and_then(|parent| parent.file_name()).map(|name| name.to_string_lossy().to_string()).unwrap_or_else(|| repo.clone());
            let Some(parsed) = parse_skill(&text, &folder) else {
                failed.push(json!({"path": path, "reason": "no frontmatter description"}));
                continue;
            };
            if self.known(&parsed.name).await && !overwrite {
                skipped.push(parsed.name);
                continue;
            }
            match self.save_without_reload(&parsed.name, &text, &format!("github:{owner}/{repo}/{}", path.trim_end_matches("/SKILL.md"))).await {
                Ok(()) => imported.push(parsed.name),
                Err(reason) => failed.push(json!({"path": path, "reason": reason})),
            }
        }
        self.reload().await;
        Ok(json!({"source": format!("{owner}/{repo}@{reference}"), "found": paths.len(), "imported": imported, "skipped_existing": skipped, "failed": failed}))
    }

    async fn save_without_reload(&self, name: &str, markdown: &str, source: &str) -> Result<(), String> {
        if markdown.len() > MAX_SKILL_BYTES {
            return Err(format!("larger than {} KB", MAX_SKILL_BYTES / 1024));
        }
        let directory = self.root.join(slugify(name));
        tokio::fs::create_dir_all(&directory).await.map_err(|error| error.to_string())?;
        tokio::fs::write(directory.join("SKILL.md"), markdown).await.map_err(|error| error.to_string())?;
        tokio::fs::write(directory.join(".source"), source).await.map_err(|error| error.to_string())
    }
}

fn parse_github_source(source: &str) -> Result<(String, String, Option<String>, String), String> {
    let trimmed = source.trim().trim_start_matches("github:").trim_start_matches("https://github.com/").trim_start_matches("http://github.com/").trim_end_matches('/').trim_end_matches(".git");
    let parts: Vec<&str> = trimmed.split('/').filter(|part| !part.is_empty()).collect();
    if parts.len() < 2 {
        return Err("expected owner/repo, owner/repo/path or a github.com URL".into());
    }
    let valid = |part: &str| part.chars().all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.'));
    if !valid(parts[0]) || !valid(parts[1]) {
        return Err("invalid owner or repository name".into());
    }
    let (reference, rest) = match parts.get(2) {
        Some(&"tree") | Some(&"blob") if parts.len() >= 4 => (Some(parts[3].to_string()), &parts[4..]),
        _ => (None, &parts[2..]),
    };
    let prefix = rest.join("/").trim_end_matches("/SKILL.md").trim_end_matches("SKILL.md").trim_end_matches('/').to_string();
    Ok((parts[0].to_string(), parts[1].to_string(), reference, prefix))
}

pub fn slugify(value: &str) -> String {
    let mut slug = String::new();
    for character in value.trim().to_lowercase().chars() {
        if character.is_ascii_alphanumeric() {
            slug.push(character);
        } else if !slug.ends_with('-') && !slug.is_empty() {
            slug.push('-');
        }
    }
    slug.trim_end_matches('-').to_string()
}

/// Minimal frontmatter parser: `key: value`, `tags: [a, b]` and folded `description: >` blocks.
pub fn parse_skill(text: &str, fallback_name: &str) -> Option<Skill> {
    let rest = text.trim_start().strip_prefix("---")?;
    let end = rest.find("\n---")?;
    let header = &rest[..end];
    let body = rest[end + 4..].trim_start_matches(['-', '\n', '\r']).to_string();
    let mut fields: BTreeMap<String, String> = BTreeMap::new();
    let mut current: Option<String> = None;
    for line in header.lines() {
        if let Some((key, value)) = line.split_once(':').filter(|(key, _)| !key.starts_with(' ') && !key.is_empty()) {
            let value = value.trim();
            current = Some(key.trim().to_string());
            fields.insert(key.trim().to_string(), if value == ">" || value == "|" || value == ">-" || value == "|-" { String::new() } else { value.to_string() });
        } else if let Some(key) = &current {
            let entry = fields.entry(key.clone()).or_default();
            if !entry.is_empty() { entry.push(' ') }
            entry.push_str(line.trim());
        }
    }
    let description = fields.get("description").map(|value| value.trim_matches('"').trim_matches('\'').to_string()).filter(|value| !value.is_empty())?;
    let tags = fields
        .get("tags")
        .map(|value| value.trim_matches(['[', ']']).split(',').map(|tag| tag.trim().trim_matches('"').to_string()).filter(|tag| !tag.is_empty()).collect())
        .unwrap_or_default();
    Some(Skill {
        name: fields.get("name").map(|value| slugify(value.trim_matches('"'))).filter(|value| !value.is_empty()).unwrap_or_else(|| slugify(fallback_name)),
        description,
        domain: fields.get("domain").cloned().unwrap_or_else(|| "general".into()),
        tags,
        tokens: estimate_tokens(&body),
        body,
        source: String::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn skill(name: &str, description: &str, tags: &[&str]) -> (String, Skill) {
        (name.to_string(), Skill { name: name.into(), description: description.into(), domain: "test".into(), tags: tags.iter().map(|tag| tag.to_string()).collect(), body: String::new(), tokens: 0, source: "local".into() })
    }

    #[test]
    fn parses_frontmatter_and_body() {
        let parsed = parse_skill("---\nname: color-theory\ndescription: >\n  Use when choosing\n  palettes.\ndomain: creative\ntags: [color, wcag]\n---\n# Color\nBody", "x").unwrap();
        assert_eq!(parsed.name, "color-theory");
        assert_eq!(parsed.description, "Use when choosing palettes.");
        assert_eq!(parsed.tags, vec!["color", "wcag"]);
        assert!(parsed.body.starts_with("# Color"));
    }

    #[test]
    fn index_ranks_the_relevant_skill_first() {
        let library: BTreeMap<String, Skill> = [
            skill("forex", "Use when analysing currency pairs, carry and central bank policy.", &["fx", "currency"]),
            skill("color-theory", "Use when choosing palettes, contrast and color harmony.", &["color", "design"]),
            skill("options-positioning", "Use when reading open interest walls, gamma and max pain.", &["options", "gamma"]),
        ]
        .into_iter()
        .collect();
        let index = Index::build(&library);
        let top = index.search("dealer gamma and open interest walls on SPY options", 3);
        assert_eq!(index.names[top[0].0], "options-positioning");
        let palette = index.search("pick a brand colour palette with good contrast", 3);
        assert_eq!(index.names[palette[0].0], "color-theory");
        assert!(index.search("", 3).is_empty());
    }

    #[test]
    fn parses_github_sources() {
        assert_eq!(parse_github_source("anthropics/skills").unwrap(), ("anthropics".into(), "skills".into(), None, String::new()));
        assert_eq!(parse_github_source("https://github.com/anthropics/skills/tree/main/skills/pdf").unwrap(), ("anthropics".into(), "skills".into(), Some("main".into()), "skills/pdf".into()));
        assert!(parse_github_source("nope").is_err());
    }
}
