# Your skills

This folder is **your** skill library. View Engine ships with none; everything here is local to your machine
(git-ignored except this file).

A skill is a folder with a `SKILL.md`:

```
skills/my-skill/SKILL.md
```

```markdown
---
name: my-skill
description: Use when … (one sentence — this is what the orchestrator and the skill index match against)
domain: research
tags: [keyword, keyword]
---
# Title
…expert knowledge, method, checklist, output contract…
```

Ways to add skills:
- **UI** → Settings → Skills → *New skill*, or edit an existing one.
- **Import from GitHub** → Settings → Skills → *Import*, e.g. `anthropics/skills` or
  `https://github.com/owner/repo/tree/main/some/path`. Only `SKILL.md` text is imported (no scripts are run).
- Drop folders in here and press *Reload*.

How skills are chosen: an index over name, description and tags shortlists the best matches for a task; the
orchestrator attaches skills when it hires an agent, and agents hired without skills get index matches for their
role and objective. Only the attached skills' bodies enter that agent's prompt. Outcome statistics (critic
verdicts, scored predictions) nudge ranking toward skills that have helped.
