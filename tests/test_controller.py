from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import timedelta
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import autopilot  # noqa: E402
import validate_state  # noqa: E402


class Harness:
    def __init__(self, root: Path, campaign_id: str = "test-campaign"):
        self.root = root
        self.campaign_id = campaign_id

    def call(self, operation: str, **values):
        request = {"command": operation, **values}
        if operation != "start":
            request.setdefault("campaign_id", self.campaign_id)
        return autopilot.dispatch(self.root, request)

    def start(self):
        return self.call("start", campaign_id=self.campaign_id, goal="Earn the first lawful customer payment")

    def complete_discovery(self):
        answers = {
            "outcome": "One verified external customer payment",
            "success_proof": "A settled payment tied to a bounded offer",
            "constraints": "Ten hours and a small test budget",
            "assets": "Python automation and technical writing",
            "past_attempts": "Shared tutorials without a specific offer",
            "boundaries": "No employer data, spam, or regulated advice",
            "reachable_buyers": "Independent software consultants",
            "work_preferences": "Async delivery and short interviews",
            "market_tolerance": "Ten direct rejections",
            "execution_system": "One bounded action per weekday",
        }
        result = None
        for question_id in autopilot.QUESTION_IDS:
            result = self.call("answer", question_id=question_id, answer=answers[question_id])
        return result

    def opportunities(self, *, demand_kind: str = "demand_evidence"):
        values = []
        for index in range(10):
            values.append(
                {
                    "id": f"direction-{index}",
                    "title": f"Automation diagnostic {index}",
                    "summary": "A paid diagnostic for consultants with repetitive delivery work",
                    "buyer": "Independent software consultants",
                    "deliverable": "A bounded automation opportunity report",
                    "reachable_channel": "Opt-in consultant community",
                    "smallest_test": "Offer three interviews a one-page sample",
                    "principal_risk": "The report may not justify its price",
                    "compliance": {"status": "allowed", "rationale": "Lawful general business automation"},
                    "scores": {
                        "demand_evidence": 80 - index,
                        "reachable_buyers": 75,
                        "asset_fit": 90,
                        "validation_speed": 85,
                        "unit_economics": 70,
                        "risk_control": 80,
                    },
                    "evidence": [
                        {
                            "kind": demand_kind,
                            "claim": "Consultants describe repeated manual delivery work",
                            "label": "attributed",
                            "source": f"mock://market/{index}",
                            "observed_at": "2026-09-01",
                        }
                    ],
                }
            )
        return values

    def rank_and_select(self, *, demand_kind: str = "demand_evidence"):
        ranked = self.call("rank", opportunities=self.opportunities(demand_kind=demand_kind))
        selected = ranked["ranking"][0]["id"]
        self.call("select", opportunity_id=selected)
        return selected

    def positive_signal(self):
        return self.call(
            "record_evidence",
            evidence={
                "kind": "direct_market_signal",
                "claim": "A qualified buyer requested the priced diagnostic",
                "label": "confirmed",
                "source": "mock://buyer-response/1",
                "observed_at": autopilot.iso_now(),
                "outcome": "positive",
            },
        )

    def offer(self):
        return {
            "id": "automation-diagnostic",
            "buyer": "Independent software consultants",
            "problem": "Repeated manual delivery tasks consume billable time",
            "deliverable": "A prioritized automation diagnostic",
            "price": "25.00",
            "currency": "USD",
            "fulfillment_standard": "Three evidenced opportunities and one implementation outline",
            "payment_proof": "Settled live provider event",
        }

    def contract(self, *, total=1000, daily=500, action_limit=3, expired=False):
        now = autopilot.utc_now()
        if expired:
            starts = now - timedelta(days=2)
            expires = now - timedelta(days=1)
        else:
            starts = now - timedelta(minutes=1)
            expires = now + timedelta(days=7)
        return {
            "objective": "Validate and sell one automation diagnostic",
            "allowed_channels": ["mock-market"],
            "allowed_accounts": ["mock-account"],
            "allowed_content_types": ["offer-announcement", "buyer-reply"],
            "allowed_action_kinds": ["local", "build", "publish", "message"],
            "data_boundaries": ["No private client or employer data"],
            "forbidden_actions": ["delete", "payment", "purchase", "sensitive_transfer"],
            "total_budget_cents": total,
            "daily_budget_cents": daily,
            "daily_action_limit": action_limit,
            "currency": "USD",
            "revenue_goal_amount": "1.00",
            "starts_at": starts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "expires_at": expires.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "stop_conditions": ["Authorization expires", "Budget cap reached", "User revokes permission"],
        }

    def advance_to_authorized(self, **contract_overrides):
        self.start()
        self.complete_discovery()
        self.rank_and_select()
        self.positive_signal()
        contract = self.contract(**contract_overrides)
        return self.call("authorize", offer=self.offer(), contract=contract, user_confirmed=True)

    def action(self, index=1, *, kind="local", cost=0, content=None, key=None):
        external = kind in autopilot.EXTERNAL_ACTION_KINDS
        return {
            "kind": kind,
            "target": f"target-{index}",
            "channel": "mock-market" if external else "",
            "account": "mock-account" if external else "",
            "content_type": "offer-announcement" if kind in {"publish", "message"} else "",
            "content": content if content is not None else f"bounded action {index}",
            "estimated_cost_cents": cost,
            "idempotency_key": key or f"action-{index}",
        }

    def advance_to_operate(self):
        self.advance_to_authorized()
        self.call("checkpoint", reason="Validated artifact is ready", transition_to="launch", artifacts_ready=True)
        planned = self.call("plan_action", action=self.action(1, kind="publish"))
        self.call("record_action", action_digest=planned["action"]["digest"], outcome="success", actual_cost_cents=0)
        self.call("checkpoint", reason="Launch is externally visible", transition_to="operate", launch_verified=True)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.h = Harness(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def assert_error(self, code, callable_):
        with self.assertRaises(autopilot.ControllerError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)

    def test_state_transitions_and_artifacts_directory(self):
        started = self.h.start()
        self.assertEqual(started["campaign"]["phase"], "intake")
        discovered = self.h.complete_discovery()
        self.assertEqual(discovered["campaign"]["phase"], "opportunities")
        selected = self.h.rank_and_select()
        self.assertTrue(selected.startswith("direction-"))
        self.assertEqual(self.h.positive_signal()["campaign"]["phase"], "offer")
        authorized = self.h.call("authorize", offer=self.h.offer(), contract=self.h.contract(), user_confirmed=True)
        self.assertEqual(authorized["campaign"]["phase"], "build")
        self.assertTrue((self.root / self.h.campaign_id / "artifacts").is_dir())

    def test_demand_score_is_capped_without_demand_evidence(self):
        self.h.start()
        self.h.complete_discovery()
        result = self.h.call("rank", opportunities=self.h.opportunities(demand_kind="market_context"))
        for item in result["ranking"]:
            self.assertFalse(item["demand_evidence_present"])
            self.assertEqual(item["scores"]["demand_evidence"], 20)

    def test_authorization_requires_explicit_user_confirmation_record(self):
        self.h.start()
        self.h.complete_discovery()
        self.h.rank_and_select()
        self.h.positive_signal()
        self.assert_error(
            "authorization_confirmation_required",
            lambda: self.h.call("authorize", offer=self.h.offer(), contract=self.h.contract()),
        )
        authorized = self.h.call(
            "authorize",
            offer=self.h.offer(),
            contract=self.h.contract(),
            user_confirmed=True,
        )
        self.assertIsNotNone(authorized["campaign"]["authorization"]["approved_at"])

    def test_disallowed_business_direction_is_rejected(self):
        self.h.start()
        self.h.complete_discovery()
        opportunities = self.h.opportunities()
        opportunities[0]["compliance"] = {"status": "rejected", "rationale": "Spam is outside scope"}
        self.assert_error("business_not_allowed", lambda: self.h.call("rank", opportunities=opportunities))

    def test_path_traversal_and_secret_fields_are_rejected(self):
        self.assert_error(
            "invalid_campaign_id",
            lambda: autopilot.dispatch(self.root, {"command": "start", "campaign_id": "../escape", "goal": "x"}),
        )
        self.assert_error(
            "secret_rejected",
            lambda: autopilot.dispatch(
                self.root,
                {"command": "start", "campaign_id": "safe", "goal": "x", "api_key": "must-not-be-stored"},
            ),
        )

    def test_symlinked_or_junction_state_root_is_rejected(self):
        target = Path(self.temp.name) / "target"
        link = Path(self.temp.name) / "link"
        target.mkdir()
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("directory links are not available in this environment")
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                text=True,
                capture_output=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest("directory junctions are not available in this environment")
        try:
            self.assert_error("unsafe_path", lambda: autopilot.CampaignStore(link, "campaign"))
        finally:
            if os.path.lexists(link):
                os.rmdir(link)

    def test_authorization_tamper_pauses_campaign(self):
        self.h.advance_to_authorized()
        state_path = self.root / self.h.campaign_id / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["authorization"]["allowed_channels"].append("attacker-channel")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assert_error("authorization_not_current", lambda: self.h.call("plan_action", action=self.h.action()))
        summary = self.h.call("inspect")["campaign"]
        self.assertEqual(summary["status"], "paused")
        self.assertEqual(summary["pause_reason"], "authorization_digest_mismatch")

    def test_local_pending_action_does_not_consume_external_daily_limit(self):
        self.h.advance_to_authorized(action_limit=1)
        self.h.call("plan_action", action=self.h.action(1, kind="local"))
        first_external = self.h.call("plan_action", action=self.h.action(2, kind="publish"))
        self.assertTrue(first_external["ok"])
        self.assert_error(
            "daily_action_limit",
            lambda: self.h.call("plan_action", action=self.h.action(3, kind="message")),
        )
        self.assertEqual(self.h.call("inspect")["campaign"]["pause_reason"], "daily_external_action_limit")

    def test_budget_reservation_prevents_concurrent_overspend(self):
        self.h.advance_to_authorized(total=10, daily=10)
        self.h.call("plan_action", action=self.h.action(1, cost=10))
        self.assert_error("budget_exceeded", lambda: self.h.call("plan_action", action=self.h.action(2, cost=1)))
        summary = self.h.call("inspect")["campaign"]
        self.assertEqual(summary["budget"]["reserved_cents"], 10)
        self.assertEqual(summary["status"], "paused")

    def test_idempotency_replay_and_collision(self):
        self.h.advance_to_authorized()
        action = self.h.action(1, key="stable-key")
        first = self.h.call("plan_action", action=action)
        replay = self.h.call("plan_action", action=action)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        collision = self.h.action(1, key="stable-key", content="different content")
        self.assert_error("idempotency_collision", lambda: self.h.call("plan_action", action=collision))
        self.assertEqual(self.h.call("inspect")["campaign"]["status"], "paused")

    def test_hard_confirmation_kind_cannot_be_recorded_without_confirmation(self):
        self.h.start()
        self.h.complete_discovery()
        self.h.rank_and_select()
        self.h.positive_signal()
        contract = self.h.contract()
        contract["allowed_action_kinds"].append("purchase")
        contract["forbidden_actions"].remove("purchase")
        self.h.call("authorize", offer=self.h.offer(), contract=contract, user_confirmed=True)
        planned = self.h.call("plan_action", action=self.h.action(1, kind="purchase"))
        self.assertEqual(planned["action"]["status"], "pending_confirmation")
        self.assert_error(
            "confirmation_required",
            lambda: self.h.call(
                "record_action",
                action_digest=planned["action"]["digest"],
                outcome="success",
                actual_cost_cents=0,
            ),
        )
        recorded = self.h.call(
            "record_action",
            action_digest=planned["action"]["digest"],
            outcome="success",
            actual_cost_cents=0,
            confirmed=True,
        )
        self.assertEqual(recorded["action"]["outcome"], "success")

    def test_outside_authorization_pauses_before_external_action(self):
        self.h.advance_to_authorized()
        action = self.h.action(1, kind="publish")
        action["channel"] = "not-authorized"
        self.assert_error("outside_authorization", lambda: self.h.call("plan_action", action=action))
        summary = self.h.call("inspect")["campaign"]
        self.assertEqual(summary["status"], "paused")
        self.assertEqual(summary["pause_reason"], "unauthorized_channel")

    def test_unknown_outcome_pause_requires_resolution_to_resume(self):
        self.h.advance_to_authorized()
        planned = self.h.call("plan_action", action=self.h.action())
        result = self.h.call(
            "record_action",
            action_digest=planned["action"]["digest"],
            outcome="unknown",
            actual_cost_cents=0,
        )
        self.assertEqual(result["campaign"]["status"], "paused")
        unresolved = self.h.call("resume")
        self.assertTrue(unresolved["resume_required"])
        self.assertEqual(unresolved["campaign"]["status"], "paused")
        resumed = self.h.call("resume", resolution="Checked the mock destination and confirmed no action occurred")
        self.assertEqual(resumed["campaign"]["status"], "active")

    def test_three_consecutive_failures_create_checkpoint_and_pause(self):
        self.h.advance_to_authorized()
        for index in range(3):
            planned = self.h.call("plan_action", action=self.h.action(index + 1, kind="build"))
            outcome = self.h.call(
                "record_action",
                action_digest=planned["action"]["digest"],
                outcome="failure",
                actual_cost_cents=0,
            )
        self.assertEqual(outcome["campaign"]["status"], "paused")
        state = autopilot.CampaignStore(self.root, self.h.campaign_id).load()
        self.assertEqual(state["checkpoints"][-1]["reason"], "three_consecutive_build_failures")

    def test_expired_authorization_stops_automation(self):
        self.h.advance_to_authorized(expired=True)
        resumed = self.h.call("resume")
        self.assertEqual(resumed["campaign"]["status"], "paused")
        self.assertTrue(resumed["campaign"]["automation"]["should_stop"])
        self.assertEqual(resumed["campaign"]["pause_reason"], "authorization_expired")

    def test_stop_and_user_revocation_stop_automation(self):
        self.h.start()
        result = self.h.call("stop", reason="User revoked authorization")
        self.assertEqual(result["campaign"]["phase"], "stopped")
        self.assertTrue(result["campaign"]["automation"]["should_stop"])

    def test_revenue_authenticity_rejections(self):
        self.h.advance_to_operate()
        base = {
            "amount": "1.00",
            "currency": "USD",
            "proof_type": "provider_event",
            "verified_by": "provider_connector",
            "settled_at": autopilot.iso_now(),
            "mode": "live",
            "status": "settled",
            "payer_relation": "external_customer",
            "campaign_id": self.h.campaign_id,
            "offer_id": "automation-diagnostic",
            "proof_reference": "mock-provider:event-1",
            "fulfillment_obligation": "Deliver the diagnostic within two business days",
            "refunded": False,
            "disputed": False,
            "self_purchase": False,
            "circular": False,
            "founder_transfer": False,
            "test_fixture": False,
            "coupon_only": False,
        }
        for field, value in (("self_purchase", True), ("test_fixture", True), ("coupon_only", True)):
            payment = {**base, field: value}
            with self.subTest(field=field):
                self.assert_error("unverified_revenue", lambda payment=payment: self.h.call("verify_revenue", payment=payment))
        assertion = {**base, "proof_type": "user_assertion"}
        self.assert_error("unverified_revenue", lambda: self.h.call("verify_revenue", payment=assertion))

    def test_export_is_redacted_and_delete_is_two_step(self):
        self.h.start()
        self.h.call("answer", question_id="assets", answer="Sensitive local profile details")
        exported = self.h.call("export")
        document = json.loads(Path(exported["export_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("profile", document)
        preview = self.h.call("delete")["deletion_preview"]
        self.assertFalse(preview["recoverable"])
        deleted = self.h.call("delete", confirm_digest=preview["confirm_digest"])
        self.assertTrue(deleted["deleted"])
        self.assertFalse((self.root / self.h.campaign_id).exists())

    def test_offline_validator_detects_budget_tampering(self):
        self.h.advance_to_authorized()
        store = autopilot.CampaignStore(self.root, self.h.campaign_id)
        valid = validate_state.validate_campaign(store)
        self.assertTrue(valid["ok"], valid)
        state_path = store.state_path
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["budget"]["spent_cents"] = 999
        state_path.write_text(json.dumps(state), encoding="utf-8")
        invalid = validate_state.validate_campaign(store)
        self.assertFalse(invalid["ok"])
        self.assertTrue(any("spent_cents" in error for error in invalid["errors"]))


if __name__ == "__main__":
    unittest.main()
