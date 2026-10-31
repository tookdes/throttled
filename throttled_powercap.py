"""Intel RAPL powercap helpers.

The kernel exposes MSR-backed RAPL under intel-rapl and, on supported
platforms, the chipset/MCHBAR mirror under intel-rapl-mmio.  Prefer the MMIO
powercap zone over /dev/mem when it exists: the kernel owns platform discovery,
addressing, and access policy.
"""

from pathlib import Path
import re


DEFAULT_POWERCAP_ROOT = Path("/sys/devices/virtual/powercap")


class PowercapError(RuntimeError):
    pass


def _read_text(path):
    try:
        return Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_int(path):
    value = _read_text(path)
    if value is None:
        return None
    try:
        return int(value, 10)
    except ValueError:
        return None


class PowercapZone:
    def __init__(self, path):
        self.path = Path(path)

    @property
    def name(self):
        return _read_text(self.path / "name")

    def constraint_indices(self):
        indices = []
        for entry in self.path.glob("constraint_*_power_limit_uw"):
            match = re.fullmatch(r"constraint_(\d+)_power_limit_uw", entry.name)
            if match:
                indices.append(int(match.group(1)))
        return sorted(set(indices))

    def constraint_name(self, index):
        return _read_text(self.path / f"constraint_{index}_name")

    def _role_index(self, role):
        aliases = {
            "pl1": {"long_term", "long-term", "sustained", "constraint_0"},
            "pl2": {"short_term", "short-term", "burst", "constraint_1"},
        }
        indices = self.constraint_indices()
        for index in indices:
            name = (self.constraint_name(index) or "").strip().lower()
            if name in aliases[role]:
                return index

        fallback = 0 if role == "pl1" else 1
        return fallback if fallback in indices else None

    def _constraint_paths(self, role):
        index = self._role_index(role)
        if index is None:
            return None
        return {
            "index": index,
            "name": self.path / f"constraint_{index}_name",
            "power": self.path / f"constraint_{index}_power_limit_uw",
            "time": self.path / f"constraint_{index}_time_window_us",
            "min_power": self.path / f"constraint_{index}_min_power_uw",
            "max_power": self.path / f"constraint_{index}_max_power_uw",
            "min_time": self.path / f"constraint_{index}_min_time_window_us",
            "max_time": self.path / f"constraint_{index}_max_time_window_us",
        }

    def read_constraint(self, role):
        paths = self._constraint_paths(role)
        if paths is None:
            return None
        return {
            "index": paths["index"],
            "name": _read_text(paths["name"]),
            "power_limit_uw": _read_int(paths["power"]),
            "time_window_us": _read_int(paths["time"]),
            "min_power_uw": _read_int(paths["min_power"]),
            "max_power_uw": _read_int(paths["max_power"]),
            "min_time_window_us": _read_int(paths["min_time"]),
            "max_time_window_us": _read_int(paths["max_time"]),
        }

    def read(self):
        return {
            "path": str(self.path),
            "name": self.name,
            "enabled": _read_int(self.path / "enabled"),
            "pl1": self.read_constraint("pl1"),
            "pl2": self.read_constraint("pl2"),
        }

    def snapshot(self):
        return self.read()

    @staticmethod
    def _write_int(path, value):
        try:
            Path(path).write_text(str(int(value)), encoding="ascii")
        except OSError as exc:
            raise PowercapError(f"unable to write {path}: {exc}") from exc

    def write(self, *, pl1_w=None, pl2_w=None, tau1_s=None, tau2_s=None):
        requested = {
            "pl1": {"power": pl1_w, "time": tau1_s},
            "pl2": {"power": pl2_w, "time": tau2_s},
        }
        touched = []
        try:
            for role, values in requested.items():
                paths = self._constraint_paths(role)
                if paths is None:
                    if any(value is not None for value in values.values()):
                        raise PowercapError(f"{self.path}: {role.upper()} constraint is unavailable")
                    continue

                if values["power"] is not None:
                    before = _read_int(paths["power"])
                    if before is None:
                        raise PowercapError(f"unable to read {paths['power']}")
                    target = round(values["power"] * 1_000_000)
                    minimum = _read_int(paths["min_power"])
                    maximum = _read_int(paths["max_power"])
                    if minimum is not None and target < minimum:
                        raise PowercapError(f"{role.upper()} {target} uW is below kernel minimum {minimum} uW")
                    if maximum is not None and target > maximum:
                        raise PowercapError(f"{role.upper()} {target} uW is above kernel maximum {maximum} uW")
                    if target != before:
                        touched.append((paths["power"], before))
                        self._write_int(paths["power"], target)

                if values["time"] is not None:
                    before = _read_int(paths["time"])
                    if before is None:
                        raise PowercapError(f"unable to read {paths['time']}")
                    target = round(values["time"] * 1_000_000)
                    minimum = _read_int(paths["min_time"])
                    maximum = _read_int(paths["max_time"])
                    if minimum is not None and target < minimum:
                        raise PowercapError(f"{role.upper()} {target} us is below kernel minimum {minimum} us")
                    if maximum is not None and target > maximum:
                        raise PowercapError(f"{role.upper()} {target} us is above kernel maximum {maximum} us")
                    if target != before:
                        touched.append((paths["time"], before))
                        self._write_int(paths["time"], target)
        except PowercapError as exc:
            rollback_errors = []
            for path, original in reversed(touched):
                try:
                    self._write_int(path, original)
                    if _read_int(path) != original:
                        rollback_errors.append(f"{path}: verify mismatch")
                except PowercapError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            suffix = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else "; rollback attempted"
            raise PowercapError(str(exc) + suffix) from exc

        return self.read()

    def restore(self, snapshot):
        kwargs = {}
        for role, prefix in (("pl1", "1"), ("pl2", "2")):
            item = snapshot.get(role)
            if not item:
                continue
            if item.get("power_limit_uw") is not None:
                kwargs[f"pl{prefix}_w"] = item["power_limit_uw"] / 1_000_000
            if item.get("time_window_us") is not None:
                kwargs[f"tau{prefix}_s"] = item["time_window_us"] / 1_000_000
        return self.write(**kwargs)


def _find_package_zone(driver, root=DEFAULT_POWERCAP_ROOT):
    driver_root = Path(root) / driver
    if not driver_root.is_dir():
        return None

    candidates = []
    for path in sorted(driver_root.glob(f"{driver}:*")):
        if not path.is_dir():
            continue
        zone = PowercapZone(path)
        name = (zone.name or "").lower()
        score = 0 if name in ("package-0", "package_0") else 1
        # Top-level package zones have one colon in the basename. Child domains
        # (core/uncore/dram) have additional colon components.
        score += path.name.count(":")
        candidates.append((score, path.name, zone))
    return min(candidates, default=(None, None, None))[2]


def find_msr_package_zone(root=DEFAULT_POWERCAP_ROOT):
    return _find_package_zone("intel-rapl", root)


def find_mmio_package_zone(root=DEFAULT_POWERCAP_ROOT):
    return _find_package_zone("intel-rapl-mmio", root)


def probe_powercap(root=DEFAULT_POWERCAP_ROOT):
    result = {}
    for label, finder in (("msr", find_msr_package_zone), ("mmio", find_mmio_package_zone)):
        zone = finder(root)
        result[label] = None if zone is None else zone.read()
    return result
