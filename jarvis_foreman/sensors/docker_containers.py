"""Check named Docker containers are running (docker CLI)."""

from __future__ import annotations

import shutil
import subprocess

from .base import Sensor, Signal


class DockerContainersSensor(Sensor):
    id = "docker_containers"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        names: list[str] = list(sc.get("containers") or [])
        if not names:
            return []
        if not shutil.which("docker"):
            return [
                Signal(
                    sensor_id=self.id,
                    severity="amber",
                    headline="docker CLI missing",
                    dedupe_key="docker:cli_missing",
                    detail={"error": "docker not on PATH"},
                )
            ]
        try:
            out = subprocess.check_output(
                ["docker", "ps", "--format", "{{.Names}}"],
                text=True,
                timeout=8,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            return [
                Signal(
                    sensor_id=self.id,
                    severity="amber",
                    headline="docker ps failed",
                    dedupe_key="docker:ps_failed",
                    detail={"error": str(e)},
                )
            ]
        running = {line.strip() for line in out.splitlines() if line.strip()}
        missing = [n for n in names if n not in running]
        if not missing:
            return [
                Signal(
                    sensor_id=self.id,
                    severity="green",
                    headline=f"containers ok ({len(names)})",
                    detail={"running": names},
                )
            ]
        sev = "red" if len(missing) == len(names) else "amber"
        return [
            Signal(
                sensor_id=self.id,
                severity=sev,
                headline=f"containers down: {', '.join(missing)}",
                # Which containers are down is the condition. Same set = same episode,
                # even if they flap; a different set is genuinely new news.
                dedupe_key="docker:down:" + ",".join(sorted(missing)),
                detail={"missing": missing, "running_checked": names},
            )
        ]
