#!/usr/bin/env python3
"""Direct control surface for the HP EliteBook 830 G6 bring-up.

The daemon remains responsible for persistent profiles.  This command is a
small, explicit register-control tool used to reach ThrottleStop parity on the
830 G6.  Every write path:
  * validates the target machine unless --force-target is supplied;
  * pre-reads all affected registers;
  * preserves unrelated bits;
  * verifies readback;
  * attempts rollback if a write or verification fails.

Kernel-managed intel-rapl-mmio is used when available. Direct /dev/mem MCHBAR
writes are intentionally not guessed for the 830 G6 and remain restricted to
the daemon's existing validated PCI allow-list.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from throttled_powercap import PowercapError, find_mmio_package_zone
from throttled_probe import (
    MSR,
    MsrAccess,
    ProbeError,
    TARGET_DMI_PRODUCT,
    bit,
    cpu_target_status,
    decode_perf_limit_reasons,
    decode_pkg_power_limit,
    decode_rapl_units,
    format_report,
    probe_machine,
    read_cpuinfo,
    read_dmi,
)


TURBO_DISABLE_BIT = 38
BD_PROCHOT_ENABLE_BIT = 0
BD_PROCHOT_LOCK_BIT = 23
HWP_EPP_SHIFT = 24
HWP_EPP_MASK = 0xFF << HWP_EPP_SHIFT
HWP_PACKAGE_CONTROL_BIT = 42
SPEEDSTEP_ENABLE_BIT = 16
PERF_TARGET_RATIO_SHIFT = 8
PERF_TARGET_RATIO_MASK = 0xFF << PERF_TARGET_RATIO_SHIFT
CLOCK_MODULATION_ENABLE_BIT = 4

FIVR_PLANES = {"core": 0, "gpu": 1, "cache": 2, "uncore": 3, "analogio": 4}
FIVR_TICKS_PER_MV = 1.024
FIVR_MIN_TICKS = -(1 << 10)
FIVR_MAX_TICKS = 0
ICCMAX_PLANES = {"core": 0, "gpu": 1, "cache": 2}
ICCMAX_STEPS_PER_A = 4
ICCMAX_MAX_FIELD = 0x3FF
OC_MAILBOX_BUSY_BIT = 63
OC_MAILBOX_STATUS_SHIFT = 32
OC_MAILBOX_STATUS_MASK = 0xFF
OC_MAILBOX_TIMEOUT_S = 0.01
OC_MAILBOX_POLL_S = 0.00001
OC_BACKUP_DIR = Path("/var/lib/throttled/oc-backups")
OC_BACKUP_SCHEMA_VERSION = 1

PKG_PL1_POWER_MASK = 0x7FFF
PKG_PL1_ENABLE_BIT = 15
PKG_PL1_CLAMP_BIT = 16
PKG_PL1_TIME_SHIFT = 17
PKG_PL1_TIME_MASK = 0x7F << PKG_PL1_TIME_SHIFT
PKG_PL2_POWER_SHIFT = 32
PKG_PL2_POWER_MASK = 0x7FFF << PKG_PL2_POWER_SHIFT
PKG_PL2_ENABLE_BIT = 47
PKG_PL2_CLAMP_BIT = 48
PKG_PL2_TIME_SHIFT = 49
PKG_PL2_TIME_MASK = 0x7F << PKG_PL2_TIME_SHIFT
PKG_POWER_LOCK_BIT = 63


class ControlError(RuntimeError):
    pass


def parse_on_off(value):
    normalized = value.lower()
    if normalized in ("on", "enable", "enabled", "1", "true"):
        return True
    if normalized in ("off", "disable", "disabled", "0", "false"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


def parse_epp(value):
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("EPP must be an integer in the range 0..255") from exc
    if not 0 <= parsed <= 255:
        raise argparse.ArgumentTypeError("EPP must be in the range 0..255")
    return parsed


def positive_finite(value):
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and greater than zero")
    return parsed


def parse_undervolt_mv(value):
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("voltage offset must be a number") from exc
    minimum = FIVR_MIN_TICKS / FIVR_TICKS_PER_MV
    if not math.isfinite(parsed) or not minimum <= parsed <= 0:
        raise argparse.ArgumentTypeError(f"voltage offset must be between {minimum:g} and 0 mV")
    return parsed


def parse_iccmax(value):
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("IccMax must be a number") from exc
    maximum = ICCMAX_MAX_FIELD / ICCMAX_STEPS_PER_A
    if not math.isfinite(parsed) or not 0 < parsed <= maximum:
        raise argparse.ArgumentTypeError(f"IccMax must be greater than 0 and at most {maximum:g} A")
    return parsed


def parse_ratio(value):
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ratio must be an integer") from exc
    if not 1 <= parsed <= 0xFF:
        raise argparse.ArgumentTypeError("ratio must be in the range 1..255")
    return parsed


def nonnegative_int(value):
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative integer")
    return parsed


def parse_temperature(value):
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("temperature must be an integer in Celsius") from exc
    if not 1 <= parsed <= 125:
        raise argparse.ArgumentTypeError("temperature must be in the range 1..125 C")
    return parsed


def set_bit(value, number, enabled):
    if enabled:
        return value | (1 << number)
    return value & ~(1 << number)


def replace_field(value, mask, shift, field):
    return (value & ~mask) | ((field << shift) & mask)


def encode_power_watts(watts, power_unit):
    if not math.isfinite(watts) or watts <= 0:
        raise ControlError("power limit must be finite and greater than zero")
    raw = int(round(watts / power_unit))
    if not 1 <= raw <= 0x7FFF:
        maximum = 0x7FFF * power_unit
        raise ControlError(f"power limit {watts:g} W is outside the representable range (max {maximum:g} W)")
    return raw


def encode_time_window(seconds, time_unit):
    """Return the smallest representable RAPL time window not below the request.

    This matches the daemon's conservative policy: a requested Tau is never
    silently shortened by quantisation.
    """
    if not math.isfinite(seconds) or seconds <= 0:
        raise ControlError("time window must be finite and greater than zero")

    choices = []
    for y in range(32):
        for z in range(4):
            represented = (2**y) * (1.0 + z / 4.0) * time_unit
            if represented >= seconds:
                choices.append((represented, y | (z << 5)))
    if not choices:
        raise ControlError(f"time window {seconds:g}s exceeds the representable RAPL range")
    represented, field = min(choices, key=lambda item: item[0])
    return field, represented


def update_hwp_epp(raw, epp):
    return replace_field(raw, HWP_EPP_MASK, HWP_EPP_SHIFT, epp)


def update_turbo(raw, enabled):
    return set_bit(raw, TURBO_DISABLE_BIT, not enabled)


def update_bd_prochot(raw, enabled):
    current = bit(raw, BD_PROCHOT_ENABLE_BIT)
    if bit(raw, BD_PROCHOT_LOCK_BIT) and current != enabled:
        raise ControlError("BD PROCHOT is locked by MSR_POWER_CTL bit 23")
    return set_bit(raw, BD_PROCHOT_ENABLE_BIT, enabled)


def update_speedstep(raw, enabled):
    return set_bit(raw, SPEEDSTEP_ENABLE_BIT, enabled)


def force_clock_modulation_full(raw):
    # Clearing the on-demand clock-modulation enable bit gives 100% duty cycle
    # while preserving reserved/duty-cycle fields.
    return set_bit(raw, CLOCK_MODULATION_ENABLE_BIT, False)


def update_perf_target_ratio(raw, ratio):
    if not 1 <= ratio <= 0xFF:
        raise ControlError(f"multiplier ratio must be in the range 1..255, got {ratio}")
    return replace_field(raw, PERF_TARGET_RATIO_MASK, PERF_TARGET_RATIO_SHIFT, ratio)


def update_temperature_target(raw, target_c):
    if bit(raw, 31):
        raise ControlError("MSR_TEMPERATURE_TARGET TCC offset is locked (bit 31)")
    tjmax = (raw >> 16) & 0xFF
    if not tjmax:
        raise ControlError("MSR_TEMPERATURE_TARGET does not expose a valid TjMax")
    offset = tjmax - target_c
    if not 0 <= offset <= 0x3F:
        raise ControlError(
            f"target {target_c} C cannot be represented: TjMax={tjmax} C requires offset {offset}"
        )
    return replace_field(raw, 0x3F << 24, 24, offset)


def counter_delta(before, after, width=64):
    return (after - before) % (1 << width)


def effective_mhz(aperf0, mperf0, aperf1, mperf1, base_mhz):
    dm = counter_delta(mperf0, mperf1)
    if dm == 0:
        return 0.0
    return counter_delta(aperf0, aperf1) / dm * base_mhz


def c0_percent(mperf0, mperf1, elapsed, base_mhz):
    if elapsed <= 0 or base_mhz <= 0:
        return 0.0
    active_ticks = counter_delta(mperf0, mperf1)
    return max(0.0, min(100.0, active_ticks / (base_mhz * 1_000_000 * elapsed) * 100.0))


def core_temperature(raw_therm, tjmax):
    if not bit(raw_therm, 31):
        return None
    return tjmax - ((raw_therm >> 16) & 0x7F)


def package_watts(energy0, energy1, elapsed, energy_unit):
    if elapsed <= 0:
        return 0.0
    return counter_delta(energy0, energy1, 32) * energy_unit / elapsed


def update_turbo_ratios(raw, ratios):
    updated = raw
    for core_count, ratio in ratios.items():
        if not 1 <= core_count <= 8:
            raise ControlError(f"unsupported active-core count: {core_count}")
        if not 1 <= ratio <= 0xFF:
            raise ControlError(f"invalid turbo ratio for {core_count}C: {ratio}")
        shift = (core_count - 1) * 8
        updated = replace_field(updated, 0xFF << shift, shift, ratio)
    return updated


def fivr_mv_to_ticks(mv):
    minimum = FIVR_MIN_TICKS / FIVR_TICKS_PER_MV
    if not math.isfinite(mv) or not minimum <= mv <= 0:
        raise ControlError(f"FIVR offset must be between {minimum:g} and 0 mV")
    ticks = int(round(mv * FIVR_TICKS_PER_MV))
    if not FIVR_MIN_TICKS <= ticks <= FIVR_MAX_TICKS:
        raise ControlError("FIVR offset does not fit the signed 11-bit field")
    return ticks


def fivr_ticks_from_response(raw):
    field = (raw >> 21) & 0x7FF
    return field if field < 0x400 else field - 0x800


def fivr_ticks_to_mv(ticks):
    return ticks / FIVR_TICKS_PER_MV


def encode_fivr_read(plane):
    return 0x8000001000000000 | (FIVR_PLANES[plane] << 40)


def encode_fivr_write_ticks(plane, ticks):
    return 0x8000001100000000 | (FIVR_PLANES[plane] << 40) | ((ticks & 0x7FF) << 21)


def encode_iccmax_read(plane):
    return 0x8000001600000000 | (ICCMAX_PLANES[plane] << 40)


def iccmax_to_field(amps):
    field = int(amps * ICCMAX_STEPS_PER_A)
    if not 1 <= field <= ICCMAX_MAX_FIELD:
        raise ControlError("IccMax does not fit the 10-bit quarter-ampere field")
    return field


def encode_iccmax_write_field(plane, field):
    return 0x8000001700000000 | (ICCMAX_PLANES[plane] << 40) | field


def wait_oc_mailbox_ready(msr, cpu=0):
    deadline = time.monotonic() + OC_MAILBOX_TIMEOUT_S
    while True:
        raw = msr.read(cpu, "MSR_OC_MAILBOX")
        if not bit(raw, OC_MAILBOX_BUSY_BIT):
            return raw
        if time.monotonic() >= deadline:
            raise ControlError("OC mailbox timed out waiting for completion")
        time.sleep(OC_MAILBOX_POLL_S)


def execute_oc_mailbox(msr, command, cpu=0):
    wait_oc_mailbox_ready(msr, cpu)
    msr.write(cpu, "MSR_OC_MAILBOX", command)
    result = wait_oc_mailbox_ready(msr, cpu)
    status = (result >> OC_MAILBOX_STATUS_SHIFT) & OC_MAILBOX_STATUS_MASK
    if status:
        raise ControlError(f"OC mailbox command failed with status 0x{status:02x}")
    return result


def read_mailbox_fivr_ticks(msr, plane):
    return fivr_ticks_from_response(execute_oc_mailbox(msr, encode_fivr_read(plane)))


def read_mailbox_iccmax_field(msr, plane):
    return execute_oc_mailbox(msr, encode_iccmax_read(plane)) & ICCMAX_MAX_FIELD


def write_mailbox(msr, value):
    # EliteBook 830 G6 is a single-package target; one package mailbox command
    # through CPU0 is sufficient and avoids redundant commands on sibling CPUs.
    execute_oc_mailbox(msr, value, cpu=0)


def update_pkg_power_limit(raw, units, *, pl1=None, pl2=None, tau1=None, tau2=None,
                           pl1_enable=None, pl2_enable=None, pl1_clamp=None, pl2_clamp=None):
    if bit(raw, PKG_POWER_LOCK_BIT):
        raise ControlError("MSR_PKG_POWER_LIMIT is locked (bit 63); refusing to write")

    updated = raw
    details = {}

    if pl1 is not None:
        field = encode_power_watts(pl1, units["power_w"])
        updated = replace_field(updated, PKG_PL1_POWER_MASK, 0, field)
        details["pl1_w"] = field * units["power_w"]
    if pl2 is not None:
        field = encode_power_watts(pl2, units["power_w"])
        updated = replace_field(updated, PKG_PL2_POWER_MASK, PKG_PL2_POWER_SHIFT, field)
        details["pl2_w"] = field * units["power_w"]
    if tau1 is not None:
        field, represented = encode_time_window(tau1, units["time_s"])
        updated = replace_field(updated, PKG_PL1_TIME_MASK, PKG_PL1_TIME_SHIFT, field)
        details["tau1_s"] = represented
    if tau2 is not None:
        field, represented = encode_time_window(tau2, units["time_s"])
        updated = replace_field(updated, PKG_PL2_TIME_MASK, PKG_PL2_TIME_SHIFT, field)
        details["tau2_s"] = represented

    if pl1_enable is not None:
        updated = set_bit(updated, PKG_PL1_ENABLE_BIT, pl1_enable)
    if pl2_enable is not None:
        updated = set_bit(updated, PKG_PL2_ENABLE_BIT, pl2_enable)
    if pl1_clamp is not None:
        updated = set_bit(updated, PKG_PL1_CLAMP_BIT, pl1_clamp)
    if pl2_clamp is not None:
        updated = set_bit(updated, PKG_PL2_CLAMP_BIT, pl2_clamp)

    return updated, details


def validate_target(force=False):
    cpu = read_cpuinfo()
    dmi = read_dmi()
    status = cpu_target_status(cpu, dmi)
    if not force and not (status["cpuid_match"] and status["dmi_match"]):
        raise ControlError(
            f"refusing writes: expected {TARGET_DMI_PRODUCT} with Whiskey Lake target CPUID; "
            f"got DMI={dmi.get('product')!r}, family/model/stepping="
            f"{cpu['family']}/{cpu['model']}/{cpu['stepping']}. Use --force-target only if intentional."
        )
    return cpu, dmi


def _current_machine_identity():
    cpu = read_cpuinfo()
    dmi = read_dmi()
    return {
        "target": TARGET_DMI_PRODUCT,
        "dmi_product": dmi.get("product"),
        "cpu": {
            "vendor_id": cpu["vendor_id"],
            "family": cpu["family"],
            "model": cpu["model"],
            "stepping": cpu["stepping"],
        },
    }


def write_oc_recovery(kind, values):
    """Persist pre-write OC mailbox state before any voltage/current change."""
    identity = _current_machine_identity()
    record = {
        "schema_version": OC_BACKUP_SCHEMA_VERSION,
        **identity,
        "kind": kind,
        "values": {name: int(value) for name, value in values.items()},
    }
    try:
        OC_BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(OC_BACKUP_DIR, 0o700)
        path = OC_BACKUP_DIR / f"{kind}-{time.time_ns()}.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ControlError(
            f"refusing {kind} write because recovery backup could not be created: {exc}"
        ) from exc
    return path


def load_oc_recovery(path, kind):
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"unable to read OC recovery file {path!r}: {exc}") from exc
    if not isinstance(record, dict) or record.get("schema_version") != OC_BACKUP_SCHEMA_VERSION:
        raise ControlError("unsupported or malformed OC recovery file")
    if record.get("kind") != kind:
        raise ControlError(f"OC recovery kind {record.get('kind')!r} does not match {kind!r}")

    current = _current_machine_identity()
    if record.get("target") != TARGET_DMI_PRODUCT or record.get("dmi_product") != current["dmi_product"]:
        raise ControlError("OC recovery file DMI target does not match this machine")
    if record.get("cpu") != current["cpu"]:
        raise ControlError("OC recovery file CPU identity does not match this machine")

    values = record.get("values")
    if not isinstance(values, dict):
        raise ControlError("OC recovery file values are missing")
    return values


def daemon_is_active():
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", "throttled.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except OSError:
        return False


def ensure_msr_writes_allowed():
    """Enable the msr module write policy only for explicit write-capable commands."""
    path = "/sys/module/msr/parameters/allow_writes"
    try:
        current = Path(path).read_text(encoding="ascii").strip().lower()
    except OSError:
        return
    if current in ("on", "1", "y", "yes"):
        return
    try:
        Path(path).write_text("on", encoding="ascii")
    except OSError as exc:
        raise ControlError(
            f"MSR writes are disabled and {path} could not be set to on: {exc}"
        ) from exc


def validate_write_environment(args):
    if os.geteuid() != 0:
        raise ControlError("register writes require root; run with sudo")
    validate_target(args.force_target)
    if daemon_is_active() and not args.allow_daemon_race:
        raise ControlError(
            "throttled.service is active and may overwrite one-shot register changes; "
            "stop it first or pass --allow-daemon-race"
        )
    ensure_msr_writes_allowed()


def apply_transaction(msr, writes):
    """Apply [(cpu, register, new_value)] atomically as far as MSRs allow.

    All originals are read before the first write.  On failure, registers that
    were already changed are restored in reverse order and the original error is
    re-raised with rollback diagnostics appended.
    """
    originals = []
    for cpu, register, new_value in writes:
        original = msr.read(cpu, register)
        originals.append((cpu, register, original, new_value))

    completed = []
    try:
        for cpu, register, original, new_value in originals:
            if new_value == original:
                completed.append((cpu, register, original))
                continue
            msr.write(cpu, register, new_value)
            completed.append((cpu, register, original))
            readback = msr.read(cpu, register)
            if readback != new_value:
                raise ControlError(
                    f"verify failed for cpu{cpu} {register}: wrote 0x{new_value:016x}, "
                    f"read 0x{readback:016x}"
                )
    except (ProbeError, ControlError) as exc:
        rollback_errors = []
        for cpu, register, original in reversed(completed):
            try:
                msr.write(cpu, register, original)
                if msr.read(cpu, register) != original:
                    rollback_errors.append(f"cpu{cpu} {register}: verify mismatch")
            except (ProbeError, ControlError) as rollback_exc:
                rollback_errors.append(f"cpu{cpu} {register}: {rollback_exc}")
        suffix = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else "; rollback attempted"
        raise ControlError(str(exc) + suffix) from exc

    return [
        {
            "cpu": cpu,
            "register": register,
            "before": original,
            "after": new_value,
            "changed": original != new_value,
        }
        for cpu, register, original, new_value in originals
    ]


def command_status(args, msr):
    report = probe_machine(msr)
    print(format_report(report))
    return 0


def command_epp(args, msr):
    validate_write_environment(args)

    pm_enable = msr.read(0, "IA32_PM_ENABLE")
    if not bit(pm_enable, 0):
        raise ControlError("Intel HWP/Speed Shift is not enabled (IA32_PM_ENABLE bit 0 is clear)")

    writes = []
    package_control = False
    for cpu in msr.cpus():
        raw = msr.read(cpu, "IA32_HWP_REQUEST")
        package_control = package_control or bit(raw, HWP_PACKAGE_CONTROL_BIT)
        writes.append((cpu, "IA32_HWP_REQUEST", update_hwp_epp(raw, args.value)))

    if package_control:
        if "IA32_HWP_REQUEST_PKG" not in MSR:
            raise ControlError("HWP package control is active but package-request MSR support is unavailable")
        pkg = msr.read(0, "IA32_HWP_REQUEST_PKG")
        writes.insert(0, (0, "IA32_HWP_REQUEST_PKG", update_hwp_epp(pkg, args.value)))

    results = apply_transaction(msr, writes)
    print(f"Speed Shift EPP set to {args.value} on {len(msr.cpus())} logical CPUs.")
    if package_control:
        print("Package-control HWP request was updated as well.")
    print(f"Verified {sum(1 for result in results if result['changed'])} changed register values.")
    return 0


def command_turbo(args, msr):
    validate_write_environment(args)
    enabled = args.state
    writes = []
    for cpu in msr.cpus():
        raw = msr.read(cpu, "IA32_MISC_ENABLE")
        writes.append((cpu, "IA32_MISC_ENABLE", update_turbo(raw, enabled)))
    apply_transaction(msr, writes)
    print(f"Turbo {'enabled' if enabled else 'disabled'} and verified.")
    return 0


def command_bd_prochot(args, msr):
    validate_write_environment(args)
    enabled = args.state
    writes = []
    for cpu in msr.cpus():
        raw = msr.read(cpu, "MSR_POWER_CTL")
        writes.append((cpu, "MSR_POWER_CTL", update_bd_prochot(raw, enabled)))
    apply_transaction(msr, writes)
    print(f"BD PROCHOT {'enabled' if enabled else 'disabled'} and verified.")
    return 0


def command_speedstep(args, msr):
    validate_write_environment(args)
    if bit(msr.read(0, "IA32_PM_ENABLE"), 0):
        raise ControlError(
            "SpeedStep/EIST control is not meaningful while HWP/Speed Shift is enabled; "
            "disable HWP in firmware before using this legacy control"
        )
    writes = []
    for cpu in msr.cpus():
        raw = msr.read(cpu, "IA32_MISC_ENABLE")
        writes.append((cpu, "IA32_MISC_ENABLE", update_speedstep(raw, args.state)))
    apply_transaction(msr, writes)
    print(f"SpeedStep/EIST {'enabled' if args.state else 'disabled'} and verified.")
    return 0


def command_clock_mod(args, msr):
    validate_write_environment(args)
    writes = []
    for cpu in msr.cpus():
        raw = msr.read(cpu, "IA32_CLOCK_MODULATION")
        writes.append((cpu, "IA32_CLOCK_MODULATION", force_clock_modulation_full(raw)))
    apply_transaction(msr, writes)
    print("On-demand clock modulation disabled (100% duty cycle) and verified.")
    return 0


def command_temp_target(args, msr):
    validate_write_environment(args)
    platform = msr.read(0, "MSR_PLATFORM_INFO")
    if not bit(platform, 30):
        raise ControlError("MSR_PLATFORM_INFO reports programmable temperature target is unavailable")

    raw = msr.read(0, "MSR_TEMPERATURE_TARGET")
    updated = update_temperature_target(raw, args.celsius)
    apply_transaction(msr, [(0, "MSR_TEMPERATURE_TARGET", updated)])
    print(f"Temperature target set to {args.celsius} C and verified.")
    return 0


def command_multiplier(args, msr):
    validate_write_environment(args)
    if bit(msr.read(0, "IA32_PM_ENABLE"), 0):
        raise ControlError(
            "IA32_PERF_CTL multiplier requests are ignored while HWP/Speed Shift is enabled; "
            "use EPP/HWP controls instead"
        )
    platform = msr.read(0, "MSR_PLATFORM_INFO")
    min_ratio = (platform >> 48) & 0xFF
    max_non_turbo = (platform >> 8) & 0xFF
    turbo_raw = msr.read(0, "MSR_TURBO_RATIO_LIMIT")
    turbo_ratios = [(turbo_raw >> shift) & 0xFF for shift in range(0, 64, 8)]
    detected_max = max([max_non_turbo, *[ratio for ratio in turbo_ratios if ratio]], default=max_non_turbo)
    detected_min = min_ratio or 1
    if not args.force_ratio and not detected_min <= args.ratio <= detected_max:
        raise ControlError(
            f"multiplier {args.ratio}x is outside the detected range "
            f"{detected_min}..{detected_max}; use --force-ratio if intentional"
        )

    writes = []
    for cpu in msr.cpus():
        raw = msr.read(cpu, "IA32_PERF_CTL")
        writes.append((cpu, "IA32_PERF_CTL", update_perf_target_ratio(raw, args.ratio)))
    apply_transaction(msr, writes)
    print(f"IA32_PERF_CTL target multiplier set to {args.ratio}x and verified.")
    return 0


def sample_monitor_counters(msr):
    counters = {}
    for cpu in msr.cpus():
        counters[cpu] = (
            msr.read(cpu, "IA32_APERF"),
            msr.read(cpu, "IA32_MPERF"),
        )
    energy = msr.read(0, "MSR_PKG_ENERGY_STATUS")
    return counters, energy


def command_monitor(args, msr):
    interval = args.interval
    platform = msr.read(0, "MSR_PLATFORM_INFO")
    base_ratio = (platform >> 8) & 0xFF
    base_mhz = base_ratio * 100.0
    temp_target = msr.read(0, "MSR_TEMPERATURE_TARGET")
    tjmax = (temp_target >> 16) & 0xFF or 100
    rapl_units = decode_rapl_units(msr.read(0, "MSR_RAPL_POWER_UNIT"))

    iteration = 0
    try:
        while True:
            before, energy0 = sample_monitor_counters(msr)
            start = time.monotonic()
            time.sleep(interval)
            after, energy1 = sample_monitor_counters(msr)
            elapsed = time.monotonic() - start
            pkg_w = package_watts(energy0, energy1, elapsed, rapl_units["energy_j"])

            pkg_limit_raw = msr.read(0, "MSR_PERF_LIMIT_REASONS")
            pkg_limits = decode_perf_limit_reasons(pkg_limit_raw)["active"]
            pkg_limit_text = ",".join(pkg_limits) if pkg_limits else "OK"
            print(
                f"Package {pkg_w:.2f} W | base {base_mhz:.0f} MHz | interval {elapsed:.3f}s "
                f"| Limits {pkg_limit_text}"
            )
            print(f"{'CPU':>3} {'Ratio':>5} {'MHz':>7} {'Effect':>7} {'C0%':>6} {'Temp':>6}")
            for cpu in msr.cpus():
                perf = msr.read(cpu, "IA32_PERF_STATUS")
                ratio = (perf >> 8) & 0xFF
                mhz = ratio * 100
                therm = msr.read(cpu, "IA32_THERM_STATUS")
                temp = core_temperature(therm, tjmax)
                eff = effective_mhz(
                    before[cpu][0],
                    before[cpu][1],
                    after[cpu][0],
                    after[cpu][1],
                    base_mhz,
                )
                c0 = c0_percent(before[cpu][1], after[cpu][1], elapsed, base_mhz)
                temp_text = "n/a" if temp is None else f"{temp}C"
                print(f"{cpu:>3} {ratio:>5} {mhz:>7} {eff:>7.0f} {c0:>6.1f} {temp_text:>6}")

            iteration += 1
            if args.count and iteration >= args.count:
                break
            print()
    except KeyboardInterrupt:
        pass
    return 0


def command_limits(args, msr):
    raw = msr.read(0, "MSR_PERF_LIMIT_REASONS")
    decoded = decode_perf_limit_reasons(raw)
    active = ",".join(decoded["active"]) if decoded["active"] else "-"
    logged = ",".join(decoded["logged"]) if decoded["logged"] else "-"
    print(f"package: active={active} | logged={logged} | raw=0x{raw:016x}")
    return 1 if args.fail_on_active and decoded["active"] else 0


def command_turbo_ratio(args, msr):
    validate_write_environment(args)
    platform = msr.read(0, "MSR_PLATFORM_INFO")
    if not bit(platform, 28):
        raise ControlError("MSR_PLATFORM_INFO reports programmable turbo ratios are not supported")

    ratios = {
        core_count: value
        for core_count, value in enumerate(
            (args.ratio1, args.ratio2, args.ratio3, args.ratio4, args.ratio5, args.ratio6, args.ratio7, args.ratio8),
            start=1,
        )
        if value is not None
    }
    if not ratios:
        raise ControlError("turbo-ratio requires at least one --1c .. --8c value")
    if not args.force_ratio and any(ratio > 80 for ratio in ratios.values()):
        raise ControlError("turbo ratios above 80x require --force-ratio")

    current = msr.read(0, "MSR_TURBO_RATIO_LIMIT")
    updated = update_turbo_ratios(current, ratios)
    apply_transaction(msr, [(0, "MSR_TURBO_RATIO_LIMIT", updated)])
    rendered = ", ".join(f"{cores}C={ratio}x" for cores, ratio in sorted(ratios.items()))
    print(f"Turbo ratio limits updated and verified: {rendered}")
    return 0


def command_fivr_read(args, msr):
    validate_write_environment(args)
    values = {}
    for plane in FIVR_PLANES:
        ticks = read_mailbox_fivr_ticks(msr, plane)
        values[plane] = fivr_ticks_to_mv(ticks)
    for plane, mv in values.items():
        print(f"{plane:8s} {mv:9.3f} mV")
    return 0


def command_fivr_set(args, msr):
    validate_write_environment(args)
    requested = {
        "core": args.core,
        "gpu": args.gpu,
        "cache": args.cache,
        "uncore": args.uncore,
        "analogio": args.analogio,
    }
    requested = {plane: value for plane, value in requested.items() if value is not None}
    if not requested:
        raise ControlError("fivr set requires at least one voltage plane")

    original = {plane: read_mailbox_fivr_ticks(msr, plane) for plane in FIVR_PLANES}
    backup_path = write_oc_recovery("fivr", original)
    print(f"OC recovery backup written before voltage changes: {backup_path}")
    desired = dict(original)
    desired.update({plane: fivr_mv_to_ticks(mv) for plane, mv in requested.items()})
    if not args.allow_core_cache_mismatch and desired["core"] != desired["cache"]:
        raise ControlError(
            "Core and Cache offsets would differ; set both to the same value or use --allow-core-cache-mismatch"
        )

    changed = []
    try:
        for plane in requested:
            ticks = desired[plane]
            changed.append(plane)
            write_mailbox(msr, encode_fivr_write_ticks(plane, ticks))
            readback = read_mailbox_fivr_ticks(msr, plane)
            if readback != ticks:
                raise ControlError(
                    f"FIVR verify failed for {plane}: requested {fivr_ticks_to_mv(ticks):.3f} mV, "
                    f"read {fivr_ticks_to_mv(readback):.3f} mV"
                )
    except (ProbeError, ControlError) as exc:
        rollback_errors = []
        for plane in reversed(changed):
            try:
                write_mailbox(msr, encode_fivr_write_ticks(plane, original[plane]))
                if read_mailbox_fivr_ticks(msr, plane) != original[plane]:
                    rollback_errors.append(f"{plane}: verify mismatch")
            except (ProbeError, ControlError) as rollback_exc:
                rollback_errors.append(f"{plane}: {rollback_exc}")
        suffix = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else "; rollback attempted"
        raise ControlError(str(exc) + suffix) from exc

    for plane in requested:
        print(f"{plane:8s} {fivr_ticks_to_mv(desired[plane]):9.3f} mV")
    return 0


def command_iccmax_read(args, msr):
    validate_write_environment(args)
    for plane in ICCMAX_PLANES:
        field = read_mailbox_iccmax_field(msr, plane)
        print(f"{plane:8s} {field / ICCMAX_STEPS_PER_A:9.2f} A")
    return 0


def command_iccmax_set(args, msr):
    validate_write_environment(args)
    requested = {"core": args.core, "gpu": args.gpu, "cache": args.cache}
    requested = {plane: value for plane, value in requested.items() if value is not None}
    if not requested:
        raise ControlError("iccmax set requires at least one current plane")

    original = {plane: read_mailbox_iccmax_field(msr, plane) for plane in ICCMAX_PLANES}
    backup_path = write_oc_recovery("iccmax", original)
    print(f"OC recovery backup written before current-limit changes: {backup_path}")
    desired = {plane: iccmax_to_field(value) for plane, value in requested.items()}
    changed = []
    try:
        for plane, field in desired.items():
            changed.append(plane)
            write_mailbox(msr, encode_iccmax_write_field(plane, field))
            readback = read_mailbox_iccmax_field(msr, plane)
            if readback != field:
                raise ControlError(
                    f"IccMax verify failed for {plane}: requested {field / ICCMAX_STEPS_PER_A:.2f} A, "
                    f"read {readback / ICCMAX_STEPS_PER_A:.2f} A"
                )
    except (ProbeError, ControlError) as exc:
        rollback_errors = []
        for plane in reversed(changed):
            try:
                write_mailbox(msr, encode_iccmax_write_field(plane, original[plane]))
                if read_mailbox_iccmax_field(msr, plane) != original[plane]:
                    rollback_errors.append(f"{plane}: verify mismatch")
            except (ProbeError, ControlError) as rollback_exc:
                rollback_errors.append(f"{plane}: {rollback_exc}")
        suffix = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else "; rollback attempted"
        raise ControlError(str(exc) + suffix) from exc

    for plane, field in desired.items():
        print(f"{plane:8s} {field / ICCMAX_STEPS_PER_A:9.2f} A")
    return 0


def command_fivr_restore(args, msr):
    validate_write_environment(args)
    values = load_oc_recovery(args.path, "fivr")
    if set(values) != set(FIVR_PLANES):
        raise ControlError("FIVR recovery file does not contain exactly all voltage planes")
    desired = {}
    for plane in FIVR_PLANES:
        value = values[plane]
        if not isinstance(value, int) or not FIVR_MIN_TICKS <= value <= FIVR_MAX_TICKS:
            raise ControlError(f"invalid recovery FIVR ticks for {plane}: {value!r}")
        desired[plane] = value

    current = {plane: read_mailbox_fivr_ticks(msr, plane) for plane in FIVR_PLANES}
    changed = []
    try:
        for plane, ticks in desired.items():
            if current[plane] == ticks:
                continue
            write_mailbox(msr, encode_fivr_write_ticks(plane, ticks))
            changed.append(plane)
            if read_mailbox_fivr_ticks(msr, plane) != ticks:
                raise ControlError(f"FIVR recovery verify failed for {plane}")
    except (ProbeError, ControlError) as exc:
        rollback_errors = []
        for plane in reversed(changed):
            try:
                write_mailbox(msr, encode_fivr_write_ticks(plane, current[plane]))
                if read_mailbox_fivr_ticks(msr, plane) != current[plane]:
                    rollback_errors.append(f"{plane}: verify mismatch")
            except (ProbeError, ControlError) as rollback_exc:
                rollback_errors.append(f"{plane}: {rollback_exc}")
        suffix = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else "; rollback attempted"
        raise ControlError(f"FIVR recovery failed: {exc}{suffix}") from exc

    print(f"FIVR recovery applied and verified from {args.path}.")
    return 0


def command_iccmax_restore(args, msr):
    validate_write_environment(args)
    values = load_oc_recovery(args.path, "iccmax")
    if set(values) != set(ICCMAX_PLANES):
        raise ControlError("IccMax recovery file does not contain exactly all current planes")
    desired = {}
    for plane in ICCMAX_PLANES:
        value = values[plane]
        if not isinstance(value, int) or not 0 <= value <= ICCMAX_MAX_FIELD:
            raise ControlError(f"invalid recovery IccMax field for {plane}: {value!r}")
        desired[plane] = value

    current = {plane: read_mailbox_iccmax_field(msr, plane) for plane in ICCMAX_PLANES}
    changed = []
    try:
        for plane, field in desired.items():
            if current[plane] == field:
                continue
            write_mailbox(msr, encode_iccmax_write_field(plane, field))
            changed.append(plane)
            if read_mailbox_iccmax_field(msr, plane) != field:
                raise ControlError(f"IccMax recovery verify failed for {plane}")
    except (ProbeError, ControlError) as exc:
        rollback_errors = []
        for plane in reversed(changed):
            try:
                write_mailbox(msr, encode_iccmax_write_field(plane, current[plane]))
                if read_mailbox_iccmax_field(msr, plane) != current[plane]:
                    rollback_errors.append(f"{plane}: verify mismatch")
            except (ProbeError, ControlError) as rollback_exc:
                rollback_errors.append(f"{plane}: {rollback_exc}")
        suffix = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else "; rollback attempted"
        raise ControlError(f"IccMax recovery failed: {exc}{suffix}") from exc

    print(f"IccMax recovery applied and verified from {args.path}.")
    return 0


def command_power(args, msr):
    validate_write_environment(args)

    if all(
        value is None
        for value in (
            args.pl1,
            args.pl2,
            args.tau1,
            args.tau2,
            args.pl1_enable,
            args.pl2_enable,
            args.pl1_clamp,
            args.pl2_clamp,
        )
    ):
        raise ControlError("power command requires at least one setting")

    units = decode_rapl_units(msr.read(0, "MSR_RAPL_POWER_UNIT"))
    current = msr.read(0, "MSR_PKG_POWER_LIMIT")
    updated, represented = update_pkg_power_limit(
        current,
        units,
        pl1=args.pl1,
        pl2=args.pl2,
        tau1=args.tau1,
        tau2=args.tau2,
        pl1_enable=args.pl1_enable,
        pl2_enable=args.pl2_enable,
        pl1_clamp=args.pl1_clamp,
        pl2_clamp=args.pl2_clamp,
    )

    bit_control_requested = any(
        value is not None
        for value in (args.pl1_enable, args.pl2_enable, args.pl1_clamp, args.pl2_clamp)
    )
    if bit_control_requested and not args.no_mmio_sync:
        raise ControlError(
            "PL Enable/Clamp bits cannot be synchronized through intel-rapl-mmio. "
            "For now use --no-mmio-sync only for an explicitly MSR-only validation; "
            "direct MCHBAR bit control remains gated on read-only mirror validation."
        )

    # MSR_PKG_POWER_LIMIT is package-scoped on the single-package 830 G6.
    msr_results = apply_transaction(msr, [(0, "MSR_PKG_POWER_LIMIT", updated)])

    target_decoded = decode_pkg_power_limit(updated, units)
    mmio_zone = None if args.no_mmio_sync else find_mmio_package_zone()
    mmio_requested = any(value is not None for value in (args.pl1, args.pl2, args.tau1, args.tau2))
    mmio_state = None
    if mmio_zone is not None and mmio_requested:
        try:
            mmio_state = mmio_zone.write(
                pl1_w=target_decoded["pl1"]["watts"] if args.pl1 is not None else None,
                pl2_w=target_decoded["pl2"]["watts"] if args.pl2 is not None else None,
                tau1_s=target_decoded["pl1"]["seconds"] if args.tau1 is not None else None,
                tau2_s=target_decoded["pl2"]["seconds"] if args.tau2 is not None else None,
            )
        except PowercapError as exc:
            rollback = [
                (result["cpu"], result["register"], result["before"])
                for result in msr_results
                if result["changed"]
            ]
            if rollback:
                try:
                    apply_transaction(msr, rollback)
                except ControlError as rollback_exc:
                    raise ControlError(
                        f"MMIO powercap update failed: {exc}; MSR rollback also failed: {rollback_exc}"
                    ) from exc
            raise ControlError(f"MMIO powercap update failed: {exc}; MSR changes rolled back") from exc

    decoded = decode_pkg_power_limit(msr.read(0, "MSR_PKG_POWER_LIMIT"), units)
    print(
        f"PL1 {decoded['pl1']['watts']:.3f} W / {decoded['pl1']['seconds']:.6g} s; "
        f"PL2 {decoded['pl2']['watts']:.3f} W / {decoded['pl2']['seconds']:.6g} s"
    )
    if represented:
        print(
            "Represented values: "
            + ", ".join(f"{key}={value:.6g}" for key, value in sorted(represented.items()))
        )
    if mmio_state is not None:
        mmio_pl1 = (mmio_state.get("pl1") or {}).get("power_limit_uw")
        mmio_pl2 = (mmio_state.get("pl2") or {}).get("power_limit_uw")
        rendered1 = "?" if mmio_pl1 is None else f"{mmio_pl1 / 1_000_000:.3f} W"
        rendered2 = "?" if mmio_pl2 is None else f"{mmio_pl2 / 1_000_000:.3f} W"
        print(f"Kernel intel-rapl-mmio synchronized: PL1 {rendered1}; PL2 {rendered2}.")
    elif args.no_mmio_sync:
        print("MMIO synchronization explicitly disabled.")
    else:
        print("Kernel intel-rapl-mmio zone not available; direct MCHBAR was not modified.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="HP EliteBook 830 G6 ThrottleStop-style direct controls")
    parser.add_argument(
        "--force-target",
        action="store_true",
        help="allow writes on a machine that does not match the 830 G6 target guard",
    )
    parser.add_argument(
        "--allow-daemon-race",
        action="store_true",
        help="allow one-shot writes while throttled.service is active",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show the read-only hardware/status probe")
    status.set_defaults(handler=command_status)

    epp = sub.add_parser("epp", help="set Speed Shift Energy Performance Preference")
    epp.add_argument("value", type=parse_epp, help="0 (performance) .. 255 (power saving)")
    epp.set_defaults(handler=command_epp)

    turbo = sub.add_parser("turbo", help="enable or disable Intel Turbo Boost")
    turbo.add_argument("state", type=parse_on_off, metavar="on|off")
    turbo.set_defaults(handler=command_turbo)

    bd = sub.add_parser("bd-prochot", help="enable or disable bidirectional PROCHOT")
    bd.add_argument("state", type=parse_on_off, metavar="on|off")
    bd.set_defaults(handler=command_bd_prochot)

    temp_target = sub.add_parser("temp-target", help="set programmable TCC/PROCHOT temperature target")
    temp_target.add_argument("celsius", type=parse_temperature, metavar="C")
    temp_target.set_defaults(handler=command_temp_target)

    multiplier = sub.add_parser("multiplier", help="set IA32_PERF_CTL target multiplier")
    multiplier.add_argument("ratio", type=parse_ratio, metavar="RATIO")
    multiplier.add_argument(
        "--force-ratio",
        action="store_true",
        help="bypass conservative detected multiplier range checks",
    )
    multiplier.set_defaults(handler=command_multiplier)

    limits = sub.add_parser("limits", help="show ThrottleStop-style core performance limit reasons")
    limits.add_argument("--fail-on-active", action="store_true", help="return status 1 while any limit is active")
    limits.set_defaults(handler=command_limits)

    monitor = sub.add_parser("monitor", help="live Ratio/Effective MHz/C0%%/temperature/limit monitor")
    monitor.add_argument("--interval", type=positive_finite, default=1.0, metavar="SECONDS")
    monitor.add_argument("--count", type=nonnegative_int, default=0, metavar="N", help="stop after N samples (0 = until Ctrl-C)")
    monitor.set_defaults(handler=command_monitor)

    speedstep = sub.add_parser("speedstep", help="enable or disable legacy SpeedStep/EIST")
    speedstep.add_argument("state", type=parse_on_off, metavar="on|off")
    speedstep.set_defaults(handler=command_speedstep)

    clock_mod = sub.add_parser("clock-mod", help="force clock modulation to 100% duty cycle")
    clock_mod.set_defaults(handler=command_clock_mod)

    ratio = sub.add_parser("turbo-ratio", help="set per-active-core Turbo Ratio Limit bytes")
    for cores in range(1, 9):
        ratio.add_argument(f"--{cores}c", dest=f"ratio{cores}", type=parse_ratio, metavar="RATIO")
    ratio.add_argument("--force-ratio", action="store_true", help="allow ratios above the conservative 80x ceiling")
    ratio.set_defaults(handler=command_turbo_ratio)

    fivr = sub.add_parser("fivr", help="query or set FIVR voltage offsets (OC mailbox)")
    fivr_sub = fivr.add_subparsers(dest="fivr_command", required=True)
    fivr_read = fivr_sub.add_parser("read", help="query voltage offsets; this issues an OC-mailbox WRMSR")
    fivr_read.set_defaults(handler=command_fivr_read)
    fivr_restore = fivr_sub.add_parser("restore", help="restore all voltage planes from an automatic recovery JSON")
    fivr_restore.add_argument("path", metavar="PATH")
    fivr_restore.set_defaults(handler=command_fivr_restore)
    fivr_set = fivr_sub.add_parser("set", help="set and verify voltage offsets")
    for plane in FIVR_PLANES:
        fivr_set.add_argument(f"--{plane}", type=parse_undervolt_mv, metavar="MV")
    fivr_set.add_argument(
        "--allow-core-cache-mismatch",
        action="store_true",
        help="allow Core and Cache voltage offsets to differ",
    )
    fivr_set.set_defaults(handler=command_fivr_set)

    iccmax = sub.add_parser("iccmax", help="query or set FIVR current ceilings")
    icc_sub = iccmax.add_subparsers(dest="icc_command", required=True)
    icc_read = icc_sub.add_parser("read", help="query IccMax; this issues an OC-mailbox WRMSR")
    icc_read.set_defaults(handler=command_iccmax_read)
    icc_restore = icc_sub.add_parser("restore", help="restore all IccMax planes from an automatic recovery JSON")
    icc_restore.add_argument("path", metavar="PATH")
    icc_restore.set_defaults(handler=command_iccmax_restore)
    icc_set = icc_sub.add_parser("set", help="set and verify IccMax")
    for plane in ICCMAX_PLANES:
        icc_set.add_argument(f"--{plane}", type=parse_iccmax, metavar="AMPS")
    icc_set.set_defaults(handler=command_iccmax_set)

    power = sub.add_parser("power", help="modify MSR PL1/PL2 and RAPL time windows")
    power.add_argument("--pl1", type=positive_finite, metavar="W")
    power.add_argument("--pl2", type=positive_finite, metavar="W")
    power.add_argument("--tau1", type=positive_finite, metavar="SECONDS")
    power.add_argument("--tau2", type=positive_finite, metavar="SECONDS")
    power.add_argument("--pl1-enable", type=parse_on_off, metavar="on|off")
    power.add_argument("--pl2-enable", type=parse_on_off, metavar="on|off")
    power.add_argument("--pl1-clamp", type=parse_on_off, metavar="on|off")
    power.add_argument("--pl2-clamp", type=parse_on_off, metavar="on|off")
    power.add_argument(
        "--no-mmio-sync",
        action="store_true",
        help="do not synchronize PL1/PL2/Tau through the kernel intel-rapl-mmio zone",
    )
    power.set_defaults(handler=command_power)

    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        msr = MsrAccess()
        msr.ensure()
        return args.handler(args, msr)
    except (ProbeError, ControlError, PowercapError) as exc:
        print(f"control error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
