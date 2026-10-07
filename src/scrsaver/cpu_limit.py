from __future__ import annotations

from pathlib import Path

# Typical "quite good" Windows 3.1 desktop: 386DX-33 (LA Times, 1992).
# Cheaper 386SX boxes were 16–25 MHz. 3D Maze is a Win95 OpenGL saver and
# would not have been a normal 3.1 program.
WIN31_MHZ = 33.0
CELERON_PERCENT = 15


def host_max_mhz() -> float:
    for candidate in (
        Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"),
        Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq"),
    ):
        try:
            khz = int(candidate.read_text().strip())
        except (OSError, ValueError):
            continue
        if khz > 0:
            return khz / 1000.0
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("cpu mhz"):
                return float(line.split(":")[1].strip())
    except (OSError, ValueError, IndexError):
        pass
    return 4000.0


def win31_quota_percent() -> float:
    """CPUQuota percent of one core that approximates a 33 MHz 386."""
    mhz = host_max_mhz()
    if mhz <= 0:
        return 1.0
    return max(0.3, WIN31_MHZ / mhz * 100.0)


def clamp_quota_percent(value: float) -> float:
    return max(win31_quota_percent(), min(100.0, float(value)))


def quota_property(percent: float) -> str:
    percent = clamp_quota_percent(percent)
    if percent >= 1:
        return f"CPUQuota={int(round(percent))}%"
    return f"CPUQuota={percent:.1f}%"


def estimated_mhz(percent: float) -> float:
    return host_max_mhz() * clamp_quota_percent(percent) / 100.0
