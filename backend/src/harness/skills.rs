//! SKILL.md library.
//!
//! Same layout as Claude Code / Paperclip skills: `skills/<slug>/SKILL.md` with YAML-ish frontmatter
//! (`name`, `description`, `domain`, `tags`) and a markdown body. Progressive disclosure, as in Paperclip:
//! the orchestrator only ever sees the catalog (name + description, one line each); an agent's prompt gets the
//! full body of just the skills it was hired with. Skills can be added at runtime through the API.

use std::{collections::BTreeMap, path::{Path, PathBuf}};

use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Skill {
    pub name: String,
    pub description: String,
    pub domain: String,
    pub tags: Vec<String>,
    #[serde(skip_serializing_if = "String::is_empty", default)]
    pub body: String,
    pub tokens: usize,
}

pub struct SkillLibrary {
    root: PathBuf,
    skills: RwLock<BTreeMap<String, Skill>>,
}

impl SkillLibrary {
    pub async fn load(root: PathBuf) -> Self {
        let library = Self { root, skills: RwLock::new(BTreeMap::new()) };
        library.reload().await;
        library
    }

    pub async fn reload(&self) -> usize {
        let mut loaded = BTreeMap::new();
        if let Ok(mut entries) = tokio::fs::read_dir(&self.root).await {
            while let Ok(Some(entry)) = entries.next_entry().await {
                let path = entry.path().join("SKILL.md");
                if let Ok(text) = tokio::fs::read_to_string(&path).await {
                    if let Some(skill) = parse_skill(&text, &entry.file_name().to_string_lossy()) {
                        loaded.insert(skill.name.clone(), skill);
                    }
                }
            }
        }
        let count = loaded.len();
        *self.skills.write().await = loaded;
        count
    }

    pub async fn list(&self) -> Vec<Skill> {
        self.skills.read().await.values().map(|skill| Skill { body: String::new(), ..skill.clone() }).collect()
    }

    pub async fn get(&self, name: &str) -> Option<Skill> {
        self.skills.read().await.get(name).cloned()
    }

    /// One line per skill, sorted by name so the orchestrator prompt is byte-stable (prompt-cache friendly).
    pub async fn catalog(&self) -> String {
        self.skills
            .read()
            .await
            .values()
            .map(|skill| format!("- {} [{}]: {}", skill.name, skill.domain, skill.description))
            .collect::<Vec<_>>()
            .join("\n")
    }

    /// Full bodies for the skills an agent was hired with, in a fixed order, unknown names ignored.
    pub async fn render(&self, names: &[String]) -> (String, Vec<String>) {
        let skills = self.skills.read().await;
        let mut used = Vec::new();
        let mut out = Vec::new();
        let mut sorted: Vec<&String> = names.iter().collect();
        sorted.sort();
        sorted.dedup();
        for name in sorted {
            if let Some(skill) = skills.get(name.as_str()) {
                out.push(format!("<skill name=\"{}\">\n{}\n</skill>", skill.name, skill.body.trim()));
                used.push(skill.name.clone());
            }
        }
        (out.join("\n\n"), used)
    }

    pub async fn known(&self, name: &str) -> bool {
        self.skills.read().await.contains_key(name)
    }

    /// Writes (or replaces) a skill on disk and reloads the library.
    pub async fn save(&self, name: &str, markdown: &str) -> Result<Skill, String> {
        let slug = slugify(name);
        if slug.is_empty() {
            return Err("skill name must contain letters or digits".into());
        }
        let skill = parse_skill(markdown, &slug).ok_or("SKILL.md needs frontmatter with at least `description`")?;
        let dir = self.root.join(&slug);
        tokio::fs::create_dir_all(&dir).await.map_err(|error| error.to_string())?;
        tokio::fs::write(dir.join("SKILL.md"), markdown).await.map_err(|error| error.to_string())?;
        self.reload().await;
        Ok(skill)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }
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
            fields.insert(key.trim().to_string(), if value == ">" || value == "|" { String::new() } else { value.to_string() });
        } else if let Some(key) = &current {
            let entry = fields.entry(key.clone()).or_default();
            if !entry.is_empty() { entry.push(' ') }
            entry.push_str(line.trim());
        }
    }
    let description = fields.get("description").map(|value| value.trim_matches('"').to_string()).filter(|value| !value.is_empty())?;
    let tags = fields
        .get("tags")
        .map(|value| value.trim_matches(['[', ']']).split(',').map(|tag| tag.trim().trim_matches('"').to_string()).filter(|tag| !tag.is_empty()).collect())
        .unwrap_or_default();
    Some(Skill {
        name: fields.get("name").map(|value| slugify(value)).filter(|value| !value.is_empty()).unwrap_or_else(|| slugify(fallback_name)),
        description,
        domain: fields.get("domain").cloned().unwrap_or_else(|| "general".into()),
        tags,
        tokens: super::compress::estimate_tokens(&body),
        body,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_frontmatter_and_body() {
        let skill = parse_skill("---\nname: color-theory\ndescription: >\n  Use when choosing\n  palettes.\ndomain: creative\ntags: [color, wcag]\n---\n# Color\nBody", "x").unwrap();
        assert_eq!(skill.name, "color-theory");
        assert_eq!(skill.description, "Use when choosing palettes.");
        assert_eq!(skill.tags, vec!["color", "wcag"]);
        assert!(skill.body.starts_with("# Color"));
    }
}
