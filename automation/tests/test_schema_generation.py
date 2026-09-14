from __future__ import annotations

import json
import unittest
from pathlib import Path

from automation.schemas import generate, models
from automation.supervisor import policy

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "automation/schemas"


class SchemaGenerationTests(unittest.TestCase):
    """The checked-in JSON Schemas are artifacts; the models are the contract."""

    def test_checked_in_schemas_match_the_models(self) -> None:
        stale = generate.check_schemas(SCHEMA_DIR)
        self.assertEqual(
            stale,
            [],
            "Checked-in JSON Schemas are out of date. Run: uv run python -m automation.schemas.generate",
        )

    def test_every_contract_model_has_a_generated_file(self) -> None:
        generated = {filename for _, filename in models.CONTRACTS}
        on_disk = {path.name for path in SCHEMA_DIR.glob("*.schema.json")}
        self.assertEqual(on_disk, generated)

    def test_generated_schema_declares_itself_generated(self) -> None:
        for _, filename in models.CONTRACTS:
            schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
            self.assertIn("Do not edit by hand", schema["$comment"], filename)

    def test_example_documents_validate_against_the_models(self) -> None:
        queue = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
        handoff = json.loads((REPO_ROOT / "automation/examples/example-handoff.json").read_text(encoding="utf-8"))

        self.assertTrue(policy.validate_document(queue, models.SliceQueue).is_valid)
        self.assertTrue(policy.validate_document(handoff, models.Handoff).is_valid)

    def test_optional_keys_may_be_absent_but_not_null(self) -> None:
        handoff = json.loads((REPO_ROOT / "automation/examples/example-handoff.json").read_text(encoding="utf-8"))
        handoff.pop("open_questions", None)
        self.assertTrue(policy.validate_document(handoff, models.Handoff).is_valid)

        handoff["open_questions"] = None
        result = policy.validate_document(handoff, models.Handoff)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("$.open_questions" in error for error in result.errors), result.errors)

    def test_validation_rejects_json_type_coercion(self) -> None:
        """A string is not an integer: strict models keep the JSON contract honest."""
        queue = json.loads((REPO_ROOT / "automation/examples/example-slices.json").read_text(encoding="utf-8"))
        queue["version"] = "1"

        result = policy.validate_document(queue, models.SliceQueue)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("$.version" in error for error in result.errors), result.errors)


if __name__ == "__main__":
    unittest.main()
