"""Which model and effort level an agent runs with.

Set in any of these `[agent]` tables, each optionally with per-kind sub-tables:

    [agent]                 # applies to work and review
    model = "fable-5.2"
    effort = "high"

    [agent.review]          # overrides the above for reviews
    model = "opus-5.5"

Precedence, highest first (the first table that sets a field wins, field by field):

    worker CLI flags (--model, --effort)
    task.toml          [agent.<kind>], then [agent]
    project.toml       [agent.<kind>], then [agent]
    host config        [agent.<kind>], then [agent]   (~/.config/afe-fleet/<fleet>.toml)
    built-in defaults  DEFAULTS below: Opus for work, Sonnet for review

Effort unset everywhere means the agent CLI's own default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Short names → model IDs. `fable`, `opus`, `sonnet`, `haiku` alone are passed through: the
# agent CLI resolves them to the latest model of that family. Full `claude-…` IDs pass through.
MODEL_ALIASES = {
    "fable-5.2": "claude-fable-5-2",
    "fable-5.1": "claude-fable-5-1",
    "opus-5.5": "claude-opus-5-5",
    "opus-5": "claude-opus-5",
    "sonnet-5.5": "claude-sonnet-5-5",
    "sonnet-5": "claude-sonnet-5",
    "haiku-5.5": "claude-haiku-5-5",
}
FAMILY_ALIASES = ("fable", "opus", "sonnet", "haiku")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
KINDS = ("work", "review")

# The lowest-precedence layer: what runs when nothing else says.
DEFAULTS = {"model": "opus-5.5", "review": {"model": "sonnet-5.5"}}


def normalize_model(name: object) -> str:
    m = str(name).strip()
    if m in MODEL_ALIASES:
        return MODEL_ALIASES[m]
    if m in FAMILY_ALIASES or re.fullmatch(r"claude-[a-z0-9][a-z0-9-]*", m):
        return m
    known = ", ".join([*MODEL_ALIASES, *FAMILY_ALIASES])
    raise ValueError(f"unknown model {m!r} (use one of {known}, or a full claude-… model ID)")


def normalize_effort(level: object) -> str:
    e = str(level).strip().lower()
    if e not in EFFORTS:
        raise ValueError(f"unknown effort {level!r} (use one of {', '.join(EFFORTS)})")
    return e


@dataclass(frozen=True)
class AgentChoice:
    model: str | None = None
    effort: str | None = None
    cli: str = "claude"

    def to_json(self) -> dict:
        out = {"model": self.model, "effort": self.effort}
        return out if self.cli == "claude" else {**out, "cli": self.cli}

    def describe(self) -> str:
        prefix = "" if self.cli == "claude" else f"{self.cli}: "
        return f"{prefix}{self.model or 'default model'}, {self.effort or 'default'} effort"

    def for_cli(self, cli: str) -> AgentChoice:
        """The choice as another agent CLI takes it. Models here are Claude models, so Codex keeps its
        own configured model (set one with -m in the command); effort carries over, `max` as `xhigh`."""
        if cli == "claude":
            return self
        effort = "xhigh" if self.effort == "max" else self.effort
        return AgentChoice(None, effort, cli)


def _fields(table: object, kind: str) -> list[dict]:
    """The dicts to consult in one layer, most specific first: [agent.<kind>], then [agent]."""
    if not isinstance(table, dict):
        return []
    sub = table.get(kind)
    return ([sub] if isinstance(sub, dict) else []) + [table]


def choose(kind: str, *layers: object) -> AgentChoice:
    """Resolve model and effort for `kind` from `[agent]` tables, highest precedence first.
    Raises ValueError for an unknown model or effort in any layer that is consulted."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    model = effort = None
    for layer in layers:
        for t in _fields(layer, kind):
            if model is None and t.get("model") is not None:
                model = normalize_model(t["model"])
            if effort is None and t.get("effort") is not None:
                effort = normalize_effort(t["effort"])
    return AgentChoice(model, effort)


def validate(table: object, where: str) -> None:
    """Check every model/effort value in an `[agent]` table, for both kinds."""
    if table is None:
        return
    if not isinstance(table, dict):
        raise ValueError(f"{where}: [agent] must be a table")
    try:
        for kind in KINDS:
            choose(kind, table)
    except ValueError as e:
        raise ValueError(f"{where}: {e}") from None


def cli_args(choice: AgentChoice, model_args: list[str], effort_args: list[str]) -> list[str]:
    """Arguments to append to the agent command, e.g. ["--model", "claude-opus-5-5", "--effort", "high"]."""
    out: list[str] = []
    if choice.model:
        out += [a.replace("{model}", choice.model) for a in model_args]
    if choice.effort:
        out += [a.replace("{effort}", choice.effort) for a in effort_args]
    return out
