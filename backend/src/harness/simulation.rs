//! Offline "simulated" provider: deterministic, plausible-shaped replies so the full pipeline
//! (hiring, graph scheduling, memory, UI) can be exercised with no model installed and no spend.

use serde_json::{json, Value};

fn line_value<'a>(prompt: &'a str, label: &str) -> Option<&'a str> {
    prompt.lines().find_map(|line| line.strip_prefix(label)).map(str::trim)
}

fn pick_skills(prompt: &str, words: &[&str]) -> Vec<String> {
    prompt
        .lines()
        .filter_map(|line| line.strip_prefix("- "))
        .filter_map(|line| line.split_once(' ').map(|(name, rest)| (name.to_string(), rest.to_lowercase())))
        .filter(|(_, rest)| words.iter().any(|word| rest.contains(word)))
        .map(|(name, _)| name)
        .take(2)
        .collect()
}

pub fn structured_reply(schema: &Value, prompt: &str) -> Value {
    let properties = &schema["properties"];
    if properties.get("agents").is_some() {
        let title = line_value(prompt, "Title:").unwrap_or("the question");
        let words: Vec<String> = title.split_whitespace().filter(|word| word.len() > 4).map(str::to_lowercase).collect();
        let word_refs: Vec<&str> = words.iter().map(String::as_str).collect();
        let topical = pick_skills(prompt, &word_refs);
        return json!({
            "decision_to_make": format!("What should the user decide about: {title}?"),
            "intent": {"goal": format!("Reach a defensible decision on {title}"), "audience": "the user", "success_criteria": ["Evidence-backed recommendation", "Quantified options", "Clear next actions"], "assumptions": ["Public sources are sufficient"], "unknowns": ["User budget and timeline"]},
            "rationale": "Simulated plan: intent → three parallel researchers → two analysts → strategist → critic.",
            "agents": [
                {"key": "intent_analyst", "name": "Intent Analyst", "role": "Clarifies the real decision", "kind": "intent", "objective": "Define the decision, criteria and research axes.", "skills": ["intent-discovery"], "depends_on": [], "reports_to": "orchestrator", "max_iterations": 1},
                {"key": "market_researcher", "name": "Market Researcher", "role": "Market and demand evidence", "kind": "researcher", "objective": "Gather market size, growth and demand signals.", "skills": topical.clone(), "depends_on": ["intent_analyst"], "reports_to": "orchestrator"},
                {"key": "landscape_researcher", "name": "Landscape Researcher", "role": "Players and alternatives", "kind": "researcher", "objective": "Map key players, alternatives and recent moves.", "skills": ["research-methods"], "depends_on": ["intent_analyst"], "reports_to": "orchestrator"},
                {"key": "risk_researcher", "name": "Regulation & Risk Researcher", "role": "Rules and constraints", "kind": "researcher", "objective": "Find regulatory, legal and practical constraints.", "skills": ["law-regulation-compliance"], "depends_on": ["intent_analyst"], "reports_to": "orchestrator"},
                {"key": "options_analyst", "name": "Options Analyst", "role": "Comparison matrix", "kind": "analyst", "objective": "Score the options against the criteria.", "skills": ["decision-strategy"], "depends_on": ["market_researcher", "landscape_researcher"], "reports_to": "strategist"},
                {"key": "risk_analyst", "name": "Risk Analyst", "role": "Risk register", "kind": "analyst", "objective": "Build likelihood × impact register with mitigations.", "skills": ["risk-management"], "depends_on": ["risk_researcher", "landscape_researcher"], "reports_to": "strategist"},
                {"key": "strategist", "name": "Chief Strategist", "role": "Decision synthesis", "kind": "strategist", "objective": "Recommend a decision with confidence and next actions.", "skills": ["decision-strategy", "synthesis-report-writing"], "depends_on": ["options_analyst", "risk_analyst"], "reports_to": "orchestrator"},
                {"key": "critic", "name": "Red Team Critic", "role": "Adversarial review", "kind": "critic", "objective": "Challenge the recommendation and evidence.", "skills": ["critic-red-team"], "depends_on": ["strategist"], "reports_to": "orchestrator"}
            ]
        });
    }
    if properties.get("queries").is_some() {
        let objective = line_value(prompt, "Objective:").unwrap_or("research");
        return json!({"queries": [objective.to_string(), format!("{objective} statistics 2026"), format!("{objective} report")]});
    }
    if properties.get("creates").is_some() {
        return json!({"creates": [{"text": "Simulated observation consolidated from this run's findings.", "source_facts": ["F0", "F1"]}], "updates": []});
    }
    json!({})
}

pub fn text_reply(prompt: &str) -> String {
    let name = line_value(prompt, "Name:").unwrap_or("Agent");
    let kind = line_value(prompt, "Kind:").unwrap_or("specialist");
    let objective = line_value(prompt, "Objective:").unwrap_or("the objective");
    let mut report = json!({
        "status": "done",
        "summary": format!("{name} (simulated) completed: {objective}"),
        "key_findings": [
            {"claim": format!("{name} found a simulated data point relevant to: {objective}"), "evidence": "simulated 42%", "source": "https://example.com/simulated", "confidence": 0.5}
        ],
        "numbers": {"simulated metric (%, 2026)": "42"},
        "risks": ["Simulated provider: no real research was performed"],
        "open_questions": [],
        "sources": [{"title": "Simulated source", "url": "https://example.com/simulated"}]
    });
    if kind == "strategist" {
        report["decision"] = json!({
            "recommendation": "Proceed with the highest-scoring option in a small pilot (simulated).",
            "confidence": 0.55,
            "options": [{"name": "Pilot", "score": 7.5, "pros": ["Low cost"], "cons": ["Slower"]}, {"name": "Full launch", "score": 6.0, "pros": ["Speed"], "cons": ["Risk"]}],
            "flip_conditions": ["If costs exceed budget by 30%"],
            "next_actions": [{"action": "Run real research with a live provider", "owner": "user", "when": "now"}]
        });
    }
    if kind == "critic" {
        report["verdict"] = json!("accept");
        report["issues"] = json!([{"severity": "medium", "target_agent": "strategist", "problem": "Simulated evidence only"}]);
    }
    format!("## {name}\n\nSimulated report for: {objective}\n\n- Finding: simulated 42% (example source)\n\n```json\n{}\n```", serde_json::to_string_pretty(&report).unwrap_or_default())
}
