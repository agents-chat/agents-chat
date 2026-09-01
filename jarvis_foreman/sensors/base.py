from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import ForemanConfig


@dataclass
class Signal:
    sensor_id: str
    severity: str  # green|amber|red
    headline: str
    detail: dict[str, Any] = field(default_factory=dict)
    # Stable identity of the *underlying condition*, used for noise control.
    # Never derive this from `headline`: headlines carry live counts ("451 runs")
    # that churn on every poll, which mints a fresh cooldown slot each tick and
    # defeats suppression entirely. Name the thing that is broken, not its metrics.
    dedupe_key: str = ""

    def key(self) -> str:
        return self.dedupe_key or f"{self.sensor_id}:{self.severity}"


class Sensor(ABC):
    id: str = "base"

    def __init__(self, cfg: "ForemanConfig"):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.sensor_cfg(self.id).get("enabled", True))

    @abstractmethod
    def poll(self) -> list[Signal]:
        """Return zero or more signals. Prefer one summary signal per poll."""
        ...
