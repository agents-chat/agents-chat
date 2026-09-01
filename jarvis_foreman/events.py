"""Stable event contract for the foreman stack (versioned)."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class EventKind(str, Enum):
    SIGNAL = "signal"
    FLAG = "flag"
    HANDOFF = "handoff"
    ACK = "ack"
    CHANNEL_OUT = "channel_out"


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:16]}"


class EpisodeState(str, Enum):
    """Where a flag sits in the life of one continuous problem."""

    NEW = "new"  # first time we've seen this condition
    ONGOING = "ongoing"  # same condition, re-surfaced after a cooldown
    RECOVERED = "recovered"  # the condition cleared


@dataclass
class FlagEvent:
    """One unit of foreman traffic. Field `v` is the schema version.

    Episode fields (`state`/`repeats`/`since_ts`) are additive at v1: consumers
    that predate them read the defaults and behave exactly as before.
    """

    source: str
    kind: str
    severity: str
    headline: str
    v: int = 1
    id: str = field(default_factory=new_event_id)
    ts: float = field(default_factory=time.time)
    detail: dict[str, Any] = field(default_factory=dict)
    sensor_id: str | None = None
    target_seat: str = "senior_pm"
    channel_hints: list[str] = field(default_factory=list)
    requires_owner: bool = False
    # Noise-control context, so a channel can say "still red, 23× since 12:15a"
    # in one message instead of posting twenty-three of them.
    state: str = EpisodeState.NEW.value
    repeats: int = 0
    since_ts: float | None = None
    dedupe_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FlagEvent":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
