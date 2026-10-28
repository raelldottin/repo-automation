"""Contract tests for the Hindsight memory adapter (RA-CP-003).

Every call goes to an in-process fake server on 127.0.0.1 or to a patched opener; nothing here
reaches a live Hindsight, writes to ``~/.hermes`` or touches a memory store.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from automation.context import memory
from automation.context.build_context import build_context_bundle
from automation.supervisor import policy

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "automation/examples"
CONFIG = EXAMPLES / "memory-config.json"
RECALL_RESPONSE = json.loads((EXAMPLES / "memory-recall-response.json").read_text(encoding="utf-8"))
SECRET = "test-key-not-a-real-credential"


class FakeHindsight:
    """Answers every POST with a canned (status, body) after an optional delay, and records requests."""

    def __init__(self) -> None:
        self.status = 200
        self.body: Any = RECALL_RESPONSE
        self.delay = 0.0
        self.headers: dict[str, str] = {}
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                fake.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": json.loads(self.rfile.read(length))}
                )
                time.sleep(fake.delay)
                payload = fake.body if isinstance(fake.body, bytes) else json.dumps(fake.body).encode()
                try:
                    self.send_response(fake.status)
                    for name, value in fake.headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    server = FakeHindsight()
    yield server
    server.server.shutdown()
    server.server.server_close()


def make_config(endpoint: str, **overrides: Any) -> memory.MemoryConfig:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["endpoint"] = endpoint
    data["timeout_seconds"] = 2
    for key, value in overrides.items():
        if key in data["budgets"]:
            data["budgets"][key] = value
        else:
            data[key] = value
    return memory.MemoryConfig.model_validate(data)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_API_KEY", SECRET)


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_example_config_loads():
    config = memory.load_config(CONFIG)
    assert config.projects["example-project"].bank_id == "example-bank"
    assert config.projects["example-project"].operations == ["recall"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://127.0.0.1",
        "http://hindsight.example.com",
        "https://user:pw@hindsight.example.com",
        "https://hindsight.example.com/?key=x",
    ],
)
def test_endpoint_rules(endpoint):
    with pytest.raises(ValueError):
        make_config(endpoint)
    make_config("https://hindsight.example.com")
    make_config("http://[::1]:8888")
    make_config("http://localhost:8888")


def test_bank_ids_must_be_unique():
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["projects"]["other-project"]["bank_id"] = "example-bank"
    with pytest.raises(ValueError, match="exactly one project"):
        memory.MemoryConfig.model_validate(data)


def test_recall_keeps_only_verified_provenance(fake, api_key):
    config = make_config(fake.endpoint)

    result = memory.recall(config, "example-project", "example-bank", "focus handoff", REPO_ROOT)

    assert [item["id"] for item in result["results"]] == ["fact-001", "fact-002"]
    assert result["results"][0]["evidence_ref"] == "handoff:automation/examples/example-handoff.json"
    assert result["rejected"] == [
        {"id": "fact-003", "reason": "missing provenance: source_path, source_sha256, evidence_ref"},
        {"id": "fact-004", "reason": "source changed since retained"},
        {"id": "fact-005", "reason": "source missing"},
        {"id": "fact-006", "reason": "source_path escapes the repository"},
    ]
    assert result["status"] == "ok" and result["notice"] == memory.UNTRUSTED_NOTICE
    request = fake.requests[0]
    assert request["path"] == "/v1/default/banks/example-bank/memories/recall"
    assert request["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert request["body"] == {"query": "focus handoff", "types": ["world", "experience"], "budget": "low", "max_tokens": 1024}


def test_changed_source_is_rejected(fake, api_key, tmp_path):
    shutil.copytree(EXAMPLES, tmp_path / "automation/examples")
    config = make_config(fake.endpoint)
    assert len(memory.recall(config, "example-project", "example-bank", "q", tmp_path)["results"]) == 2

    with (tmp_path / "automation/examples/example-handoff.json").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    result = memory.recall(config, "example-project", "example-bank", "q", tmp_path)

    assert [item["id"] for item in result["results"]] == ["fact-002"]
    assert {"id": "fact-001", "reason": "source changed since retained"} in result["rejected"]


def test_ephemeral_cwd(fake, api_key, tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    with pytest.raises(FileNotFoundError):
        os.getcwd()

    config = memory.load_config(CONFIG).model_copy(update={"endpoint": fake.endpoint})
    result = memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT)

    assert [item["id"] for item in result["results"]] == ["fact-001", "fact-002"]


def test_timeout_is_unavailable(fake, api_key):
    fake.delay = 1.0
    config = make_config(fake.endpoint, timeout_seconds=0.2)

    with pytest.raises(memory.MemoryUnavailable, match="timeout"):
        memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT)


def test_connection_refused_is_unavailable(api_key):
    config = make_config(f"http://127.0.0.1:{closed_port()}")

    with pytest.raises(memory.MemoryUnavailable, match="connection refused"):
        memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT)


def test_egress_denied_is_unavailable(api_key, monkeypatch):
    def deny(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError(PermissionError(1, "Operation not permitted"))

    monkeypatch.setattr(memory._OPENER, "open", deny)
    config = make_config("https://hindsight.example.com")

    with pytest.raises(memory.MemoryUnavailable, match="egress denied"):
        memory.recall(config, "example-project", "example-bank", "q", REPO_ROOT)


@pytest.mark.parametrize(
    ("project", "bank_id", "operation", "message"),
    [
        ("example-project", "other-bank", "recall", "not granted to project"),
        ("unknown-project", "example-bank", "recall", "no memory bank"),
        ("example-project", "example-bank", "retain", "retain is not granted"),
        ("example-project", "example-bank", "reflect", "reflect is not granted"),
    ],
)
def test_unauthorized_calls_never_reach_the_server(fake, api_key, project, bank_id, operation, message):
    config = make_config(fake.endpoint)
    calls = {
        "recall": lambda: memory.recall(config, project, bank_id, "q", REPO_ROOT),
        "retain": lambda: memory.retain(config, project, bank_id, REPO_ROOT, "automation/examples/example-slices.json", "c"),
        "reflect": lambda: memory.reflect(config, project, bank_id, "q"),
    }

    with pytest.raises(memory.MemoryAccessError, match=message):
        calls[operation]()

    assert fake.requests == []


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_access_errors(fake, api_key, status):
    fake.status = status
    fake.body = {"detail": "nope"}

    with pytest.raises(memory.MemoryAccessError, match=f"HTTP {status}"):
        memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT)


def test_missing_credential_names_the_variable_only(fake, monkeypatch):
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)

    with pytest.raises(memory.MemoryAccessError, match="HINDSIGHT_API_KEY is not set"):
        memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT)
    assert fake.requests == []


def test_redirects_are_not_followed(fake, api_key):
    fake.status = 307
    fake.headers = {"Location": "http://127.0.0.1:1/elsewhere"}

    with pytest.raises(memory.MemoryUnavailable, match="HTTP 307"):
        memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT)
    assert len(fake.requests) == 1


def test_empty_recall(fake, api_key):
    fake.body = {"results": []}

    result = memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT)

    assert result["status"] == "empty"
    assert result["results"] == [] and result["rejected"] == [] and result["truncated"] is False


@pytest.mark.parametrize("body", [b"not json", {"results": "nope"}, {"unexpected": []}])
def test_malformed_response_is_unavailable(fake, api_key, body):
    fake.body = body

    with pytest.raises(memory.MemoryUnavailable):
        memory.recall(make_config(fake.endpoint), "example-project", "example-bank", "q", REPO_ROOT)


def test_oversized_response_is_unavailable(fake, api_key):
    with pytest.raises(memory.MemoryUnavailable, match="max_response_bytes"):
        memory.recall(make_config(fake.endpoint, max_response_bytes=64), "example-project", "example-bank", "q", REPO_ROOT)


def test_budget_truncation():
    budgets = make_config("http://127.0.0.1:1", max_results=1, max_result_chars=20, max_total_chars=20).budgets

    result = memory.shape_recall(RECALL_RESPONSE, REPO_ROOT, budgets)

    assert [item["id"] for item in result["results"]] == ["fact-001"]
    assert len(result["results"][0]["text"]) == 20
    assert result["dropped"] == 1 and result["truncated"] is True


def test_total_char_budget_spans_results():
    budgets = make_config("http://127.0.0.1:1", max_result_chars=60, max_total_chars=80).budgets

    result = memory.shape_recall(RECALL_RESPONSE, REPO_ROOT, budgets)

    assert [len(item["text"]) for item in result["results"]] == [60, 20]
    assert sum(len(item["text"]) for item in result["results"]) == 80


def test_shape_recall_is_deterministic():
    budgets = memory.load_config(CONFIG).budgets
    first = json.dumps(memory.shape_recall(RECALL_RESPONSE, REPO_ROOT, budgets), sort_keys=True)
    reordered = {"results": list(reversed(RECALL_RESPONSE["results"]))}
    rejected_first = memory.shape_recall(RECALL_RESPONSE, REPO_ROOT, budgets)["rejected"]

    assert json.dumps(memory.shape_recall(RECALL_RESPONSE, REPO_ROOT, budgets), sort_keys=True) == first
    assert memory.shape_recall(reordered, REPO_ROOT, budgets)["rejected"] == rejected_first


def test_retain_sends_digest_and_evidence(fake, api_key):
    fake.body = {"success": True, "bank_id": "other-bank", "items_count": 1, "async": False}
    source = "automation/examples/example-slices.json"

    receipt = memory.retain(make_config(fake.endpoint), "other-project", "other-bank", REPO_ROOT, source, "commit:abc1234")

    item = fake.requests[0]["body"]["items"][0]
    assert fake.requests[0]["path"] == "/v1/default/banks/other-bank/memories"
    assert fake.requests[0]["body"]["async"] is False
    assert item["metadata"] == {
        "source_path": source,
        "source_sha256": memory.sha256_file(REPO_ROOT / source),
        "evidence_ref": "commit:abc1234",
    }
    assert receipt["source_sha256"] == item["metadata"]["source_sha256"]
    assert receipt["server_success"] is True


@pytest.mark.parametrize(
    ("source", "evidence", "message"),
    [("../outside.md", "c", "escapes"), ("/etc/hosts", "c", "repo-relative"), ("README.md", "", "evidence reference")],
)
def test_retain_refuses_bad_sources(fake, api_key, source, evidence, message):
    with pytest.raises(memory.MemoryAccessError, match=message):
        memory.retain(make_config(fake.endpoint), "other-project", "other-bank", REPO_ROOT, source, evidence)
    assert fake.requests == []


def test_retain_respects_request_budget(fake, api_key):
    config = make_config(fake.endpoint, max_request_bytes=100)

    with pytest.raises(memory.MemoryAccessError, match="max_request_bytes"):
        memory.retain(config, "other-project", "other-bank", REPO_ROOT, "automation/examples/example-slices.json", "c")
    assert fake.requests == []


def test_reflect_is_marked_generated_and_bounded(fake, api_key):
    fake.body = {"text": "x" * 5000, "usage": {"total_tokens": 9}}

    result = memory.reflect(make_config(fake.endpoint), "other-project", "other-bank", "summarize")

    assert result["kind"] == "generated-reflection"
    assert len(result["text"]) == 2000 and result["truncated"] is True
    assert fake.requests[0]["body"] == {"query": "summarize", "budget": "low", "max_tokens": 512}


def bundle(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir(exist_ok=True)
    shutil.copy2(EXAMPLES / "example-handoff.json", handoff_dir / "20260421T153000Z-today-nonfocus-add-to-focus.json")
    queue = policy.load_queue(EXAMPLES / "example-slices.json")
    selected = policy.select_next_slice(queue)
    assert selected is not None
    return build_context_bundle(
        repo_root=REPO_ROOT,
        queue_path=EXAMPLES / "example-slices.json",
        handoff_dir=handoff_dir,
        slice_id=selected["slice_id"],
        max_doc_chars=200,
        **kwargs,
    )


PRE_MEMORY_KEYS = {
    "generated_at",
    "policy_sentence",
    "repo",
    "slice",
    "queue",
    "validation_ownership",
    "previous_handoff",
    "previous_handoff_summary",
    "execution_constraints",
    "handoff_template",
    "documents",
}


def test_bundle_without_memory_is_unchanged(tmp_path):
    plain = bundle(tmp_path)
    explicit_none = bundle(tmp_path, memory_config=None, memory_project=None)

    assert set(plain) == PRE_MEMORY_KEYS
    plain.pop("generated_at")
    explicit_none.pop("generated_at")
    assert plain == explicit_none


def test_bundle_with_memory_has_bounded_section(fake, api_key, tmp_path):
    section = bundle(tmp_path, memory_config=make_config(fake.endpoint), memory_project="example-project")["memory"]

    assert section["source"] == "hindsight" and section["bank_id"] == "example-bank"
    assert [item["id"] for item in section["results"]] == ["fact-001", "fact-002"]
    assert fake.requests[0]["body"]["query"] == "Add regression coverage for Continue Add to Focus interaction"


def test_bundle_records_unavailable_memory(api_key, tmp_path):
    config = make_config(f"http://127.0.0.1:{closed_port()}")

    section = bundle(tmp_path, memory_config=config, memory_project="example-project")["memory"]

    assert section["status"] == "unavailable" and section["error"] == "connection refused"
    assert section["results"] == []


def test_bundle_refuses_unmapped_project(fake, api_key, tmp_path):
    with pytest.raises(memory.MemoryAccessError):
        bundle(tmp_path, memory_config=make_config(fake.endpoint), memory_project="unknown-project")
    with pytest.raises(policy.ConfigError):
        bundle(tmp_path, memory_config=make_config(fake.endpoint))
    assert fake.requests == []
