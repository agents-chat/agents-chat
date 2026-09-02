from __future__ import annotations

import os
import shutil

from .base import Sensor, Signal


class DiskSensor(Sensor):
    id = "disk"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        path = sc.get("path") or os.getcwd()
        amber_gb = float(sc.get("amber_free_gb", 10))
        red_gb = float(sc.get("red_free_gb", 2))
        try:
            usage = shutil.disk_usage(path)
        except Exception as e:
            return [
                Signal(
                    sensor_id=self.id,
                    severity="amber",
                    headline="disk check failed",
                    dedupe_key="disk:check_failed",
                    detail={"error": str(e), "path": path},
                )
            ]
        free_gb = usage.free / (1024**3)
        if free_gb <= red_gb:
            sev = "red"
        elif free_gb <= amber_gb:
            sev = "amber"
        else:
            sev = "green"
        return [
            Signal(
                sensor_id=self.id,
                severity=sev,
                headline=f"disk free {free_gb:.1f} GiB",
                # Free space drifts by megabytes every poll, so never key on the number.
                # Severity stays out of the key too: amber→red is an escalation of one
                # episode, which the bus handles, not a different problem.
                dedupe_key="disk:ok" if sev == "green" else "disk:low_space",
                detail={
                    "path": path,
                    "free_gb": round(free_gb, 2),
                    "total_gb": round(usage.total / (1024**3), 2),
                },
            )
        ]
