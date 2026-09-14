"""Canonical contract models for the supervisor queue and the agent handoff.

These Pydantic models are the *single source of truth*. The JSON Schema files next to
this module are generated from them by ``automation/schemas/generate.py``; they are
checked in only so non-Python consumers (editors, other languages, CI linters) can read
the contract without importing this package. ``make schemas-check`` — and
``automation/tests/test_schema_generation.py`` — fail if the checked-in files drift.

Validation happens at the door: ``policy.load_queue`` and ``policy.load_handoff`` parse
raw JSON through these models and then hand the *original* dictionaries to the rest of
the harness, which stays dict-based.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SliceStatus = Literal["queued", "in_progress", "blocked", "deferred", "done", "failed"]
HandoffStatus = Literal["done", "blocked", "failed"]
RepoCleanStatus = Literal["clean", "dirty", "unknown"]
GitMirrorStatus = Literal["mirrored", "not-mirrored", "not-relevant", "not-checked"]
ProofLevel = Literal[
    "doc-only",
    "domain-tested",
    "build-tested",
    "running-app-smoke",
    "flow-verified",
    "screenshot-verified",
    "device-verified",
    "testflight-verified",
]

NonEmptyStr = Annotated[str, Field(min_length=1)]
StrList = list[NonEmptyStr]
NonEmptyStrList = Annotated[StrList, Field(min_length=1)]
Sha256 = Annotated[str, Field(min_length=64, max_length=64)]

# "Optional" in these contracts means the key may be *absent*, never that its value may
# be null: harness code reads optional keys with ``.get(key, "")``. Pydantic does not
# validate defaults, so a default of None makes the key optional without widening the
# accepted type.
Omittable = Field(default=None)


class Contract(BaseModel):
    """Contract documents reject unknown keys: a typo is a contract violation.

    ``strict`` keeps validation JSON-shaped: ``"3"`` is not an integer and ``true`` is
    not a ``1``, exactly as the hand-written schemas these models replaced required.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class QueuePolicy(Contract):
    consecutive_autonomous_limit: Annotated[int, Field(ge=1)]
    handoff_timeout_seconds: Annotated[int, Field(ge=1)]
    agent_command_template: str
    supervisor_owned_paths: NonEmptyStrList


class SliceRecord(Contract):
    slice_id: NonEmptyStr
    title: NonEmptyStr
    status: SliceStatus
    priority: Annotated[int, Field(ge=0)]
    domain: NonEmptyStr
    allowed_paths: NonEmptyStrList
    required_validations: StrList
    depends_on: StrList
    max_files_changed: Annotated[int, Field(ge=1)]
    notes: str
    entry_condition: str = ""
    recommended_unblocker: str = ""
    manual_proof: StrList = Field(default_factory=list)
    required_proof_level: Annotated[ProofLevel, Omittable]


class SliceQueue(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, title="Automation Slice Queue")

    version: Annotated[int, Field(ge=1)]
    policy: QueuePolicy
    slices: list[SliceRecord]


class ContractStatusChange(Contract):
    contract: NonEmptyStr
    before: NonEmptyStr
    after: NonEmptyStr
    proof: StrList


class ProvidedProof(BaseModel):
    """Manual-proof records stay open: which extra fields a proof carries is the
    proving repository's business, not this contract's."""

    model_config = ConfigDict(strict=True)

    proof_type: NonEmptyStr
    path: NonEmptyStr
    commit_sha: NonEmptyStr
    created_at: NonEmptyStr
    slice_id: NonEmptyStr
    sha256: Sha256


class Handoff(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, title="Automation Handoff")

    slice_id: NonEmptyStr
    status: HandoffStatus
    summary: NonEmptyStr
    files_touched: StrList
    validations_passed: StrList
    validations_failed: StrList
    proof_level: ProofLevel
    missing_proof_levels: list[ProofLevel]
    contract_status_changes: list[ContractStatusChange]
    residual_risks: NonEmptyStrList
    recommended_next_slice: str
    recommended_next_reason: str
    repo_clean_status: RepoCleanStatus
    git_mirror_status: GitMirrorStatus
    dirty_paths_outside_scope: StrList
    timestamp: NonEmptyStr
    open_questions: StrList = Field(default_factory=list)
    provided_proofs: list[ProvidedProof] = Field(default_factory=list)
    verified_commit_sha: Annotated[NonEmptyStr, Omittable]


# (model, generated file) pairs: the only place the mapping is declared.
CONTRACTS: tuple[tuple[type[BaseModel], str], ...] = (
    (SliceQueue, "slice.schema.json"),
    (Handoff, "handoff.schema.json"),
)
