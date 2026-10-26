"""Strict, provider-independent contracts for ongoing personal assistance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AutomationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def safe_text(value: str) -> str:
    from lumen.context.memory.redaction import contains_secret

    if not value.strip():
        raise ValueError("Must not be blank")
    if contains_secret(value):
        raise ValueError("Do not include credentials or personal identifiers")
    return value.strip()


class Schedule(AutomationModel):
    kind: Literal["daily", "weekdays", "weekly"] = "weekdays"
    timezone: str = "Asia/Shanghai"
    hour: int = Field(default=8, ge=0, le=23)
    minute: int = Field(default=30, ge=0, le=59)
    weekday: int = Field(default=0, ge=0, le=6)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as error:
            raise ValueError("Use an installed IANA time zone") from error
        return value

    def instant(self, day: datetime) -> datetime | None:
        if self.kind == "weekdays" and day.weekday() >= 5:
            return None
        if self.kind == "weekly" and day.weekday() != self.weekday:
            return None
        local = day.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0, tzinfo=None)
        zone = ZoneInfo(self.timezone)
        # A gap moves to the first valid minute; a fold always uses its first occurrence.
        for _ in range(1441):
            candidate = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == local:
                return candidate
            local += timedelta(minutes=1)
        raise ValueError("No valid scheduled instant within one day")

    def next_after(self, after: datetime) -> datetime:
        day = after.astimezone(ZoneInfo(self.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(9):
            candidate = self.instant(day + timedelta(days=offset))
            if candidate is not None and candidate > after:
                return candidate
        raise ValueError("Schedule has no next occurrence")

    def latest_due(self, now: datetime) -> datetime:
        day = now.astimezone(ZoneInfo(self.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(9):
            candidate = self.instant(day - timedelta(days=offset))
            if candidate is not None and candidate <= now:
                return candidate
        raise ValueError("Schedule has no due occurrence")


class ContextRef(AutomationModel):
    kind: Literal["goal", "resource", "session"]
    id: str = Field(min_length=1, max_length=200)
    revision: int | None = Field(default=None, ge=1)


class AutomationInput(AutomationModel):
    name: str = Field(min_length=1, max_length=160)
    intent: str = Field(min_length=1, max_length=8000)
    profile: Literal["workspace_snapshot", "research_readonly"] = "workspace_snapshot"
    paths: tuple[str, ...] = Field(default=(), max_length=16)
    context_refs: tuple[ContextRef, ...] = Field(default=(), max_length=16)
    schedule: Schedule = Field(default_factory=Schedule)
    request_limit: int = Field(default=6, ge=1, le=50)
    timeout_seconds: int = Field(default=180, ge=10, le=1800)
    notify: bool = True
    end_at: datetime | None = None

    @model_validator(mode="after")
    def source_scope(self) -> AutomationInput:
        if self.profile == "workspace_snapshot" and not self.paths:
            raise ValueError("File snapshot follow-ups require selected files")
        if self.profile == "research_readonly" and self.paths:
            raise ValueError("Research uses linked resources; select the file snapshot profile for paths")
        return self

    @field_validator("name", "intent")
    @classmethod
    def nonblank(cls, value: str) -> str:
        return safe_text(value)

    @field_validator("paths")
    @classmethod
    def relative_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for value in paths:
            safe_text(value)
            path = PurePosixPath(value)
            if (
                not path.parts
                or "\\" in value
                or path.is_absolute()
                or any(part.startswith(".") for part in path.parts)
            ):
                raise ValueError("Sources must be non-hidden workspace-relative files")
        return tuple(dict.fromkeys(paths))

    @field_validator("end_at")
    @classmethod
    def aware_end(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("end_at requires a timezone")
        return value


class Definition(AutomationModel):
    id: str
    revision: int = Field(ge=1)
    spec: AutomationInput
    enabled: bool = False
    validation: str | None = None
    next_due: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Phase(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SETTLING = "settling"
    SETTLED = "settled"


class Result(StrEnum):
    COMPLETE = "complete"
    NO_CHANGE = "no_change"
    PARTIAL = "partial"
    NEEDS_USER_ACTION = "needs_user_action"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    BUDGET_REACHED = "budget_reached"
    SKIPPED = "skipped"


class SourceReceipt(AutomationModel):
    path: str
    revision: str | None = None
    error: str | None = None
    artifact_ref: str | None = None
    read_at: datetime | None = None
    coverage: Literal["snapshot", "search_snippet", "page_excerpt"] = "snapshot"


class Occurrence(AutomationModel):
    id: str
    automation_id: str
    definition_revision: int
    kind: Literal["scheduled", "manual", "trial"]
    edition: str
    session_id: str
    interaction_id: str
    created_at: datetime
    phase: Phase = Phase.QUEUED
    result: Result | None = None
    fingerprint: str | None = None
    receipts: tuple[SourceReceipt, ...] = ()
    artifact_ref: str | None = None
    detail: str = ""
    deadline: datetime | None = None
    spec_snapshot: AutomationInput | None = None
    preference_snapshot: tuple[str, ...] = ()
    session_available: bool = False


class Milestone(AutomationModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    done: bool = False

    _title = field_validator("title")(safe_text)


class GoalInput(AutomationModel):
    title: str = Field(min_length=1, max_length=200)
    intent: str = Field(default="", max_length=8000)
    status: Literal["active", "completed", "archived"] = "active"
    milestones: tuple[Milestone, ...] = Field(default=(), max_length=30)
    automation_ids: tuple[str, ...] = ()

    _title = field_validator("title")(safe_text)

    @field_validator("intent")
    @classmethod
    def safe_intent(cls, value: str) -> str:
        return safe_text(value) if value.strip() else ""

    @field_validator("milestones")
    @classmethod
    def unique_milestones(cls, milestones: tuple[Milestone, ...]) -> tuple[Milestone, ...]:
        if len({item.id for item in milestones}) != len(milestones) or any(
            not item.id for item in milestones
        ):
            raise ValueError("Milestone identities must be nonempty and unique")
        return milestones


class GoalExecutionStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    BUDGET_REACHED = "budget_reached"
    REVIEW = "review"
    ACCEPTED = "accepted"


class GoalEvidence(AutomationModel):
    run_id: str
    summary: str = Field(min_length=1, max_length=3000)
    call_ids: tuple[str, ...] = Field(default=(), max_length=20)

    _summary = field_validator("summary")(safe_text)


class GoalExecution(AutomationModel):
    id: str
    status: GoalExecutionStatus = GoalExecutionStatus.PAUSED
    criteria: str = Field(min_length=1, max_length=8000)
    turn_limit: int | None = Field(default=None, ge=1, le=100)
    turns: int = Field(default=0, ge=0)
    run_id: str | None = None
    detail: str = Field(default="", max_length=3000)
    evidence: tuple[GoalEvidence, ...] = Field(default=(), max_length=20)

    _criteria = field_validator("criteria")(safe_text)


class GoalCommand(AutomationModel):
    action: Literal["start", "resume", "pause"]
    goal_id: str
    request_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    criteria: str = Field(default="", max_length=8000)
    turn_limit: int | None = Field(default=None, ge=1, le=100)


class GoalReport(AutomationModel):
    status: Literal["continue", "blocked", "complete"]
    summary: str = Field(min_length=1, max_length=3000)
    evidence_call_ids: tuple[str, ...] = Field(default=(), max_length=20)

    _summary = field_validator("summary")(safe_text)


class Goal(GoalInput):
    id: str
    revision: int
    primary_session_id: str | None = None
    deleted_at: datetime | None = None
    execution: GoalExecution | None = None


class Resource(AutomationModel):
    id: str
    title: str
    kind: Literal["report", "note", "trial", "work_product", "file"]
    artifact_ref: str
    session_id: str | None = None
    created_at: datetime
    saved: bool = False
    revision: int = 1
    work_product_id: str | None = None
    status: str = "active"
    published: bool = True
    updated_at: datetime | None = None
    interaction_id: str | None = None
    primary_session_id: str | None = None
    file_path: str | None = None
    file_type: Literal[
        "document", "site", "image", "spreadsheet", "presentation", "code", "file",
    ] | None = None
    byte_size: int | None = Field(default=None, ge=0)
    deleted_at: datetime | None = None


def resource_file_type(path: str) -> Literal[
    "document", "site", "image", "spreadsheet", "presentation", "code", "file",
]:
    extension = PurePosixPath(path).suffix.lower()
    if extension in {".html", ".htm"}:
        return "site"
    if extension in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}:
        return "image"
    if extension in {".csv", ".tsv", ".xlsx", ".xls"}:
        return "spreadsheet"
    if extension in {".pptx", ".ppt"}:
        return "presentation"
    if extension in {".md", ".markdown", ".txt", ".pdf", ".docx", ".doc", ".rtf"}:
        return "document"
    if extension in {".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".json", ".yaml", ".yml", ".sh"}:
        return "code"
    return "file"


class FeedItem(AutomationModel):
    sequence: int
    id: str
    title: str
    result: Result
    detail: str
    created_at: datetime
    resource_id: str | None = None
    occurrence_id: str | None = None
    read: bool = False
    saved: bool = False
    goal_id: str | None = None
    resource_revision: int | None = None
    session_id: str | None = None


class Idea(AutomationModel):
    id: str
    title: str
    reason: str
    intent: str
    category: Literal["work", "life", "learning"]
    dismissed: bool = False
    context_refs: tuple[ContextRef, ...] = ()
    source: Literal["context", "template"] = "template"
    source_label: str = "通用起点"
    outcome: str = ""
    steps: tuple[str, ...] = ()
    input_hint: str = ""
    destination: Literal["task", "goal", "automation"] = "task"
    target_id: str | None = None


class DiscussionInput(AutomationModel):
    context: ContextRef


class PublishInput(AutomationModel):
    session_id: str = Field(min_length=1, max_length=200)
    interaction_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    goal_id: str | None = None
    target_resource_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    _title = field_validator("title")(safe_text)


class Collection(AutomationModel):
    id: str
    title: str = Field(min_length=1, max_length=200)
    revision: int = 1
    _title = field_validator("title")(safe_text)


class GoalDetail(AutomationModel):
    goal: Goal
    resources: tuple[Resource, ...]
    context_resources: tuple[Resource, ...]
    definitions: tuple[Definition, ...]
    occurrences: tuple[Occurrence, ...]


class AssistantState(AutomationModel):
    owner: bool
    status: str
    workspace_source_available: bool = True
    definitions: tuple[Definition, ...] = ()
    occurrences: tuple[Occurrence, ...] = ()
    feed: tuple[FeedItem, ...] = ()
    resources: tuple[Resource, ...] = ()
    goals: tuple[Goal, ...] = ()
    ideas: tuple[Idea, ...] = ()
    collections: tuple[Collection, ...] = ()
    research_available: bool = False
    active_run_id: str | None = None
    active_session_id: str | None = None


class AutomationCommand(AutomationModel):
    request_id: str = Field(min_length=1, max_length=200)
    action: Literal["create", "update", "enable", "pause", "run", "trial"]
    id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    spec: AutomationInput | None = None
    goal_id: str | None = None


class AssistantEdit(AutomationModel):
    action: Literal[
        "save",
        "read",
        "idea",
        "goal",
        "note",
        "edit_resource",
        "visibility",
        "link_resource",
        "collection",
        "collect",
        "trash_resource",
        "restore_resource",
        "rename_resource",
        "trash_goal",
        "restore_goal",
        "delete_collection",
    ]
    id: str = Field(min_length=1, max_length=200)
    value: bool = True
    expected_revision: int | None = None
    goal: GoalInput | None = None
    title: str = Field(default="", max_length=200)
    content: str = Field(default="", max_length=100_000)
    goal_id: str | None = None
    collection_id: str | None = None
    role: Literal["context", "output"] = "context"
    source_session_id: str | None = None
    follow_up_policy: Literal["pause", "keep"] | None = None


class PreferenceCommand(AutomationModel):
    action: Literal["remember", "edit", "forget", "confirm"]
    id: str | None = None
    content: str = Field(default="", max_length=4000)
    scope: Literal["user", "project"] = "project"
    expected_updated_at: str | None = None


class ResourcePage(AutomationModel):
    items: tuple[Resource, ...]
    next_cursor: int | None


class FeedPage(AutomationModel):
    items: tuple[FeedItem, ...]
    next_cursor: int | None
