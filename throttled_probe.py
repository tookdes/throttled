#!/usr/bin/env python3
"""Read-oriented hardware probe for the HP EliteBook 830 G6 target.

This intentionally lives beside throttled.py while the 830 G6 bring-up is in
progress.  It does not import throttled.py, so running a probe cannot
accidentally enter the daemon's write/reapply path.

The normal probe only performs RDMSR and sysfs/PCI reads.  Snapshot restore is
available, but requires both --restore-snapshot and --apply-restore.
"""

import argparse
import json
import os
import re
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from throttled_powercap import PowercapError, find_mmio_package_zone, probe_powercap
from throttled_mchbar import (
    MCHBAR_ADDRESS_MASKS_BY_PCI_DEVICE,
    MCHBAR_ENABLE_BIT,
    MCHBAR_PACKAGE_POWER_LIMIT_OFFSET,
    MCHBAR_PACKAGE_POWER_LIMIT_SIZE,
    MCHBAR_ADDRESS_MASK_39_15,
    MCHBAR_ADDRESS_MASK_39_17,
    MCHBAR_ADDRESS_MASK_42_17,
)

try:
    from mmio import MMIO, MMIOError
except ImportError:
    MMIO = None

    class MMIOError(Exception):
        pass


TARGET_DMI_PRODUCT = "HP EliteBook 830 G6"
SNAPSHOT_SCHEMA_VERSION = 2
TARGET_CPUIDS = {
    # Intel microcode guidance identifies Whiskey Lake-U as CPUID 806EB/806EC.
    (6, 142, 11): "Whiskey Lake-U (806EB)",
    (6, 142, 12): "Whiskey Lake-U (806EC)",
}

MSR = {
    "MSR_PLATFORM_INFO": 0xCE,
    "MSR_OC_MAILBOX": 0x150,
    "IA32_APERF": 0xE8,
    "IA32_MPERF": 0xE7,
    "IA32_MISC_ENABLE": 0x1A0,
    "IA32_PERF_CTL": 0x199,
    "IA32_PERF_STATUS": 0x198,
    "IA32_CLOCK_MODULATION": 0x19A,
    "IA32_THERM_STATUS": 0x19C,
    "MSR_TEMPERATURE_TARGET": 0x1A2,
    "MSR_TURBO_RATIO_LIMIT": 0x1AD,
    "MSR_POWER_CTL": 0x1FC,
    "IA32_PACKAGE_THERM_STATUS": 0x1B1,
    "MSR_RAPL_POWER_UNIT": 0x606,
    "MSR_PKG_POWER_LIMIT": 0x610,
    "MSR_PKG_ENERGY_STATUS": 0x611,
    "MSR_CONFIG_TDP_CONTROL": 0x64B,
    "MSR_PERF_LIMIT_REASONS": 0x64F,
    "IA32_PM_ENABLE": 0x770,
    "IA32_HWP_CAPABILITIES": 0x771,
    "IA32_HWP_REQUEST_PKG": 0x772,
    "IA32_HWP_REQUEST": 0x774,
}

# Snapshot only registers already controlled by throttled or needed for the
# first 830 G6 control milestones.  OC mailbox state is deliberately excluded:
# reading FIVR offsets itself requires a WRMSR mailbox transaction.
SNAPSHOT_PACKAGE_REGS = (
    "MSR_PKG_POWER_LIMIT",
    "MSR_TEMPERATURE_TARGET",
    "MSR_CONFIG_TDP_CONTROL",
    "MSR_TURBO_RATIO_LIMIT",
    "IA32_HWP_REQUEST_PKG",
)
SNAPSHOT_PER_CPU_REGS = (
    "MSR_POWER_CTL",
    "IA32_HWP_REQUEST",
    "IA32_MISC_ENABLE",
    "IA32_CLOCK_MODULATION",
    "IA32_PERF_CTL",
)

PCI_HOST_BRIDGE_SYSFS_PATH = Path("/sys/bus/pci/devices/0000:00:00.0")


# MSR 0x64F layout for Skylake/Kaby/Coffee/Whiskey Lake (Intel SDM
# Table 2-39). Do not substitute the Haswell/Broadwell 0x690 layout.
CORE_PERF_LIMIT_BITS = {
    0: "PROCHOT",
    1: "Thermal",
    4: "Residency-Regulation",
    5: "RATL",
    6: "VR-Thermal",
    7: "VR-TDC",
    8: "Other/EDP",
    10: "Package-PL1",
    11: "Package-PL2",
    12: "Max-Turbo",
    13: "Turbo-Transition",
}

THERM_STATUS_BITS = {
    0: "Thermal",
    2: "PROCHOT",
    4: "Critical-Temp",
    6: "Threshold-1",
    8: "Threshold-2",
    10: "Power-Limit",
    12: "Current-Limit",
    14: "Cross-Domain",
}


class ProbeError(RuntimeError):
    pass


def bit(value, number):
    return bool((value >> number) & 1)


def parse_cpuinfo(text):
    """Parse the first processor block from /proc/cpuinfo."""
    block = text.split("\n\n", 1)[0]
    values = {}
    for row in block.splitlines():
        if ":" not in row:
            continue
        key, value = (item.strip() for item in row.split(":", 1))
        values[key] = value

    required = ("vendor_id", "cpu family", "model", "stepping")
    missing = [key for key in required if key not in values]
    if missing:
        raise ProbeError("cpuinfo is missing: " + ", ".join(missing))

    flags = set(values.get("flags", "").split())
    return {
        "vendor_id": values["vendor_id"],
        "family": int(values["cpu family"], 0),
        "model": int(values["model"], 0),
        "stepping": int(values["stepping"], 0),
        "model_name": values.get("model name", "unknown"),
        "microcode": values.get("microcode"),
        "flags": flags,
    }


def read_cpuinfo(path="/proc/cpuinfo"):
    return parse_cpuinfo(Path(path).read_text(encoding="utf-8", errors="replace"))


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def read_dmi():
    return {
        "vendor": read_text("/sys/class/dmi/id/sys_vendor"),
        "product": read_text("/sys/class/dmi/id/product_name"),
        "version": read_text("/sys/class/dmi/id/product_version"),
        "bios_vendor": read_text("/sys/class/dmi/id/bios_vendor"),
        "bios_version": read_text("/sys/class/dmi/id/bios_version"),
        "bios_date": read_text("/sys/class/dmi/id/bios_date"),
    }


def read_lockdown():
    value = read_text("/sys/kernel/security/lockdown")
    if value is None:
        return "unknown"
    match = re.search(r"\[([^]]+)\]", value)
    return match.group(1) if match else value


class MsrAccess:
    def __init__(self, root="/dev/cpu"):
        self.root = Path(root)

    def ensure(self):
        if self.cpus():
            return
        try:
            subprocess.run(
                ["modprobe", "msr"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ProbeError(f"unable to load msr module: {exc}") from exc
        if not self.cpus():
            raise ProbeError("no /dev/cpu/*/msr devices found after loading msr")

    def cpus(self):
        if not self.root.exists():
            return []
        cpus = []
        for entry in self.root.iterdir():
            if entry.name.isdigit() and (entry / "msr").exists():
                cpus.append(int(entry.name))
        return sorted(cpus)

    def _path(self, cpu):
        return self.root / str(cpu) / "msr"

    def read_addr(self, cpu, address):
        try:
            fd = os.open(self._path(cpu), os.O_RDONLY)
            try:
                os.lseek(fd, address, os.SEEK_SET)
                data = os.read(fd, 8)
            finally:
                os.close(fd)
        except OSError as exc:
            raise ProbeError(f"RDMSR cpu{cpu} 0x{address:x} failed: {exc}") from exc
        if len(data) != 8:
            raise ProbeError(f"RDMSR cpu{cpu} 0x{address:x} returned {len(data)} bytes")
        return struct.unpack("<Q", data)[0]

    def read(self, cpu, name):
        return self.read_addr(cpu, MSR[name])

    def write_addr(self, cpu, address, value):
        try:
            fd = os.open(self._path(cpu), os.O_WRONLY)
            try:
                os.lseek(fd, address, os.SEEK_SET)
                written = os.write(fd, struct.pack("<Q", value))
            finally:
                os.close(fd)
        except OSError as exc:
            raise ProbeError(f"WRMSR cpu{cpu} 0x{address:x} failed: {exc}") from exc
        if written != 8:
            raise ProbeError(f"WRMSR cpu{cpu} 0x{address:x} wrote {written} bytes")

    def write(self, cpu, name, value):
        self.write_addr(cpu, MSR[name], value)


def try_read(msr, cpu, name):
    try:
        return msr.read(cpu, name), None
    except ProbeError as exc:
        return None, str(exc)


def decode_rapl_units(raw):
    return {
        "power_w": 1.0 / (2 ** (raw & 0xF)),
        "energy_j": 1.0 / (2 ** ((raw >> 8) & 0x1F)),
        "time_s": 1.0 / (2 ** ((raw >> 16) & 0xF)),
    }


def decode_time_window(field, time_unit):
    y = field & 0x1F
    z = (field >> 5) & 0x3
    return (2 ** y) * (1.0 + z / 4.0) * time_unit


def decode_pkg_power_limit(raw, units):
    power_unit = units["power_w"]
    time_unit = units["time_s"]
    return {
        "raw": raw,
        "locked": bit(raw, 63),
        "pl1": {
            "enabled": bit(raw, 15),
            "clamp": bit(raw, 16),
            "watts": (raw & 0x7FFF) * power_unit,
            "seconds": decode_time_window((raw >> 17) & 0x7F, time_unit),
        },
        "pl2": {
            "enabled": bit(raw, 47),
            "clamp": bit(raw, 48),
            "watts": ((raw >> 32) & 0x7FFF) * power_unit,
            "seconds": decode_time_window((raw >> 49) & 0x7F, time_unit),
        },
    }


def decode_hwp_capabilities(raw):
    return {
        "highest": raw & 0xFF,
        "guaranteed": (raw >> 8) & 0xFF,
        "most_efficient": (raw >> 16) & 0xFF,
        "lowest": (raw >> 24) & 0xFF,
    }


def decode_hwp_request(raw):
    return {
        "min": raw & 0xFF,
        "max": (raw >> 8) & 0xFF,
        "desired": (raw >> 16) & 0xFF,
        "epp": (raw >> 24) & 0xFF,
        "activity_window": (raw >> 32) & 0x3FF,
        "package_control": bit(raw, 42),
    }


def decode_turbo_ratio_limit(raw, max_cores=8):
    return {str(cores): (raw >> (8 * (cores - 1))) & 0xFF for cores in range(1, max_cores + 1)}


def decode_perf_limit_reasons(raw):
    active = [name for number, name in CORE_PERF_LIMIT_BITS.items() if bit(raw, number)]
    logged = [name for number, name in CORE_PERF_LIMIT_BITS.items() if bit(raw, number + 16)]
    return {"raw": raw, "active": active, "logged": logged}


def decode_thermal_status(raw):
    current = [name for number, name in THERM_STATUS_BITS.items() if bit(raw, number)]
    logged = [name for number, name in THERM_STATUS_BITS.items() if bit(raw, number + 1)]
    return {
        "raw": raw,
        "current": current,
        "logged": logged,
        "digital_readout": (raw >> 16) & 0x7F,
        "valid": bit(raw, 31),
    }


def read_host_bridge_identity():
    vendor = read_text(PCI_HOST_BRIDGE_SYSFS_PATH / "vendor")
    device = read_text(PCI_HOST_BRIDGE_SYSFS_PATH / "device")
    try:
        return int(vendor, 16), int(device, 16)
    except (TypeError, ValueError):
        return None


def read_mchbar_dword(register, method=None):
    """Read one MCHBAR PCI config DWORD, trying an explicit access method when requested."""
    cmd = ["setpci", "-s", "0000:00:00.0", register]
    if method:
        cmd[1:1] = ["-A", method]
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}", value):
        return None
    return int(value, 16)


def read_mchbar_base(device_id):
    address_mask = MCHBAR_ADDRESS_MASKS_BY_PCI_DEVICE.get(device_id)
    if address_mask is None:
        return None

    allowed = address_mask | MCHBAR_ENABLE_BIT
    for method in ("ecam", None):
        low = read_mchbar_dword("48.l", method=method)
        high = read_mchbar_dword("4c.l", method=method)
        if low is None or high is None:
            continue
        value = low | (high << 32)
        if not value & MCHBAR_ENABLE_BIT:
            continue
        if value & ~allowed:
            continue
        base = value & address_mask
        if base:
            return base
    return None


def probe_mmio(msr_pkg_raw):
    identity = read_host_bridge_identity()
    if identity is None:
        return {"available": False, "reason": "host bridge identity unavailable"}

    vendor, device = identity
    out = {
        "vendor_id": vendor,
        "device_id": device,
        "allowlisted": device in MCHBAR_ADDRESS_MASKS_BY_PCI_DEVICE,
        "available": False,
    }
    if vendor != 0x8086:
        out["reason"] = "host bridge is not Intel"
        return out
    if not out["allowlisted"]:
        out["reason"] = "PCI device is not in throttled's validated MCHBAR allow-list"
        return out
    if MMIO is None:
        out["reason"] = "mmio.py unavailable"
        return out

    base = read_mchbar_base(device)
    if base is None:
        out["reason"] = "valid enabled MCHBAR base could not be read"
        return out

    out["base"] = base
    try:
        mapping = MMIO(
            base + MCHBAR_PACKAGE_POWER_LIMIT_OFFSET,
            MCHBAR_PACKAGE_POWER_LIMIT_SIZE,
            readonly=True,
        )
        try:
            raw = mapping.read64(0)
        finally:
            mapping.close()
    except (MMIOError, OSError, AttributeError) as exc:
        out["reason"] = f"MMIO read failed: {exc}"
        return out

    out["available"] = True
    out["pkg_power_limit_raw"] = raw
    out["matches_msr"] = msr_pkg_raw is not None and raw == msr_pkg_raw
    return out


def cpu_target_status(cpu, dmi):
    cpuid = (cpu["family"], cpu["model"], cpu["stepping"])
    return {
        "cpuid_name": TARGET_CPUIDS.get(cpuid),
        "cpuid_match": cpuid in TARGET_CPUIDS,
        "dmi_match": (dmi.get("product") or "").strip() == TARGET_DMI_PRODUCT,
    }


def collect_snapshot(msr, cpu, dmi):
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "target": TARGET_DMI_PRODUCT,
        "cpu": {
            "vendor_id": cpu["vendor_id"],
            "family": cpu["family"],
            "model": cpu["model"],
            "stepping": cpu["stepping"],
            "model_name": cpu["model_name"],
            "microcode": cpu.get("microcode"),
        },
        "dmi": dmi,
        "package_msrs": {},
        "per_cpu_msrs": {},
        "power_backends": {},
        "notes": [
            "FIVR/OC-mailbox voltage and IccMax state is not captured because querying it requires WRMSR.",
            "Kernel powercap and an MMIO==MSR-validated direct MCHBAR mirror are captured when available.",
        ],
    }
    for name in SNAPSHOT_PACKAGE_REGS:
        value, error = try_read(msr, 0, name)
        snapshot["package_msrs"][name] = {"value": f"0x{value:016x}"} if error is None else {"error": error}

    pkg_item = snapshot["package_msrs"].get("MSR_PKG_POWER_LIMIT", {})
    pkg_raw = int(pkg_item["value"], 16) if "value" in pkg_item else None
    powercap_state = probe_powercap()
    snapshot["power_backends"]["kernel_mmio"] = powercap_state.get("mmio")
    snapshot["power_backends"]["direct_mchbar"] = probe_mmio(pkg_raw)

    current_cpus = msr.cpus()
    for logical_cpu in current_cpus:
        registers = {}
        for name in SNAPSHOT_PER_CPU_REGS:
            value, error = try_read(msr, logical_cpu, name)
            registers[name] = {"value": f"0x{value:016x}"} if error is None else {"error": error}
        snapshot["per_cpu_msrs"][str(logical_cpu)] = registers

    errors = snapshot_completeness_errors(snapshot, current_cpus)
    snapshot["complete"] = not errors
    snapshot["errors"] = errors
    return snapshot


def snapshot_completeness_errors(snapshot, current_cpus):
    """Return every missing/unreadable control register in a stock snapshot."""
    errors = []
    package_msrs = snapshot.get("package_msrs")
    if not isinstance(package_msrs, dict):
        errors.append("package_msrs is missing or is not an object")
    else:
        for name in SNAPSHOT_PACKAGE_REGS:
            item = package_msrs.get(name)
            if not isinstance(item, dict):
                errors.append(f"package {name}: missing entry")
            elif "value" not in item:
                errors.append(f"package {name}: {item.get('error', 'missing value')}")

    per_cpu = snapshot.get("per_cpu_msrs")
    if not isinstance(per_cpu, dict):
        errors.append("per_cpu_msrs is missing or is not an object")
        return errors

    for cpu in current_cpus:
        registers = per_cpu.get(str(cpu))
        if not isinstance(registers, dict):
            errors.append(f"cpu{cpu}: missing register object")
            continue
        for name in SNAPSHOT_PER_CPU_REGS:
            item = registers.get(name)
            if not isinstance(item, dict):
                errors.append(f"cpu{cpu} {name}: missing entry")
            elif "value" not in item:
                errors.append(f"cpu{cpu} {name}: {item.get('error', 'missing value')}")

    backends = snapshot.get("power_backends")
    if not isinstance(backends, dict):
        errors.append("power_backends is missing or is not an object")
        return errors
    if "kernel_mmio" not in backends:
        errors.append("power_backends.kernel_mmio is missing")
    elif backends["kernel_mmio"] is not None:
        kernel_mmio = backends["kernel_mmio"]
        if not isinstance(kernel_mmio, dict):
            errors.append("power_backends.kernel_mmio is not an object")
        else:
            for role in ("pl1", "pl2"):
                if role not in kernel_mmio:
                    errors.append(f"power_backends.kernel_mmio.{role} is missing")

    if "direct_mchbar" not in backends:
        errors.append("power_backends.direct_mchbar is missing")
    else:
        direct = backends["direct_mchbar"]
        if not isinstance(direct, dict):
            errors.append("power_backends.direct_mchbar is not an object")
        elif direct.get("available"):
            if direct.get("matches_msr") is not True:
                errors.append("captured direct MCHBAR was not equal to MSR_PKG_POWER_LIMIT")
            if not isinstance(direct.get("pkg_power_limit_raw"), int):
                errors.append("captured direct MCHBAR raw value is missing")
    return errors


def validate_snapshot_completeness(snapshot, current_cpus):
    errors = snapshot_completeness_errors(snapshot, current_cpus)
    if errors:
        raise ProbeError("snapshot is incomplete: " + "; ".join(errors))


def validate_snapshot_schema(snapshot, current_cpus):
    if not isinstance(snapshot, dict):
        raise ProbeError("snapshot root must be a JSON object")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ProbeError(
            f"unsupported snapshot schema_version {snapshot.get('schema_version')!r}; "
            f"expected {SNAPSHOT_SCHEMA_VERSION}"
        )
    if snapshot.get("target") != TARGET_DMI_PRODUCT:
        raise ProbeError(
            f"snapshot target {snapshot.get('target')!r} does not match {TARGET_DMI_PRODUCT!r}"
        )

    saved_per_cpu = snapshot.get("per_cpu_msrs")
    if not isinstance(saved_per_cpu, dict):
        raise ProbeError("snapshot per_cpu_msrs must be an object")
    try:
        saved_cpus = {int(cpu) for cpu in saved_per_cpu}
    except (TypeError, ValueError) as exc:
        raise ProbeError("snapshot contains an invalid logical CPU key") from exc

    current_set = set(current_cpus)
    if saved_cpus != current_set:
        raise ProbeError(
            f"snapshot logical CPUs {sorted(saved_cpus)} do not match current online CPUs {sorted(current_set)}"
        )


def validate_snapshot_identity(snapshot, cpu, dmi=None):
    saved = snapshot.get("cpu", {})
    current = (cpu["vendor_id"], cpu["family"], cpu["model"], cpu["stepping"])
    expected = (
        saved.get("vendor_id"),
        saved.get("family"),
        saved.get("model"),
        saved.get("stepping"),
    )
    if current != expected:
        raise ProbeError(f"snapshot CPU identity {expected!r} does not match current CPU {current!r}")

    if dmi is not None:
        saved_product = (snapshot.get("dmi") or {}).get("product")
        current_product = dmi.get("product")
        if not saved_product or not current_product:
            raise ProbeError("snapshot/current DMI product is missing; refusing restore")
        if saved_product != current_product:
            raise ProbeError(
                f"snapshot DMI product {saved_product!r} does not match current product {current_product!r}"
            )


def _snapshot_hex_value(item, register):
    try:
        value = int(item["value"], 16)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError(f"invalid snapshot value for {register}: {item!r}") from exc
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ProbeError(f"snapshot value for {register} is outside the 64-bit MSR range")
    return value


def build_restore_plan(snapshot, current_cpus):
    validate_snapshot_completeness(snapshot, current_cpus)
    plan = []
    for name in SNAPSHOT_PACKAGE_REGS:
        item = snapshot["package_msrs"][name]
        value = _snapshot_hex_value(item, name)
        # 830 G6 is a single-package target. Package-scoped state is
        # snapshotted from CPU0 and restored through CPU0 only.
        plan.append((0, name, value))

    saved_per_cpu = snapshot["per_cpu_msrs"]
    for cpu in current_cpus:
        registers = saved_per_cpu[str(cpu)]
        for name in SNAPSHOT_PER_CPU_REGS:
            item = registers[name]
            plan.append((cpu, name, _snapshot_hex_value(item, name)))
    return plan


def apply_restore_plan(msr, plan):
    """Apply and verify a restore plan, rolling back partial progress on failure."""
    prepared = []
    for cpu, name, value in plan:
        prepared.append((cpu, name, msr.read(cpu, name), value))

    completed = []
    results = []
    try:
        for cpu, name, original, value in prepared:
            if value == original:
                results.append(
                    {
                        "cpu": cpu,
                        "register": name,
                        "written": value,
                        "readback": original,
                        "match": True,
                    }
                )
                continue

            msr.write(cpu, name, value)
            completed.append((cpu, name, original))
            readback = msr.read(cpu, name)
            if readback != value:
                raise ProbeError(
                    f"restore verify failed for cpu{cpu} {name}: "
                    f"wrote 0x{value:016x}, read 0x{readback:016x}"
                )
            results.append(
                {
                    "cpu": cpu,
                    "register": name,
                    "written": value,
                    "readback": readback,
                    "match": True,
                }
            )
    except ProbeError as exc:
        rollback_errors = []
        for cpu, name, original in reversed(completed):
            try:
                msr.write(cpu, name, original)
                if msr.read(cpu, name) != original:
                    rollback_errors.append(f"cpu{cpu} {name}: verify mismatch")
            except ProbeError as rollback_exc:
                rollback_errors.append(f"cpu{cpu} {name}: {rollback_exc}")
        suffix = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else "; rollback attempted"
        raise ProbeError(str(exc) + suffix) from exc

    return results


def _open_snapshot_mchbar(snapshot, current_msr_raw):
    direct = (snapshot.get("power_backends") or {}).get("direct_mchbar") or {}
    if not direct.get("available") or not direct.get("matches_msr"):
        return None, None

    identity = read_host_bridge_identity()
    if identity is None:
        raise ProbeError("snapshot captured direct MCHBAR but current host bridge identity is unavailable")
    vendor, device = identity
    if vendor != direct.get("vendor_id") or device != direct.get("device_id"):
        raise ProbeError(
            f"snapshot host bridge {direct.get('vendor_id')}:{direct.get('device_id')} "
            f"does not match current {vendor}:{device}"
        )
    base = read_mchbar_base(device)
    if base is None or MMIO is None:
        raise ProbeError("snapshot captured direct MCHBAR but current validated mapping is unavailable")

    address = base + MCHBAR_PACKAGE_POWER_LIMIT_OFFSET
    mapping = None
    try:
        with MMIO(address, MCHBAR_PACKAGE_POWER_LIMIT_SIZE, readonly=True) as probe_mapping:
            current_raw = probe_mapping.read64(0)
        if current_raw != current_msr_raw:
            raise ProbeError(
                f"refusing direct MCHBAR restore because current MMIO {current_raw:#x} "
                f"does not match current MSR {current_msr_raw:#x}"
            )

        mapping = MMIO(address, MCHBAR_PACKAGE_POWER_LIMIT_SIZE)
        rw_raw = mapping.read64(0)
        if rw_raw != current_msr_raw:
            mapping.close()
            mapping = None
            raise ProbeError(
                f"direct MCHBAR changed after read-only preflight: MMIO {rw_raw:#x}, "
                f"MSR {current_msr_raw:#x}"
            )
    except ProbeError:
        if mapping is not None:
            mapping.close()
        raise
    except (MMIOError, OSError, AttributeError) as exc:
        if mapping is not None:
            mapping.close()
        raise ProbeError(f"unable to open current direct MCHBAR for restore: {exc}") from exc

    return mapping, current_raw


def _restore_direct_mchbar(mapping, target_raw):
    current = mapping.read64(0)
    if ((current >> 63) & 1) and current != target_raw:
        raise ProbeError("direct MCHBAR package power limit is locked; cannot restore it")
    if current != target_raw:
        mapping.write64(0, target_raw)
    readback = mapping.read64(0)
    if readback != target_raw:
        raise ProbeError(
            f"direct MCHBAR restore verify failed: wrote {target_raw:#x}, read {readback:#x}"
        )


def apply_full_restore(msr, plan, snapshot):
    """Restore MSRs plus every package-power backend captured by snapshot v2."""
    current_msr_raw = msr.read(0, "MSR_PKG_POWER_LIMIT")
    rollback_plan = [(cpu, name, msr.read(cpu, name)) for cpu, name, _ in plan]

    backends = snapshot.get("power_backends") or {}
    saved_powercap = backends.get("kernel_mmio")
    powercap_zone = None
    powercap_before = None
    if saved_powercap is not None:
        powercap_zone = find_mmio_package_zone()
        if powercap_zone is None:
            raise ProbeError("snapshot captured intel-rapl-mmio but the current kernel exposes no matching zone")
        powercap_before = powercap_zone.snapshot()

    direct_mapping, direct_before = _open_snapshot_mchbar(snapshot, current_msr_raw)
    direct_saved = backends.get("direct_mchbar") or {}
    direct_target = direct_saved.get("pkg_power_limit_raw") if direct_mapping is not None else None

    try:
        apply_restore_plan(msr, plan)

        if powercap_zone is not None:
            restored_powercap = powercap_zone.restore(saved_powercap)
            for role in ("pl1", "pl2"):
                expected = saved_powercap.get(role)
                actual = restored_powercap.get(role)
                if expected is None:
                    continue
                if actual is None:
                    raise ProbeError(f"restored powercap {role.upper()} constraint disappeared")
                for field in ("power_limit_uw", "time_window_us"):
                    if expected.get(field) is not None and actual.get(field) != expected.get(field):
                        raise ProbeError(
                            f"powercap {role.upper()} {field} restore mismatch: "
                            f"expected {expected.get(field)}, read {actual.get(field)}"
                        )

        if direct_mapping is not None:
            _restore_direct_mchbar(direct_mapping, direct_target)

        target_msr = int(snapshot["package_msrs"]["MSR_PKG_POWER_LIMIT"]["value"], 16)
        if msr.read(0, "MSR_PKG_POWER_LIMIT") != target_msr:
            raise ProbeError("package power MSR changed during backend restore")

        if direct_mapping is not None and direct_mapping.read64(0) != target_msr:
            raise ProbeError("restored direct MCHBAR mirror does not match restored package power MSR")
    except (ProbeError, PowercapError, OSError) as exc:
        rollback_errors = []
        if direct_mapping is not None and direct_before is not None:
            try:
                _restore_direct_mchbar(direct_mapping, direct_before)
            except (ProbeError, OSError) as rollback_exc:
                rollback_errors.append(f"MCHBAR: {rollback_exc}")
        if powercap_zone is not None and powercap_before is not None:
            try:
                powercap_zone.restore(powercap_before)
            except PowercapError as rollback_exc:
                rollback_errors.append(f"powercap: {rollback_exc}")
        try:
            apply_restore_plan(msr, rollback_plan)
        except ProbeError as rollback_exc:
            rollback_errors.append(f"MSR: {rollback_exc}")
        suffix = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else "; rollback attempted"
        raise ProbeError(f"full snapshot restore failed: {exc}{suffix}") from exc
    finally:
        if direct_mapping is not None:
            direct_mapping.close()


def probe_machine(msr):
    cpu = read_cpuinfo()
    if cpu["vendor_id"] != "GenuineIntel":
        raise ProbeError("this probe currently supports Intel CPUs only")
    dmi = read_dmi()
    cpus = msr.cpus()
    if not cpus:
        raise ProbeError("no MSR CPU devices available")

    report = {
        "target": cpu_target_status(cpu, dmi),
        "dmi": dmi,
        "cpu": {key: value for key, value in cpu.items() if key != "flags"},
        "kernel": {
            "lockdown": read_lockdown(),
            "msr_allow_writes": read_text("/sys/module/msr/parameters/allow_writes"),
        },
    }

    platform_raw, platform_error = try_read(msr, 0, "MSR_PLATFORM_INFO")
    report["platform"] = {"error": platform_error} if platform_error else {
        "raw": platform_raw,
        "max_non_turbo_ratio": (platform_raw >> 8) & 0xFF,
        "programmable_turbo_ratio": bit(platform_raw, 28),
        "programmable_tdp_limit": bit(platform_raw, 29),
        "programmable_temperature_target": bit(platform_raw, 30),
    }

    rapl_raw, rapl_error = try_read(msr, 0, "MSR_RAPL_POWER_UNIT")
    pkg_raw, pkg_error = try_read(msr, 0, "MSR_PKG_POWER_LIMIT")
    power = {
        "rapl_units_error": rapl_error,
        "pkg_power_limit_error": pkg_error,
    }
    if rapl_error is None:
        power["units"] = decode_rapl_units(rapl_raw)
    if rapl_error is None and pkg_error is None:
        power["package"] = decode_pkg_power_limit(pkg_raw, power["units"])
    report["power"] = power
    report["powercap"] = probe_powercap()
    report["mmio"] = probe_mmio(pkg_raw if pkg_error is None else None)

    hwp = {
        "cpu_flags": {
            "hwp": "hwp" in cpu["flags"],
            "hwp_epp": "hwp_epp" in cpu["flags"],
            "hwp_activity_window": "hwp_act_window" in cpu["flags"],
            "hwp_package_request": "hwp_pkg_req" in cpu["flags"],
        },
        "per_cpu": {},
    }
    pm_raw, pm_error = try_read(msr, 0, "IA32_PM_ENABLE")
    hwp["pm_enable_error"] = pm_error
    if pm_error is None:
        hwp["enabled"] = bit(pm_raw, 0)
        hwp["pm_enable_raw"] = pm_raw
    cap_raw, cap_error = try_read(msr, 0, "IA32_HWP_CAPABILITIES")
    hwp["capabilities_error"] = cap_error
    if cap_error is None:
        hwp["capabilities"] = decode_hwp_capabilities(cap_raw)
    for logical_cpu in cpus:
        request_raw, error = try_read(msr, logical_cpu, "IA32_HWP_REQUEST")
        hwp["per_cpu"][str(logical_cpu)] = (
            {"error": error} if error else {"raw": request_raw, **decode_hwp_request(request_raw)}
        )

    package_control = any(
        data.get("package_control", False)
        for data in hwp["per_cpu"].values()
        if isinstance(data, dict)
    )
    hwp["package_control_active"] = package_control
    if package_control:
        pkg_request_raw, pkg_request_error = try_read(msr, 0, "IA32_HWP_REQUEST_PKG")
        hwp["package_request_error"] = pkg_request_error
        if pkg_request_error is None:
            hwp["package_request"] = {"raw": pkg_request_raw, **decode_hwp_request(pkg_request_raw)}
    report["hwp"] = hwp

    temp_raw, temp_error = try_read(msr, 0, "MSR_TEMPERATURE_TARGET")
    thermal = {"temperature_target_error": temp_error}
    if temp_error is None:
        tjmax = (temp_raw >> 16) & 0xFF
        tcc_offset = (temp_raw >> 24) & 0x3F
        thermal.update(
            {
                "temperature_target_raw": temp_raw,
                "tjmax_c": tjmax,
                "tcc_offset_c": tcc_offset,
                "effective_tcc_c": tjmax - tcc_offset,
            }
        )

    power_ctl_raw, power_ctl_error = try_read(msr, 0, "MSR_POWER_CTL")
    thermal["power_ctl_error"] = power_ctl_error
    if power_ctl_error is None:
        thermal["power_ctl_raw"] = power_ctl_raw
        thermal["bd_prochot_enabled"] = bit(power_ctl_raw, 0)

    pkg_therm_raw, pkg_therm_error = try_read(msr, 0, "IA32_PACKAGE_THERM_STATUS")
    thermal["package_status_error"] = pkg_therm_error
    if pkg_therm_error is None:
        thermal["package_status"] = decode_thermal_status(pkg_therm_raw)
    report["thermal"] = thermal

    misc_raw, misc_error = try_read(msr, 0, "IA32_MISC_ENABLE")
    turbo_raw, turbo_error = try_read(msr, 0, "MSR_TURBO_RATIO_LIMIT")
    turbo = {
        "misc_enable_error": misc_error,
        "ratio_limit_error": turbo_error,
    }
    if misc_error is None:
        turbo["misc_enable_raw"] = misc_raw
        turbo["turbo_disabled"] = bit(misc_raw, 38)
    if turbo_error is None:
        turbo["ratio_limit_raw"] = turbo_raw
        turbo["ratios"] = decode_turbo_ratio_limit(turbo_raw)
    report["turbo"] = turbo

    limits_raw, limits_error = try_read(msr, 0, "MSR_PERF_LIMIT_REASONS")
    report["limits"] = {"error": limits_error} if limits_error else decode_perf_limit_reasons(limits_raw)

    report["fivr"] = {
        "oc_mailbox_address": "0x150",
        "actively_queried": False,
        "reason": "strict probe does not issue the WRMSR transaction required to query OC mailbox state",
        "expected_on_target": report["target"]["cpuid_match"],
    }
    return report


def fmt_bool(value):
    return "yes" if value else "no"


def fmt_list(values):
    return ", ".join(values) if values else "-"


def format_report(report):
    lines = []
    dmi = report["dmi"]
    cpu = report["cpu"]
    target = report["target"]

    lines.append("ThrottleStop replacement bring-up probe")
    lines.append("=" * 39)
    lines.append(f"System: {dmi.get('vendor') or '?'} {dmi.get('product') or '?'}")
    lines.append(f"BIOS: {dmi.get('bios_vendor') or '?'} {dmi.get('bios_version') or '?'} ({dmi.get('bios_date') or '?'})")
    lines.append(f"CPU: {cpu['model_name']}")
    lines.append(f"CPUID: family {cpu['family']} model {cpu['model']} stepping {cpu['stepping']}")
    lines.append(
        f"830 G6 target: DMI={fmt_bool(target['dmi_match'])}, "
        f"CPUID={fmt_bool(target['cpuid_match'])}"
        + (f" ({target['cpuid_name']})" if target.get("cpuid_name") else "")
    )
    lines.append(f"Kernel lockdown: {report['kernel']['lockdown']}")
    lines.append(f"msr.allow_writes: {report['kernel']['msr_allow_writes'] or 'unknown'}")

    platform = report["platform"]
    lines.append("")
    lines.append("Platform")
    if "error" in platform:
        lines.append(f"  unavailable: {platform['error']}")
    else:
        lines.append(f"  Max non-turbo ratio: {platform['max_non_turbo_ratio']}x")
        lines.append(f"  Programmable turbo ratio: {fmt_bool(platform['programmable_turbo_ratio'])}")
        lines.append(f"  Programmable TDP: {fmt_bool(platform['programmable_tdp_limit'])}")
        lines.append(f"  Programmable temperature target: {fmt_bool(platform['programmable_temperature_target'])}")

    hwp = report["hwp"]
    lines.append("")
    lines.append("Speed Shift / HWP")
    lines.append(f"  CPU flag HWP: {fmt_bool(hwp['cpu_flags']['hwp'])}")
    lines.append(f"  CPU flag HWP EPP: {fmt_bool(hwp['cpu_flags']['hwp_epp'])}")
    if hwp.get("pm_enable_error"):
        lines.append(f"  IA32_PM_ENABLE: unavailable ({hwp['pm_enable_error']})")
    else:
        lines.append(f"  Enabled: {fmt_bool(hwp.get('enabled'))}")
    if "capabilities" in hwp:
        cap = hwp["capabilities"]
        lines.append(
            "  Capabilities: "
            f"low={cap['lowest']} efficient={cap['most_efficient']} "
            f"guaranteed={cap['guaranteed']} high={cap['highest']}"
        )
    epps = [
        f"cpu{cpu_id}={data['epp']}"
        for cpu_id, data in hwp["per_cpu"].items()
        if "epp" in data
    ]
    lines.append("  Per-CPU EPP: " + (", ".join(epps) if epps else "unavailable"))
    if hwp.get("package_control_active"):
        package_request = hwp.get("package_request")
        if package_request is not None:
            lines.append(
                f"  Package control: active; effective package EPP={package_request['epp']} "
                "(per-CPU EPP may be overridden)"
            )
        else:
            lines.append(
                "  Package control: active; package request unavailable "
                f"({hwp.get('package_request_error') or 'unknown error'})"
            )
    else:
        lines.append("  Package control: inactive")

    power = report["power"]
    lines.append("")
    lines.append("Turbo Power Limits")
    if "package" not in power:
        lines.append(f"  unavailable: {power.get('pkg_power_limit_error') or power.get('rapl_units_error')}")
    else:
        pkg = power["package"]
        lines.append(
            f"  PL1: {pkg['pl1']['watts']:.3f} W / {pkg['pl1']['seconds']:.6g} s "
            f"(enabled={fmt_bool(pkg['pl1']['enabled'])}, clamp={fmt_bool(pkg['pl1']['clamp'])})"
        )
        lines.append(
            f"  PL2: {pkg['pl2']['watts']:.3f} W / {pkg['pl2']['seconds']:.6g} s "
            f"(enabled={fmt_bool(pkg['pl2']['enabled'])}, clamp={fmt_bool(pkg['pl2']['clamp'])})"
        )
        lines.append(f"  MSR lock: {fmt_bool(pkg['locked'])}")

    powercap = report.get("powercap", {})
    for label, title in (("msr", "Kernel intel-rapl"), ("mmio", "Kernel intel-rapl-mmio")):
        zone = powercap.get(label)
        if zone is None:
            lines.append(f"  {title}: unavailable")
            continue
        pl1 = zone.get("pl1") or {}
        pl2 = zone.get("pl2") or {}
        pl1_w = pl1.get("power_limit_uw")
        pl2_w = pl2.get("power_limit_uw")
        rendered1 = "?" if pl1_w is None else f"{pl1_w / 1_000_000:.3f} W"
        rendered2 = "?" if pl2_w is None else f"{pl2_w / 1_000_000:.3f} W"
        lines.append(f"  {title}: {zone.get('path')} (PL1 {rendered1}, PL2 {rendered2})")

    mmio = report["mmio"]
    lines.append(
        f"  Direct MCHBAR mirror: {'readable' if mmio.get('available') else 'unavailable'}"
        + (f", matches MSR={fmt_bool(mmio.get('matches_msr'))}" if mmio.get("available") else f" ({mmio.get('reason', 'unknown')})")
    )
    if "device_id" in mmio:
        lines.append(f"  Host bridge: {mmio['vendor_id']:#06x}:{mmio['device_id']:#06x}")

    thermal = report["thermal"]
    lines.append("")
    lines.append("Thermal / PROCHOT")
    if "tjmax_c" in thermal:
        lines.append(
            f"  TjMax: {thermal['tjmax_c']} C; TCC offset: {thermal['tcc_offset_c']} C; "
            f"effective TCC: {thermal['effective_tcc_c']} C"
        )
    else:
        lines.append(f"  Temperature target unavailable: {thermal.get('temperature_target_error')}")
    if "bd_prochot_enabled" in thermal:
        lines.append(f"  BD PROCHOT: {'enabled' if thermal['bd_prochot_enabled'] else 'disabled'}")
    else:
        lines.append(f"  BD PROCHOT: unavailable ({thermal.get('power_ctl_error')})")
    if "package_status" in thermal:
        status = thermal["package_status"]
        lines.append(f"  Package thermal current: {fmt_list(status['current'])}")
        lines.append(f"  Package thermal logged: {fmt_list(status['logged'])}")

    turbo = report["turbo"]
    lines.append("")
    lines.append("Turbo Ratio")
    if "turbo_disabled" in turbo:
        lines.append(f"  Turbo: {'disabled' if turbo['turbo_disabled'] else 'enabled'}")
    else:
        lines.append(f"  Turbo enable state unavailable: {turbo.get('misc_enable_error')}")
    if "ratios" in turbo:
        ratios = ", ".join(f"{cores}C={ratio}x" for cores, ratio in turbo["ratios"].items() if ratio)
        lines.append(f"  Ratio limits: {ratios or '-'}")
    else:
        lines.append(f"  Ratio limits unavailable: {turbo.get('ratio_limit_error')}")

    limits = report["limits"]
    lines.append("")
    lines.append("Performance Limit Reasons")
    if "error" in limits:
        lines.append(f"  unavailable: {limits['error']}")
    else:
        lines.append(f"  Active: {fmt_list(limits['active'])}")
        lines.append(f"  Logged: {fmt_list(limits['logged'])}")
        lines.append(f"  Raw: 0x{limits['raw']:016x}")

    fivr = report["fivr"]
    lines.append("")
    lines.append("FIVR / OC mailbox")
    lines.append("  Active query: no")
    lines.append(f"  Status: {fivr['reason']}")
    lines.append("")
    lines.append("This milestone intentionally performs no OC-mailbox WRMSR query and no setting changes.")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="HP EliteBook 830 G6 ThrottleStop replacement bring-up probe")
    parser.add_argument("--json", action="store_true", help="print the probe as JSON")
    parser.add_argument("--snapshot", metavar="PATH", help="save restorable raw MSRs after probing")
    parser.add_argument("--restore-snapshot", metavar="PATH", help="show a restore plan for a saved snapshot")
    parser.add_argument(
        "--apply-restore",
        action="store_true",
        help="actually perform --restore-snapshot writes; without this flag restore is dry-run only",
    )
    return parser.parse_args(argv)


def _main(argv=None):
    args = parse_args(argv)
    if os.geteuid() != 0:
        raise ProbeError("MSR access requires root; run this command with sudo")

    msr = MsrAccess()
    msr.ensure()
    cpu = read_cpuinfo()
    dmi = read_dmi()

    if args.restore_snapshot:
        try:
            snapshot = json.loads(Path(args.restore_snapshot).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProbeError(f"unable to read snapshot {args.restore_snapshot!r}: {exc}") from exc
        current_cpus = msr.cpus()
        validate_snapshot_schema(snapshot, current_cpus)
        validate_snapshot_identity(snapshot, cpu, dmi)
        validate_snapshot_completeness(snapshot, current_cpus)
        plan = build_restore_plan(snapshot, current_cpus)
        print(f"Restore plan: {len(plan)} WRMSR operations")
        for logical_cpu, name, value in plan:
            print(f"  cpu{logical_cpu}: {name} <- 0x{value:016x}")
        backends = snapshot.get("power_backends") or {}
        if backends.get("kernel_mmio") is not None:
            print("  backend: restore captured intel-rapl-mmio PL1/PL2 watts/Tau")
        direct = backends.get("direct_mchbar") or {}
        if direct.get("available") and direct.get("matches_msr"):
            print(
                "  backend: restore validated direct MCHBAR PACKAGE_POWER_LIMIT "
                f"<- 0x{direct['pkg_power_limit_raw']:016x}"
            )
        if not args.apply_restore:
            print("Dry run only. Add --apply-restore to write and verify these values.")
            return 0
        apply_full_restore(msr, plan, snapshot)
        print("Snapshot restore verified across captured MSR/MMIO power backends.")
        return 0

    report = probe_machine(msr)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))

    if args.snapshot:
        snapshot = collect_snapshot(msr, cpu, dmi)
        path = Path(args.snapshot)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            raise ProbeError(f"unable to write snapshot {str(path)!r}: {exc}") from exc
        if snapshot["complete"]:
            print(f"Snapshot written to {path} (complete)")
        else:
            print(f"Snapshot written to {path} but is NOT restorable:", file=sys.stderr)
            for error in snapshot["errors"]:
                print(f"  - {error}", file=sys.stderr)
            return 2
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except ProbeError as exc:
        print(f"probe error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
