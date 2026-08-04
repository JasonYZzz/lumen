"""Tests for the Agent Skills system (discovery, parsing, prompt injection).

Covers the core ``skills.py`` module: frontmatter parsing, name validation,
directory discovery, project-over-user precedence, system-prompt formatting,
manual-invocation expansion, and ``disable-model-invocation``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lumen.skills import (
    Skill,
    SkillLoader,
    SkillWorkingSet,
    expand_skill_for_message,
    format_skills_for_prompt,
    load_skill,
)


@pytest.fixture(autouse=True)
def _isolate_user_skill_directory(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep discovery tests independent of the developer's real home directory."""

    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_skill(
    directory: Path,
    name: str | None = None,
    description: str = "A test skill.",
    body: str = "Do the thing.",
    disable_model_invocation: bool = False,
) -> Path:
    """Write a SKILL.md into ``directory`` and return its path."""

    directory.mkdir(parents=True, exist_ok=True)
    frontmatter_lines = ["---"]
    if name is not None:
        frontmatter_lines.append(f"name: {name}")
    frontmatter_lines.append(f"description: {description}")
    if disable_model_invocation:
        frontmatter_lines.append("disable-model-invocation: true")
    frontmatter_lines.append("---")
    content = "\n".join(frontmatter_lines) + "\n" + body + "\n"
    skill_path = directory / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_parse_skill_frontmatter(tmp_path: Path) -> None:
    """Correctly parses name, description, and body from YAML frontmatter."""

    skill_path = _write_skill(tmp_path / "my-skill", name="my-skill", description="Use it for X.")
    skill = load_skill(skill_path, "project")
    assert skill is not None
    assert skill.name == "my-skill"
    assert skill.description == "Use it for X."
    assert skill.body == "Do the thing."
    assert skill.source == "project"


def test_skill_name_defaults_to_dirname(tmp_path: Path) -> None:
    """When frontmatter omits ``name``, the parent directory name is used."""

    skill_path = _write_skill(tmp_path / "auto-name", name=None)
    skill = load_skill(skill_path, "project")
    assert skill is not None
    assert skill.name == "auto-name"


def test_skill_missing_description_dropped(tmp_path: Path) -> None:
    """A skill without a description returns None (spec: drop silently)."""

    skill_dir = tmp_path / "no-desc"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: no-desc\n---\nbody\n", encoding="utf-8")
    skill = load_skill(skill_dir / "SKILL.md", "project")
    assert skill is None


def test_skill_invalid_name_dropped(tmp_path: Path) -> None:
    """Names with underscores, leading hyphens, or consecutive hyphens are rejected.

    Note: uppercase names are lowercased (``MySkill`` → ``myskill``) rather
    than rejected — this is a friendlier normalisation that matches pi's
    behaviour. Only truly invalid patterns (underscores, leading/trailing
    hyphens, consecutive hyphens) are dropped.
    """

    for bad_name in ("my_skill", "-leading", "double--hyphen", "trailing-"):
        skill_path = _write_skill(tmp_path / f"dir-{bad_name}", name=bad_name)
        assert load_skill(skill_path, "project") is None, f"should reject {bad_name!r}"


def test_skill_disable_model_invocation(tmp_path: Path) -> None:
    """``disable-model-invocation: true`` is parsed correctly."""

    skill_path = _write_skill(tmp_path / "manual-only", disable_model_invocation=True)
    skill = load_skill(skill_path, "project")
    assert skill is not None
    assert skill.disable_model_invocation is True


def test_skill_no_frontmatter_still_loads(tmp_path: Path) -> None:
    """A bare markdown file with no frontmatter is loaded if it has content.

    Without frontmatter, ``description`` is empty → the skill is dropped per
    spec. This test documents that behaviour.
    """

    skill_dir = tmp_path / "bare"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Just markdown\n\nNo frontmatter.\n", encoding="utf-8")
    # No frontmatter → no description → dropped.
    assert load_skill(skill_dir / "SKILL.md", "project") is None


# ---------------------------------------------------------------------------
# Discovery (SkillLoader)
# ---------------------------------------------------------------------------


def test_discover_project_skills(tmp_path: Path) -> None:
    """Skills in ``<workspace>/.lumen/skills/`` are discovered."""

    _write_skill(tmp_path / ".lumen" / "skills" / "skill-a", name="skill-a")
    _write_skill(tmp_path / ".lumen" / "skills" / "skill-b", name="skill-b")
    loader = SkillLoader(tmp_path)
    skills = loader.discover()
    names = [s.name for s in skills]
    assert names == ["skill-a", "skill-b"]  # sorted


def test_untrusted_loader_excludes_project_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _write_skill(tmp_path / ".lumen" / "skills" / "project-skill", name="project-skill")
    _write_skill(fake_home / ".lumen" / "skills" / "user-skill", name="user-skill")

    skills = SkillLoader(tmp_path, include_project=False).discover()

    assert [(skill.name, skill.source) for skill in skills] == [("user-skill", "user")]


def test_discover_user_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skills in ``~/.lumen/skills/`` are discovered."""

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _write_skill(fake_home / ".lumen" / "skills" / "user-skill", name="user-skill")
    # The workspace has no project skills.
    loader = SkillLoader(tmp_path)
    skills = loader.discover()
    assert len(skills) == 1
    assert skills[0].name == "user-skill"
    assert skills[0].source == "user"


def test_discover_project_overrides_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On name collision, the project skill wins and a warning is emitted."""

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Same skill name in both locations.
    _write_skill(
        fake_home / ".lumen" / "skills" / "shared",
        name="shared",
        description="user version",
    )
    _write_skill(
        tmp_path / ".lumen" / "skills" / "shared",
        name="shared",
        description="project version",
    )
    loader = SkillLoader(tmp_path)
    skills = loader.discover()
    assert len(skills) == 1
    assert skills[0].description == "project version"
    assert any("shared" in w for w in loader.warnings)


def test_discover_empty_workspace(tmp_path: Path) -> None:
    """An empty workspace with no skill dirs returns an empty list."""

    loader = SkillLoader(tmp_path)
    assert loader.discover() == []


def test_discover_standalone_md_file(tmp_path: Path) -> None:
    """A top-level ``.md`` file (not in a subdirectory) is loaded as a skill."""

    skills_dir = tmp_path / ".lumen" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "standalone.md").write_text(
        "---\nname: standalone\ndescription: A lone file.\n---\nbody\n", encoding="utf-8"
    )
    loader = SkillLoader(tmp_path)
    skills = loader.discover()
    assert len(skills) == 1
    assert skills[0].name == "standalone"


def test_discover_skips_git_and_pycache(tmp_path: Path) -> None:
    """.git and __pycache__ directories inside skills/ are never scanned."""

    skills_dir = tmp_path / ".lumen" / "skills"
    # Put a "SKILL.md" inside .git — it must not be discovered.
    _write_skill(skills_dir / ".git" / "fake", name="fake")
    _write_skill(skills_dir / "real-skill", name="real-skill")
    loader = SkillLoader(tmp_path)
    skills = loader.discover()
    names = [s.name for s in skills]
    assert "real-skill" in names
    assert "fake" not in names


def test_discovery_rejects_symlinked_skill_outside_root(tmp_path: Path) -> None:
    external = tmp_path / "external"
    _write_skill(external / "escaped", name="escaped")
    skills_dir = tmp_path / "workspace" / ".lumen" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "linked").symlink_to(external / "escaped", target_is_directory=True)

    loader = SkillLoader(tmp_path / "workspace")

    assert loader.discover() == []
    assert any("symlink" in warning for warning in loader.warnings)


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


def test_format_skills_for_prompt_structure() -> None:
    """The system-prompt block has the correct XML structure."""

    skills = [
        Skill(
            name="commit",
            description="Write commit messages.",
            file_path=Path("/skills/commit/SKILL.md"),
            base_dir=Path("/skills/commit"),
            body="body",
            source="project",
        )
    ]
    result = format_skills_for_prompt(skills)
    assert "<available_skills>" in result
    assert "</available_skills>" in result
    assert "<name>commit</name>" in result
    assert "<description>Write commit messages.</description>" in result
    assert "/skills/commit/SKILL.md" in result
    assert "Use the load_skill tool with the skill name" in result
    assert "Use the read_file tool" not in result


def test_format_skills_excludes_disabled() -> None:
    """Skills with disable_model_invocation=True are hidden from the prompt."""

    skills = [
        Skill(
            name="visible",
            description="A visible skill.",
            file_path=Path("/a"),
            base_dir=Path("/"),
            body="",
            source="project",
        ),
        Skill(
            name="hidden",
            description="A hidden skill.",
            file_path=Path("/b"),
            base_dir=Path("/"),
            body="",
            source="project",
            disable_model_invocation=True,
        ),
    ]
    result = format_skills_for_prompt(skills)
    assert "visible" in result
    assert "hidden" not in result


def test_format_skills_empty_returns_empty() -> None:
    """No skills → empty string (no prompt block at all)."""

    assert format_skills_for_prompt([]) == ""


def test_format_skills_xml_escapes_description() -> None:
    """Descriptions with <, >, & are XML-escaped to avoid breaking the block."""

    skills = [
        Skill(
            name="x",
            description="Use <tags> & stuff.",
            file_path=Path("/x"),
            base_dir=Path("/"),
            body="",
            source="project",
        )
    ]
    result = format_skills_for_prompt(skills)
    assert "&lt;tags&gt;" in result
    assert "&amp; stuff." in result


# ---------------------------------------------------------------------------
# Manual invocation expansion
# ---------------------------------------------------------------------------


def test_expand_skill_for_message_basic() -> None:
    """The <skill> XML wrapper contains the body and base-dir note."""

    skill = Skill(
        name="review",
        description="Review code.",
        file_path=Path("/skills/review/SKILL.md"),
        base_dir=Path("/skills/review"),
        body="Step 1: read code.\nStep 2: comment.",
        source="project",
    )
    result = expand_skill_for_message(skill)
    assert '<skill name="review"' in result
    assert "/skills/review/SKILL.md" in result
    assert "References are relative to /skills/review." in result
    assert "Step 1: read code." in result
    assert "</skill>" in result


def test_expand_skill_with_args() -> None:
    """Arguments are appended after the </skill> block."""

    skill = Skill(
        name="review",
        description="d",
        file_path=Path("/r/SKILL.md"),
        base_dir=Path("/r"),
        body="body",
        source="project",
    )
    result = expand_skill_for_message(skill, args="src/main.py")
    assert result.endswith("src/main.py")


# ---------------------------------------------------------------------------
# ResourceManager.load_skill_by_name seam (Phase 2.5)
# ---------------------------------------------------------------------------


def _config_with_skills(tmp_path: Path) -> object:
    from lumen.config import load_config

    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 1
agent:
  name: skill-test
  model:
    id: test
tools:
  builtins: []
sessions:
  directory: sessions
""",
        encoding="utf-8",
    )
    return load_config(config_path)


def test_load_skill_by_name_returns_only_discovered_skill(tmp_path: Path) -> None:
    """The seam returns a discovered skill by name and rejects unknown names,
    so /skill: and any future tool can reach skills without widening read_file."""
    from lumen.resources import ResourceManager

    skills_dir = tmp_path / ".lumen" / "skills" / "tdd"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\ndescription: Write tests first.\n---\nRed, green, refactor.",
        encoding="utf-8",
    )
    config = _config_with_skills(tmp_path)
    manager = ResourceManager(config, workspace=tmp_path)  # type: ignore[arg-type]

    skill = manager.load_skill_by_name("tdd")
    assert skill is not None
    assert skill.name == "tdd"
    assert "Red, green, refactor" in skill.body
    # An unknown name yields None — no fallback to arbitrary file reads.
    assert manager.load_skill_by_name("does-not-exist") is None


def test_load_skill_by_name_path_is_confined_to_discovered_roots(tmp_path: Path) -> None:
    """A skill's resolved file_path must live under a discovery root, never an
    arbitrary absolute path — so the seam cannot be abused to read outside the
    workspace."""
    from lumen.resources import ResourceManager

    skills_dir = tmp_path / ".lumen" / "skills" / "audit"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\ndescription: Audit code.\n---\nCheck standards.", encoding="utf-8"
    )
    config = _config_with_skills(tmp_path)
    manager = ResourceManager(config, workspace=tmp_path)  # type: ignore[arg-type]

    skill = manager.load_skill_by_name("audit")
    assert skill is not None
    # The discovered skill's path resolves inside the workspace skill root.
    assert tmp_path in skill.file_path.parents or skill.file_path == tmp_path


def test_resource_manager_registers_confined_model_skill_tool(tmp_path: Path) -> None:
    from lumen.resources import ResourceManager

    _write_skill(
        tmp_path / ".lumen" / "skills" / "review",
        name="review",
        body="Review every changed line.",
    )
    manager = ResourceManager(_config_with_skills(tmp_path), workspace=tmp_path)  # type: ignore[arg-type]

    entry = manager.registry.entries["load_skill"]
    rendered = entry.spec.function("review")

    assert entry.spec.risk.value == "read"
    assert '<skill name="review"' in rendered
    assert "Review every changed line." in rendered


def test_model_skill_tool_refuses_manual_only_skill(tmp_path: Path) -> None:
    from lumen.resources import ResourceManager

    _write_skill(
        tmp_path / ".lumen" / "skills" / "manual",
        name="manual",
        disable_model_invocation=True,
    )
    manager = ResourceManager(_config_with_skills(tmp_path), workspace=tmp_path)  # type: ignore[arg-type]

    assert "load_skill" not in manager.registry.entries
    assert "read_skill_resource" not in manager.registry.entries


def test_model_skill_resource_refuses_manual_only_skill_when_reader_exists(tmp_path: Path) -> None:
    from lumen.resources import ResourceManager

    _write_skill(tmp_path / ".lumen" / "skills" / "visible", name="visible")
    manual_dir = tmp_path / ".lumen" / "skills" / "manual"
    _write_skill(manual_dir, name="manual", disable_model_invocation=True)
    (manual_dir / "reference.md").write_text("private manual resource", encoding="utf-8")
    manager = ResourceManager(_config_with_skills(tmp_path), workspace=tmp_path)  # type: ignore[arg-type]
    reader = manager.registry.entries["read_skill_resource"].spec.function

    with pytest.raises(FileNotFoundError, match="model-invocable"):
        reader("manual", path="reference.md")


async def test_model_loads_discovered_skill_in_end_to_end_run(tmp_path: Path) -> None:
    from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from lumen.resources import ResourceManager
    from lumen.runtime import AgentRuntime, ToolApproval

    _write_skill(
        tmp_path / ".lumen" / "skills" / "review",
        name="review",
        body="Review every changed line before answering.",
    )
    config = _config_with_skills(tmp_path)
    manager = ResourceManager(config, workspace=tmp_path)  # type: ignore[arg-type]

    async def model_stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = next(
            (
                part
                for message in reversed(messages)
                if isinstance(message, ModelRequest)
                for part in reversed(message.parts)
                if isinstance(part, ToolReturnPart)
            ),
            None,
        )
        if result is None:
            yield {
                0: DeltaToolCall(
                    "load_skill",
                    '{"name":"review"}',
                    tool_call_id="load-skill-1",
                )
            }
        else:
            assert "Review every changed line before answering." in str(result.content)
            yield "Loaded and followed the review skill."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_stream),
        tools=manager.local_tools,
        toolsets=[],
        instructions=manager.instructions,
        limits=manager.config.agent.limits,
        tool_metadata=manager.tool_metadata,
    )

    async def emit(_event: object) -> None:
        return None

    async def approve(_request: object) -> ToolApproval:
        raise AssertionError("read-only load_skill must not require approval")

    outcome = await runtime.run("review the changes", [], emit, approve)  # type: ignore[arg-type]

    assert outcome.output == "Loaded and followed the review skill."


async def test_model_reads_skill_resource_in_end_to_end_run(tmp_path: Path) -> None:
    from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from lumen.resources import ResourceManager
    from lumen.runtime import AgentRuntime, ToolApproval

    skill_dir = tmp_path / ".lumen" / "skills" / "arp-report"
    _write_skill(skill_dir, name="arp-report", body="Read reference.md before answering.")
    (skill_dir / "reference.md").write_text("required ARP template", encoding="utf-8")
    manager = ResourceManager(_config_with_skills(tmp_path), workspace=tmp_path)  # type: ignore[arg-type]

    async def model_stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = next(
            (
                part
                for message in reversed(messages)
                if isinstance(message, ModelRequest)
                for part in reversed(message.parts)
                if isinstance(part, ToolReturnPart)
            ),
            None,
        )
        if result is None:
            yield {
                0: DeltaToolCall(
                    "read_skill_resource",
                    '{"name":"arp-report","path":"reference.md"}',
                    tool_call_id="read-resource-1",
                )
            }
        else:
            assert "required ARP template" in str(result.content)
            yield "Loaded the ARP template."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_stream),
        tools=manager.local_tools,
        toolsets=[],
        instructions=manager.instructions,
        limits=manager.config.agent.limits,
        tool_metadata=manager.tool_metadata,
    )

    async def emit(_event: object) -> None:
        return None

    async def approve(_request: object) -> ToolApproval:
        raise AssertionError("read-only skill resource must not require approval")

    outcome = await runtime.run("build an ARP report", [], emit, approve)  # type: ignore[arg-type]

    assert outcome.output == "Loaded the ARP template."


def test_model_skill_resource_tool_reads_confined_relative_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model-invocable skill may safely load files relative to its own directory."""
    fake_home = tmp_path / "home"
    skill_dir = fake_home / ".lumen" / "skills" / "arp-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: arp-report\ndescription: Build an ARP report.\n---\n"
        "Read [the reference](reference.md) before producing the report.\n",
        encoding="utf-8",
    )
    (skill_dir / "reference.md").write_text("ARP reference body", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    from lumen.resources import ResourceManager

    manager = ResourceManager(_config_with_skills(tmp_path), workspace=tmp_path)  # type: ignore[arg-type]
    loader = manager.registry.entries["read_skill_resource"].spec.function

    rendered = loader("arp-report", path="reference.md")

    assert "ARP reference body" in rendered
    assert "reference.md" in rendered


@pytest.mark.parametrize("path", ["../secret.md", "/tmp/secret.md"])
def test_model_skill_resource_tool_rejects_path_escape(tmp_path: Path, path: str) -> None:
    from lumen.resources import ResourceManager
    from lumen.tools.workspace import WorkspaceViolation

    _write_skill(
        tmp_path / ".lumen" / "skills" / "arp-report",
        name="arp-report",
        body="Read reference.md.",
    )
    manager = ResourceManager(_config_with_skills(tmp_path), workspace=tmp_path)  # type: ignore[arg-type]
    loader = manager.registry.entries["read_skill_resource"].spec.function

    with pytest.raises(WorkspaceViolation):
        loader("arp-report", path=path)


def test_model_skill_resource_tool_rejects_symlink_escape(tmp_path: Path) -> None:
    from lumen.resources import ResourceManager
    from lumen.tools.workspace import WorkspaceViolation

    skill_dir = tmp_path / ".lumen" / "skills" / "arp-report"
    _write_skill(skill_dir, name="arp-report", body="Read reference.md.")
    secret = tmp_path / "outside.md"
    secret.write_text("do not leak", encoding="utf-8")
    (skill_dir / "reference.md").symlink_to(secret)
    manager = ResourceManager(_config_with_skills(tmp_path), workspace=tmp_path)  # type: ignore[arg-type]
    loader = manager.registry.entries["read_skill_resource"].spec.function

    with pytest.raises(WorkspaceViolation):
        loader("arp-report", path="reference.md")


def test_skill_working_set_caps_bodies_and_evicts_oldest(tmp_path: Path) -> None:
    skills: list[Skill] = []
    for name, body in (("one", "111111"), ("two", "222222"), ("three", "333333")):
        loaded = load_skill(_write_skill(tmp_path / name, name=name, body=body), "project")
        assert loaded is not None
        skills.append(loaded)
    working = SkillWorkingSet(
        max_skill_tokens=5,
        max_total_tokens=8,
        token_counter=len,
    )

    first = working.activate(skills[0])
    second = working.activate(skills[1])
    third = working.activate(skills[2])

    assert first.truncated is True
    assert second.name not in {item.name for item in working.active()}
    assert [item.name for item in working.active()] == [third.name]


def test_reactivating_skill_refreshes_working_set_recency(tmp_path: Path) -> None:
    one = load_skill(_write_skill(tmp_path / "one", name="one", body="1111"), "project")
    two = load_skill(_write_skill(tmp_path / "two", name="two", body="2222"), "project")
    three = load_skill(_write_skill(tmp_path / "three", name="three", body="3333"), "project")
    assert one is not None and two is not None and three is not None
    working = SkillWorkingSet(max_total_tokens=8, max_skill_tokens=8, token_counter=len)
    working.activate(one)
    working.activate(two)
    working.activate(one)
    working.activate(three)

    assert [item.name for item in working.active()] == [one.name, three.name]
