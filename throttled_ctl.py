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

MCHBAR/MMIO writes are intentionally not enabled until the 830 G6 host bridge
mapping has been validated on real hardware.
"""

import argparse
import math
import os
import subprocess
import sys

from throttled_probe import (
    MSR,
    MsrAccess,
    ProbeError,
    TARGET_DMI_PRODUCT,
    bit,
    cpu_target_status,
    decode_hwp_request,
    decode_pkg_power_limit,
    decode_rapl_units,
    format_report,
    probe_machine,
    read_cpuinfo,
    read_dmi,
)


TURBO_DISABLE_BIT = 38
BD_PROCHOT_ENABLE_BIT = 0
HWP_EPP_SHIFT = 24
HWP_EPP_MASK = 0xFF << HWP_EPP_SHIFT
HWP_PACKAGE_CONTROL_BIT = 42

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
    """Return the closest representable RAPL time-window field."""
    if not math.isfinite(seconds) or seconds <= 0:
        raise ControlError("time window must be finite and greater than zero")

    choices = []
    for y in range(32):
        for z in range(4):
            represented = (2**y) * (1.0 + z / 4.0) * time_unit
            choices.append((abs(represented - seconds), represented, y | (z << 5)))
    _, represented, field = min(choices, key=lambda item: (item[0], item[1]))
    return field, represented


def update_hwp_epp(raw, epp):
    return replace_field(raw, HWP_EPP_MASK, HWP_EPP_SHIFT, epp)


def update_turbo(raw, enabled):
    return set_bit(raw, TURBO_DISABLE_BIT, not enabled)


def update_bd_prochot(raw, enabled):
    return set_bit(raw, BD_PROCHOT_ENABLE_BIT, enabled)


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


def validate_write_environment(args):
    if os.geteuid() != 0:
        raise ControlError("register writes require root; run with sudo")
    validate_target(args.force_target)
    if daemon_is_active() and not args.allow_daemon_race:
        raise ControlError(
            "throttled.service is active and may overwrite one-shot register changes; "
            "stop it first or pass --allow-daemon-race"
        )


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
            readback = msr.read(cpu, register)
            if readback != new_value:
                raise ControlError(
                    f"verify failed for cpu{cpu} {register}: wrote 0x{new_value:016x}, "
                    f"read 0x{readback:016x}"
                )
            completed.append((cpu, register, original))
    except (ProbeError, ControlError) as exc:
        rollback_errors = []
        for cpu, register, original in reversed(completed):
            try:
                msr.write(cpu, register, original)
                if msr.read(cpu, register) != original:
                    rollback_errors.append(f"cpu{cpu} {register}: verify mismatch")
            except ProbeError as rollback_exc:
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

    # Package scoped on the target, but the existing daemon intentionally writes
    # all online logical CPUs.  Match that behavior and verify each view.
    writes = [(cpu, "MSR_PKG_POWER_LIMIT", updated) for cpu in msr.cpus()]
    apply_transaction(msr, writes)

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
    print("MSR power limits verified. MMIO was not modified.")
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

    power = sub.add_parser("power", help="modify MSR PL1/PL2 and RAPL time windows")
    power.add_argument("--pl1", type=positive_finite, metavar="W")
    power.add_argument("--pl2", type=positive_finite, metavar="W")
    power.add_argument("--tau1", type=positive_finite, metavar="SECONDS")
    power.add_argument("--tau2", type=positive_finite, metavar="SECONDS")
    power.add_argument("--pl1-enable", type=parse_on_off, metavar="on|off")
    power.add_argument("--pl2-enable", type=parse_on_off, metavar="on|off")
    power.add_argument("--pl1-clamp", type=parse_on_off, metavar="on|off")
    power.add_argument("--pl2-clamp", type=parse_on_off, metavar="on|off")
    power.set_defaults(handler=command_power)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    msr = MsrAccess()
    msr.ensure()
    return args.handler(args, msr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProbeError, ControlError) as exc:
        print(f"control error: {exc}", file=sys.stderr)
        raise SystemExit(1)
