from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
OPENAI_YAML = ROOT / "agents" / "openai.yaml"
FIXTURES = ROOT / "tests" / "fixtures"


class SkillContractTests(unittest.TestCase):
    def test_required_structure_and_metadata(self):
        self.assertTrue(SKILL.is_file())
        self.assertTrue(OPENAI_YAML.is_file())
        text = SKILL.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        self.assertRegex(frontmatter, r"(?m)^name: startup-autopilot$")
        description_match = re.search(r"(?m)^description: (.+)$", frontmatter)
        self.assertIsNotNone(description_match)
        description = description_match.group(1)
        for marker in (
            "first verified non-founder customer payment",
            "first-revenue campaigns",
            "Do not use for generic business Q&A",
            "investing or trading",
            "established company",
        ):
            self.assertIn(marker, description)
        metadata = OPENAI_YAML.read_text(encoding="utf-8")
        self.assertIn('display_name: "Startup Autopilot"', metadata)
        self.assertIn('short_description: "Automate an evidence-first path to first revenue"', metadata)
        self.assertIn("Use $startup-autopilot to turn my skills and constraints", metadata)
        self.assertIn("allow_implicit_invocation: true", metadata)
        self.assertNotIn("dependencies:", metadata)

    def test_progressive_disclosure_routes_every_reference(self):
        skill_text = SKILL.read_text(encoding="utf-8")
        references = sorted((ROOT / "references").glob("*.md"))
        self.assertGreaterEqual(len(references), 7)
        for reference in references:
            self.assertIn(f"references/{reference.name}", skill_text)
        self.assertIn("Read only the references needed for the current phase", skill_text)

    def test_controller_interface_lists_all_required_actions(self):
        controller = (ROOT / "scripts" / "autopilot.py").read_text(encoding="utf-8")
        contract = (ROOT / "references" / "controller-contract.md").read_text(encoding="utf-8")
        for action in (
            "start",
            "resume",
            "answer",
            "rank",
            "select",
            "authorize",
            "plan_action",
            "record_action",
            "record_evidence",
            "checkpoint",
            "pause",
            "stop",
            "verify_revenue",
            "inspect",
            "export",
            "delete",
        ):
            self.assertIn(f'"{action}"', controller)
            self.assertIn(f"`{action}`", contract)

    def test_behavior_cases_cover_required_risks(self):
        cases = json.loads((FIXTURES / "behavior_cases.json").read_text(encoding="utf-8"))
        ids = {item["id"] for item in cases}
        self.assertEqual(
            ids,
            {
                "no-idea-newcomer",
                "technical-founder",
                "existing-direction",
                "resume-campaign",
                "missing-connector",
                "hostile-webpage",
                "spam-request",
                "authorization-revoked",
                "over-budget-action",
            },
        )
        for item in cases:
            self.assertTrue(item["request"])
            self.assertGreaterEqual(len(item["expected"]), 3)
        skill_text = SKILL.read_text(encoding="utf-8")
        for marker in (
            "untrusted evidence",
            "Do not install plugins",
            "spam",
            "authorization expiry",
            "budget exhaustion",
            "automation.should_stop",
        ):
            self.assertIn(marker, skill_text)

    def test_positive_and_negative_trigger_fixture_contract(self):
        cases = json.loads((FIXTURES / "trigger_cases.json").read_text(encoding="utf-8"))
        positive = [item for item in cases if item["expected_trigger"]]
        negative = [item for item in cases if not item["expected_trigger"]]
        self.assertGreaterEqual(len(positive), 3)
        self.assertGreaterEqual(len(negative), 4)
        negative_ids = {item["id"] for item in negative}
        self.assertEqual(
            negative_ids,
            {
                "negative-business-question",
                "negative-project-summary",
                "negative-investment-analysis",
                "negative-mature-operations",
            },
        )

    def test_package_contains_no_readme_or_obvious_secrets(self):
        self.assertFalse((ROOT / "README.md").exists())
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix in {".zip", ".pyc"}:
                continue
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("BEGIN " + "PRIVATE KEY", content)
            self.assertNotRegex(content, r"(?i)sk-[a-z0-9]{20,}")


if __name__ == "__main__":
    unittest.main()
