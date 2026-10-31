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

from throttled_powercap import probe_powercap

try:
    from mmio import MMIO, MMIOError
except ImportError:
    MMIO = None

    class MMIOError(Exception):
        pass


TARGET_DMI_PRODUCT = "HP EliteBook 830 G6"
TARGET_CPUIDS = {
    (6, 142, 11): "Whiskey Lake-U",
}

MSR = {
    "MSR_PLATFORM_INFO": 0xCE,
    "IA32_APERF": 0xE8,
    "IA32_MPERF": 0xE7,
    "IA32_MISC_ENABLE": 0x1A0,
    "IA32_PERF_CTL": 0x199,
    "IA32_PERF_STATUS": 0x198,
    "IA32_CLOCK_MODULATION": 0x19A,
    "MSR_TEMPERATURE_TARGET": 0x1A2,
    "MSR_TURBO_RATIO_LIMIT": 0x1AD,
    "MSR_POWER_CTL": 0x1FC,
    "IA32_PACKAGE_THERM_STATUS": 0x1B1,
    "MSR_RAPL_POWER_UNIT": 0x606,
    "MSR_PKG_POWER_LIMIT": 0x610,
    "MSR_PKG_ENERGY_STATUS": 0x611,
    "MSR_CONFIG_TDP_CONTROL": 0x64B,
    "MSR_CORE_PERF_LIMIT_REASONS": 0x64F,
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
MCHBAR_ENABLE_BIT = 1
MCHBAR_PACKAGE_POWER_LIMIT_OFFSET = 0x59A0
MCHBAR_PACKAGE_POWER_LIMIT_SIZE = 8
MCHBAR_ADDRESS_MASK_39_15 = ((1 << 39) - 1) & ~((1 << 15) - 1)
MCHBAR_ADDRESS_MASK_39_17 = ((1 << 39) - 1) & ~((1 << 17) - 1)
MCHBAR_ADDRESS_MASK_42_17 = ((1 << 42) - 1) & ~((1 << 17) - 1)

# Kept in sync with the current MIT-licensed throttled MCHBAR allow-list.
MCHBAR_ADDRESS_MASKS_BY_PCI_DEVICE = {
    0x5914: MCHBAR_ADDRESS_MASK_39_15,
    0x3EC4: MCHBAR_ADDRESS_MASK_39_15,
    0x9B54: MCHBAR_ADDRESS_MASK_39_15,
    0x9B61: MCHBAR_ADDRESS_MASK_39_15,
    0x9A14: MCHBAR_ADDRESS_MASK_39_17,
    0x4601: MCHBAR_ADDRESS_MASK_42_17,
    0x4602: MCHBAR_ADDRESS_MASK_42_17,
    0x4621: MCHBAR_ADDRESS_MASK_42_17,
    0x4641: MCHBAR_ADDRESS_MASK_42_17,
    0x4660: MCHBAR_ADDRESS_MASK_42_17,
    0x4668: MCHBAR_ADDRESS_MASK_42_17,
    0x4648: MCHBAR_ADDRESS_MASK_42_17,
    0xA703: MCHBAR_ADDRESS_MASK_42_17,
    0x4640: MCHBAR_ADDRESS_MASK_42_17,
    0x4630: MCHBAR_ADDRESS_MASK_42_17,
    0xA700: MCHBAR_ADDRESS_MASK_42_17,
    0xA740: MCHBAR_ADDRESS_MASK_42_17,
    0xA704: MCHBAR_ADDRESS_MASK_42_17,
    0xA702: MCHBAR_ADDRESS_MASK_42_17,
    0xA706: MCHBAR_ADDRESS_MASK_42_17,
    0xA707: MCHBAR_ADDRESS_MASK_42_17,
    0xA708: MCHBAR_ADDRESS_MASK_42_17,
    0xA716: MCHBAR_ADDRESS_MASK_42_17,
    0xA718: MCHBAR_ADDRESS_MASK_42_17,
    0x7D21: MCHBAR_ADDRESS_MASK_42_17,
    0x7D22: MCHBAR_ADDRESS_MASK_42_17,
    0x7D23: MCHBAR_ADDRESS_MASK_42_17,
    0x7D24: MCHBAR_ADDRESS_MASK_42_17,
    0x7D01: MCHBAR_ADDRESS_MASK_42_17,
    0x7D02: MCHBAR_ADDRESS_MASK_42_17,
    0x7D14: MCHBAR_ADDRESS_MASK_42_17,
    0x7D06: MCHBAR_ADDRESS_MASK_42_17,
    0x7D20: MCHBAR_ADDRESS_MASK_42_17,
    0x7D30: MCHBAR_ADDRESS_MASK_42_17,
}

CORE_PERF_LIMIT_BITS = {
    0: "PROCHOT",
    1: "Thermal",
    4: "Graphics",
    5: "Auto-HWP",
    6: "VR-Thermal",
    8: "Current/EDP",
    9: "Core-Power",
    10: "Package-PL1",
    11: "Package-PL2",
    12: "Multi-Core-Turbo",
    13: "Transitions",
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


def read_mchbar_dword(register):
    try:
        result = subprocess.run(
            ["setpci", "-s", "0000:00:00.0", register],
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
    low = read_mchbar_dword("48.l")
    high = read_mchbar_dword("4c.l")
    if low is None or high is None:
        return None
    value = low | (high << 32)
    if not value & MCHBAR_ENABLE_BIT:
        return None
    allowed = address_mask | MCHBAR_ENABLE_BIT
    if value & ~allowed:
        return None
    base = value & address_mask
    return base or None


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
        "schema_version": 1,
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
        "notes": [
            "FIVR/OC-mailbox voltage and IccMax state is not captured because querying it requires WRMSR.",
            "MMIO package power limits are not restored by this milestone.",
        ],
    }
    for name in SNAPSHOT_PACKAGE_REGS:
        value, error = try_read(msr, 0, name)
        snapshot["package_msrs"][name] = {"value": f"0x{value:016x}"} if error is None else {"error": error}

    for logical_cpu in msr.cpus():
        registers = {}
        for name in SNAPSHOT_PER_CPU_REGS:
            value, error = try_read(msr, logical_cpu, name)
            registers[name] = {"value": f"0x{value:016x}"} if error is None else {"error": error}
        snapshot["per_cpu_msrs"][str(logical_cpu)] = registers
    return snapshot


def validate_snapshot_identity(snapshot, cpu):
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


def build_restore_plan(snapshot, current_cpus):
    plan = []
    for name in SNAPSHOT_PACKAGE_REGS:
        item = snapshot.get("package_msrs", {}).get(name, {})
        if "value" in item:
            value = int(item["value"], 16)
            for cpu in current_cpus:
                plan.append((cpu, name, value))

    saved_per_cpu = snapshot.get("per_cpu_msrs", {})
    for cpu in current_cpus:
        registers = saved_per_cpu.get(str(cpu), {})
        for name in SNAPSHOT_PER_CPU_REGS:
            item = registers.get(name, {})
            if "value" in item:
                plan.append((cpu, name, int(item["value"], 16)))
    return plan


def apply_restore_plan(msr, plan):
    results = []
    for cpu, name, value in plan:
        msr.write(cpu, name, value)
        readback = msr.read(cpu, name)
        results.append(
            {
                "cpu": cpu,
                "register": name,
                "written": value,
                "readback": readback,
                "match": readback == value,
            }
        )
    return results


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

    limits_raw, limits_error = try_read(msr, 0, "MSR_CORE_PERF_LIMIT_REASONS")
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
    lines.append("  EPP: " + (", ".join(epps) if epps else "unavailable"))

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


def main(argv=None):
    args = parse_args(argv)
    if os.geteuid() != 0:
        raise ProbeError("MSR access requires root; run this command with sudo")

    msr = MsrAccess()
    msr.ensure()
    cpu = read_cpuinfo()
    dmi = read_dmi()

    if args.restore_snapshot:
        snapshot = json.loads(Path(args.restore_snapshot).read_text(encoding="utf-8"))
        validate_snapshot_identity(snapshot, cpu)
        plan = build_restore_plan(snapshot, msr.cpus())
        print(f"Restore plan: {len(plan)} WRMSR operations")
        for logical_cpu, name, value in plan:
            print(f"  cpu{logical_cpu}: {name} <- 0x{value:016x}")
        if not args.apply_restore:
            print("Dry run only. Add --apply-restore to write and verify these values.")
            return 0
        results = apply_restore_plan(msr, plan)
        mismatches = [item for item in results if not item["match"]]
        if mismatches:
            for item in mismatches:
                print(
                    f"VERIFY FAIL cpu{item['cpu']} {item['register']}: "
                    f"wrote 0x{item['written']:016x}, read 0x{item['readback']:016x}",
                    file=sys.stderr,
                )
            return 2
        print("Snapshot restore verified.")
        return 0

    report = probe_machine(msr)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))

    if args.snapshot:
        snapshot = collect_snapshot(msr, cpu, dmi)
        path = Path(args.snapshot)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Snapshot written to {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProbeError as exc:
        print(f"probe error: {exc}", file=sys.stderr)
        raise SystemExit(1)
