#!/usr/bin/env python3
"""Deterministic local controller for the Startup Autopilot skill.

The controller never performs network or market-facing actions. It validates
campaign state, authorization, budgets, evidence, and action records so the
calling Codex agent can execute work through the tools available to it.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator


SCHEMA_VERSION = "1.0"
MAX_INPUT_BYTES = 256 * 1024
MAX_TEXT = 8_000
CAMPAIGN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

QUESTION_IDS = (
    "outcome",
    "success_proof",
    "constraints",
    "assets",
    "past_attempts",
    "boundaries",
    "reachable_buyers",
    "work_preferences",
    "market_tolerance",
    "execution_system",
)

PHASES = (
    "intake",
    "discovery",
    "opportunities",
    "validation",
    "offer",
    "build",
    "launch",
    "operate",
    "revenue_verified",
    "stopped",
)

SCORE_WEIGHTS = {
    "demand_evidence": Decimal("0.25"),
    "reachable_buyers": Decimal("0.20"),
    "asset_fit": Decimal("0.20"),
    "validation_speed": Decimal("0.15"),
    "unit_economics": Decimal("0.10"),
    "risk_control": Decimal("0.10"),
}

EVIDENCE_LABELS = {"confirmed", "attributed", "inferred", "unknown"}
DEMAND_EVIDENCE_KINDS = {
    "buyer_pain",
    "customer_interview",
    "demand_evidence",
    "direct_market_signal",
    "payment_evidence",
    "pricing_evidence",
    "willingness_to_pay",
}
SUPPORTED_ACTION_KINDS = {
    "read",
    "local",
    "research",
    "build",
    "metered_tool",
    "deploy",
    "publish",
    "message",
    "purchase",
    "payment",
    "account_change",
    "permission_change",
    "legal_acceptance",
    "credential_entry",
    "sensitive_transfer",
    "delete",
    "irreversible",
    "captcha",
}
EXTERNAL_ACTION_KINDS = SUPPORTED_ACTION_KINDS - {"read", "local", "research", "build"}
HARD_CONFIRMATION_KINDS = {
    "purchase",
    "payment",
    "account_change",
    "permission_change",
    "legal_acceptance",
    "credential_entry",
    "sensitive_transfer",
    "delete",
    "irreversible",
    "captcha",
}
NON_IDEMPOTENT_KINDS = HARD_CONFIRMATION_KINDS | {"deploy", "publish", "message"}
ACTION_OUTCOMES = {"success", "failure", "unknown", "cancelled"}
SECRET_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "credential",
    "credentials",
    "private_key",
}


class ControllerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_time(value: Any, field: str) -> datetime:
    text = require_text(value, field, 80)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControllerError("invalid_time", f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ControllerError("invalid_time", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_money(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ControllerError("invalid_money", f"{field} must be a positive decimal")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ControllerError("invalid_money", f"{field} must be a positive decimal") from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ControllerError("invalid_money", f"{field} must be positive with at most two decimal places")
    return amount


def require_text(value: Any, field: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControllerError("invalid_request", f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise ControllerError("input_too_large", f"{field} exceeds {limit} characters")
    return text


def optional_text(value: Any, field: str, limit: int = MAX_TEXT) -> str:
    if value in (None, ""):
        return ""
    return require_text(value, field, limit)


def require_int(value: Any, field: str, minimum: int = 0, maximum: int = 10**9) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ControllerError("invalid_request", f"{field} must be an integer from {minimum} to {maximum}")
    return value


def require_string_list(value: Any, field: str, *, allow_empty: bool = False, maximum: int = 100) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value) or len(value) > maximum:
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ControllerError("invalid_request", f"{field} must be {qualifier} with at most {maximum} items")
    result = []
    for index, item in enumerate(value):
        result.append(require_text(item, f"{field}[{index}]", 500))
    if len(set(result)) != len(result):
        raise ControllerError("invalid_request", f"{field} must not contain duplicates")
    return result


def reject_secret_fields(value: Any, path: str = "request") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in SECRET_KEYS:
                raise ControllerError("secret_rejected", f"{path}.{key} must not contain credentials or secrets")
            reject_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secret_fields(child, f"{path}[{index}]")


def validate_campaign_id(value: Any) -> str:
    campaign_id = require_text(value, "campaign_id", 63)
    if not CAMPAIGN_RE.fullmatch(campaign_id):
        raise ControllerError("invalid_campaign_id", "campaign_id must contain lowercase letters, digits, or internal hyphens")
    return campaign_id


def validate_bounded_id(value: Any, field: str) -> str:
    item_id = require_text(value, field, 63)
    if not ID_RE.fullmatch(item_id):
        raise ControllerError("invalid_request", f"{field} must contain lowercase letters, digits, or internal hyphens")
    return item_id


def is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    if os.name == "nt":
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                return True
        except OSError:
            return False
    return False


def reject_symlinked_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if os.path.lexists(current) and is_link_like(current):
            raise ControllerError("unsafe_path", f"linked or reparse-point path component is not allowed: {current}")


def secure_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class CampaignStore:
    def __init__(self, root: str | Path, campaign_id: str):
        self.root = Path(root).expanduser().absolute()
        reject_symlinked_components(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        secure_mode(self.root, 0o700)
        self.campaign_id = validate_campaign_id(campaign_id)
        self.directory = self.root / self.campaign_id
        reject_symlinked_components(self.directory)
        root_resolved = self.root.resolve(strict=False)
        candidate_resolved = self.directory.resolve(strict=False)
        try:
            candidate_resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise ControllerError("unsafe_path", "campaign directory escapes the state root") from exc
        self.state_path = self.directory / "state.json"
        self.authorization_path = self.directory / "authorization.json"
        self.evidence_path = self.directory / "evidence.jsonl"
        self.actions_path = self.directory / "actions.jsonl"
        self.export_path = self.directory / "export.json"
        self.artifacts_path = self.directory / "artifacts"
        self.lock_path = self.root / f".{self.campaign_id}.lock"

    @contextmanager
    def lock(self) -> Iterator[None]:
        deadline = time.monotonic() + 3.0
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
                os.fsync(fd)
            except FileExistsError:
                try:
                    stale = time.time() - self.lock_path.stat().st_mtime > 120
                except FileNotFoundError:
                    continue
                if stale:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise ControllerError("campaign_busy", "campaign is locked by another process")
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(fd)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def exists(self) -> bool:
        return self.state_path.is_file()

    def load(self) -> dict[str, Any]:
        if not self.exists():
            raise ControllerError("campaign_not_found", f"campaign '{self.campaign_id}' does not exist")
        reject_symlinked_components(self.state_path)
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            state = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise ControllerError("invalid_state", "campaign state cannot be read as valid JSON") from exc
        validate_state(state, self.campaign_id)
        return state

    def save(self, state: dict[str, Any]) -> None:
        validate_state(state, self.campaign_id)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        secure_mode(self.directory, 0o700)
        state["updated_at"] = iso_now()
        temp = self.directory / f".state-{secrets.token_hex(8)}.tmp"
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with open(temp, "x", encoding="utf-8", newline="\n") as handle:
                secure_mode(temp, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_path)
            secure_mode(self.state_path, 0o600)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def write_json(self, path: Path, value: Any) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.directory / f".{path.name}-{secrets.token_hex(8)}.tmp"
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with open(temp, "x", encoding="utf-8", newline="\n") as handle:
                secure_mode(temp, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            secure_mode(path, 0o600)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def append_event(self, path: Path, event: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = canonical_json(event) + "\n"
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            secure_mode(path, 0o600)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def validate_state(state: Any, campaign_id: str | None = None) -> None:
    if not isinstance(state, dict):
        raise ControllerError("invalid_state", "state must be an object")
    required = {"schema_version", "campaign_id", "status", "phase", "created_at", "updated_at"}
    if not required.issubset(state):
        raise ControllerError("invalid_state", "state is missing required fields")
    if state["schema_version"] != SCHEMA_VERSION:
        raise ControllerError("unsupported_schema", f"expected schema version {SCHEMA_VERSION}")
    validate_campaign_id(state["campaign_id"])
    if campaign_id is not None and state["campaign_id"] != campaign_id:
        raise ControllerError("invalid_state", "state campaign_id does not match its directory")
    if state["status"] not in {"active", "paused", "stopped", "complete"}:
        raise ControllerError("invalid_state", "unknown campaign status")
    if state["phase"] not in PHASES:
        raise ControllerError("invalid_state", "unknown campaign phase")
    if not isinstance(state.get("profile"), dict) or not isinstance(state.get("actions"), dict):
        raise ControllerError("invalid_state", "profile and actions must be objects")
    if not isinstance(state.get("budget"), dict) or not isinstance(state.get("failure_streaks"), dict):
        raise ControllerError("invalid_state", "budget and failure_streaks must be objects")


def new_state(campaign_id: str, goal: str) -> dict[str, Any]:
    now = iso_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "goal": goal,
        "status": "active",
        "phase": "intake",
        "created_at": now,
        "updated_at": now,
        "profile": {},
        "opportunities": [],
        "selected_opportunity_id": None,
        "validation": {"direct_positive_signal": False},
        "offer": None,
        "authorization": None,
        "authorization_digest": None,
        "authorization_approved_at": None,
        "budget": {"reserved_cents": 0, "spent_cents": 0, "daily": {}},
        "actions": {},
        "evidence_count": 0,
        "failure_streaks": {},
        "checkpoints": [],
        "pause_reason": None,
        "stop_reason": None,
        "revenue": None,
        "automation": None,
    }


def authorization_health(state: dict[str, Any]) -> tuple[bool, str]:
    authorization = state.get("authorization")
    stored_digest = state.get("authorization_digest")
    if not isinstance(authorization, dict) or not isinstance(stored_digest, str):
        return False, "authorization_missing"
    if digest({"offer": state.get("offer"), "contract": authorization}) != stored_digest:
        return False, "authorization_digest_mismatch"
    now = utc_now()
    starts = parse_time(authorization.get("starts_at"), "authorization.starts_at")
    expires = parse_time(authorization.get("expires_at"), "authorization.expires_at")
    if now < starts:
        return False, "authorization_not_started"
    if now >= expires:
        return False, "authorization_expired"
    return True, "current"


def safe_summary(state: dict[str, Any], include_profile: bool = False) -> dict[str, Any]:
    health = authorization_health(state) if state.get("authorization") else (False, "authorization_missing")
    summary = {
        "schema_version": state["schema_version"],
        "campaign_id": state["campaign_id"],
        "goal": state["goal"],
        "status": state["status"],
        "phase": state["phase"],
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "answered_questions": sorted(state["profile"].keys()),
        "opportunity_count": len(state["opportunities"]),
        "selected_opportunity_id": state["selected_opportunity_id"],
        "direct_positive_signal": state["validation"]["direct_positive_signal"],
        "authorization": {
            "current": health[0],
            "reason": health[1],
            "digest": state.get("authorization_digest"),
            "approved_at": state.get("authorization_approved_at"),
        },
        "budget": copy.deepcopy(state["budget"]),
        "action_count": len(state["actions"]),
        "evidence_count": state["evidence_count"],
        "pause_reason": state.get("pause_reason"),
        "stop_reason": state.get("stop_reason"),
        "revenue": copy.deepcopy(state.get("revenue")),
        "automation": automation_directive(state),
        "next_action": next_action(state),
    }
    if include_profile:
        summary["profile"] = copy.deepcopy(state["profile"])
    return summary


def automation_directive(state: dict[str, Any]) -> dict[str, Any]:
    if state["status"] == "complete" or state["phase"] == "revenue_verified":
        return {"should_stop": True, "reason": "revenue_verified"}
    if state["status"] == "stopped" or state["phase"] == "stopped":
        return {"should_stop": True, "reason": "campaign_stopped"}
    if state["status"] == "paused":
        return {"should_stop": True, "reason": state.get("pause_reason") or "campaign_paused"}
    if state["phase"] in {"build", "launch", "operate"}:
        healthy, reason = authorization_health(state)
        if not healthy:
            return {"should_stop": True, "reason": reason}
    return {"should_stop": False, "reason": None}


def next_action(state: dict[str, Any]) -> dict[str, Any]:
    if state["status"] == "paused":
        return {"action": "resolve_checkpoint_then_resume", "reason": state.get("pause_reason")}
    if state["status"] in {"stopped", "complete"}:
        return {"action": "none", "reason": state["status"]}
    phase = state["phase"]
    if phase in {"build", "launch", "operate"}:
        healthy, reason = authorization_health(state)
        if not healthy:
            return {"action": "checkpoint_and_pause", "reason": reason}
    if phase in {"intake", "discovery"}:
        missing = [item for item in QUESTION_IDS if item not in state["profile"]]
        if missing:
            return {"action": "answer", "question_id": missing[0], "remaining": len(missing)}
        return {"action": "improve_discovery_context"}
    if phase == "opportunities":
        return {"action": "rank" if not state["opportunities"] else "select"}
    if phase == "validation":
        return {"action": "run_smallest_reversible_test"}
    if phase == "offer":
        return {"action": "authorize"}
    if phase == "build":
        return {"action": "build_validated_deliverable_then_checkpoint"}
    if phase == "launch":
        return {"action": "plan_and_verify_one_launch_action"}
    if phase == "operate":
        return {"action": "run_one_bounded_operating_cycle_or_verify_revenue"}
    if phase == "revenue_verified":
        return {"action": "handoff"}
    return {"action": "none"}


def require_active(state: dict[str, Any]) -> None:
    if state["status"] != "active":
        raise ControllerError("campaign_not_active", f"campaign status is {state['status']}")


def validate_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControllerError("invalid_evidence", "evidence must be an object")
    evidence = copy.deepcopy(value)
    evidence["kind"] = require_text(evidence.get("kind"), "evidence.kind", 80)
    evidence["claim"] = require_text(evidence.get("claim"), "evidence.claim", 2_000)
    label = require_text(evidence.get("label"), "evidence.label", 20)
    if label not in EVIDENCE_LABELS:
        raise ControllerError("invalid_evidence", "evidence.label is not supported")
    evidence["label"] = label
    evidence["source"] = require_text(evidence.get("source"), "evidence.source", 2_000)
    observed_at = require_text(evidence.get("observed_at"), "evidence.observed_at", 80)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_at):
            datetime.strptime(observed_at, "%Y-%m-%d")
        else:
            parse_time(observed_at, "evidence.observed_at")
    except ControllerError:
        raise
    except ValueError as exc:
        raise ControllerError("invalid_evidence", "evidence.observed_at must be an ISO date or timezone-aware timestamp") from exc
    evidence["observed_at"] = observed_at
    evidence["outcome"] = optional_text(evidence.get("outcome"), "evidence.outcome", 80)
    if evidence["kind"] == "direct_market_signal":
        if evidence["outcome"] not in {"positive", "negative", "neutral", "unknown"}:
            raise ControllerError("invalid_evidence", "a direct market signal needs a supported outcome")
        if evidence["outcome"] == "positive" and evidence["label"] not in {"confirmed", "attributed"}:
            raise ControllerError("invalid_evidence", "a positive direct market signal cannot be inferred or unknown")
    return evidence


def validate_opportunity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControllerError("invalid_opportunity", "each opportunity must be an object")
    opportunity = copy.deepcopy(value)
    opportunity["id"] = validate_bounded_id(opportunity.get("id"), "opportunity.id")
    for field in ("title", "summary", "buyer", "deliverable", "reachable_channel", "smallest_test", "principal_risk"):
        opportunity[field] = require_text(opportunity.get(field), f"opportunity.{field}", 2_000)
    compliance = opportunity.get("compliance")
    if not isinstance(compliance, dict) or compliance.get("status") != "allowed":
        raise ControllerError("business_not_allowed", "opportunity compliance.status must be allowed")
    opportunity["compliance"] = {
        "status": "allowed",
        "rationale": require_text(compliance.get("rationale"), "opportunity.compliance.rationale", 1_000),
    }
    scores = opportunity.get("scores")
    if not isinstance(scores, dict):
        raise ControllerError("invalid_opportunity", "opportunity.scores must be an object")
    normalized_scores: dict[str, int] = {}
    for axis in SCORE_WEIGHTS:
        normalized_scores[axis] = require_int(scores.get(axis), f"opportunity.scores.{axis}", 0, 100)
    opportunity["scores"] = normalized_scores
    raw_evidence = opportunity.get("evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence or len(raw_evidence) > 50:
        raise ControllerError("invalid_opportunity", "opportunity.evidence must contain 1 to 50 items")
    opportunity["evidence"] = [validate_evidence(item) for item in raw_evidence]
    has_demand_evidence = any(
        item["kind"] in DEMAND_EVIDENCE_KINDS and item["label"] != "unknown"
        for item in opportunity["evidence"]
    )
    if not has_demand_evidence and normalized_scores["demand_evidence"] > 20:
        normalized_scores["demand_evidence"] = 20
    opportunity["demand_evidence_present"] = has_demand_evidence
    weighted = sum(Decimal(normalized_scores[axis]) * weight for axis, weight in SCORE_WEIGHTS.items())
    opportunity["weighted_score"] = float(weighted.quantize(Decimal("0.1")))
    return opportunity


def validate_offer(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControllerError("invalid_offer", "offer must be an object")
    offer = copy.deepcopy(value)
    offer["id"] = validate_bounded_id(offer.get("id"), "offer.id")
    for field in ("buyer", "problem", "deliverable", "fulfillment_standard", "payment_proof"):
        offer[field] = require_text(offer.get(field), f"offer.{field}", 2_000)
    offer["price"] = str(parse_money(offer.get("price"), "offer.price"))
    currency = require_text(offer.get("currency"), "offer.currency", 3).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ControllerError("invalid_offer", "offer.currency must be a three-letter code")
    offer["currency"] = currency
    return offer


def validate_contract(value: Any, offer: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControllerError("invalid_contract", "contract must be an object")
    contract = copy.deepcopy(value)
    contract["objective"] = require_text(contract.get("objective"), "contract.objective", 2_000)
    contract["allowed_channels"] = require_string_list(contract.get("allowed_channels"), "contract.allowed_channels", allow_empty=True)
    contract["allowed_accounts"] = require_string_list(contract.get("allowed_accounts"), "contract.allowed_accounts", allow_empty=True)
    contract["allowed_content_types"] = require_string_list(contract.get("allowed_content_types"), "contract.allowed_content_types", allow_empty=True)
    kinds = require_string_list(contract.get("allowed_action_kinds"), "contract.allowed_action_kinds", allow_empty=True)
    unknown = sorted(set(kinds) - SUPPORTED_ACTION_KINDS)
    if unknown:
        raise ControllerError("invalid_contract", f"unsupported allowed_action_kinds: {', '.join(unknown)}")
    contract["allowed_action_kinds"] = kinds
    contract["data_boundaries"] = require_string_list(contract.get("data_boundaries"), "contract.data_boundaries")
    contract["forbidden_actions"] = require_string_list(contract.get("forbidden_actions"), "contract.forbidden_actions")
    contract["stop_conditions"] = require_string_list(contract.get("stop_conditions"), "contract.stop_conditions")
    contract["total_budget_cents"] = require_int(contract.get("total_budget_cents"), "contract.total_budget_cents", 0)
    contract["daily_budget_cents"] = require_int(contract.get("daily_budget_cents"), "contract.daily_budget_cents", 0)
    contract["daily_action_limit"] = require_int(contract.get("daily_action_limit"), "contract.daily_action_limit", 0, 10_000)
    if contract["daily_budget_cents"] > contract["total_budget_cents"]:
        raise ControllerError("invalid_contract", "daily budget cannot exceed total budget")
    currency = require_text(contract.get("currency"), "contract.currency", 3).upper()
    if currency != offer["currency"]:
        raise ControllerError("invalid_contract", "contract and offer currencies must match")
    contract["currency"] = currency
    contract["revenue_goal_amount"] = str(parse_money(contract.get("revenue_goal_amount", "1.00"), "contract.revenue_goal_amount"))
    starts = parse_time(contract.get("starts_at"), "contract.starts_at")
    expires = parse_time(contract.get("expires_at"), "contract.expires_at")
    if expires <= starts:
        raise ControllerError("invalid_contract", "contract.expires_at must be after starts_at")
    contract["starts_at"] = starts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    contract["expires_at"] = expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return contract


def validate_action(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControllerError("invalid_action", "action must be an object")
    action = copy.deepcopy(value)
    kind = require_text(action.get("kind"), "action.kind", 80)
    if kind not in SUPPORTED_ACTION_KINDS:
        raise ControllerError("invalid_action", "action.kind is not supported")
    action["kind"] = kind
    action["target"] = require_text(action.get("target"), "action.target", 1_000)
    action["channel"] = optional_text(action.get("channel"), "action.channel", 200)
    action["account"] = optional_text(action.get("account"), "action.account", 200)
    action["content_type"] = optional_text(action.get("content_type"), "action.content_type", 200)
    action["content"] = optional_text(action.get("content"), "action.content", MAX_TEXT)
    action["estimated_cost_cents"] = require_int(action.get("estimated_cost_cents", 0), "action.estimated_cost_cents", 0)
    action["idempotency_key"] = require_text(action.get("idempotency_key"), "action.idempotency_key", 200)
    return action


def ensure_authorized(state: dict[str, Any]) -> dict[str, Any]:
    healthy, reason = authorization_health(state)
    if not healthy:
        raise ControllerError("authorization_not_current", reason)
    assert isinstance(state["authorization"], dict)
    return state["authorization"]


def pause_with_checkpoint(state: dict[str, Any], reason: str) -> None:
    state["status"] = "paused"
    state["pause_reason"] = reason
    state["checkpoints"].append(
        {
            "recorded_at": iso_now(),
            "phase": state["phase"],
            "reason": reason,
            "automatic": True,
        }
    )
    state["checkpoints"] = state["checkpoints"][-100:]


def pause_and_raise(
    store: CampaignStore,
    state: dict[str, Any],
    code: str,
    message: str,
    reason: str,
) -> None:
    pause_with_checkpoint(state, reason)
    store.save(state)
    raise ControllerError(code, message)


def utc_day() -> str:
    return utc_now().date().isoformat()


def action_fingerprint(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": action["kind"],
        "target": action["target"],
        "channel": action["channel"],
        "account": action["account"],
        "content_type": action["content_type"],
        "content_digest": hashlib.sha256(action["content"].encode("utf-8")).hexdigest(),
        "estimated_cost_cents": action["estimated_cost_cents"],
        "idempotency_key": action["idempotency_key"],
    }


def start_action(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    raw_id = request.get("campaign_id")
    campaign_id = validate_campaign_id(raw_id) if raw_id else f"campaign-{utc_now():%Y%m%d}-{secrets.token_hex(3)}"
    goal = require_text(request.get("goal"), "goal", 2_000)
    store = CampaignStore(root, campaign_id)
    with store.lock():
        if store.exists():
            raise ControllerError("campaign_exists", f"campaign '{campaign_id}' already exists")
        state = new_state(campaign_id, goal)
        store.save(state)
        store.artifacts_path.mkdir(mode=0o700)
        secure_mode(store.artifacts_path, 0o700)
    return {"ok": True, "campaign": safe_summary(state)}


def load_request_store(root: Path, request: dict[str, Any]) -> CampaignStore:
    return CampaignStore(root, validate_campaign_id(request.get("campaign_id")))


def handle_resume(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    resolution = optional_text(request.get("resolution"), "resolution", 2_000)
    with store.lock():
        state = store.load()
        if state["status"] == "paused":
            if not resolution:
                return {"ok": True, "campaign": safe_summary(state), "resume_required": True}
            if state["phase"] in {"build", "launch", "operate"}:
                healthy, reason = authorization_health(state)
                if not healthy:
                    raise ControllerError("cannot_resume", reason)
            state["checkpoints"].append(
                {
                    "recorded_at": iso_now(),
                    "phase": state["phase"],
                    "reason": "resume_resolution",
                    "resolution": resolution,
                }
            )
            state["checkpoints"] = state["checkpoints"][-100:]
            state["status"] = "active"
            state["pause_reason"] = None
            store.save(state)
        elif state["status"] == "active" and state["phase"] in {"build", "launch", "operate"}:
            healthy, reason = authorization_health(state)
            if not healthy:
                pause_with_checkpoint(state, reason)
                store.save(state)
        return {"ok": True, "campaign": safe_summary(state)}


def handle_answer(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    question_id = require_text(request.get("question_id"), "question_id", 80)
    if question_id not in QUESTION_IDS:
        raise ControllerError("invalid_question", "question_id is not part of discovery")
    answer = require_text(request.get("answer"), "answer", 4_000)
    with store.lock():
        state = store.load()
        require_active(state)
        if state["phase"] not in {"intake", "discovery"}:
            raise ControllerError("invalid_transition", "answers can only be recorded during discovery")
        state["profile"][question_id] = answer
        state["phase"] = "discovery"
        missing = [item for item in QUESTION_IDS if item not in state["profile"]]
        if not missing:
            unknown = {"prefer_not_to_say", "unknown", "none", "n/a", "na"}
            key_context = [state["profile"].get(key, "").strip().lower() for key in ("assets", "boundaries", "reachable_buyers")]
            if any(value not in unknown for value in key_context):
                state["phase"] = "opportunities"
        store.save(state)
        return {"ok": True, "campaign": safe_summary(state)}


def handle_rank(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    raw = request.get("opportunities")
    if not isinstance(raw, list) or len(raw) != 10:
        raise ControllerError("invalid_opportunity_set", "rank requires exactly ten opportunities")
    normalized = [validate_opportunity(item) for item in raw]
    ids = [item["id"] for item in normalized]
    if len(set(ids)) != 10:
        raise ControllerError("invalid_opportunity_set", "opportunity IDs must be unique")
    normalized.sort(key=lambda item: (-item["weighted_score"], item["id"]))
    for index, item in enumerate(normalized, 1):
        item["rank"] = index
    with store.lock():
        state = store.load()
        require_active(state)
        if state["phase"] != "opportunities":
            raise ControllerError("invalid_transition", "opportunities can only be ranked in the opportunities phase")
        state["opportunities"] = normalized
        store.save(state)
        return {"ok": True, "campaign": safe_summary(state), "ranking": normalized}


def handle_select(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    opportunity_id = validate_bounded_id(request.get("opportunity_id"), "opportunity_id")
    with store.lock():
        state = store.load()
        require_active(state)
        if state["phase"] != "opportunities" or not state["opportunities"]:
            raise ControllerError("invalid_transition", "rank opportunities before selecting one")
        if opportunity_id not in {item["id"] for item in state["opportunities"]}:
            raise ControllerError("unknown_opportunity", "opportunity_id is not in the ranked set")
        state["selected_opportunity_id"] = opportunity_id
        state["phase"] = "validation"
        store.save(state)
        return {"ok": True, "campaign": safe_summary(state)}


def handle_record_evidence(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    evidence = validate_evidence(request.get("evidence"))
    with store.lock():
        state = store.load()
        require_active(state)
        evidence["campaign_id"] = state["campaign_id"]
        evidence["recorded_at"] = iso_now()
        evidence["event_digest"] = digest(evidence)
        if evidence["kind"] == "direct_market_signal" and evidence["outcome"] == "positive":
            if state["phase"] != "validation":
                raise ControllerError("invalid_transition", "a validating signal is only accepted during validation")
            state["validation"]["direct_positive_signal"] = True
            state["phase"] = "offer"
        state["evidence_count"] += 1
        store.append_event(store.evidence_path, evidence)
        store.save(state)
        return {"ok": True, "evidence_digest": evidence["event_digest"], "campaign": safe_summary(state)}


def handle_authorize(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    if request.get("user_confirmed") is not True:
        raise ControllerError(
            "authorization_confirmation_required",
            "authorize requires user_confirmed: true after the complete charter is shown to the user",
        )
    offer = validate_offer(request.get("offer"))
    contract = validate_contract(request.get("contract"), offer)
    with store.lock():
        state = store.load()
        require_active(state)
        if state["phase"] != "offer" or not state["validation"]["direct_positive_signal"]:
            raise ControllerError("invalid_transition", "authorization requires a positive direct market signal")
        authorization_digest = digest({"offer": offer, "contract": contract})
        state["offer"] = offer
        state["authorization"] = contract
        state["authorization_digest"] = authorization_digest
        state["authorization_approved_at"] = iso_now()
        state["phase"] = "build"
        store.write_json(
            store.authorization_path,
            {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": state["campaign_id"],
                "offer": offer,
                "contract": contract,
                "digest": authorization_digest,
                "approved_at": state["authorization_approved_at"],
            },
        )
        store.save(state)
        return {"ok": True, "authorization_digest": authorization_digest, "campaign": safe_summary(state)}


def handle_plan_action(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    action = validate_action(request.get("action"))
    fingerprint = action_fingerprint(action)
    action_digest = digest(fingerprint)
    with store.lock():
        state = store.load()
        require_active(state)
        if state["phase"] not in {"build", "launch", "operate"}:
            raise ControllerError("invalid_transition", "actions require an authorized execution phase")
        healthy, authorization_reason = authorization_health(state)
        if not healthy:
            pause_and_raise(
                store,
                state,
                "authorization_not_current",
                authorization_reason,
                authorization_reason,
            )
        assert isinstance(state["authorization"], dict)
        contract = state["authorization"]
        if action["kind"] not in contract["allowed_action_kinds"]:
            pause_and_raise(store, state, "outside_authorization", "action kind is not authorized", "unauthorized_action_kind")
        if action["kind"] in contract["forbidden_actions"]:
            pause_and_raise(store, state, "outside_authorization", "action kind is explicitly forbidden", "forbidden_action_kind")
        if action["kind"] in EXTERNAL_ACTION_KINDS:
            if action["channel"] not in contract["allowed_channels"]:
                pause_and_raise(store, state, "outside_authorization", "channel is not authorized", "unauthorized_channel")
            if action["account"] not in contract["allowed_accounts"]:
                pause_and_raise(store, state, "outside_authorization", "account is not authorized", "unauthorized_account")
        if action["kind"] in {"message", "publish"} and action["content_type"] not in contract["allowed_content_types"]:
            pause_and_raise(store, state, "outside_authorization", "content type is not authorized", "unauthorized_content_type")
        for existing in state["actions"].values():
            if existing["idempotency_key"] == action["idempotency_key"]:
                if existing["fingerprint"] == fingerprint:
                    return {"ok": True, "action": copy.deepcopy(existing), "idempotent_replay": True}
                pause_and_raise(
                    store,
                    state,
                    "idempotency_collision",
                    "idempotency key was already used for different content",
                    "idempotency_collision",
                )
        cost = action["estimated_cost_cents"]
        budget = state["budget"]
        if budget["spent_cents"] + budget["reserved_cents"] + cost > contract["total_budget_cents"]:
            pause_and_raise(store, state, "budget_exceeded", "total campaign budget would be exceeded", "total_budget_exhausted")
        day = utc_day()
        daily = budget["daily"].setdefault(day, {"spent_cents": 0, "action_count": 0})
        reserved_today = sum(
            item["estimated_cost_cents"]
            for item in state["actions"].values()
            if item["day"] == day and item["status"] in {"planned", "pending_confirmation"}
        )
        pending_today = sum(
            1
            for item in state["actions"].values()
            if item["day"] == day
            and item["kind"] in EXTERNAL_ACTION_KINDS
            and item["status"] in {"planned", "pending_confirmation"}
        )
        if daily["spent_cents"] + reserved_today + cost > contract["daily_budget_cents"]:
            pause_and_raise(store, state, "daily_budget_exceeded", "daily campaign budget would be exceeded", "daily_budget_exhausted")
        if action["kind"] in EXTERNAL_ACTION_KINDS and daily["action_count"] + pending_today + 1 > contract["daily_action_limit"]:
            pause_and_raise(store, state, "daily_action_limit", "daily external action limit would be exceeded", "daily_external_action_limit")
        requires_confirmation = action["kind"] in HARD_CONFIRMATION_KINDS
        record = {
            "digest": action_digest,
            "fingerprint": fingerprint,
            "idempotency_key": action["idempotency_key"],
            "kind": action["kind"],
            "target": action["target"],
            "channel": action["channel"],
            "account": action["account"],
            "content_type": action["content_type"],
            "content_digest": fingerprint["content_digest"],
            "estimated_cost_cents": cost,
            "actual_cost_cents": None,
            "created_at": iso_now(),
            "day": day,
            "status": "pending_confirmation" if requires_confirmation else "planned",
            "requires_confirmation": requires_confirmation,
            "host_confirmation_may_apply": action["kind"] in NON_IDEMPOTENT_KINDS,
            "outcome": None,
        }
        state["actions"][action_digest] = record
        budget["reserved_cents"] += cost
        store.append_event(store.actions_path, {"event": "planned", "recorded_at": iso_now(), **record})
        store.save(state)
        return {"ok": True, "action": copy.deepcopy(record), "idempotent_replay": False}


def handle_record_action(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    action_digest = require_text(request.get("action_digest"), "action_digest", 64)
    outcome = require_text(request.get("outcome"), "outcome", 20)
    if outcome not in ACTION_OUTCOMES:
        raise ControllerError("invalid_action_outcome", "outcome is not supported")
    actual_cost = require_int(request.get("actual_cost_cents", 0), "actual_cost_cents", 0)
    confirmed = request.get("confirmed", False)
    if type(confirmed) is not bool:
        raise ControllerError("invalid_request", "confirmed must be a boolean")
    note = optional_text(request.get("note"), "note", 2_000)
    with store.lock():
        state = store.load()
        if action_digest not in state["actions"]:
            raise ControllerError("unknown_action", "action digest is not planned")
        record = state["actions"][action_digest]
        if record["status"] not in {"planned", "pending_confirmation"}:
            if record["outcome"] == outcome and record["actual_cost_cents"] == actual_cost:
                return {"ok": True, "action": copy.deepcopy(record), "idempotent_replay": True, "campaign": safe_summary(state)}
            raise ControllerError("action_already_recorded", "action outcome has already been recorded")
        if record["requires_confirmation"] and outcome != "cancelled" and not confirmed:
            raise ControllerError("confirmation_required", "this action requires an actual host or user confirmation")
        budget = state["budget"]
        budget["reserved_cents"] = max(0, budget["reserved_cents"] - record["estimated_cost_cents"])
        budget["spent_cents"] += actual_cost
        daily = budget["daily"].setdefault(record["day"], {"spent_cents": 0, "action_count": 0})
        daily["spent_cents"] += actual_cost
        if record["kind"] in EXTERNAL_ACTION_KINDS and outcome != "cancelled":
            daily["action_count"] += 1
        record["status"] = "recorded"
        record["outcome"] = outcome
        record["actual_cost_cents"] = actual_cost
        record["recorded_at"] = iso_now()
        record["note"] = note
        kind = record["kind"]
        if outcome == "success":
            state["failure_streaks"][kind] = 0
        elif outcome == "failure":
            state["failure_streaks"][kind] = state["failure_streaks"].get(kind, 0) + 1
            if state["failure_streaks"][kind] >= 3:
                pause_with_checkpoint(state, f"three_consecutive_{kind}_failures")
        elif outcome == "unknown":
            pause_with_checkpoint(state, f"uncertain_{kind}_outcome")
        contract = state.get("authorization") or {}
        if budget["spent_cents"] > contract.get("total_budget_cents", 0) or daily["spent_cents"] > contract.get("daily_budget_cents", 0):
            pause_with_checkpoint(state, "recorded_cost_exceeded_authorized_budget")
        store.append_event(store.actions_path, {"event": "recorded", "recorded_at": iso_now(), **record})
        store.save(state)
        return {"ok": True, "action": copy.deepcopy(record), "idempotent_replay": False, "campaign": safe_summary(state)}


def handle_checkpoint(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    reason = require_text(request.get("reason"), "reason", 2_000)
    transition_to = optional_text(request.get("transition_to"), "transition_to", 40)
    with store.lock():
        state = store.load()
        require_active(state)
        checkpoint = {"recorded_at": iso_now(), "phase": state["phase"], "reason": reason}
        if transition_to:
            if state["phase"] == "build" and transition_to == "launch":
                if request.get("artifacts_ready") is not True:
                    raise ControllerError("stage_gate_failed", "build -> launch requires artifacts_ready: true")
            elif state["phase"] == "launch" and transition_to == "operate":
                successful_launch = any(
                    item["kind"] in EXTERNAL_ACTION_KINDS and item["outcome"] == "success"
                    for item in state["actions"].values()
                )
                if request.get("launch_verified") is not True or not successful_launch:
                    raise ControllerError("stage_gate_failed", "launch -> operate requires a verified successful external action")
            else:
                raise ControllerError("invalid_transition", f"cannot transition {state['phase']} -> {transition_to}")
            checkpoint["transition_to"] = transition_to
            state["phase"] = transition_to
        state["checkpoints"].append(checkpoint)
        state["checkpoints"] = state["checkpoints"][-100:]
        store.save(state)
        return {"ok": True, "checkpoint": checkpoint, "campaign": safe_summary(state)}


def handle_pause(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    reason = require_text(request.get("reason"), "reason", 2_000)
    with store.lock():
        state = store.load()
        require_active(state)
        pause_with_checkpoint(state, reason)
        store.save(state)
        return {"ok": True, "campaign": safe_summary(state)}


def handle_stop(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    reason = require_text(request.get("reason"), "reason", 2_000)
    with store.lock():
        state = store.load()
        if state["status"] == "complete":
            raise ControllerError("campaign_complete", "a completed campaign cannot be stopped")
        state["status"] = "stopped"
        state["phase"] = "stopped"
        state["stop_reason"] = reason
        store.save(state)
        return {"ok": True, "campaign": safe_summary(state)}


def handle_verify_revenue(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    payment = request.get("payment")
    if not isinstance(payment, dict):
        raise ControllerError("invalid_payment", "payment must be an object")
    payment = copy.deepcopy(payment)
    amount = parse_money(payment.get("amount"), "payment.amount")
    currency = require_text(payment.get("currency"), "payment.currency", 3).upper()
    proof_type = require_text(payment.get("proof_type"), "payment.proof_type", 40)
    verified_by = require_text(payment.get("verified_by"), "payment.verified_by", 80)
    if proof_type not in {"provider_event", "redacted_payment_record"}:
        raise ControllerError("unverified_revenue", "proof_type is not acceptable")
    if verified_by not in {"provider_connector", "redacted_document_review"}:
        raise ControllerError("unverified_revenue", "payment must be independently inspectable")
    settled_at = parse_time(payment.get("settled_at"), "payment.settled_at")
    required_exact = {
        "mode": "live",
        "status": "settled",
        "payer_relation": "external_customer",
    }
    for field, expected in required_exact.items():
        if payment.get(field) != expected:
            raise ControllerError("unverified_revenue", f"payment.{field} must be {expected}")
    for flag in ("refunded", "disputed", "self_purchase", "circular", "founder_transfer", "test_fixture", "coupon_only"):
        if payment.get(flag) is not False:
            raise ControllerError("unverified_revenue", f"payment.{flag} must be explicitly false")
    payment["proof_reference"] = require_text(payment.get("proof_reference"), "payment.proof_reference", 1_000)
    payment["fulfillment_obligation"] = require_text(payment.get("fulfillment_obligation"), "payment.fulfillment_obligation", 2_000)
    with store.lock():
        state = store.load()
        require_active(state)
        if state["phase"] != "operate":
            raise ControllerError("invalid_transition", "revenue can only be verified during operate")
        contract = ensure_authorized(state)
        offer = state.get("offer") or {}
        if payment.get("campaign_id") != state["campaign_id"] or payment.get("offer_id") != offer.get("id"):
            raise ControllerError("unverified_revenue", "payment must identify the current campaign and offer")
        created_at = parse_time(state["created_at"], "state.created_at")
        if settled_at < created_at or settled_at > utc_now() + timedelta(minutes=5):
            raise ControllerError("unverified_revenue", "payment.settled_at must fall within the current campaign timeline")
        if currency != contract["currency"] or amount < Decimal(contract["revenue_goal_amount"]):
            raise ControllerError("revenue_below_goal", "payment does not meet the authorized revenue goal")
        revenue = {
            "amount": str(amount),
            "currency": currency,
            "proof_type": proof_type,
            "verified_by": verified_by,
            "proof_reference": payment["proof_reference"],
            "settled_at": settled_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "offer_id": offer["id"],
            "fulfillment_obligation": payment["fulfillment_obligation"],
            "verified_at": iso_now(),
        }
        revenue["evidence_digest"] = digest(revenue)
        state["revenue"] = revenue
        state["phase"] = "revenue_verified"
        state["status"] = "complete"
        state["evidence_count"] += 1
        store.append_event(store.evidence_path, {"kind": "verified_revenue", "campaign_id": state["campaign_id"], **revenue})
        store.save(state)
        return {"ok": True, "status": "revenue_verified", "revenue": revenue, "campaign": safe_summary(state)}


def handle_inspect(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    include_profile = request.get("include_profile", False)
    if type(include_profile) is not bool:
        raise ControllerError("invalid_request", "include_profile must be a boolean")
    with store.lock():
        state = store.load()
        return {"ok": True, "campaign": safe_summary(state, include_profile=include_profile)}


def handle_export(store: CampaignStore) -> dict[str, Any]:
    with store.lock():
        state = store.load()
        exported = safe_summary(state, include_profile=False)
        exported["opportunities"] = [
            {key: value for key, value in item.items() if key != "evidence"}
            for item in state["opportunities"]
        ]
        exported["offer"] = copy.deepcopy(state.get("offer"))
        exported["authorization_digest"] = state.get("authorization_digest")
        store.write_json(store.export_path, exported)
        return {"ok": True, "export_path": str(store.export_path), "export_digest": digest(exported)}


def handle_delete(store: CampaignStore, request: dict[str, Any]) -> dict[str, Any]:
    with store.lock():
        state = store.load()
        preview_digest = digest({"campaign_id": state["campaign_id"], "updated_at": state["updated_at"], "action": "delete"})
        supplied = request.get("confirm_digest")
        if supplied is None:
            return {
                "ok": True,
                "deletion_preview": {
                    "campaign_id": state["campaign_id"],
                    "directory": str(store.directory),
                    "confirm_digest": preview_digest,
                    "recoverable": False,
                },
            }
        if supplied != preview_digest:
            raise ControllerError("delete_confirmation_mismatch", "confirm_digest does not match the current campaign state")
        reject_symlinked_components(store.directory)
        shutil.rmtree(store.directory)
        return {"ok": True, "deleted": True, "campaign_id": state["campaign_id"], "recoverable": False}


def dispatch(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    reject_secret_fields(request)
    command_value = request.get("command")
    if command_value is None and isinstance(request.get("action"), str):
        command_value = request.get("action")
    command = require_text(command_value, "command", 40)
    if command == "start":
        return start_action(root, request)
    store = load_request_store(root, request)
    handlers = {
        "resume": lambda: handle_resume(store, request),
        "answer": lambda: handle_answer(store, request),
        "rank": lambda: handle_rank(store, request),
        "select": lambda: handle_select(store, request),
        "record_evidence": lambda: handle_record_evidence(store, request),
        "authorize": lambda: handle_authorize(store, request),
        "plan_action": lambda: handle_plan_action(store, request),
        "record_action": lambda: handle_record_action(store, request),
        "checkpoint": lambda: handle_checkpoint(store, request),
        "pause": lambda: handle_pause(store, request),
        "stop": lambda: handle_stop(store, request),
        "verify_revenue": lambda: handle_verify_revenue(store, request),
        "inspect": lambda: handle_inspect(store, request),
        "export": lambda: handle_export(store),
        "delete": lambda: handle_delete(store, request),
    }
    if command not in handlers:
        raise ControllerError("unknown_action", f"unsupported command: {command}")
    return handlers[command]()


def read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ControllerError("input_too_large", f"request exceeds {MAX_INPUT_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerError("invalid_json", "stdin must contain one UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise ControllerError("invalid_json", "request must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Startup Autopilot local campaign controller")
    parser.add_argument("--state-dir", default=str(Path.cwd() / "work" / "startup-autopilot"))
    args = parser.parse_args()
    try:
        request = read_request()
        result = dispatch(Path(args.state_dir), request)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except ControllerError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": {"code": "internal_error", "message": "controller failed safely"}}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
