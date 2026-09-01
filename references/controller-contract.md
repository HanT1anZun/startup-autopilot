# Controller contract

Use `scripts/autopilot.py` as the deterministic boundary around campaign state. The script uses Python's standard library only.

## Invocation

```text
printf JSON | python scripts/autopilot.py --state-dir <cwd>/work/startup-autopilot
```

On PowerShell, provide the JSON through the process's standard input using a safe structured invocation. Do not interpolate private answers into a shell command. The controller reads one JSON object, capped at 256 KiB, and emits one JSON object.

Use `command` for the controller operation name. For example, a planning request is `{ "command": "plan_action", "campaign_id": "...", "action": { ... } }`. Legacy requests whose top-level `action` is a string remain accepted for commands that do not also need an action object.

Every request except an initial generated-ID start contains `campaign_id`. Campaign IDs use lowercase letters, digits, and hyphens and are at most 63 characters.

## Actions

- `start`: optional `campaign_id`, required non-empty `goal`. Creates a campaign in `intake`.
- `resume`: returns status, phase, next action, and authorization health. It does not clear a pause unless the request includes a non-empty `resolution` describing what changed.
- `answer`: requires `question_id` and `answer`. Use the ten IDs in `discovery.md`.
- `rank`: requires exactly ten opportunity objects following `opportunity-scoring.md`.
- `select`: requires `opportunity_id`; enters `validation`.
- `record_evidence`: requires an evidence object. A positive `direct_market_signal` advances validation to `offer`.
- `authorize`: requires an `offer`, `contract`, and `user_confirmed: true` after the exact charter has been shown and approved; stores an approval timestamp and authorization digest, then enters `build`.
- `plan_action`: requires an action object. It returns an action digest and whether an additional confirmation is required.
- `record_action`: requires the action digest, outcome, and actual cost. Record immediately after an attempted external action.
- `checkpoint`: records a milestone and may advance `build -> launch` or `launch -> operate` when its gate is satisfied.
- `pause`, `resume`, `stop`: control campaign execution without losing evidence.
- `verify_revenue`: requires the payment evidence in `revenue-verification.md`.
- `inspect`: returns a safe summary; add `include_profile: true` only when raw local answers are genuinely needed.
- `export`: writes a redacted `export.json` inside the campaign directory.
- `delete`: first call without `confirm_digest` for a deletion preview, then repeat with the exact digest.

## Opportunity object

```json
{
  "id": "bounded-id",
  "title": "Plain-language direction",
  "summary": "What is sold, to whom, and why",
  "buyer": "Specific reachable buyer",
  "deliverable": "Bounded result",
  "reachable_channel": "How this buyer can be reached",
  "smallest_test": "Fast reversible demand test",
  "principal_risk": "Largest delivery or market risk",
  "compliance": {
    "status": "allowed",
    "rationale": "Why this direction is lawful and in scope"
  },
  "scores": {
    "demand_evidence": 0,
    "reachable_buyers": 0,
    "asset_fit": 0,
    "validation_speed": 0,
    "unit_economics": 0,
    "risk_control": 0
  },
  "evidence": [
    {
      "claim": "Material market claim",
      "label": "attributed",
      "source": "local:research/source-1",
      "observed_at": "2026-09-01"
    }
  ]
}
```

Scores are integers from 0 to 100. The controller computes the weighted total and ranking. If no non-unknown evidence item uses a demand-related kind, it caps `demand_evidence` at 20 and returns `demand_evidence_present: false`.

## Action object

```json
{
  "kind": "message",
  "target": "Named destination",
  "channel": "email",
  "account": "approved-account-alias",
  "content_type": "approved-outreach",
  "content": "Exact content or an artifact reference",
  "estimated_cost_cents": 0,
  "idempotency_key": "campaign-specific-key"
}
```

Never treat a controller `planned` result as permission to bypass a host confirmation. A `pending_confirmation` result is a hard stop until the required confirmation is actually obtained.

If `plan_action` returns `idempotent_replay: true`, inspect the existing record and do not execute the external action again. Unknown outcomes are recorded as `unknown` and paused; they are never retried automatically.

## Payment object

`verify_revenue` requires `amount`, three-letter `currency`, `proof_type`, `verified_by`, `settled_at`, `mode`, `status`, `payer_relation`, `campaign_id`, `offer_id`, `proof_reference`, and `fulfillment_obligation`. It also requires explicit `false` values for `refunded`, `disputed`, `self_purchase`, `circular`, `founder_transfer`, `test_fixture`, and `coupon_only`.

## Error behavior

Errors use `{ "ok": false, "error": { "code": "...", "message": "..." } }`. Do not silently retry non-idempotent work. Resolve or checkpoint the error and persist the outcome.
