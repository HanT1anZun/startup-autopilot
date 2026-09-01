#!/usr/bin/env python3
"""Offline integrity validator for Startup Autopilot campaign state."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from autopilot import (
    ACTION_OUTCOMES,
    EXTERNAL_ACTION_KINDS,
    SCHEMA_VERSION,
    CampaignStore,
    ControllerError,
    authorization_health,
    canonical_json,
    digest,
    is_link_like,
    reject_symlinked_components,
    validate_campaign_id,
)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError("invalid_file", f"cannot read valid JSON from {path.name}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise ControllerError("invalid_file", f"{path.name}:{line_number} is blank")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ControllerError("invalid_file", f"{path.name}:{line_number} is not an object")
                events.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError("invalid_file", f"cannot read valid JSONL from {path.name}") from exc
    return events


def find_symlinks(directory: Path) -> list[str]:
    found: list[str] = []
    for current, dirnames, filenames in os.walk(directory, topdown=True, followlinks=False):
        current_path = Path(current)
        retained: list[str] = []
        for name in dirnames:
            child = current_path / name
            if is_link_like(child):
                found.append(str(child.relative_to(directory)))
            else:
                retained.append(name)
        dirnames[:] = retained
        for name in filenames:
            child = current_path / name
            if is_link_like(child):
                found.append(str(child.relative_to(directory)))
    return found


def validate_actions(state: dict[str, Any], errors: list[str]) -> None:
    actions = state["actions"]
    reserved = 0
    spent = 0
    expected_daily: dict[str, dict[str, int]] = {}
    idempotency_keys: set[str] = set()
    for key, record in actions.items():
        if not isinstance(record, dict):
            errors.append(f"action {key} is not an object")
            continue
        if key != record.get("digest") or digest(record.get("fingerprint")) != key:
            errors.append(f"action {key} digest does not match its fingerprint")
        idem = record.get("idempotency_key")
        if not isinstance(idem, str) or not idem:
            errors.append(f"action {key} has no idempotency key")
        elif idem in idempotency_keys:
            errors.append(f"duplicate idempotency key: {idem}")
        else:
            idempotency_keys.add(idem)
        status = record.get("status")
        estimated = record.get("estimated_cost_cents")
        actual = record.get("actual_cost_cents")
        day = record.get("day")
        if type(estimated) is not int or estimated < 0:
            errors.append(f"action {key} has invalid estimated cost")
            continue
        if not isinstance(day, str):
            errors.append(f"action {key} has invalid day")
            continue
        daily = expected_daily.setdefault(day, {"spent_cents": 0, "action_count": 0})
        if status in {"planned", "pending_confirmation"}:
            reserved += estimated
            if actual is not None:
                errors.append(f"pending action {key} already has an actual cost")
        elif status == "recorded":
            if type(actual) is not int or actual < 0:
                errors.append(f"recorded action {key} has invalid actual cost")
                continue
            spent += actual
            daily["spent_cents"] += actual
            outcome = record.get("outcome")
            if outcome not in ACTION_OUTCOMES:
                errors.append(f"recorded action {key} has invalid outcome")
            if record.get("kind") in EXTERNAL_ACTION_KINDS and outcome != "cancelled":
                daily["action_count"] += 1
        else:
            errors.append(f"action {key} has invalid status")
    budget = state["budget"]
    if budget.get("reserved_cents") != reserved:
        errors.append("budget.reserved_cents does not match pending action reservations")
    if budget.get("spent_cents") != spent:
        errors.append("budget.spent_cents does not match recorded action costs")
    actual_daily = budget.get("daily")
    if not isinstance(actual_daily, dict) or canonical_json(actual_daily) != canonical_json(expected_daily):
        errors.append("budget.daily does not match recorded actions")


def validate_authorization(store: CampaignStore, state: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
    authorization = state.get("authorization")
    if authorization is None:
        if store.authorization_path.exists():
            errors.append("authorization.json exists but state has no authorization")
        return
    expected = digest({"offer": state.get("offer"), "contract": authorization})
    if state.get("authorization_digest") != expected:
        errors.append("authorization digest mismatch")
    if not store.authorization_path.is_file():
        errors.append("authorization.json is missing")
        return
    document = read_json(store.authorization_path)
    if not isinstance(document, dict):
        errors.append("authorization.json is not an object")
        return
    expected_document = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": state["campaign_id"],
        "offer": state.get("offer"),
        "contract": authorization,
        "digest": expected,
        "approved_at": state.get("authorization_approved_at"),
    }
    if canonical_json(document) != canonical_json(expected_document):
        errors.append("authorization.json does not match state")
    try:
        healthy, reason = authorization_health(state)
        if not healthy:
            warnings.append(f"authorization is not current: {reason}")
    except ControllerError as exc:
        errors.append(f"authorization is malformed: {exc.code}")


def validate_campaign(store: CampaignStore) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    state = store.load()
    symlinks = find_symlinks(store.directory)
    if symlinks:
        errors.append("symlinked campaign entries are not allowed: " + ", ".join(symlinks))
    if not store.artifacts_path.is_dir():
        errors.append("artifacts directory is missing")
    validate_authorization(store, state, errors, warnings)
    validate_actions(state, errors)
    evidence_events = read_jsonl(store.evidence_path)
    action_events = read_jsonl(store.actions_path)
    if state.get("evidence_count") != len(evidence_events):
        errors.append("evidence_count does not match evidence.jsonl")
    planned_digests = {
        event.get("digest") for event in action_events if event.get("event") == "planned"
    }
    if planned_digests != set(state["actions"]):
        errors.append("actions.jsonl planned records do not match state actions")
    return {
        "campaign_id": state["campaign_id"],
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "phase": state["phase"],
        "status": state["status"],
        "counts": {
            "actions": len(state["actions"]),
            "action_events": len(action_events),
            "evidence_events": len(evidence_events),
        },
    }


def campaign_ids(root: Path, selected: str | None) -> list[str]:
    if selected is not None:
        return [validate_campaign_id(selected)]
    result: list[str] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_dir() and not is_link_like(child) and (child / "state.json").is_file():
            result.append(validate_campaign_id(child.name))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Startup Autopilot state without network access")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--campaign-id")
    args = parser.parse_args()
    try:
        root = Path(args.state_dir).expanduser().absolute()
        reject_symlinked_components(root)
        if not root.is_dir():
            raise ControllerError("state_root_not_found", "state directory does not exist")
        ids = campaign_ids(root, args.campaign_id)
        if not ids:
            raise ControllerError("campaign_not_found", "no campaigns were found")
        results = [validate_campaign(CampaignStore(root, item)) for item in ids]
        payload = {"ok": all(item["ok"] for item in results), "campaigns": results}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload["ok"] else 1
    except ControllerError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": {"code": "internal_error", "message": "state validation failed safely"}}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
