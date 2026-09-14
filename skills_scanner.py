#!/usr/bin/env python3
"""
Skill scanner for 大眼X — YAML frontmatter + markdown.
Backward compatible with old plain-markdown format.

Format:
  ---
  name: skill-name
  description: One-line description
  tags: [tag1, tag2]
  triggers: [keyword1, keyword2]
  status: trial   # 试用期技能；实测验证有效后改为 active（缺省即 active）
  ---

  # skill-name

  Full instructions. Free-form markdown.

Skills live in skills/ directory. AI creates them with write_file or create_skill,
reads them with read_file. Scanner builds an index for the system prompt.
In frozen mode, user-created skills go to exe_dir/skills/ (writable).
"""
import os
import re
import sys

# In frozen mode, user skills go to exe directory (writable)
if getattr(sys, 'frozen', False):
    _EXE_DIR = os.path.dirname(sys.executable)
    SKILLS_DIR = os.path.join(_EXE_DIR, "skills")
else:
    SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

TEMPLATES_DIR = os.path.join(SKILLS_DIR, "_templates")

# ── YAML-like frontmatter parser (no pyyaml dependency) ──


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse --- delimited frontmatter. Returns (meta_dict, body_text)."""
    meta = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1].strip()
            body = parts[2].strip()
            for line in fm_text.split("\n"):
                line = line.strip()
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if val.startswith("[") and val.endswith("]"):
                        val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
                    meta[key] = val
    return meta, body


def scan_skills():
    """Scan skills/ directory, return list of {name, description, path, tags, triggers}."""
    skills = []
    if not os.path.isdir(SKILLS_DIR):
        return skills
    for fn in sorted(os.listdir(SKILLS_DIR)):
        if not fn.endswith(".md"):
            continue
        # Skip template files
        if fn.startswith("_"):
            continue
        filepath = os.path.join(SKILLS_DIR, fn)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read(2000)
        except Exception:
            continue

        meta, body = _parse_frontmatter(content)

        name = meta.get("name")
        desc = meta.get("description", "")
        tags = meta.get("tags", [])
        triggers = meta.get("triggers", [])

        if not name:
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("# "):
                    name = line[2:].strip()
                    break
        if not name:
            name = fn.replace(".md", "")

        status = meta.get("status", "active")
        skills.append({
            "name": name,
            "description": desc or name,
            "path": filepath,
            "tags": tags if isinstance(tags, list) else [tags],
            "triggers": triggers if isinstance(triggers, list) else [triggers],
            "status": status if isinstance(status, str) and status else "active",
        })
    return skills


def build_skill_index():
    """Build a compact skill index string for the system prompt."""
    skills = scan_skills()
    if not skills:
        return ""
    lines = []
    for s in skills:
        tags_str = f" [{', '.join(s['tags'])}]" if s.get("tags") else ""
        lines.append(f"skills/{s['name']}.md — {s['description']}{tags_str}")
    return "\n".join(lines)


def list_templates():
    """List available skill templates."""
    if not os.path.isdir(TEMPLATES_DIR):
        return []
    templates = []
    for fn in sorted(os.listdir(TEMPLATES_DIR)):
        if fn.endswith(".md"):
            name = fn.replace(".md", "")
            fp = os.path.join(TEMPLATES_DIR, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    content = f.read(1000)
                meta, _ = _parse_frontmatter(content)
                desc = meta.get("description", name)
            except Exception:
                desc = name
            templates.append({"name": name, "description": desc, "path": fp})
    return templates


def create_skill_from_template(template_name: str, skill_name: str, **overrides) -> str:
    """Create a new skill from a template. Returns path to created file."""
    os.makedirs(SKILLS_DIR, exist_ok=True)
    template_path = os.path.join(TEMPLATES_DIR, f"{template_name}.md")
    if not os.path.isfile(template_path):
        return None

    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace("{{NAME}}", skill_name)
    for key, val in overrides.items():
        content = content.replace("{{" + key.upper() + "}}", str(val))

    out_path = os.path.join(SKILLS_DIR, f"{skill_name}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path
