"""Generate the checked-in JSON Schemas from the canonical Pydantic models.

    uv run python -m automation.schemas.generate            # write
    uv run python -m automation.schemas.generate --check     # CI gate: fail on drift

The checked-in ``*.schema.json`` files are build artifacts. Editing them by hand is a
mistake the ``--check`` mode is here to catch: edit ``models.py`` and regenerate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel

from .models import CONTRACTS

SCHEMA_DIR = Path(__file__).resolve().parent
BANNER = "Generated from automation/schemas/models.py. Do not edit by hand."


def render_schema(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    # A generated file should say so in a place every consumer already reads.
    ordered = {"$comment": BANNER, **schema}
    return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"


def write_schemas(schema_dir: Path = SCHEMA_DIR) -> list[Path]:
    written = []
    for model, filename in CONTRACTS:
        path = schema_dir / filename
        path.write_text(render_schema(model), encoding="utf-8")
        written.append(path)
    return written


def check_schemas(schema_dir: Path = SCHEMA_DIR) -> list[str]:
    """Return one message per file whose contents differ from the models."""
    stale = []
    for model, filename in CONTRACTS:
        path = schema_dir / filename
        expected = render_schema(model)
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            reason = "missing" if not actual else "out of date"
            stale.append(f"{path.relative_to(schema_dir.parent.parent)}: {reason}")
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if regeneration would change a file")
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    args = parser.parse_args(argv)

    if args.check:
        stale = check_schemas(args.schema_dir)
        if stale:
            print("Checked-in JSON Schemas are out of date:", file=sys.stderr)
            for message in stale:
                print(f"  - {message}", file=sys.stderr)
            print("Run: uv run python -m automation.schemas.generate", file=sys.stderr)
            return 1
        print("JSON Schemas match automation/schemas/models.py.")
        return 0

    for path in write_schemas(args.schema_dir):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
