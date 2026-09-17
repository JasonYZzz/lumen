"""Agent Skills: directory-discovered instruction packs for Lumen.

This is the Lumen port of pi/coding-agent's ``skills.ts`` and follows the
open Agent Skills specification (https://agentskills.io/specification) used by
Claude Code, Codex CLI, and ZCode.

A *skill* is **not** a tool — it is a prompt fragment / instruction package.
The model sees a compact catalog (name + description) in the system prompt at
all times (progressive disclosure); the full instruction body loads on demand
through the confined ``load_skill`` tool, or the user types ``/skill:<name>``
which expands the body into a ``<skill>`` message. Sibling references load
through ``read_skill_resource`` rather than the workspace-only ``read_file``.

Discovery scans two locations (project overrides user on name collision):

1. ``<workspace>/.lumen/skills/`` — project-local skills
2. ``~/.lumen/skills/`` — user-global skills

Each skill is a directory containing a ``SKILL.md`` file with YAML frontmatter::

    ---
    name: commit-message          # optional; defaults to directory name
    description: Write concise…   # required; tells the model WHEN to use it
    disable-model-invocation: false  # optional; true = /skill: manual only
    ---
    Markdown instruction body…

The body can reference sibling files (scripts, references) by relative path;
the skill's ``base_dir`` (parent of SKILL.md) is the resolution root.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import cast

import yaml

from lumen.constants import IGNORED_DIRS

#: Maximum description length (spec: 1024 chars). Skills exceeding this are
#: still loaded — we truncate in the prompt, not at parse time.
_MAX_DESCRIPTION = 1024

#: Maximum name length (spec: 64 chars).
_MAX_NAME = 64

#: Regex for valid skill names: lowercase kebab-case, no leading/trailing
#: hyphens, no consecutive hyphens. Matches the agentskills.io spec.
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Frontmatter fence: opening/closing ``---`` lines.
_FRONTMATTER_FENCE = "---"


def _xml_escape(text: str) -> str:
    """Minimal XML escape for text content: &, <, >."""

    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr_escape(text: str) -> str:
    """XML escape for attribute values: &, <, >, \"."""

    return _xml_escape(text).replace('"', "&quot;")


@dataclass(frozen=True, slots=True)
class Skill:
    """One parsed skill, ready for prompt injection or manual invocation.

    Attributes:
        name: Kebab-case identifier (e.g. ``commit-message``).
        description: 1-1024 char string telling the model *when* to use it.
        file_path: Absolute path to the ``SKILL.md`` file.
        base_dir: Parent directory of ``file_path`` — the resolution root for
            relative references in the body.
        body: Markdown instruction text after the frontmatter.
        source: ``"project"`` or ``"user"``, indicating which discovery root
            the skill came from.
        disable_model_invocation: If True, the skill is hidden from the
            ``<available_skills>`` system prompt block and can only be loaded
            via the ``/skill:<name>`` manual command.
    """

    name: str
    description: str
    file_path: Path
    base_dir: Path
    body: str
    source: str
    disable_model_invocation: bool = False
    scripts: dict[str, Path] = field(default_factory=dict[str, Path])


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split ``text`` into ``(frontmatter_dict, body)``.

    The frontmatter is a YAML block delimited by ``---`` fences at the very
    start of the file. If no fences are present, returns ``({}, text)``.
    YAML parse errors produce an empty dict (the caller treats a missing
    ``description`` as a drop condition).

    Mirrors pi's ``utils/frontmatter.ts``: only an opening fence at line 1
    counts; the closing fence is the next standalone ``---`` line.
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return {}, text
    # Find the closing fence.
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_FENCE:
            end = i
            break
    if end == -1:
        # Unclosed frontmatter — treat the whole file as body.
        return {}, text
    yaml_block = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip()
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        parsed = None
    if not isinstance(parsed, dict):
        return {}, body
    return cast(dict[str, object], parsed), body


def _validate_name(raw: object, fallback: str) -> str | None:
    """Return a valid skill name, or None if invalid.

    If ``raw`` is None, use ``fallback`` (the directory name). Validate
    against the spec pattern and length. Returns None on failure so the
    caller can emit a diagnostic and drop the skill.
    """

    if raw is not None and not isinstance(raw, str):
        return None
    name = (raw or fallback).strip().lower()
    if not name or len(name) > _MAX_NAME:
        return None
    if not _NAME_PATTERN.match(name):
        return None
    return name


def load_skill(
    file_path: Path,
    source: str,
    warnings: list[str] | None = None,
) -> Skill | None:
    """Parse a single ``SKILL.md`` file into a ``Skill``, or None on failure.

    Returns None (caller should warn) when:
    - The file cannot be read.
    - The frontmatter has no ``description`` (spec: drop silently).
    - The name (explicit or derived) fails validation.

    All other issues (extra frontmatter fields, long descriptions) are
    accepted — extra fields are ignored, long descriptions are truncated at
    prompt-formatting time.
    """

    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    frontmatter, body = _parse_frontmatter(text)
    description = str(frontmatter.get("description", "")).strip()
    if not description:
        # Spec: a skill without a description is dropped (the model has no
        # way to know when to use it).
        return None
    name = _validate_name(
        frontmatter.get("name"),
        fallback=file_path.parent.name,
    )
    if name is None:
        return None
    disable = bool(frontmatter.get("disable-model-invocation", False))
    scripts = _parse_scripts(file_path, name, frontmatter.get("scripts"), warnings)
    return Skill(
        name=name,
        description=description[:_MAX_DESCRIPTION],
        file_path=file_path.resolve(),
        base_dir=file_path.parent.resolve(),
        body=body,
        source=source,
        disable_model_invocation=disable,
        scripts=scripts,
    )


def _parse_scripts(
    file_path: Path,
    skill_name: str,
    raw: object,
    warnings: list[str] | None,
) -> dict[str, Path]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        if warnings is not None:
            warnings.append(f"skill {skill_name!r}: scripts must be a mapping")
        return {}
    base_dir = file_path.parent.resolve()
    scripts: dict[str, Path] = {}
    for raw_name, raw_path in cast(dict[object, object], raw).items():
        script_name = str(raw_name).strip()
        relative = Path(str(raw_path))
        resolved = (base_dir / relative).resolve(strict=False)
        reason: str | None = None
        if not script_name:
            reason = "script name is empty"
        elif relative.is_absolute() or not resolved.is_relative_to(base_dir):
            reason = f"script {relative} escapes base_dir"
        elif resolved.suffix.lower() not in {".sh", ".bash", ".py"}:
            reason = f"script {relative} has a disallowed interpreter"
        elif not resolved.is_file():
            reason = f"script {relative} does not exist"
        if reason is not None:
            if warnings is not None:
                warnings.append(f"skill {skill_name!r}: {reason}; dropped")
            continue
        scripts[script_name] = resolved
    return scripts


class SkillLoader:
    """Discovers and parses skills from project and user directories.

    Usage::

        loader = SkillLoader(workspace)
        skills = loader.discover()
        # skills is a list[Skill], de-duplicated (project overrides user).

    The ``warnings`` attribute collects non-fatal diagnostics (unreadable
    files, name collisions) for surfacing to the user at startup.
    """

    #: Directories skipped during the recursive scan (never useful as skill
    #: roots, and .git in particular can contain thousands of files).
    #: Shared via ``lumen.constants.IGNORED_DIRS``.
    _SKIP_DIRS = IGNORED_DIRS

    def __init__(
        self,
        workspace: Path,
        *,
        include_project: bool = True,
        include_builtin: bool = False,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._project_dir = self.workspace / ".lumen" / "skills"
        self._user_dir = Path.home() / ".lumen" / "skills"
        self._builtin_dir = Path(str(files("lumen").joinpath("builtin_skills")))
        self.include_project = include_project
        self.include_builtin = include_builtin
        self.warnings: list[str] = []

    def discover(self) -> list[Skill]:
        """Scan both roots, returning de-duplicated skills sorted by name.

        Project skills (``<workspace>/.lumen/skills/``) take precedence
        over user skills (``~/.lumen/skills/``): on a name collision the
        user-level skill is dropped with a warning.
        """

        builtin = self._scan_root(self._builtin_dir, "builtin") if self.include_builtin else []
        project = self._scan_root(self._project_dir, "project") if self.include_project else []
        user = self._scan_root(self._user_dir, "user")
        # Merge: project > user > builtin.
        by_name: dict[str, Skill] = {}
        for skill in builtin:
            by_name[skill.name] = skill
        for skill in user:
            if skill.name in by_name:
                self.warnings.append(f"skill '{skill.name}' overrides builtin version ({skill.file_path})")
            by_name[skill.name] = skill
        for skill in project:
            if skill.name in by_name:
                self.warnings.append(
                    f"skill '{skill.name}' exists in both project and user dirs; "
                    f"using project version ({skill.file_path})"
                )
            by_name[skill.name] = skill
        return sorted(by_name.values(), key=lambda s: s.name)

    def _scan_root(self, root: Path, source: str) -> list[Skill]:
        """Scan one discovery root, returning parsed skills.

        Scanning rules (mirrors pi ``loadSkillsFromDir``):
        - A directory containing ``SKILL.md`` is a skill root — we stop
          recursing into it.
        - Otherwise, recurse into subdirectories looking for ``SKILL.md``.
        - Top-level ``.md`` files are also loaded as standalone skills
          (their stem becomes the name fallback).
        - ``_SKIP_DIRS`` are pruned in place.
        """

        if not root.is_dir():
            return []
        skills: list[Skill] = []
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        for entry in entries:
            if entry.name in self._SKIP_DIRS or entry.name.startswith(".lumen-install-"):
                continue
            if entry.is_symlink():
                self.warnings.append(f"ignored symlinked skill path outside trusted roots: {entry}")
                continue
            if entry.is_file() and entry.suffix == ".md":
                skill = load_skill(entry, source, self.warnings)
                if skill is not None:
                    skills.append(skill)
                continue
            if entry.is_dir():
                skill_md = entry / "SKILL.md"
                if skill_md.is_file():
                    skill = load_skill(skill_md, source, self.warnings)
                    if skill is not None:
                        skills.append(skill)
                else:
                    # Recurse one level (shallow — skills are flat dirs).
                    skills.extend(self._scan_subdir(entry, source))
        return skills

    def _scan_subdir(self, directory: Path, source: str) -> list[Skill]:
        """Recurse into a subdirectory looking for SKILL.md files."""

        skills: list[Skill] = []
        try:
            for entry in sorted(directory.iterdir(), key=lambda p: p.name):
                if entry.name in self._SKIP_DIRS:
                    continue
                if entry.is_symlink():
                    self.warnings.append(f"ignored symlinked skill path outside trusted roots: {entry}")
                    continue
                if entry.is_dir():
                    skill_md = entry / "SKILL.md"
                    if skill_md.is_file():
                        skill = load_skill(skill_md, source, self.warnings)
                        if skill is not None:
                            skills.append(skill)
                    else:
                        skills.extend(self._scan_subdir(entry, source))
        except OSError:
            pass
        return skills


def format_skills_for_prompt(skills: list[Skill]) -> str:
    """Generate the ``<available_skills>`` XML block for the system prompt.

    Only model-invocable skills (``disable_model_invocation=False``) appear.
    The block tells the model to use the confined ``load_skill`` tool by name
    when the task matches a skill description.

    Format follows pi's ``formatSkillsForPrompt`` (skills.ts:335-361) and the
    agentskills.io "integrate-skills" section.
    """

    visible = [s for s in skills if not s.disable_model_invocation]
    if not visible:
        return ""
    lines = [
        "以下 Skill 为特定任务提供专门指令。",
        "任务与说明匹配时, 使用 load_skill 并传入 Skill 名称。",
        (
            "已加载 Skill 引用相对文件时, 使用 read_skill_resource 并传入 Skill 名称和相对路径; "
            "不要用 read_file 读取 Skill 资源。"
        ),
        "",
        "<available_skills>",
    ]
    for skill in visible:
        # XML-escape all free-text content. ``name`` is validated kebab-case
        # (safe by construction) but we escape for defence-in-depth.
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml_escape(skill.name)}</name>")
        lines.append(f"    <description>{_xml_escape(skill.description)}</description>")
        lines.append(f"    <location>{_xml_escape(str(skill.file_path))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def expand_skill_for_message(skill: Skill, args: str = "") -> str:
    """Wrap a skill body in a ``<skill>`` XML block for manual invocation.

    Called by the ``/skill:<name>`` command handler. The block is sent as a
    user message so the model treats it as an instruction to follow. The
    ``location`` and base-dir note tell the model where to find sibling
    files referenced by relative paths.

    Mirrors pi's ``_expandSkillCommand`` (agent-session.ts:1289-1313).
    """

    parts = [
        f'<skill name="{_xml_attr_escape(skill.name)}" location="{_xml_attr_escape(str(skill.file_path))}">',
        f"References are relative to {skill.base_dir}.",
        (
            'Read referenced files with read_skill_resource(name="'
            f'{_xml_attr_escape(skill.name)}", path="relative/path").'
        ),
        skill.body,
        "</skill>",
    ]
    if skill.scripts:
        declared = ", ".join(sorted(skill.scripts))
        parts.insert(3, f"Declared scripts ({declared}) may be run with run_skill_script.")
    if args:
        parts.append(args)
    return "\n".join(parts)


__all__ = [
    "Skill",
    "SkillLoader",
    "expand_skill_for_message",
    "format_skills_for_prompt",
    "load_skill",
]
