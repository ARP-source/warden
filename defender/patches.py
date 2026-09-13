"""Patch construction, validation and safety guards.

A patch produced by a model is untrusted input. Three guards stand between a
proposal and the running Target:

1. *Whitelist* - a policy patch may only set parameters that
   ``target.policy.PATCHABLE_CHECKS`` names, and a prompt patch may only add
   clause tags from ``target.prompt.DEFENSE_CLAUSES``. Nothing can introduce new
   executable logic or arbitrary prompt text.
2. *Bounds* - numeric parameters must sit inside the ranges declared alongside
   the whitelist.
3. *Monotonic hardening* - a patch may only tighten. Disabling a check, raising a
   refund ceiling or lowering a justification requirement is refused. Without
   this, the loop could "fix" a failing attack by quietly relaxing the very
   control that was supposed to stop it.

Reverting is separate from patching: a rollback re-activates an earlier committed
version rather than proposing a looser one, so guard 3 never blocks a revert.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from target.policy import PATCHABLE_CHECKS, PolicyValidationError, validate_policy
from target.prompt import DEFENSE_CLAUSES, PromptValidationError, validate_prompt

# For each numeric knob: the direction that counts as tightening.
# "down" means a smaller value is stricter; "up" means a larger value is stricter.
TIGHTEN_DIRECTION: dict[tuple[str, str], str] = {
    ("refund_per_call_limit", "limit_usd"): "down",
    ("refund_session_cap", "cap_usd"): "down",
    ("email_requires_justification", "min_chars"): "up",
}


class PatchRejected(ValueError):
    """A proposed patch is not safe to install."""


@dataclass
class ProposedPatch:
    """A validated, ready-to-commit patch."""

    diagnosis: str
    patch_kind: str
    policy_doc: dict[str, Any] | None = None
    prompt_doc: dict[str, Any] | None = None
    policy_changes: dict[str, Any] = field(default_factory=dict)
    prompt_added: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def touches_policy(self) -> bool:
        return self.policy_doc is not None

    @property
    def touches_prompt(self) -> bool:
        return self.prompt_doc is not None

    def summary(self) -> dict[str, Any]:
        return {
            "diagnosis": self.diagnosis,
            "patch_kind": self.patch_kind,
            "policy_changes": self.policy_changes,
            "prompt_added": self.prompt_added,
        }


def _assert_hardening_only(current: dict[str, Any], proposed: dict[str, Any]) -> None:
    """Refuse any change that loosens enforcement."""
    for name, conf in proposed.get("checks", {}).items():
        cur = current.get("checks", {}).get(name, {})
        if cur.get("enabled") is True and conf.get("enabled") is False:
            raise PatchRejected(
                f"patch would disable check {name!r}; patches may only tighten enforcement"
            )
        for key, value in conf.items():
            if key == "enabled":
                continue
            if key not in cur:
                continue
            direction = TIGHTEN_DIRECTION.get((name, key))
            if direction is None:
                continue
            before, after = float(cur[key]), float(value)
            if direction == "down" and after > before:
                raise PatchRejected(
                    f"patch would raise {name}.{key} from {before} to {after}, "
                    f"which loosens the control"
                )
            if direction == "up" and after < before:
                raise PatchRejected(
                    f"patch would lower {name}.{key} from {before} to {after}, "
                    f"which loosens the control"
                )


def build_patch(proposal: dict[str, Any], current_policy: dict[str, Any],
                current_prompt: dict[str, Any]) -> ProposedPatch:
    """Turn a model proposal into a validated patch, or refuse it.

    Raises PatchRejected with a reason a human can act on. The reason is fed back
    to the model on retry, so a rejected patch is a correctable mistake rather
    than a dead end.
    """
    if not isinstance(proposal, dict):
        raise PatchRejected("proposal must be a JSON object")

    diagnosis = str(proposal.get("diagnosis", "")).strip() or "no diagnosis given"
    checks = proposal.get("policy_checks") or {}
    defenses = proposal.get("prompt_defenses") or []

    if not isinstance(checks, dict):
        raise PatchRejected("policy_checks must be an object")
    if not isinstance(defenses, list):
        raise PatchRejected("prompt_defenses must be a list of clause tags")
    if not checks and not defenses:
        raise PatchRejected("proposal changes nothing; name at least one check or clause")

    policy_doc: dict[str, Any] | None = None
    policy_changes: dict[str, Any] = {}
    if checks:
        unknown = [k for k in checks if k not in PATCHABLE_CHECKS]
        if unknown:
            raise PatchRejected(
                "unknown checks " + ", ".join(sorted(unknown))
                + "; allowed checks are " + ", ".join(sorted(PATCHABLE_CHECKS))
            )
        merged = copy.deepcopy(current_policy)
        merged.setdefault("checks", {})
        for name, conf in checks.items():
            if not isinstance(conf, dict):
                raise PatchRejected(f"check {name!r} must be an object")
            before = copy.deepcopy(merged["checks"].get(name, {}))
            merged["checks"].setdefault(name, {}).update(conf)
            after = merged["checks"][name]
            if before != after:
                policy_changes[name] = {"before": before, "after": copy.deepcopy(after)}
        try:
            validated = validate_policy(merged)
        except PolicyValidationError as exc:
            raise PatchRejected(f"policy patch is invalid: {exc}") from exc
        _assert_hardening_only(current_policy, validated)
        if not policy_changes:
            policy_doc = None
        else:
            validated["note"] = diagnosis[:300]
            policy_doc = validated

    prompt_doc: dict[str, Any] | None = None
    added: list[str] = []
    if defenses:
        unknown_tags = [t for t in defenses if t not in DEFENSE_CLAUSES]
        if unknown_tags:
            raise PatchRejected(
                "unknown defence clauses " + ", ".join(sorted(str(t) for t in unknown_tags))
                + "; allowed clauses are " + ", ".join(sorted(DEFENSE_CLAUSES))
            )
        existing = list(current_prompt.get("clauses", []))
        added = [t for t in defenses if t not in existing]
        if added:
            candidate = {"base": current_prompt.get("base", ""), "clauses": existing + added}
            try:
                prompt_doc = validate_prompt(candidate)
            except PromptValidationError as exc:
                raise PatchRejected(f"prompt patch is invalid: {exc}") from exc

    if policy_doc is None and prompt_doc is None:
        raise PatchRejected(
            "proposal is a no-op: every named check and clause is already in force"
        )

    kind = ("both" if policy_doc and prompt_doc else ("policy" if policy_doc else "prompt"))
    return ProposedPatch(
        diagnosis=diagnosis, patch_kind=kind, policy_doc=policy_doc, prompt_doc=prompt_doc,
        policy_changes=policy_changes, prompt_added=added, raw=proposal,
    )
