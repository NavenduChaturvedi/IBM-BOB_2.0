"""Shared data model passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FileKind(str, Enum):
    PYTHON = "python"
    MIGRATION = "migration"
    DOCKERFILE = "dockerfile"
    REQUIREMENTS = "requirements"
    CONFIG = "config"
    K8S = "k8s"
    WORKFLOW = "workflow"
    TEST = "test"
    OTHER = "other"


class ChangeType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    BODY_CHANGED = "body_changed"
    SIGNATURE_CHANGED = "signature_changed"


class Severity(str, Enum):
    HIGH = "high"
    MED = "med"
    LOW = "low"


# --- Diff parser output -----------------------------------------------------

@dataclass
class ChangedFile:
    path: str
    status: str  # git name-status letter: A, M, D, R
    kind: FileKind
    old_path: str | None = None  # set for renames
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    parse_error: str | None = None  # set when a .py file doesn't parse; symbols unknown


@dataclass
class ChangedSymbol:
    file: str
    module: str  # dotted module name, e.g. app.payments.pricing
    qualname: str  # e.g. calculate_discount or Cart.total
    kind: str  # function | class | method
    change: ChangeType
    lineno: int
    old_sig: str | None = None
    new_sig: str | None = None

    @property
    def fq_name(self) -> str:
        return f"{self.module}.{self.qualname}"


@dataclass
class DiffResult:
    base: str
    head: str
    base_sha: str
    head_sha: str
    commits: list[str]
    files: list[ChangedFile]
    symbols: list[ChangedSymbol]


# --- Blast radius output ----------------------------------------------------

@dataclass
class Importer:
    file: str
    line: int
    module: str  # the changed module being imported


@dataclass
class CallShape:
    """How a call site passes arguments; lets the checklist spot calls a new signature breaks."""
    n_positional: int
    keywords: list[str]
    has_star_args: bool
    has_star_kwargs: bool
    text: str


@dataclass
class Caller:
    file: str
    line: int
    enclosing_symbol: str | None  # qualname within file; None when used at module level
    hop: int  # 1 = uses the changed symbol directly, 2 = calls a hop-1 caller
    via_symbol: str  # fq name of the symbol being called at this site
    root_symbol: str  # fq name of the changed symbol this traces back to
    usage: str = "call"  # call | reference
    confidence: str = "high"  # high | low (method matched by attribute name only)
    call: CallShape | None = None


@dataclass
class BlastRadius:
    importers: list[Importer]
    callers: list[Caller]
    files_scanned: int = 0
    truncated: bool = False  # True if the hop-2 fan-out cap was hit

    def callers_of(self, fq_name: str, hop: int = 1) -> list[Caller]:
        return [c for c in self.callers if c.via_symbol == fq_name and c.hop == hop]


# --- Checklist output -------------------------------------------------------

@dataclass
class ChecklistItem:
    rule_id: str
    severity: Severity
    text: str
    evidence: list[str] = field(default_factory=list)


# --- Artifact scan / runbook ------------------------------------------------

@dataclass
class MigrationOp:
    op: str  # add_column, drop_table, AddField, RunPython, ...
    target: str  # "payments.refund_status", "payments", "Payment.refund_status"
    risky: bool  # breaks old code or loses data (drops, renames, NOT NULL without default, raw SQL)
    note: str = ""  # e.g. "nullable", "NOT NULL, no server_default"

    def __str__(self) -> str:
        return f"{self.op} {self.target}" + (f" ({self.note})" if self.note else "")


@dataclass
class Migration:
    tool: str  # alembic | django
    file: str
    revision: str  # alembic revision id, or django migration name
    down_revision: str | None  # None = first migration
    has_downgrade: bool
    in_diff: bool
    app: str | None = None  # django app label
    description: str = ""
    upgrade_ops: list[MigrationOp] = field(default_factory=list)
    downgrade_ops: list[MigrationOp] = field(default_factory=list)


@dataclass
class K8sDeployment:
    name: str
    namespace: str
    file: str
    kind: str = "Deployment"  # Deployment | StatefulSet | DaemonSet
    containers: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)


@dataclass
class FeatureFlag:
    name: str
    file: str
    line: int


@dataclass
class EnvVar:
    name: str
    file: str
    line: int
    required: bool  # os.environ["X"] -> KeyError if unset; getenv/get -> optional
    at_import: bool  # read at module level, so a missing value fails on startup


@dataclass
class RollbackFacts:
    """Everything the runbook is allowed to reference. Bob output is validated against this."""
    base_sha: str
    head_sha: str
    commits: list[str]
    migrations: list[Migration] = field(default_factory=list)
    k8s_deployments: list[K8sDeployment] = field(default_factory=list)
    dockerfiles: list[str] = field(default_factory=list)
    compose_services: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    feature_flags: list[FeatureFlag] = field(default_factory=list)
    new_env_vars: list[EnvVar] = field(default_factory=list)
    declared_env: dict[str, list[str]] = field(default_factory=dict)  # env var -> files that set it
    test_files: list[str] = field(default_factory=list)  # test files at head (for pytest commands)


@dataclass
class Runbook:
    markdown: str
    source: str  # "template" | "bob (validated)" | "template (bob rejected)"
    rejections: list[str] = field(default_factory=list)


@dataclass
class Report:
    diff: DiffResult
    blast: BlastRadius
    checklist: list[ChecklistItem]
    facts: RollbackFacts
    runbook: Runbook
