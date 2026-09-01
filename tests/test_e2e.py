from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = SKILL_ROOT / "scripts" / "autopilot.py"
VALIDATOR = SKILL_ROOT / "scripts" / "validate_state.py"


class MockTools:
    """No-network stand-in for local build, publishing, and provider inspection."""

    def execute(self, planned_action):
        return {
            "outcome": "success",
            "actual_cost_cents": 0,
            "note": f"mock tool verified {planned_action['kind']} at {planned_action['target']}",
        }

    def live_payment_event(self, campaign_id, offer_id):
        return {
            "amount": "25.00",
            "currency": "USD",
            "proof_type": "provider_event",
            "verified_by": "provider_connector",
            "settled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "mode": "live",
            "status": "settled",
            "payer_relation": "external_customer",
            "campaign_id": campaign_id,
            "offer_id": offer_id,
            "proof_reference": "mock-provider:event-live-001",
            "fulfillment_obligation": "Deliver the purchased diagnostic within two business days",
            "refunded": False,
            "disputed": False,
            "self_purchase": False,
            "circular": False,
            "founder_transfer": False,
            "test_fixture": False,
            "coupon_only": False,
        }


class CliCampaign:
    def __init__(self, state_dir: Path, campaign_id="mock-e2e"):
        self.state_dir = state_dir
        self.campaign_id = campaign_id

    def call(self, operation, **values):
        request = {"command": operation, **values}
        if operation != "start":
            request.setdefault("campaign_id", self.campaign_id)
        completed = subprocess.run(
            [sys.executable, str(CONTROLLER), "--state-dir", str(self.state_dir)],
            input=json.dumps(request),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"controller returned invalid JSON: {completed.stdout!r} {completed.stderr!r}"
            ) from exc
        self.assertEqual(completed.returncode, 0, result)
        self.assertTrue(result["ok"], result)
        return result

    def assertEqual(self, left, right, message=None):
        if left != right:
            raise AssertionError(message or f"{left!r} != {right!r}")

    def assertTrue(self, value, message=None):
        if not value:
            raise AssertionError(message or f"expected truthy value, got {value!r}")


class EndToEndTests(unittest.TestCase):
    def test_no_network_first_revenue_campaign(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "campaigns"
            campaign = CliCampaign(state_dir)
            tools = MockTools()

            started = campaign.call(
                "start",
                campaign_id=campaign.campaign_id,
                goal="Turn existing automation skills into one verified customer payment",
            )
            self.assertEqual(started["campaign"]["phase"], "intake")

            answers = {
                "outcome": "First verified customer payment",
                "success_proof": "Settled payment for the selected offer",
                "constraints": "Ten hours, USD 10 test budget, English market",
                "assets": "Python automation and technical writing",
                "past_attempts": "Unpriced educational content",
                "boundaries": "No spam, regulated advice, private data, or impersonation",
                "reachable_buyers": "Opt-in community of independent consultants",
                "work_preferences": "Async fixed-scope delivery",
                "market_tolerance": "Up to ten rejections",
                "execution_system": "One verified action per cycle",
            }
            for question_id, answer in answers.items():
                discovery = campaign.call("answer", question_id=question_id, answer=answer)
            self.assertEqual(discovery["campaign"]["phase"], "opportunities")

            opportunities = []
            for index in range(10):
                opportunities.append(
                    {
                        "id": f"mock-direction-{index}",
                        "title": f"Consultant automation diagnostic {index}",
                        "summary": "Sell a fixed-scope automation diagnostic to independent consultants",
                        "buyer": "Independent software consultants",
                        "deliverable": "Prioritized workflow automation diagnostic",
                        "reachable_channel": "Opt-in consultant community",
                        "smallest_test": "Show a one-page sample and price to three buyers",
                        "principal_risk": "Buyer may prefer to implement without a diagnostic",
                        "compliance": {"status": "allowed", "rationale": "Lawful general automation service"},
                        "scores": {
                            "demand_evidence": 75 - index,
                            "reachable_buyers": 80,
                            "asset_fit": 90,
                            "validation_speed": 85,
                            "unit_economics": 75,
                            "risk_control": 85,
                        },
                        "evidence": [
                            {
                                "kind": "demand_evidence",
                                "claim": "Buyers report recurring manual delivery work",
                                "label": "attributed",
                                "source": f"mock://research/{index}",
                                "observed_at": "2026-09-01",
                            }
                        ],
                    }
                )
            ranked = campaign.call("rank", opportunities=opportunities)
            selected_id = ranked["ranking"][0]["id"]
            campaign.call("select", opportunity_id=selected_id)
            signal = campaign.call(
                "record_evidence",
                evidence={
                    "kind": "direct_market_signal",
                    "claim": "A qualified buyer asked to purchase the diagnostic at the stated price",
                    "label": "confirmed",
                    "source": "mock://buyer/qualified-response",
                    "observed_at": datetime.now(timezone.utc).date().isoformat(),
                    "outcome": "positive",
                },
            )
            self.assertEqual(signal["campaign"]["phase"], "offer")

            now = datetime.now(timezone.utc)
            offer = {
                "id": "consultant-automation-diagnostic",
                "buyer": "Independent software consultants",
                "problem": "Manual delivery tasks consume billable time",
                "deliverable": "Prioritized automation diagnostic",
                "price": "25.00",
                "currency": "USD",
                "fulfillment_standard": "Three evidenced opportunities plus one implementation outline",
                "payment_proof": "Settled live provider event",
            }
            contract = {
                "objective": "Sell and fulfill one consultant automation diagnostic",
                "allowed_channels": ["mock-market"],
                "allowed_accounts": ["mock-account"],
                "allowed_content_types": ["offer-announcement"],
                "allowed_action_kinds": ["build", "publish"],
                "data_boundaries": ["Use only opt-in and public non-sensitive data"],
                "forbidden_actions": ["message", "delete", "payment", "purchase", "sensitive_transfer"],
                "total_budget_cents": 1000,
                "daily_budget_cents": 500,
                "daily_action_limit": 1,
                "currency": "USD",
                "revenue_goal_amount": "1.00",
                "starts_at": (now - timedelta(minutes=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(days=7)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "stop_conditions": ["Revenue verified", "Authorization expires", "User revokes"],
            }
            authorized = campaign.call("authorize", offer=offer, contract=contract, user_confirmed=True)
            self.assertEqual(authorized["campaign"]["phase"], "build")

            artifact = state_dir / campaign.campaign_id / "artifacts" / "diagnostic-sample.txt"
            artifact.write_text("Mock diagnostic sample for offline end-to-end verification.\n", encoding="utf-8")
            build_action = {
                "kind": "build",
                "target": str(artifact),
                "content": "Create the validation-bounded diagnostic sample",
                "estimated_cost_cents": 0,
                "idempotency_key": "build-sample-v1",
            }
            planned_build = campaign.call("plan_action", action=build_action)["action"]
            build_result = tools.execute(planned_build)
            campaign.call("record_action", action_digest=planned_build["digest"], **build_result)
            campaign.call("checkpoint", reason="Sample artifact verified", transition_to="launch", artifacts_ready=True)

            publish_action = {
                "kind": "publish",
                "target": "mock-market://offer/diagnostic",
                "channel": "mock-market",
                "account": "mock-account",
                "content_type": "offer-announcement",
                "content": "Fixed-scope automation diagnostic for independent consultants — USD 25.",
                "estimated_cost_cents": 0,
                "idempotency_key": "publish-offer-v1",
            }
            planned_publish = campaign.call("plan_action", action=publish_action)["action"]
            publish_result = tools.execute(planned_publish)
            campaign.call("record_action", action_digest=planned_publish["digest"], **publish_result)
            campaign.call("checkpoint", reason="Mock launch verified", transition_to="operate", launch_verified=True)

            payment = tools.live_payment_event(campaign.campaign_id, offer["id"])
            completed = campaign.call("verify_revenue", payment=payment)
            self.assertEqual(completed["status"], "revenue_verified")
            self.assertEqual(completed["campaign"]["status"], "complete")
            self.assertTrue(completed["campaign"]["automation"]["should_stop"])

            validation = subprocess.run(
                [sys.executable, str(VALIDATOR), "--state-dir", str(state_dir), "--campaign-id", campaign.campaign_id],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            report = json.loads(validation.stdout)
            self.assertEqual(validation.returncode, 0, report)
            self.assertTrue(report["ok"], report)


if __name__ == "__main__":
    unittest.main()
