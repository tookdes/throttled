#!/usr/bin/env python3
import argparse
import asyncio
import configparser
import fcntl
import glob
import gzip
import json
import math
import os
import re
import struct
import subprocess
import sys
import traceback
from collections import defaultdict
from datetime import datetime
from errno import EACCES, EIO, ENOENT, EPERM
from platform import uname
from subprocess import check_output, CalledProcessError, PIPE
from threading import Event, Lock, Thread, current_thread, main_thread
from time import monotonic, sleep, time

from mmio import MMIO, MMIOError
from throttled_powercap import PowercapError, find_mmio_package_zone
from throttled_probe import TARGET_DMI_PRODUCT, read_cpuinfo as probe_read_cpuinfo, read_dmi as probe_read_dmi
from throttled_mchbar import (
    MCHBAR_ADDRESS_MASKS_BY_PCI_DEVICE,
    MCHBAR_ENABLE_BIT,
    MCHBAR_PACKAGE_POWER_LIMIT_OFFSET,
    MCHBAR_PACKAGE_POWER_LIMIT_SIZE,
    MCHBAR_ADDRESS_MASK_39_15,
    MCHBAR_ADDRESS_MASK_39_17,
    MCHBAR_ADDRESS_MASK_42_17,
)
from throttled_version import __version__

DEFAULT_SYSFS_POWER_PATH = '/sys/class/power_supply/AC*/online'
UPOWER_SERVICE = 'org.freedesktop.UPower'
UPOWER_PATH = '/org/freedesktop/UPower'
LOGIN1_SERVICE = 'org.freedesktop.login1'
LOGIN1_PATH = '/org/freedesktop/login1'
LOGIN1_MANAGER_INTERFACE = 'org.freedesktop.login1.Manager'
DBUS_PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'
VOLTAGE_PLANES = {'CORE': 0, 'GPU': 1, 'CACHE': 2, 'UNCORE': 3, 'ANALOGIO': 4}
CURRENT_PLANES = {'CORE': 0, 'GPU': 1, 'CACHE': 2}
TRIP_TEMP_RANGE = [40, 97]
PKG_POWER_LIMIT_POWER_MASK = (1 << 15) - 1
PKG_POWER_LIMIT_TIME_WINDOW_MASK = (1 << 7) - 1
PKG_POWER_LIMIT_LOCK_BIT = 63
PKG_POWER_LIMIT_CONTROL_MASK = (1 << 15) | (1 << 16) | (1 << 47) | (1 << 48)
POWER_PROFILES = ('AC', 'BATTERY')
UNDERVOLT_KEYS = ('UNDERVOLT', 'UNDERVOLT.AC', 'UNDERVOLT.BATTERY')
ICCMAX_KEYS = ('ICCMAX', 'ICCMAX.AC', 'ICCMAX.BATTERY')
DAEMON_LOCK_PATH = '/run/throttled/daemon.lock'
_daemon_lock_file = None
power = {'source': None, 'method': 'polling'}
# Serialize config/source publication separately from low-level hardware writes.
config_lock = Lock()
hardware_lock = Lock()
MSR_DICT = {
    'MSR_PLATFORM_INFO': 0xCE,
    'MSR_OC_MAILBOX': 0x150,
    'IA32_MISC_ENABLE': 0x1A0,
    'IA32_PERF_STATUS': 0x198,
    'IA32_THERM_STATUS': 0x19C,
    'MSR_TEMPERATURE_TARGET': 0x1A2,
    'MSR_POWER_CTL': 0x1FC,
    'MSR_RAPL_POWER_UNIT': 0x606,
    'MSR_PKG_POWER_LIMIT': 0x610,
    'MSR_INTEL_PKG_ENERGY_STATUS': 0x611,
    'MSR_DRAM_ENERGY_STATUS': 0x619,
    'MSR_PP1_ENERGY_STATUS': 0x641,
    'MSR_CONFIG_TDP_CONTROL': 0x64B,
    'IA32_PM_ENABLE': 0x770,
    'IA32_HWP_REQUEST_PKG': 0x772,
    'IA32_HWP_REQUEST': 0x774,
}

HWP_PERFORMANCE_VALUE = 0x20
HWP_DEFAULT_VALUE = 0x80
HWP_INTERVAL = 60
TURBO_DISABLE_BIT = 38
BD_PROCHOT_ENABLE_BIT = 0
UNDERVOLT_TICKS_PER_MV = 1.024
UNDERVOLT_MIN_TICKS = -(1 << 10)
UNDERVOLT_MAX_TICKS = 0
ICCMAX_STEPS_PER_A = 4
ICCMAX_MAX_FIELD = 0x3FF
OC_MAILBOX_BUSY_BIT = 63
OC_MAILBOX_STATUS_SHIFT = 32
OC_MAILBOX_STATUS_MASK = 0xFF
OC_MAILBOX_TIMEOUT_S = 0.05
OC_MAILBOX_POLL_S = 0.00001
OC_BACKUP_DIR = '/var/lib/throttled/oc-backups'
OC_BACKUP_SCHEMA_VERSION = 1
PCI_HOST_BRIDGE_SYSFS_PATH = '/sys/bus/pci/devices/0000:00:00.0'
PCI_VENDOR_ID_INTEL = 0x8086


TESTMSR = False
UNSUPPORTED_FEATURES = []
MONITOR_POWER_DOMAINS = {
    'Package': 'MSR_INTEL_PKG_ENERGY_STATUS',
    'Graphics': 'MSR_PP1_ENERGY_STATUS',
    'DRAM': 'MSR_DRAM_ENERGY_STATUS',
}


class bcolors:
    YELLOW = '\033[93m'
    GREEN = '\033[92m'
    RED = '\033[91m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


OK = bcolors.GREEN + bcolors.BOLD + 'OK' + bcolors.RESET
ERR = bcolors.RED + bcolors.BOLD + 'ERR' + bcolors.RESET
LIM = bcolors.YELLOW + bcolors.BOLD + 'LIM' + bcolors.RESET

log_history = set()

ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*m')


def _format(prefix, msg):
    if args.log:
        tstamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        return f'{tstamp}: {prefix}{ANSI_ESCAPE_RE.sub("", msg)}'
    return f'{prefix}{msg}'


def log(msg, oneshot=False, end='\n'):
    outfile = args.log if args.log else sys.stdout
    if not oneshot or msg.strip() not in log_history:
        print(_format('', msg), file=outfile, end=end)
        if oneshot:
            log_history.add(msg.strip())


def fatal(msg, code=1, end='\n'):
    outfile = args.log if args.log else sys.stderr
    print(_format('[E] ', msg), file=outfile, end=end)
    if current_thread() is not main_thread():
        # sys.exit() would only kill the calling thread, leaving a zombie
        # daemon that looks healthy to systemd but no longer touches MSRs
        outfile.flush()
        os._exit(code)
    sys.exit(code)


def warning(msg, oneshot=True, end='\n'):
    outfile = args.log if args.log else sys.stderr
    if not oneshot or msg.strip() not in log_history:
        print(_format('[W] ', msg), file=outfile, end=end)
        if oneshot:
            log_history.add(msg.strip())


def get_cpu_list():
    """Return sorted CPU indices that expose a /dev/cpu/N entry."""
    try:
        entries = os.listdir('/dev/cpu')
    except FileNotFoundError:
        return []
    return sorted(int(x) for x in entries if x.isdigit())


def get_msr_list():
    """Return the per-CPU MSR device paths in CPU-index order."""
    return [f'/dev/cpu/{cpu:d}/msr' for cpu in get_cpu_list()]


def _ensure_msr_module(cpu=None):
    """Return all MSR devices, or the MSR device for CPU N, loading the module if needed."""
    if cpu is not None:
        target = f'/dev/cpu/{cpu:d}/msr'
        if not os.path.exists(target) and not os.path.exists('/sys/module/msr'):
            try:
                subprocess.check_call(('modprobe', 'msr'))
            except subprocess.CalledProcessError:
                fatal('Unable to load the msr module.')
        if not os.path.exists(target):
            fatal(f'CPU {cpu:d} has no MSR device under /dev/cpu; it may have gone offline.')
        return target

    msr_list = get_msr_list()
    if not msr_list or not os.path.exists(msr_list[0]):
        try:
            subprocess.check_call(('modprobe', 'msr'))
        except subprocess.CalledProcessError:
            fatal('Unable to load the msr module.')
        msr_list = get_msr_list()
    if not msr_list or not os.path.exists(msr_list[0]):
        fatal('No MSR devices found under /dev/cpu after loading the msr module.')
    return msr_list


def writemsr(msr, val, cpu=None):
    """Write a 64-bit value to the named MSR on every online CPU or CPU N."""
    if cpu is not None and cpu < 0:
        fatal('Wrong writemsr cpu param')
    try:
        msr_list = [_ensure_msr_module(cpu)] if cpu is not None else _ensure_msr_module()
        for addr in msr_list:
            f = os.open(addr, os.O_WRONLY)
            try:
                os.lseek(f, MSR_DICT[msr], os.SEEK_SET)
                os.write(f, struct.pack('Q', val))
            finally:
                os.close(f)
    except (IOError, OSError) as e:
        if TESTMSR:
            raise e
        if cpu is not None and e.errno == ENOENT:
            fatal(f'CPU {cpu:d} went offline while writing MSR {msr} ({MSR_DICT[msr]:x}); aborting.')
        if e.errno == EPERM or e.errno == EACCES:
            fatal(
                f'Unable to write to MSR {msr} ({MSR_DICT[msr]:x}). Check that the msr kernel module '
                'is loaded with allow_writes=on and that kernel lockdown is disabled (many kernels '
                'enable lockdown automatically when Secure Boot is on).'
            )
        elif e.errno == EIO:
            fatal(f'Unable to write to MSR {msr} ({MSR_DICT[msr]:x}). Unknown error.')
        else:
            raise e


def readmsr(msr, from_bit=0, to_bit=63, cpu=None, flatten=False):
    """Read the named MSR and return the [from_bit, to_bit] field as
    an unsigned integer. By default returns one value per CPU; with
    cpu=N returns just CPU N, with flatten=True returns the shared value
    (warning if CPUs disagree).
    """
    if cpu is not None and cpu < 0:
        fatal('Wrong readmsr cpu param')
    if from_bit > to_bit:
        fatal('Wrong readmsr bit params')
    try:
        msr_list = [_ensure_msr_module(cpu)] if cpu is not None else _ensure_msr_module()
        output = []
        for addr in msr_list:
            f = os.open(addr, os.O_RDONLY)
            try:
                os.lseek(f, MSR_DICT[msr], os.SEEK_SET)
                val = struct.unpack('Q', os.read(f, 8))[0]
            finally:
                os.close(f)
            output.append(get_value_for_bits(val, from_bit, to_bit))
        if flatten:
            if len(set(output)) > 1:
                warning(f'Found multiple values for {msr:s} ({MSR_DICT[msr]:x}). This should never happen.')
            return output[0]
        if cpu is not None:
            return output[0]
        return output
    except (IOError, OSError) as e:
        if TESTMSR:
            raise e
        if cpu is not None and e.errno == ENOENT:
            fatal(f'CPU {cpu:d} went offline while reading MSR {msr} ({MSR_DICT[msr]:x}); aborting.')
        if e.errno == EPERM or e.errno == EACCES:
            fatal(
                f'Unable to read from MSR {msr} ({MSR_DICT[msr]:x}). Check that the msr kernel module '
                'is loaded and not restricted by kernel lockdown.'
            )
        elif e.errno == EIO:
            fatal(f'Unable to read to MSR {msr} ({MSR_DICT[msr]:x}). Unknown error.')
        else:
            raise e


def get_value_for_bits(val, from_bit=0, to_bit=63):
    """Extract bits [from_bit, to_bit] (inclusive) from val."""
    mask = sum(2**x for x in range(from_bit, to_bit + 1))
    return (val & mask) >> from_bit


def set_msr_allow_writes():
    """Try to enable msr.allow_writes; tolerate kernels that don't expose it."""
    log('[I] Trying to unlock MSR allow_writes.')
    if not os.path.exists('/sys/module/msr'):
        try:
            subprocess.check_call(('modprobe', 'msr'))
        except subprocess.CalledProcessError:
            return
    if os.path.exists('/sys/module/msr/parameters/allow_writes'):
        try:
            with open('/sys/module/msr/parameters/allow_writes', 'w') as f:
                f.write('on')
        except OSError:
            warning('Unable to set MSR allow_writes to on. You might experience warnings in kernel logs.')


def get_dbus_fast():
    """Import dbus-fast lazily so tests and --help do not need a live DBus stack."""
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType

    return MessageBus, BusType


def unwrap_dbus_value(value):
    return value.value if hasattr(value, 'value') else value


async def get_upower_on_battery_async():
    MessageBus, BusType = get_dbus_fast()
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(UPOWER_SERVICE, UPOWER_PATH)
        upower = bus.get_proxy_object(UPOWER_SERVICE, UPOWER_PATH, introspection)
        properties = upower.get_interface(DBUS_PROPERTIES_INTERFACE)
        return bool(unwrap_dbus_value(await properties.call_get(UPOWER_SERVICE, 'OnBattery')))
    finally:
        bus.disconnect()


def get_upower_on_battery():
    return asyncio.run(get_upower_on_battery_async())


def is_on_battery(config):
    """Return True if the system is on battery power.

    Every adapter matched by Sysfs_Power_Path is checked and any one online
    means AC; falls back to UPower over D-Bus on unreadable paths.
    """
    paths = sorted(glob.glob(config.get('GENERAL', 'Sysfs_Power_Path', fallback=DEFAULT_SYSFS_POWER_PATH)))
    values = []
    errors = []
    for path in paths:
        try:
            with open(path) as f:
                value = int(f.read())
            if value not in (0, 1):
                raise ValueError(f'expected 0 or 1, got {value!r}')
            values.append(value)
        except (IOError, OSError, ValueError) as e:
            errors.append(f'{path}: {e}')

    if values and any(value == 1 for value in values):
        if errors:
            warning(f'Sysfs_Power_Path read failed for {len(errors)} path(s): {"; ".join(errors)}')
        return False
    if values and not errors:
        return True
    if errors:
        warning(f'Sysfs_Power_Path read failed for {len(errors)} path(s): {"; ".join(errors)}. Trying upower method.')
    else:
        warning('No valid Sysfs_Power_Path found! Trying upower method')
    try:
        return get_upower_on_battery()
    except Exception:
        pass

    warning('No valid power detection methods found. Assuming that the system is running on battery power.')
    return True


def _current_config(config_or_state):
    return config_or_state['config'] if isinstance(config_or_state, dict) else config_or_state


def _current_regs(config_or_state):
    return config_or_state.get('regs') if isinstance(config_or_state, dict) else None


def config_is_enabled(config):
    """Return whether hardware changes are enabled in the loaded config."""
    return config.getboolean('GENERAL', 'Enabled', fallback=False)


def handle_sleep_prepare(sleeping, config_or_state):
    if sleeping:
        return

    with config_lock:
        config = _current_config(config_or_state)
        regs = _current_regs(config_or_state)
        source = power['source']

    if not config_is_enabled(config) or source not in POWER_PROFILES:
        return

    with hardware_lock:
        mmio_powercap = find_mmio_package_zone()
        mchbar_mmio = None
        if regs is not None and 'MSR_PKG_POWER_LIMIT' in regs.get(source, {}):
            baseline = readmsr('MSR_PKG_POWER_LIMIT', cpu=0)
            mchbar_mmio = open_mchbar_power_limit_mmio(expected_msr=baseline)
        try:
            apply_runtime_profile(
                config,
                regs,
                source,
                mmio_powercap=mmio_powercap,
                mchbar_mmio=mchbar_mmio,
                apply_mailbox=True,
                apply_hwp=_hwp_should_apply_event(config, source),
            )
        finally:
            if mchbar_mmio is not None:
                mchbar_mmio.close()


def handle_ac_properties_changed(if_name, changed, invalidated):
    if "OnBattery" in changed:
        with config_lock:
            power['method'] = 'dbus'
            power['source'] = 'BATTERY' if bool(unwrap_dbus_value(changed['OnBattery'])) else 'AC'


def should_listen_for_resume(config):
    if not config_is_enabled(config):
        return False

    if any(
        config.getfloat(key, plane, fallback=0) != 0
        for keys, planes in ((UNDERVOLT_KEYS, VOLTAGE_PLANES), (ICCMAX_KEYS, CURRENT_PLANES))
        for key in keys
        for plane in planes
    ):
        return True

    if config.getboolean('AC', 'HWP_Mode', fallback=False):
        return True

    resume_options = (
        'HWP_EPP',
        'Disable_Turbo',
        'BDPROCHOT_Enabled',
        'Trip_Temp_C',
        'cTDP',
        'PL1_Tdp_W',
        'PL1_Duration_s',
        'PL2_Tdp_W',
        'PL2_Duration_s',
        'PL1_Enable',
        'PL1_Clamp',
        'PL2_Enable',
        'PL2_Clamp',
    )
    return any(
        profile in config
        and (
            any(config.has_option(profile, option) for option in resume_options)
            or config.getboolean(profile, 'Disable_BDPROCHOT', fallback=False)
        )
        for profile in POWER_PROFILES
    )


async def setup_dbus_signal_handlers(config_or_state):
    from dbus_fast import DBusError, ErrorType

    config = _current_config(config_or_state)
    MessageBus, BusType = get_dbus_fast()
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    context = {'bus': bus}
    try:
        upower_introspection = await bus.introspect(UPOWER_SERVICE, UPOWER_PATH)
        upower = bus.get_proxy_object(UPOWER_SERVICE, UPOWER_PATH, upower_introspection)
        upower_properties = upower.get_interface(DBUS_PROPERTIES_INTERFACE)
        upower_properties.on_properties_changed(handle_ac_properties_changed)
        context['upower'] = upower
        context['upower_properties'] = upower_properties

        resume_required = should_listen_for_resume(config)
        if resume_required or (
            config_is_enabled(config) and config.getboolean('GENERAL', 'Autoreload', fallback=False)
        ):
            try:
                login1_introspection = await bus.introspect(LOGIN1_SERVICE, LOGIN1_PATH)
            except DBusError as e:
                if e.type != ErrorType.SERVICE_UNKNOWN.value or resume_required:
                    raise
                warning('login1 is unavailable; resume-time reapplication is disabled.')
                return context
            login1 = bus.get_proxy_object(LOGIN1_SERVICE, LOGIN1_PATH, login1_introspection)
            login1_manager = login1.get_interface(LOGIN1_MANAGER_INTERFACE)
            login1_manager.on_prepare_for_sleep(lambda sleeping: handle_sleep_prepare(sleeping, config_or_state))
            context['login1'] = login1
            context['login1_manager'] = login1_manager

        return context
    except Exception:
        bus.disconnect()
        raise


async def run_dbus_loop(config_or_state):
    context = await setup_dbus_signal_handlers(config_or_state)
    try:
        await context['bus'].wait_for_disconnect()
    finally:
        context['bus'].disconnect()


def get_cpu_platform_info():
    """Decode MSR_PLATFORM_INFO into a dict of named feature bits."""
    features_msr_value = readmsr('MSR_PLATFORM_INFO', cpu=0)
    cpu_platform_info = {}
    for key, value in platform_info_bits.items():
        cpu_platform_info[key] = int(get_value_for_bits(features_msr_value, value[0], value[1]))
    return cpu_platform_info


def get_reset_thermal_status():
    """Read IA32_THERM_STATUS for every CPU, then clear the sticky log bits."""
    thermal_status_msr_value = readmsr('IA32_THERM_STATUS')
    thermal_status = []
    for msr_value in thermal_status_msr_value:
        thermal_status_core = {}
        for key, value in thermal_status_bits.items():
            thermal_status_core[key] = int(get_value_for_bits(msr_value, value[0], value[1]))
        thermal_status.append(thermal_status_core)
    # reset log bits
    writemsr('IA32_THERM_STATUS', 0)
    return thermal_status


def get_time_unit():
    """Return the RAPL time unit in seconds (Intel SDM Vol. 4, MSR 0x606)."""
    return 1.0 / 2 ** readmsr('MSR_RAPL_POWER_UNIT', 16, 19, cpu=0)


def get_power_unit():
    """Return the RAPL power unit in watts (Intel SDM Vol. 4, MSR 0x606)."""
    return 1.0 / 2 ** readmsr('MSR_RAPL_POWER_UNIT', 0, 3, cpu=0)


def get_critical_temp():
    """Return the package critical temperature offset in degrees Celsius."""
    return readmsr('MSR_TEMPERATURE_TARGET', 16, 23, cpu=0)


def get_cur_pkg_power_limits():
    """Return the current package power-limit register and decoded fields."""
    value = readmsr('MSR_PKG_POWER_LIMIT', flatten=True)
    return {
        'RAW': value,
        'LOCKED': bool(get_value_for_bits(value, PKG_POWER_LIMIT_LOCK_BIT, PKG_POWER_LIMIT_LOCK_BIT)),
        'PL1': get_value_for_bits(value, 0, 14),
        'PL1_ENABLED': bool(get_value_for_bits(value, 15, 15)),
        'PL1_CLAMP': bool(get_value_for_bits(value, 16, 16)),
        'TW1': get_value_for_bits(value, 17, 23),
        'PL2': get_value_for_bits(value, 32, 46),
        'PL2_ENABLED': bool(get_value_for_bits(value, 47, 47)),
        'PL2_CLAMP': bool(get_value_for_bits(value, 48, 48)),
        'TW2': get_value_for_bits(value, 49, 55),
    }


def decode_time_window_field(field, time_unit=None):
    """Decode a 7-bit RAPL time-window field to seconds."""
    if time_unit is None:
        time_unit = get_time_unit()
    y = field & 0x1F
    z = (field >> 5) & 0x3
    return (2**y) * (1.0 + z / 4.0) * time_unit


def calc_time_window_vars(t):
    """Encode a time-window duration (s) as the (Y, Z) pair used by
    MSR_PKG_POWER_LIMIT."""
    time_unit = get_time_unit()
    for Y in range(2**5):
        for Z in range(2**2):
            if t <= (2**Y) * (1.0 + Z / 4.0) * time_unit:
                return (Y, Z)
    raise ValueError('Unable to find a good combination!')


def _encode_pkg_power_limit(
    pl1,
    tw1,
    pl2,
    tw2,
    *,
    base_value=0,
    pl1_enabled=True,
    pl1_clamp=True,
    pl2_enabled=True,
    pl2_clamp=False,
):
    """Encode MSR_PKG_POWER_LIMIT while preserving unrelated/reserved bits."""
    fields = (
        ('PL1', pl1, PKG_POWER_LIMIT_POWER_MASK),
        ('TW1', tw1, PKG_POWER_LIMIT_TIME_WINDOW_MASK),
        ('PL2', pl2, PKG_POWER_LIMIT_POWER_MASK),
        ('TW2', tw2, PKG_POWER_LIMIT_TIME_WINDOW_MASK),
    )
    encoded = {}
    for name, value, mask in fields:
        if not isinstance(value, int) or not 0 <= value <= mask:
            raise ValueError(f'{name:s} value {value!r} does not fit its {mask.bit_length():d}-bit field.')
        if name in ('PL1', 'PL2') and value == 0:
            raise ValueError(f'{name:s} must be at least one power-unit tick, got {value!r}.')
        encoded[name] = value

    programmable_mask = (
        PKG_POWER_LIMIT_POWER_MASK
        | (1 << 15)
        | (1 << 16)
        | (PKG_POWER_LIMIT_TIME_WINDOW_MASK << 17)
        | (PKG_POWER_LIMIT_POWER_MASK << 32)
        | (1 << 47)
        | (1 << 48)
        | (PKG_POWER_LIMIT_TIME_WINDOW_MASK << 49)
    )
    value = base_value & ~programmable_mask
    value |= encoded['PL1']
    value |= encoded['TW1'] << 17
    value |= encoded['PL2'] << 32
    value |= encoded['TW2'] << 49
    if pl1_enabled:
        value |= 1 << 15
    if pl1_clamp:
        value |= 1 << 16
    if pl2_enabled:
        value |= 1 << 47
    if pl2_clamp:
        value |= 1 << 48
    return value


def _undervolt_offset_to_ticks(offset):
    """Convert an undervolt in mV to the signed 11-bit mailbox field."""
    try:
        offset = float(offset)
    except (TypeError, ValueError) as e:
        raise ValueError(f'Undervolt offset must be a number, got {offset!r}.') from e
    minimum_mv = UNDERVOLT_MIN_TICKS / UNDERVOLT_TICKS_PER_MV
    if not math.isfinite(offset) or not minimum_mv <= offset <= 0:
        raise ValueError(f'Undervolt offset must be between {minimum_mv:g} and 0 mV, got {offset!r}.')
    ticks = int(round(offset * UNDERVOLT_TICKS_PER_MV))
    if not UNDERVOLT_MIN_TICKS <= ticks <= UNDERVOLT_MAX_TICKS:
        raise ValueError(f'Undervolt offset {offset!r} mV does not fit the signed 11-bit mailbox field.')
    return ticks


def calc_undervolt_msr(plane, offset):
    """Return the value to be written in the MSR 150h for setting the given
    offset voltage (in mV) to the given voltage plane.
    """
    if plane not in VOLTAGE_PLANES:
        raise ValueError(f'Unknown voltage plane: {plane!r}.')
    ticks = _undervolt_offset_to_ticks(offset)
    encoded_offset = (ticks & 0x7FF) << 21
    return 0x8000001100000000 | (VOLTAGE_PLANES[plane] << 40) | encoded_offset


def calc_undervolt_ticks(msr_value):
    """Return the signed 11-bit OC-mailbox voltage-offset field."""
    offset = (msr_value & 0xFFE00000) >> 21
    return offset if offset < 0x400 else offset - 0x800


def calc_undervolt_mv(msr_value):
    """Return the offset voltage (in mV) from the given raw MSR 150h value."""
    return int(round(calc_undervolt_ticks(msr_value) / UNDERVOLT_TICKS_PER_MV))


class OCMailboxError(OSError):
    """OC mailbox command was rejected, timed out, or failed verification."""


def wait_oc_mailbox_ready():
    deadline = monotonic() + OC_MAILBOX_TIMEOUT_S
    while True:
        raw = readmsr('MSR_OC_MAILBOX', cpu=0)
        if not ((raw >> OC_MAILBOX_BUSY_BIT) & 1):
            return raw
        if monotonic() >= deadline:
            raise OCMailboxError('OC mailbox timed out waiting for completion.')
        sleep(OC_MAILBOX_POLL_S)


def execute_oc_mailbox(command):
    """Execute one package OC-mailbox transaction through CPU0 and check status."""
    wait_oc_mailbox_ready()
    writemsr('MSR_OC_MAILBOX', command, cpu=0)
    result = wait_oc_mailbox_ready()
    status = (result >> OC_MAILBOX_STATUS_SHIFT) & OC_MAILBOX_STATUS_MASK
    if status:
        raise OCMailboxError(f'OC mailbox command failed with status {status:#04x}.')
    return result


def write_daemon_oc_recovery(kind, values):
    """Persist pre-write OC state in the same schema consumed by throttlectl restore."""
    try:
        cpu = probe_read_cpuinfo()
        dmi = probe_read_dmi()
        payload = {
            'schema_version': OC_BACKUP_SCHEMA_VERSION,
            'target': TARGET_DMI_PRODUCT,
            'dmi_product': dmi.get('product'),
            'cpu': {
                'vendor_id': cpu['vendor_id'],
                'family': cpu['family'],
                'model': cpu['model'],
                'stepping': cpu['stepping'],
            },
            'kind': kind,
            'values': {str(name).lower(): int(value) for name, value in values.items()},
            'source': 'daemon',
        }
        os.makedirs(OC_BACKUP_DIR, mode=0o700, exist_ok=True)
        os.chmod(OC_BACKUP_DIR, 0o700)
        stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')
        path = os.path.join(OC_BACKUP_DIR, f'{kind}-{stamp}-{os.getpid():d}.json')
        with open(path, 'x', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
        os.chmod(path, 0o600)
        return path
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise OCMailboxError(
            f'refusing {kind} update because the recovery backup could not be created: {exc}'
        ) from exc


def _rollback_undervolt_planes(planes, original):
    errors = []
    for plane in reversed(planes):
        try:
            current = get_undervolt(plane)[plane]
            original_field = original[plane] & 0xFFE00000
            if (current & 0xFFE00000) != original_field:
                restore = (
                    0x8000001100000000
                    | (VOLTAGE_PLANES[plane] << 40)
                    | original_field
                )
                execute_oc_mailbox(restore)
            verify = get_undervolt(plane)[plane]
            if (verify & 0xFFE00000) != original_field:
                errors.append(f'{plane}: verify mismatch')
        except (OSError, OCMailboxError) as exc:
            errors.append(f'{plane}: {exc}')
    return errors


def _rollback_iccmax_planes(planes, original):
    errors = []
    for plane in reversed(planes):
        try:
            current = get_icc_max(plane)[plane]
            original_field = original[plane] & ICCMAX_MAX_FIELD
            if current != original_field:
                restore = (
                    0x8000001700000000
                    | (CURRENT_PLANES[plane] << 40)
                    | original_field
                )
                execute_oc_mailbox(restore)
            verify = get_icc_max(plane)[plane]
            if verify != original_field:
                errors.append(f'{plane}: verify mismatch')
        except (OSError, OCMailboxError) as exc:
            errors.append(f'{plane}: {exc}')
    return errors


def get_undervolt(plane=None, convert=False):
    """Read current voltage offsets using the package OC mailbox protocol."""
    if 'UNDERVOLT' in UNSUPPORTED_FEATURES:
        return 0
    planes = [plane] if plane in VOLTAGE_PLANES else VOLTAGE_PLANES
    out = {}
    for plane in planes:
        result = execute_oc_mailbox(0x8000001000000000 | (VOLTAGE_PLANES[plane] << 40))
        read_value = result & 0xFFFFFFFF
        out[plane] = calc_undervolt_mv(read_value) if convert else read_value

    return out


def undervolt(config, source=None):
    """Apply all configured voltage planes transactionally with persistent recovery."""
    source = source or power['source']
    section = f'UNDERVOLT.{source}'
    if (section not in config and 'UNDERVOLT' not in config) or 'UNDERVOLT' in UNSUPPORTED_FEATURES:
        return

    original = {}
    desired = {}
    changed_planes = []
    for plane in VOLTAGE_PLANES:
        write_offset_mv = config.getfloat(
            section,
            plane,
            fallback=config.getfloat('UNDERVOLT', plane, fallback=0.0),
        )
        write_value = calc_undervolt_msr(plane, write_offset_mv)
        current_value = get_undervolt(plane)[plane]
        original[plane] = current_value
        desired[plane] = (write_value, write_value & 0xFFE00000, write_offset_mv)
        if (current_value & 0xFFE00000) != desired[plane][1]:
            changed_planes.append(plane)

    if not changed_planes:
        if args.debug:
            log('[D] All configured undervolt planes already match; skipping writes.')
        return

    backup = write_daemon_oc_recovery(
        'fivr',
        {plane: calc_undervolt_ticks(value) for plane, value in original.items()},
    )
    log(f'[I] OC recovery backup written before daemon undervolt changes: {backup}')

    try:
        for plane in changed_planes:
            write_value, desired_field, write_offset_mv = desired[plane]
            execute_oc_mailbox(write_value)
            read_value = get_undervolt(plane)[plane]
            if (read_value & 0xFFE00000) != desired_field:
                raise OCMailboxError(
                    f'Undervolt verify failed for {plane:s}: requested {write_offset_mv:.0f} mV, '
                    f'read {calc_undervolt_mv(read_value):.0f} mV.'
                )
            if args.debug:
                log(
                    f'[D] Undervolt plane {plane:s} - write {write_offset_mv:.0f} mV '
                    f'- read {calc_undervolt_mv(read_value):.0f} mV - match {OK}'
                )
    except (OSError, OCMailboxError) as exc:
        rollback_errors = _rollback_undervolt_planes(changed_planes, original)
        suffix = (
            f'; rollback issues: {"; ".join(rollback_errors)}'
            if rollback_errors
            else '; rollback verified'
        )
        raise OCMailboxError(
            f'Daemon undervolt transaction failed: {exc}; recovery backup: {backup}{suffix}'
        ) from exc


def _icc_max_to_field(current):
    """Convert an IccMax in A to the unsigned 10-bit quarter-ampere field."""
    try:
        current = float(current)
    except (TypeError, ValueError) as e:
        raise ValueError(f'IccMax must be a number, got {current!r}.') from e
    maximum_a = ICCMAX_MAX_FIELD / ICCMAX_STEPS_PER_A
    if not math.isfinite(current) or not 0 < current <= maximum_a:
        raise ValueError(f'IccMax must be between 0 (exclusive) and {maximum_a:g} A, got {current!r}.')
    # floor: quantisation must never enforce a ceiling above the configured one
    field = int(current * ICCMAX_STEPS_PER_A)
    if not 1 <= field <= ICCMAX_MAX_FIELD:
        raise ValueError(f'IccMax {current!r} A quantises outside the unsigned 10-bit field.')
    return field


def calc_icc_max_msr(plane, current):
    """Return the value to be written in the MSR 150h for setting the given
    IccMax (in A) to the given current plane.
    """
    if plane not in CURRENT_PLANES:
        raise ValueError(f'Unknown current plane: {plane!r}.')
    return 0x8000001700000000 | (CURRENT_PLANES[plane] << 40) | _icc_max_to_field(current)


def calc_icc_max_amp(msr_value):
    """Return the max current (in A) from the given raw MSR 150h value."""
    return (msr_value & 0x3FF) / 4.0


def get_configured_power_profiles(config):
    """Return the AC/BATTERY power profiles present in the config file."""
    return [profile for profile in POWER_PROFILES if profile in config]


def get_update_rate(config, power_source):
    """Return the update rate for power_source, or any configured profile."""
    update_rate = config.getfloat(power_source, 'Update_Rate_s', fallback=None)
    if update_rate is not None:
        return update_rate

    for fallback_power_source in get_configured_power_profiles(config):
        update_rate = config.getfloat(fallback_power_source, 'Update_Rate_s', fallback=None)
        if update_rate is not None:
            return update_rate

    fatal('At least one configured power profile must define "Update_Rate_s".')


def get_icc_max(plane=None, convert=False):
    """Read IccMax through the package OC mailbox protocol."""
    if 'ICCMAX' in UNSUPPORTED_FEATURES:
        return 0
    planes = [plane] if plane in CURRENT_PLANES else CURRENT_PLANES
    out = {}
    for plane in planes:
        result = execute_oc_mailbox(0x8000001600000000 | (CURRENT_PLANES[plane] << 40))
        read_value = result & ICCMAX_MAX_FIELD
        out[plane] = calc_icc_max_amp(read_value) if convert else read_value

    return out


def set_icc_max(config, source=None):
    """Apply configured IccMax planes transactionally with persistent recovery."""
    if 'ICCMAX' in UNSUPPORTED_FEATURES:
        return
    source = source or power['source']
    section = f'ICCMAX.{source}'

    requested = {}
    for plane in CURRENT_PLANES:
        try:
            write_current_amp = config.getfloat(
                section,
                plane,
                fallback=config.getfloat('ICCMAX', plane, fallback=-1.0),
            )
        except (configparser.NoSectionError, configparser.NoOptionError):
            continue
        if write_current_amp <= 0:
            if any(config.getfloat(key, plane, fallback=-1.0) > 0 for key in ICCMAX_KEYS):
                warning(
                    f'IccMax {plane:s} is not configured for the {source:s} profile: '
                    'leaving it untouched.'
                )
            continue
        write_value = calc_icc_max_msr(plane, write_current_amp)
        requested[plane] = (write_value, write_value & ICCMAX_MAX_FIELD)

    if not requested:
        return

    # Read every plane so the backup remains directly consumable by throttlectl restore.
    original = {plane: get_icc_max(plane)[plane] for plane in CURRENT_PLANES}
    changed_planes = [
        plane for plane, (_, desired_field) in requested.items()
        if original[plane] != desired_field
    ]
    if not changed_planes:
        if args.debug:
            log('[D] All configured IccMax planes already match; skipping writes.')
        return

    backup = write_daemon_oc_recovery('iccmax', original)
    log(f'[I] OC recovery backup written before daemon IccMax changes: {backup}')

    try:
        for plane in changed_planes:
            write_value, desired_field = requested[plane]
            execute_oc_mailbox(write_value)
            read_value = get_icc_max(plane)[plane]
            if read_value != desired_field:
                raise OCMailboxError(
                    f'IccMax verify failed for {plane:s}: requested '
                    f'{calc_icc_max_amp(desired_field):.2f} A, '
                    f'read {calc_icc_max_amp(read_value):.2f} A.'
                )
            if args.debug:
                log(
                    f'[D] IccMax plane {plane:s} - write {calc_icc_max_amp(desired_field):.2f} A '
                    f'- read {calc_icc_max_amp(read_value):.2f} A - match {OK}'
                )
    except (OSError, OCMailboxError) as exc:
        rollback_errors = _rollback_iccmax_planes(changed_planes, original)
        suffix = (
            f'; rollback issues: {"; ".join(rollback_errors)}'
            if rollback_errors
            else '; rollback verified'
        )
        raise OCMailboxError(
            f'Daemon IccMax transaction failed: {exc}; recovery backup: {backup}{suffix}'
        ) from exc


def _remove_config_option(config, section, option):
    """Drop option from the layer holding it and return that layer's name."""
    if config.remove_option(section, option):
        return section
    config.remove_option(config.default_section, option)
    return config.default_section


def load_config():
    """Parse the config file, validating and clamping out-of-range values."""
    config = configparser.ConfigParser()
    config.read(args.config)

    power_profiles = get_configured_power_profiles(config)
    if not power_profiles:
        fatal('At least one power profile ([AC] or [BATTERY]) is required.')

    boolean_options = [('GENERAL', 'Enabled'), ('GENERAL', 'Autoreload'), ('AC', 'HWP_Mode')]
    boolean_options.extend(
        (profile, option)
        for profile in power_profiles
        for option in ('Disable_BDPROCHOT', 'BDPROCHOT_Enabled', 'Disable_Turbo', 'PL1_Enable', 'PL1_Clamp', 'PL2_Enable', 'PL2_Clamp')
    )
    for section, option in boolean_options:
        try:
            config.getboolean(section, option, fallback=None)
        except (ValueError, configparser.InterpolationError):
            fatal(f'The "{option:s}" parameter in [{section:s}] must be a boolean.')

    # HWP_EPP is the direct ThrottleStop-style Speed Shift EPP value.
    # Keep HWP_Mode for backward compatibility, but prefer per-profile HWP_EPP.
    for power_source in power_profiles:
        try:
            hwp_epp = config.getint(power_source, 'HWP_EPP', fallback=None)
        except (ValueError, configparser.InterpolationError):
            fatal(f'The "HWP_EPP" parameter in [{power_source:s}] must be an integer from 0 to 255.')
        if hwp_epp is not None and not 0 <= hwp_epp <= 255:
            fatal(f'The "HWP_EPP" parameter in [{power_source:s}] must be an integer from 0 to 255.')

    # config values sanity check
    for power_source in power_profiles:
        for option in ('Update_Rate_s', 'PL1_Tdp_W', 'PL1_Duration_s', 'PL2_Tdp_W', 'PL2_Duration_s'):
            value = None
            # a malformed profile value may mask a second malformed value inherited from [DEFAULT]
            for _ in range(2):
                try:
                    value = config.getfloat(power_source, option, fallback=None)
                    if value is None or math.isfinite(value):
                        break
                    raise ValueError(value)
                except (ValueError, configparser.InterpolationError):
                    value = None
                    section = _remove_config_option(config, power_source, option)
                    if option == 'Update_Rate_s':
                        fatal(f'The mandatory "Update_Rate_s" parameter in [{section:s}] must be a finite number.')
                    warning(f'Invalid "{option:s}" value in [{section:s}]: ignoring it.', oneshot=False)
            if value is not None:
                config.set(power_source, option, str(max(0.001, value)))
            elif option == 'Update_Rate_s':
                fatal(f'The mandatory "Update_Rate_s" parameter is missing in the [{power_source:s}] profile.')

        trip_temp = None
        for _ in range(2):
            try:
                trip_temp = config.getfloat(power_source, 'Trip_Temp_C', fallback=None)
                if trip_temp is None or math.isfinite(trip_temp):
                    break
                raise ValueError(trip_temp)
            except (ValueError, configparser.InterpolationError):
                trip_temp = None
                section = _remove_config_option(config, power_source, 'Trip_Temp_C')
                warning(f'Invalid "Trip_Temp_C" value in [{section:s}]: ignoring it.', oneshot=False)
        if trip_temp is not None:
            valid_trip_temp = min(TRIP_TEMP_RANGE[1], max(TRIP_TEMP_RANGE[0], trip_temp))
            if trip_temp != valid_trip_temp:
                config.set(power_source, 'Trip_Temp_C', str(valid_trip_temp))
                log(
                    f'[!] Overriding invalid "Trip_Temp_C" value in "{power_source:s}": {trip_temp:.1f} -> {valid_trip_temp:.1f}'
                )

    # handle the case where only one of UNDERVOLT.AC, UNDERVOLT.BATTERY keys exists
    # by forcing the other key to all zeros (ie. no undervolt); synthesizing it
    # before the vetting below puts its [DEFAULT]-inherited planes through it too
    if any(key in config for key in UNDERVOLT_KEYS[1:]):
        for key in UNDERVOLT_KEYS[1:]:
            if key not in config:
                config.add_section(key)

    # the mailbox field is signed 11-bit: reject anything below -1000 mV instead of wrapping it positive
    for key in UNDERVOLT_KEYS:
        for plane in VOLTAGE_PLANES:
            if key in config:
                for _ in range(2):
                    try:
                        value = config.getfloat(key, plane, fallback=0.0)
                        if not math.isfinite(value):
                            raise ValueError(f'Undervolt offset must be finite, got {value!r}.')
                        if value > 0:
                            config.set(key, plane, '0')
                            log(
                                f'[!] Overriding invalid "{key:s}" value in "{plane:s}" voltage plane: {value:.0f} -> 0'
                            )
                        else:
                            _undervolt_offset_to_ticks(value)
                        break
                    except (ValueError, configparser.InterpolationError) as e:
                        section = key if config.remove_option(key, plane) else config.default_section
                        warning(f'Invalid value for {plane:s} in [{section:s}]: {e}', oneshot=False)
                        if section == config.default_section:
                            # the plane names are shared with ICCMAX: shadow the inherited value, never touch [DEFAULT]
                            config.set(key, plane, '0')
                            break

    for key in UNDERVOLT_KEYS[1:]:
        if key in config:
            for plane in VOLTAGE_PLANES:
                config.set(key, plane, str(config.getfloat(key, plane, fallback=0.0)))

    # Check for CORE/CACHE values mismatch
    for key in UNDERVOLT_KEYS:
        if key in config:
            if config.getfloat(key, 'CORE', fallback=0) != config.getfloat(key, 'CACHE', fallback=0):
                warning('On Skylake and newer CPUs CORE and CACHE values should match!')
                break

    iccmax_enabled = False
    # check for invalid values (ie. <= 0 or > 0x3FF) in the IccMax settings
    for key in ICCMAX_KEYS:
        if key not in config:
            continue
        for option in config[key]:
            if option in config.defaults():
                continue
            if option.upper() not in CURRENT_PLANES:
                warning(f'Unknown IccMax plane "{option:s}" in [{key:s}]: ignoring it.', oneshot=False)
        for plane in CURRENT_PLANES:
            if key in config:
                for _ in range(2):
                    try:
                        value = config.getfloat(key, plane)
                        _icc_max_to_field(value)
                        iccmax_enabled = True
                        break
                    except (ValueError, configparser.InterpolationError) as e:
                        section = key if config.remove_option(key, plane) else config.default_section
                        warning(f'Invalid value for {plane:s} in [{section:s}]: {e}', oneshot=False)
                        if section == config.default_section:
                            # the plane names are shared with UNDERVOLT: shadow the inherited value, never touch [DEFAULT]
                            config.set(key, plane, '0')
                            break
                    except configparser.NoOptionError:
                        break
    if iccmax_enabled:
        warning('Warning! Raising IccMax above design limits can damage your system!')

    return config


def calc_reg_values(platform_info, config):
    """Compute the MSR values to apply for each power source from the config."""
    regs = defaultdict(dict)
    for power_source in get_configured_power_profiles(config):
        if platform_info['feature_programmable_temperature_target'] != 1:
            warning("Setting temperature target is not supported by this CPU")
        else:
            critical_temp = get_critical_temp()
            # update the allowed temp range to keep at least 3 'C from the CPU critical temperature
            global TRIP_TEMP_RANGE
            TRIP_TEMP_RANGE[1] = min(TRIP_TEMP_RANGE[1], critical_temp - 3)

            Trip_Temp_C = config.getfloat(power_source, 'Trip_Temp_C', fallback=None)
            if Trip_Temp_C is not None:
                valid_trip_temp = min(TRIP_TEMP_RANGE[1], max(TRIP_TEMP_RANGE[0], Trip_Temp_C))
                if valid_trip_temp != Trip_Temp_C:
                    log(
                        f'[!] Overriding "Trip_Temp_C" in "{power_source:s}" to stay within '
                        f'[{TRIP_TEMP_RANGE[0]:d}, {TRIP_TEMP_RANGE[1]:d}] C: '
                        f'{Trip_Temp_C:.1f} -> {valid_trip_temp:.1f}'
                    )
                    Trip_Temp_C = valid_trip_temp
                trip_offset = int(round(critical_temp - Trip_Temp_C))
                if trip_offset > 63:
                    # the offset field is 6 bits wide: a larger value would
                    # spill into adjacent bits and corrupt the register
                    log(
                        f'[!] Overriding "Trip_Temp_C" in "{power_source:s}": offset {trip_offset:d} '
                        f'exceeds the 6-bit MSR field, clamping to {critical_temp - 63:d} C'
                    )
                    trip_offset = 63
                regs[power_source]['MSR_TEMPERATURE_TARGET'] = trip_offset << 24
            else:
                log(f'[I] {power_source:s} trip temperature is disabled in config.')

        PL1_Tdp_W = config.getfloat(power_source, 'PL1_Tdp_W', fallback=None)
        PL1_Duration_s = config.getfloat(power_source, 'PL1_Duration_s', fallback=None)
        PL2_Tdp_W = config.getfloat(power_source, 'PL2_Tdp_W', fallback=None)
        PL2_Duration_s = config.getfloat(power_source, 'PL2_Duration_s', fallback=None)
        numeric_tpl_values = (PL1_Tdp_W, PL1_Duration_s, PL2_Tdp_W, PL2_Duration_s)
        numeric_tpl_configured = any(value is not None for value in numeric_tpl_values)
        tpl_boolean_options = ('PL1_Enable', 'PL1_Clamp', 'PL2_Enable', 'PL2_Clamp')
        boolean_tpl_configured = any(config.has_option(power_source, option) for option in tpl_boolean_options)

        if numeric_tpl_configured or boolean_tpl_configured:
            cur_pkg_power_limits = get_cur_pkg_power_limits()
            power_unit = get_power_unit() if PL1_Tdp_W is not None or PL2_Tdp_W is not None else None

            if PL1_Tdp_W is None:
                PL1 = cur_pkg_power_limits['PL1']
                log(f'[I] {power_source:s} PL1_Tdp_W disabled in config.')
            else:
                PL1 = int(round(PL1_Tdp_W / power_unit))

            if PL1_Duration_s is None:
                TW1 = cur_pkg_power_limits['TW1']
                log(f'[I] {power_source:s} PL1_Duration_s disabled in config.')
            else:
                Y, Z = calc_time_window_vars(PL1_Duration_s)
                TW1 = Y | (Z << 5)

            if PL2_Tdp_W is None:
                PL2 = cur_pkg_power_limits['PL2']
                log(f'[I] {power_source:s} PL2_Tdp_W disabled in config.')
            else:
                PL2 = int(round(PL2_Tdp_W / power_unit))

            if PL2_Duration_s is None:
                TW2 = cur_pkg_power_limits['TW2']
                log(f'[I] {power_source:s} PL2_Duration_s disabled in config.')
            else:
                Y, Z = calc_time_window_vars(PL2_Duration_s)
                TW2 = Y | (Z << 5)

            # Preserve upstream behavior for existing configs that specify numeric
            # TPL values but not the new boolean knobs. For a boolean-only edit,
            # unspecified flags inherit the live hardware state instead.
            boolean_defaults = {
                'PL1_Enable': True if numeric_tpl_configured else cur_pkg_power_limits['PL1_ENABLED'],
                'PL1_Clamp': True if numeric_tpl_configured else cur_pkg_power_limits['PL1_CLAMP'],
                'PL2_Enable': True if numeric_tpl_configured else cur_pkg_power_limits['PL2_ENABLED'],
                'PL2_Clamp': False if numeric_tpl_configured else cur_pkg_power_limits['PL2_CLAMP'],
            }

            if cur_pkg_power_limits.get('LOCKED', False):
                warning(
                    f'MSR_PKG_POWER_LIMIT is locked; package power-limit writes for [{power_source:s}] are disabled.'
                )
            else:
                try:
                    regs[power_source]['MSR_PKG_POWER_LIMIT'] = _encode_pkg_power_limit(
                        PL1,
                        TW1,
                        PL2,
                        TW2,
                        base_value=cur_pkg_power_limits.get('RAW', 0),
                        pl1_enabled=config.getboolean(
                            power_source, 'PL1_Enable', fallback=boolean_defaults['PL1_Enable']
                        ),
                        pl1_clamp=config.getboolean(
                            power_source, 'PL1_Clamp', fallback=boolean_defaults['PL1_Clamp']
                        ),
                        pl2_enabled=config.getboolean(
                            power_source, 'PL2_Enable', fallback=boolean_defaults['PL2_Enable']
                        ),
                        pl2_clamp=config.getboolean(
                            power_source, 'PL2_Clamp', fallback=boolean_defaults['PL2_Clamp']
                        ),
                    )
                except ValueError as e:
                    fatal(f'Invalid package power limits in [{power_source:s}]: {e}')
        else:
            log(f'[I] {power_source:s} package power limits are disabled in config.')

        # cTDP
        c_tdp_target_value = config.getint(power_source, 'cTDP', fallback=None)
        if c_tdp_target_value is not None:
            if platform_info['feature_programmable_tdp_limit'] != 1:
                log("[W] cTDP setting not supported by this CPU")
            elif platform_info['number_of_additional_tdp_profiles'] < c_tdp_target_value:
                log("[W] the configured cTDP profile is not supported by this CPU")
            else:
                valid_c_tdp_target_value = max(0, c_tdp_target_value)
                regs[power_source]['MSR_CONFIG_TDP_CONTROL'] = valid_c_tdp_target_value
    return regs


def write_msr_field_verified(msr_name, cpu, mask, desired_bits, label):
    """RMW one managed field, verify it, and restore that field on mismatch."""
    original = readmsr(msr_name, cpu=cpu)
    target = (original & ~mask) | (desired_bits & mask)
    if target == original:
        return original

    writemsr(msr_name, target, cpu=cpu)
    readback = readmsr(msr_name, cpu=cpu)
    if (readback & mask) == (target & mask):
        return readback

    rollback_error = None
    try:
        rollback_target = (readback & ~mask) | (original & mask)
        writemsr(msr_name, rollback_target, cpu=cpu)
        rollback_readback = readmsr(msr_name, cpu=cpu)
        if (rollback_readback & mask) != (original & mask):
            rollback_error = 'rollback verify mismatch'
    except (OSError, IOError) as exc:
        rollback_error = str(exc)

    suffix = f'; rollback issue: {rollback_error}' if rollback_error else '; managed field rollback verified'
    raise OSError(
        f'{label} verify failed on CPU {cpu:d}: expected 0x{target & mask:x}, '
        f'read 0x{readback & mask:x}{suffix}'
    )


def write_msr_fields_transaction(fields):
    """Apply multiple managed MSR fields atomically enough for daemon profile updates.

    Each item is (msr_name, cpu, mask, desired_bits, label). On any write or
    verify failure, every field whose write was attempted is restored to its
    pre-transaction managed bits and verified.
    """
    prepared = []
    for msr_name, cpu, mask, desired_bits, label in fields:
        original = readmsr(msr_name, cpu=cpu)
        target = (original & ~mask) | (desired_bits & mask)
        prepared.append((msr_name, cpu, mask, original, target, label))

    attempted = []
    results = {}
    try:
        for msr_name, cpu, mask, original, target, label in prepared:
            if target == original:
                results[(msr_name, cpu)] = original
                continue
            attempted.append((msr_name, cpu, mask, original, label))
            writemsr(msr_name, target, cpu=cpu)
            readback = readmsr(msr_name, cpu=cpu)
            if (readback & mask) != (target & mask):
                raise OSError(
                    f'{label} verify failed on CPU {cpu:d}: '
                    f'expected 0x{target & mask:x}, read 0x{readback & mask:x}'
                )
            results[(msr_name, cpu)] = readback
    except (OSError, IOError) as exc:
        rollback_errors = []
        for msr_name, cpu, mask, original, label in reversed(attempted):
            try:
                current = readmsr(msr_name, cpu=cpu)
                rollback_target = (current & ~mask) | (original & mask)
                if rollback_target != current:
                    writemsr(msr_name, rollback_target, cpu=cpu)
                verify = readmsr(msr_name, cpu=cpu)
                if (verify & mask) != (original & mask):
                    rollback_errors.append(f'{label} CPU {cpu:d}: verify mismatch')
            except (OSError, IOError) as rollback_exc:
                rollback_errors.append(f'{label} CPU {cpu:d}: {rollback_exc}')
        suffix = (
            f'; rollback issues: {"; ".join(rollback_errors)}'
            if rollback_errors
            else '; transaction rollback verified'
        )
        raise OSError(f'MSR field transaction failed: {exc}{suffix}') from exc

    return results


def set_hwp_epp(epp):
    """Set HWP EPP as one package/per-CPU managed-field transaction."""
    if epp is None or 'HWP' in UNSUPPORTED_FEATURES:
        return
    if not isinstance(epp, int) or not 0 <= epp <= 255:
        raise ValueError(f'HWP EPP must be an integer from 0 to 255, got {epp!r}.')
    if not (readmsr('IA32_PM_ENABLE', 0, 0, cpu=0) & 1):
        warning('HWP/Speed Shift is disabled; skipping EPP write.')
        return

    requests = []
    package_control = False
    for addr in _ensure_msr_module():
        cpu = int(os.path.basename(os.path.dirname(addr)))
        cur_val = readmsr('IA32_HWP_REQUEST', cpu=cpu)
        requests.append((cpu, cur_val))
        package_control = package_control or bool((cur_val >> 42) & 1)

    epp_mask = 0xFF << 24
    fields = []
    if package_control:
        fields.append((
            'IA32_HWP_REQUEST_PKG',
            0,
            epp_mask,
            epp << 24,
            'HWP package EPP',
        ))
    for cpu, _ in requests:
        fields.append((
            'IA32_HWP_REQUEST',
            cpu,
            epp_mask,
            epp << 24,
            f'HWP CPU {cpu:d} EPP',
        ))

    results = write_msr_fields_transaction(fields)
    if args.debug:
        if package_control:
            readback = results[('IA32_HWP_REQUEST_PKG', 0)]
            log(f'[D] HWP package - EPP "{(readback >> 24) & 0xFF:#02x}" - match {OK}')
        for cpu, _ in requests:
            readback = results[('IA32_HWP_REQUEST', cpu)]
            log(f'[D] HWP CPU {cpu:d} - EPP "{(readback >> 24) & 0xFF:#02x}" - match {OK}')


def set_hwp(performance_mode):
    """Backward-compatible HWP_Mode wrapper."""
    if performance_mode not in (True, False):
        return
    set_hwp_epp(HWP_PERFORMANCE_VALUE if performance_mode else HWP_DEFAULT_VALUE)


def _hwp_epp(config, source=None):
    source = source or power.get('source')
    if source not in POWER_PROFILES:
        return None

    epp = config.getint(source, 'HWP_EPP', fallback=None) if source in config else None
    if epp is not None:
        return epp

    legacy = config.getboolean('AC', 'HWP_Mode', fallback=None) if 'AC' in config else None
    if legacy is None:
        return None
    return HWP_PERFORMANCE_VALUE if legacy and source == 'AC' else HWP_DEFAULT_VALUE


def _turbo_setting(config, source=None):
    source = source or power.get('source')
    if source not in POWER_PROFILES or source not in config:
        return None
    disable = config.getboolean(source, 'Disable_Turbo', fallback=None)
    return None if disable is None else not disable


def _hwp_mode(config):
    """Legacy helper retained for callers/tests that still use HWP_Mode."""
    value = _hwp_epp(config)
    if value is None:
        return None
    return value == HWP_PERFORMANCE_VALUE


def _hwp_has_explicit_epp(config, source=None):
    source = source or power.get('source')
    return source in POWER_PROFILES and source in config and config.has_option(source, 'HWP_EPP')


def _legacy_hwp_enabled(config):
    return 'AC' in config and config.getboolean('AC', 'HWP_Mode', fallback=False)


def _hwp_should_reassert(config, source=None):
    """Return whether EPP should be periodically managed for this profile."""
    source = source or power.get('source')
    if _hwp_has_explicit_epp(config, source):
        return True
    return bool(_legacy_hwp_enabled(config) and source == 'AC')


def _hwp_should_apply_event(config, source=None):
    """Return whether source/resume events should actively reapply EPP."""
    source = source or power.get('source')
    return _hwp_has_explicit_epp(config, source) or _legacy_hwp_enabled(config)


def _bdprochot_setting(config, source=None):
    """Return explicit desired BD PROCHOT state while preserving legacy semantics.

    BDPROCHOT_Enabled is the reversible profile control. The historical
    Disable_BDPROCHOT option remains one-way: True disables; False/absent means
    leave the current hardware state untouched.
    """
    source = source or power.get('source')
    if source not in POWER_PROFILES or source not in config:
        return None

    explicit = config.getboolean(source, 'BDPROCHOT_Enabled', fallback=None)
    if explicit is not None:
        return explicit

    legacy_disable = config.getboolean(source, 'Disable_BDPROCHOT', fallback=None)
    if legacy_disable:
        return False
    return None


def set_turbo(enabled):
    """Set IA32_MISC_ENABLE[38] transactionally across online CPUs."""
    if enabled is None:
        return
    mask = 1 << TURBO_DISABLE_BIT
    desired = 0 if enabled else mask
    cpus = [int(os.path.basename(os.path.dirname(addr))) for addr in _ensure_msr_module()]
    fields = [
        ('IA32_MISC_ENABLE', cpu, mask, desired, 'Turbo Disable')
        for cpu in cpus
    ]
    results = write_msr_fields_transaction(fields)
    if args.debug:
        expected = 0 if enabled else 1
        for cpu in cpus:
            readback = results[('IA32_MISC_ENABLE', cpu)]
            read_value = (readback >> TURBO_DISABLE_BIT) & 1
            log(
                f'[D] Turbo CPU {cpu:d} - disable={read_value:d} '
                f'- match {OK if read_value == expected else ERR}'
            )



def set_bdprochot(enabled):
    """Set MSR_POWER_CTL[0] transactionally across online CPUs."""
    if enabled is None:
        return
    expected = 1 if enabled else 0
    mask = 1 << BD_PROCHOT_ENABLE_BIT
    desired = mask if enabled else 0
    cpus = [int(os.path.basename(os.path.dirname(addr))) for addr in _ensure_msr_module()]
    fields = [
        ('MSR_POWER_CTL', cpu, mask, desired, 'BD PROCHOT')
        for cpu in cpus
    ]
    results = write_msr_fields_transaction(fields)
    if args.debug:
        for cpu in cpus:
            readback = results[('MSR_POWER_CTL', cpu)]
            read_value = 1 if readback & mask else 0
            log(
                f'[D] BDPROCHOT CPU {cpu:d} - state "{read_value:#02x}" '
                f'- match {OK if read_value == expected else ERR}'
            )



def set_disable_bdprochot():
    """Backward-compatible helper for the historical one-way control."""
    set_bdprochot(False)


def get_config_write_time():
    """Return the config file's mtime, or None if it doesn't exist."""
    try:
        return os.stat(args.config).st_mtime
    except FileNotFoundError:
        return None


def reload_config():
    """Re-read config and compute a new register plan without touching hardware.

    The power thread publishes the new config/plan under config_lock and then
    applies the whole profile under hardware_lock, avoiding resume/reload races.
    """
    config = load_config()
    if not config_is_enabled(config):
        log('[I] Reloading changes.')
        return config, defaultdict(dict)
    regs = calc_reg_values(get_cpu_platform_info(), config)
    log('[I] Reloading changes.')
    return config, regs


def _read_mchbar_dword(method=None, register='48.l'):
    """Read one MCHBAR PCI config DWORD for device 0:0.0."""
    cmd = ['setpci', '-s', '0000:00:00.0', register]
    if method:
        cmd[1:1] = ['-A', method]
    try:
        # capture stderr: a probe method is allowed to fail (e.g. the ECAM
        # region is not mappable under STRICT_DEVMEM) and its complaint must
        # not leak to the journal; read_mchbar_base() warns if all methods fail
        raw_value = check_output(cmd, stderr=PIPE).strip()
        if re.fullmatch(rb'[0-9a-fA-F]{8}', raw_value) is None:
            return None
        return int(raw_value, 16)
    except CalledProcessError as e:
        if args.debug:
            err = (e.stderr or b'').decode(errors='replace').strip()
            log(f'[D] MCHBAR - setpci {method or "default"} probe failed: {err}')
        return None
    except OSError:
        return None


def _read_host_bridge_identity():
    """Return the D0:F0 PCI vendor/device IDs from sysfs, or None."""
    values = []
    for attribute in ('vendor', 'device'):
        try:
            with open(os.path.join(PCI_HOST_BRIDGE_SYSFS_PATH, attribute), encoding='ascii') as attribute_file:
                raw_value = attribute_file.read().strip()
            if re.fullmatch(r'0x[0-9a-fA-F]{4}', raw_value) is None:
                return None
            values.append(int(raw_value[2:], 16))
        except (OSError, UnicodeDecodeError):
            return None
    return tuple(values)


def read_mchbar_base():
    """Return a validated enabled 64-bit MCHBAR base, or None."""
    identity = _read_host_bridge_identity()
    if identity is None:
        warning('Could not identify the PCI host bridge; disabling only MMIO package power-limit writes.')
        return None

    vendor_id, device_id = identity
    if vendor_id != PCI_VENDOR_ID_INTEL:
        warning(
            f'PCI host bridge vendor {vendor_id:#06x} is not Intel; disabling only MMIO package power-limit writes.'
        )
        return None

    address_mask = MCHBAR_ADDRESS_MASKS_BY_PCI_DEVICE.get(device_id)
    if address_mask is None:
        warning(
            f'Unknown Intel PCI host bridge device {device_id:#06x}; '
            f'disabling only MMIO package power-limit writes.'
        )
        return None

    allowed_mask = address_mask | MCHBAR_ENABLE_BIT
    for method in ('ecam', None):
        low = _read_mchbar_dword(method, '48.l')
        high = _read_mchbar_dword(method, '4c.l')
        if low is None or high is None:
            continue
        mchbar = low | (high << 32)
        if not mchbar & MCHBAR_ENABLE_BIT or mchbar & ~allowed_mask:
            continue
        base = mchbar & address_mask
        if base != 0:
            return base

    warning('Could not read a valid enabled MCHBAR base via setpci; disabling only MMIO package power-limit writes.')
    return None


def open_mchbar_power_limit_mmio(expected_msr=None):
    """Open direct MCHBAR for writes only after a true read-only MMIO==MSR preflight."""
    if expected_msr is None:
        warning('Direct MCHBAR requires an MSR equality preflight; write mapping not opened.')
        return None
    mchbar_base = read_mchbar_base()
    if mchbar_base is None:
        return None
    address = mchbar_base + MCHBAR_PACKAGE_POWER_LIMIT_OFFSET
    try:
        with MMIO(address, MCHBAR_PACKAGE_POWER_LIMIT_SIZE, readonly=True) as probe_mapping:
            mmio_raw = probe_mapping.read64(0)
        if mmio_raw != expected_msr:
            warning(
                f'Direct MCHBAR preflight mismatch: MMIO={mmio_raw:#x}, MSR={expected_msr:#x}; '
                'direct MCHBAR writes are disabled for this run.'
            )
            return None

        mapping = MMIO(address, MCHBAR_PACKAGE_POWER_LIMIT_SIZE)
        # Re-check after reopening RW to close the TOCTOU window before callers write.
        rw_raw = mapping.read64(0)
        if rw_raw != expected_msr:
            warning(
                f'Direct MCHBAR changed after read-only preflight: MMIO={rw_raw:#x}, '
                f'MSR={expected_msr:#x}; direct writes are disabled.'
            )
            mapping.close()
            return None
        return mapping
    except (MMIOError, OSError) as e:
        warning(f'Unable to open/read /dev/mem MCHBAR ({e}); direct MMIO writes are disabled.')
        warning(
            'Check CONFIG_DEVMEM=y and that kernel lockdown is disabled '
            '(CONFIG_IO_STRICT_DEVMEM can also block this region).'
        )
        return None


def apply_package_power_limit(write_value, mmio_powercap=None, mchbar_mmio=None):
    """Apply one package-power value without allowing MSR/MMIO control-plane drift.

    The kernel powercap interface is used for watts/Tau because it enforces
    platform bounds. Enable/Clamp bits require the full MCHBAR mirror, which is
    used only after a same-run MMIO==MSR preflight.
    """
    msr_before = readmsr('MSR_PKG_POWER_LIMIT', cpu=0)
    if (msr_before >> PKG_POWER_LIMIT_LOCK_BIT) & 1:
        warning('MSR_PKG_POWER_LIMIT became locked; package power-limit write skipped.')
        return mmio_powercap, mchbar_mmio

    direct_before = None
    if mchbar_mmio is not None:
        try:
            direct_before = mchbar_mmio.read64(0)
        except OSError as e:
            warning(f'Unable to read validated MCHBAR mirror ({e}); disabling direct MCHBAR writes.')
            mchbar_mmio = None
        else:
            if direct_before != msr_before:
                warning(
                    f'MCHBAR/MSR diverged before write (MMIO={direct_before:#x}, MSR={msr_before:#x}); '
                    'disabling direct MCHBAR writes.'
                )
                mchbar_mmio = None
                direct_before = None

    if mchbar_mmio is None:
        warning(
            'No MMIO==MSR-validated direct MCHBAR mirror is available; skipping the entire '
            'TPL update to avoid preserving or deepening an MSR/MMIO split-brain state.'
        )
        return mmio_powercap, mchbar_mmio

    powercap_before = mmio_powercap.snapshot() if mmio_powercap is not None else None
    power_unit = None
    time_unit = None
    if mmio_powercap is not None:
        # Read immutable RAPL units before any state-changing write. If this
        # fails, fatal/readmsr exits before MSR/MMIO state can become partial.
        power_unit = get_power_unit()
        time_unit = get_time_unit()

    msr_changed = False
    try:
        if msr_before != write_value:
            writemsr('MSR_PKG_POWER_LIMIT', write_value, cpu=0)
            msr_changed = True
            readback = readmsr('MSR_PKG_POWER_LIMIT', cpu=0)
            if readback != write_value:
                raise OSError(
                    f'MSR_PKG_POWER_LIMIT verify failed: wrote {write_value:#x}, read {readback:#x}'
                )

        if mmio_powercap is not None:
            mmio_powercap.write(
                pl1_w=get_value_for_bits(write_value, 0, 14) * power_unit,
                pl2_w=get_value_for_bits(write_value, 32, 46) * power_unit,
                tau1_s=decode_time_window_field(get_value_for_bits(write_value, 17, 23), time_unit),
                tau2_s=decode_time_window_field(get_value_for_bits(write_value, 49, 55), time_unit),
            )

        if mchbar_mmio is not None:
            mmio_current = mchbar_mmio.read64(0)
            if (mmio_current >> PKG_POWER_LIMIT_LOCK_BIT) & 1:
                raise OSError('MCHBAR package power limit is locked')
            if mmio_current != write_value:
                mchbar_mmio.write64(0, write_value)
            mmio_readback = mchbar_mmio.read64(0)
            if mmio_readback != write_value:
                raise OSError(
                    f'MCHBAR package-power verify failed: wrote {write_value:#x}, read {mmio_readback:#x}'
                )
    except (OSError, PowercapError) as e:
        rollback_errors = []
        # Restore the narrower kernel powercap view first, then the full direct
        # mirror so powercap cannot overwrite a just-restored MCHBAR value.
        if powercap_before is not None and mmio_powercap is not None:
            try:
                mmio_powercap.restore(powercap_before)
            except PowercapError as rollback_error:
                rollback_errors.append(f'powercap: {rollback_error}')
        if mchbar_mmio is not None and direct_before is not None:
            try:
                if mchbar_mmio.read64(0) != direct_before:
                    mchbar_mmio.write64(0, direct_before)
                if mchbar_mmio.read64(0) != direct_before:
                    rollback_errors.append('MCHBAR verify mismatch')
            except (OSError, MMIOError) as rollback_error:
                rollback_errors.append(f'MCHBAR: {rollback_error}')
        if msr_changed:
            try:
                writemsr('MSR_PKG_POWER_LIMIT', msr_before, cpu=0)
                if readmsr('MSR_PKG_POWER_LIMIT', cpu=0) != msr_before:
                    rollback_errors.append('MSR verify mismatch')
            except (OSError, IOError) as rollback_error:
                rollback_errors.append(f'MSR: {rollback_error}')
        suffix = f'; rollback issues: {"; ".join(rollback_errors)}' if rollback_errors else '; rollback attempted'
        warning(f'Package power update failed: {e}{suffix}', oneshot=False)

    return mmio_powercap, mchbar_mmio


def apply_profile_registers(regs, power_source, mmio_powercap=None, mchbar_mmio=None):
    """Apply directly readable profile MSRs for one source and return MMIO backends."""
    if power_source not in regs:
        return mmio_powercap, mchbar_mmio

    profile_regs = regs[power_source]

    if 'MSR_TEMPERATURE_TARGET' in profile_regs:
        write_value = profile_regs['MSR_TEMPERATURE_TARGET']
        current = readmsr('MSR_TEMPERATURE_TARGET', cpu=0)
        if (current >> 31) & 1:
            warning('MSR_TEMPERATURE_TARGET TCC offset is locked; temperature-target write skipped.')
        else:
            offset_mask = 0x3F << 24
            write_msr_field_verified(
                'MSR_TEMPERATURE_TARGET',
                0,
                offset_mask,
                write_value,
                'Temperature target offset',
            )

    if 'MSR_CONFIG_TDP_CONTROL' in profile_regs:
        write_value = profile_regs['MSR_CONFIG_TDP_CONTROL']
        current = readmsr('MSR_CONFIG_TDP_CONTROL', cpu=0)
        if (current >> 31) & 1:
            warning('MSR_CONFIG_TDP_CONTROL is locked; cTDP write skipped.')
        else:
            write_msr_field_verified(
                'MSR_CONFIG_TDP_CONTROL',
                0,
                0x3,
                write_value,
                'cTDP target',
            )

    if 'MSR_PKG_POWER_LIMIT' in profile_regs:
        mmio_powercap, mchbar_mmio = apply_package_power_limit(
            profile_regs['MSR_PKG_POWER_LIMIT'],
            mmio_powercap=mmio_powercap,
            mchbar_mmio=mchbar_mmio,
        )

    return mmio_powercap, mchbar_mmio


def apply_runtime_profile(
    config,
    regs,
    power_source,
    *,
    mmio_powercap=None,
    mchbar_mmio=None,
    apply_mailbox=False,
    apply_hwp=False,
):
    """Apply one internally consistent power profile while hardware_lock is held."""
    if apply_mailbox:
        undervolt(config, source=power_source)
        set_icc_max(config, source=power_source)

    if regs is not None:
        mmio_powercap, mchbar_mmio = apply_profile_registers(
            regs,
            power_source,
            mmio_powercap=mmio_powercap,
            mchbar_mmio=mchbar_mmio,
        )

    set_bdprochot(_bdprochot_setting(config, power_source))
    set_turbo(_turbo_setting(config, power_source))
    if apply_hwp:
        set_hwp_epp(_hwp_epp(config, power_source))
    return mmio_powercap, mchbar_mmio


def power_thread(state, exit_event):
    """Crash-loud wrapper for _power_thread: an uncaught exception (or a
    sys.exit() from a helper) would only kill this thread, leaving a zombie
    daemon that looks healthy to systemd while no longer touching the hardware.
    """
    try:
        _power_thread(state, exit_event)
    except Exception:
        warning(f'power thread crashed:\n{traceback.format_exc()}', oneshot=False)
        if args.log:
            args.log.flush()
        os._exit(1)


def _power_thread(state, exit_event):
    """Daemon main loop: periodically (re-)apply throttling MSRs."""
    config, regs = state['config'], state['regs']
    mchbar_mmio = None
    has_tpl = any('MSR_PKG_POWER_LIMIT' in regs.get(profile, {}) for profile in POWER_PROFILES)
    mmio_powercap = find_mmio_package_zone() if has_tpl else None
    if has_tpl:
        pkg_baseline = readmsr('MSR_PKG_POWER_LIMIT', cpu=0)
        mchbar_mmio = open_mchbar_power_limit_mmio(expected_msr=pkg_baseline)
    if mmio_powercap is not None:
        log(f'[I] Using kernel intel-rapl-mmio zone: {mmio_powercap.path}')
    if mchbar_mmio is not None:
        log('[I] Direct MCHBAR package-power mirror passed MMIO==MSR preflight.')

    next_hwp_write = 0
    applied_source = power['source']
    last_config_write_time = (
        get_config_write_time() if config.getboolean('GENERAL', 'Autoreload', fallback=False) else None
    )
    while not exit_event.is_set():
        config_reloaded = False
        # Reload config on changes (unless it's deleted)
        if config.getboolean('GENERAL', 'Autoreload', fallback=False):
            config_write_time = get_config_write_time()
            if config_write_time and last_config_write_time != config_write_time:
                last_config_write_time = config_write_time
                new_config, new_regs = reload_config()
                with config_lock:
                    state['config'], state['regs'] = new_config, new_regs
                    config, regs = new_config, new_regs
                config_reloaded = True

        # Switch back to sysfs polling and publish the source atomically.
        if power['method'] == 'polling':
            polled_source = 'BATTERY' if is_on_battery(config) else 'AC'
            with config_lock:
                power['source'] = polled_source

        # Snapshot the source under the same publication lock used by D-Bus.
        with config_lock:
            power_source = power['source']

        # Enabled=False is a hard write barrier, including the iteration that
        # observes an enabled -> disabled autoreload transition.
        if not config_is_enabled(config):
            applied_source = power_source
            exit_event.wait(get_update_rate(config, power_source))
            continue

        # log thermal status
        if args.debug:
            thermal_status = get_reset_thermal_status()
            for index, core_thermal_status in enumerate(thermal_status):
                for key, value in core_thermal_status.items():
                    log(f'[D] core {index} thermal status: {key.replace("_", " ")} = {value}')

        source_changed = power_source != applied_source
        if source_changed:
            log(f'[I] Power source changed: {applied_source:s} -> {power_source:s}')

        if config_reloaded:
            has_tpl = any('MSR_PKG_POWER_LIMIT' in regs.get(profile, {}) for profile in POWER_PROFILES)
            if not has_tpl:
                mmio_powercap = None
                if mchbar_mmio is not None:
                    mchbar_mmio.close()
                    mchbar_mmio = None
            else:
                if mmio_powercap is None:
                    mmio_powercap = find_mmio_package_zone()
                if mchbar_mmio is None:
                    pkg_baseline = readmsr('MSR_PKG_POWER_LIMIT', cpu=0)
                    mchbar_mmio = open_mchbar_power_limit_mmio(expected_msr=pkg_baseline)

        hwp_epp = _hwp_epp(config, power_source)
        event_hwp_due = hwp_epp is not None and (
            config_reloaded or (source_changed and _hwp_should_apply_event(config, power_source))
        )
        periodic_hwp_due = (
            hwp_epp is not None
            and _hwp_should_reassert(config, power_source)
            and next_hwp_write <= time()
        )
        hwp_due = event_hwp_due or periodic_hwp_due
        with hardware_lock:
            mmio_powercap, mchbar_mmio = apply_runtime_profile(
                config,
                regs,
                power_source,
                mmio_powercap=mmio_powercap,
                mchbar_mmio=mchbar_mmio,
                apply_mailbox=source_changed or config_reloaded,
                apply_hwp=hwp_due,
            )
        applied_source = power_source
        if hwp_due:
            next_hwp_write = time() + HWP_INTERVAL

        wait_t = get_update_rate(config, power_source)
        exit_event.wait(wait_t)


def check_kernel():
    """Verify we run as root and that the kernel exposes MSR/devmem."""
    if os.geteuid() != 0:
        fatal('No root no party. Try again with sudo.')

    try:
        with open('/sys/kernel/security/lockdown') as f:
            if '[none]' not in f.read():
                warning('Kernel lockdown is active: MSR and /dev/mem writes will be blocked.')
    except OSError:
        pass

    kernel_config = None
    try:
        with open(os.path.join('/boot', f'config-{uname()[2]:s}')) as f:
            kernel_config = f.read()
    except IOError:
        config_gz_path = os.path.join('/proc', 'config.gz')
        try:
            if not os.path.isfile(config_gz_path):
                subprocess.check_call(('modprobe', 'configs'))
            with gzip.open(config_gz_path) as f:
                kernel_config = f.read().decode()
        except (subprocess.CalledProcessError, IOError):
            pass
    if kernel_config is None:
        log('[W] Unable to obtain and validate kernel config.')
        return
    elif not re.search('CONFIG_DEVMEM=y', kernel_config):
        warning('CONFIG_DEVMEM is unavailable; direct /dev/mem MCHBAR fallback will be disabled.')
    if not re.search('CONFIG_X86_MSR=(y|m)', kernel_config):
        fatal('Bad kernel config: you need CONFIG_X86_MSR builtin or as module.')


def check_cpu():
    """Identify the CPU from /proc/cpuinfo and refuse to run on unsupported models."""
    try:
        with open('/proc/cpuinfo') as f:
            cpuinfo = {}
            for row in f.readlines():
                try:
                    key, value = map(lambda x: x.strip(), row.split(':'))
                    if key == 'processor' and value == '1':
                        break
                    try:
                        cpuinfo[key] = int(value, 0)
                    except ValueError:
                        cpuinfo[key] = value
                except ValueError:
                    pass
        if cpuinfo['vendor_id'] != 'GenuineIntel':
            fatal('This tool is designed for Intel CPUs only.')

        cpuid = (cpuinfo['cpu family'], cpuinfo['model'], cpuinfo['stepping'])
        if cpuid not in supported_cpus:
            fatal(
                'Your CPU model is not supported.\n\n'
                'Please open a new issue (https://github.com/erpalma/throttled/issues) specifying:\n'
                ' - model name\n'
                ' - cpu family\n'
                ' - model\n'
                ' - stepping\n'
                'from /proc/cpuinfo.'
            )

        log(f'[I] Detected CPU architecture: Intel {supported_cpus[cpuid]:s}')
        return cpuid
    except SystemExit:
        raise
    except (OSError, KeyError, ValueError) as e:
        fatal(f'Unable to identify CPU model: {e}')


def _has_configured_section(config, sections):
    return config is not None and any(section in config for section in sections)


def _hwp_is_configured(config):
    if config is None:
        return True
    return (
        ('AC' in config and (
            config.has_option('AC', 'HWP_Mode')
            or config.has_option('AC', 'HWP_EPP')
        ))
        or ('BATTERY' in config and config.has_option('BATTERY', 'HWP_EPP'))
    )


def test_msr_rw_capabilities(config=None):
    """Probe only optional features that the active configuration will use.

    Passing None preserves the historical explicit self-test behavior for
    callers/tests. The daemon passes its config so unused OC-mailbox features
    never receive hidden WRMSR selector transactions.
    """
    global TESTMSR
    TESTMSR = True
    try:
        if config is None or _has_configured_section(config, UNDERVOLT_KEYS):
            try:
                log('[I] Testing if undervolt is supported...')
                get_undervolt()
            except (IOError, OSError):
                warning('Undervolt seems not to be supported by your system, disabling.')
                UNSUPPORTED_FEATURES.append('UNDERVOLT')
        else:
            log('[I] Undervolt is not configured; OC mailbox capability probe skipped.')

        if config is None or _has_configured_section(config, ICCMAX_KEYS):
            try:
                log('[I] Testing if IccMax is supported...')
                get_icc_max()
            except (IOError, OSError):
                warning('IccMax seems not to be supported by your system, disabling.')
                UNSUPPORTED_FEATURES.append('ICCMAX')
        else:
            log('[I] IccMax is not configured; OC mailbox capability probe skipped.')

        if _hwp_is_configured(config):
            try:
                log('[I] Testing if HWP is supported...')
                if not (readmsr('IA32_PM_ENABLE', 0, 0, cpu=0) & 1):
                    raise OSError('HWP/Speed Shift is disabled')
                readmsr('IA32_HWP_REQUEST', cpu=0)
            except (IOError, OSError):
                warning('HWP seems not to be supported/enabled by your system, disabling.')
                UNSUPPORTED_FEATURES.append('HWP')

        if args.monitor is not None:
            for domain, msr in tuple(MONITOR_POWER_DOMAINS.items()):
                try:
                    readmsr(msr, cpu=0)
                except (IOError, OSError):
                    warning(f'{domain:s} power monitoring is not supported by your system, disabling.')
                    del MONITOR_POWER_DOMAINS[domain]
    finally:
        TESTMSR = False


def monitor(exit_event, wait, cpuid=None, config=None):
    """Live-display throttling causes and power without implicit OC-mailbox queries."""
    wait = max(0.1, wait)
    prev_energy = {}
    if MONITOR_POWER_DOMAINS:
        rapl_power_unit = 0.5 ** readmsr('MSR_RAPL_POWER_UNIT', from_bit=8, to_bit=12, cpu=0)
        # the RAPL energy counters are 32 bits wide and wrap every few minutes
        energy_units = dict.fromkeys(MONITOR_POWER_DOMAINS, rapl_power_unit)
        if 'DRAM' in energy_units and cpuid is not None and cpuid[0] == 6 and cpuid[1] in FIXED_DRAM_ENERGY_UNIT_MODELS:
            energy_units['DRAM'] = 15.3e-6
        prev_energy = {
            domain: (readmsr(msr, cpu=0) * energy_units[domain], time())
            for domain, msr in MONITOR_POWER_DOMAINS.items()
        }

    if config is not None and not _has_configured_section(config, UNDERVOLT_KEYS):
        log('[D] Undervolt offsets: not configured (OC mailbox not queried)')
    elif 'UNDERVOLT' in UNSUPPORTED_FEATURES:
        log('[D] Undervolt offsets: unsupported')
    else:
        undervolt_values = get_undervolt(convert=True)
        undervolt_output = ' | '.join(f'{plane:s}: {undervolt_values[plane]:.2f} mV' for plane in VOLTAGE_PLANES)
        log(f'[D] Undervolt offsets: {undervolt_output:s}')

    if config is not None and not _has_configured_section(config, ICCMAX_KEYS):
        log('[D] IccMax: not configured (OC mailbox not queried)')
    elif 'ICCMAX' in UNSUPPORTED_FEATURES:
        log('[D] IccMax: unsupported')
    else:
        iccmax_values = get_icc_max(convert=True)
        iccmax_output = ' | '.join(f'{plane:s}: {iccmax_values[plane]:.2f} A' for plane in CURRENT_PLANES)
        log(f'[D] IccMax: {iccmax_output:s}')

    log('[D] Realtime monitoring of throttling causes:\n')
    while not exit_event.is_set():
        value = readmsr('IA32_THERM_STATUS', from_bit=0, to_bit=15, cpu=0)
        offsets = {'Thermal': 0, 'Power': 10, 'Current': 12, 'Cross-domain (e.g. GPU)': 14}
        output = (f'{cause:s}: {LIM if bool((value >> offsets[cause]) & 1) else OK:s}' for cause in offsets)

        vcore = readmsr('IA32_PERF_STATUS', from_bit=32, to_bit=47, cpu=0) / (2.0**13) * 1000
        stats2 = {'VCore': f'{vcore:.0f} mV'}
        total = 0.0
        for power_plane, msr in MONITOR_POWER_DOMAINS.items():
            energy_unit = energy_units[power_plane]
            energy_j = readmsr(msr, cpu=0) * energy_unit
            now = time()
            prev_j, prev_t = prev_energy[power_plane]
            energy_w = ((energy_j - prev_j) % (2**32 * energy_unit)) / (now - prev_t)
            prev_energy[power_plane] = (energy_j, now)
            stats2[power_plane] = f'{energy_w:.1f} W'
            # Package already includes Graphics; DRAM is a separate domain.
            if power_plane != 'Graphics':
                total += energy_w

        stats2['Total'] = f'{total:.1f} W'

        output2 = (f'{label}: {stats2[label]}' for label in stats2)
        terminator = '\n' if args.log else '\r'
        log(
            f"[{power['source']}] {' - '.join(output)}  ||  {' - '.join(output2)}{' ' * 10}",
            end=terminator,
        )
        exit_event.wait(wait)


def acquire_daemon_lock():
    """Hold a process-wide lock so one-shot controls can detect manual daemons too."""
    global _daemon_lock_file
    directory = os.path.dirname(DAEMON_LOCK_PATH)
    try:
        os.makedirs(directory, mode=0o755, exist_ok=True)
        handle = open(DAEMON_LOCK_PATH, 'a+', encoding='ascii')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(f'{os.getpid()}\n')
        handle.flush()
    except (OSError, BlockingIOError) as exc:
        try:
            handle.close()
        except (NameError, OSError):
            pass
        fatal(f'Another throttled daemon instance is already active or the daemon lock cannot be acquired: {exc}')
    _daemon_lock_file = handle


def positive_finite_float(value):
    """Parse a finite, positive command-line float."""
    try:
        number = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f'{value!r} is not a number') from e
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f'{value!r} must be finite and positive')
    return number


def build_arg_parser():
    """Create the command-line parser, disabling argparse's own color output when available."""
    try:
        parser = argparse.ArgumentParser(color=False)
    except TypeError:
        parser = argparse.ArgumentParser()
    exclusive_group = parser.add_mutually_exclusive_group()
    exclusive_group.add_argument('--debug', action='store_true', help='add some debug info and additional checks')
    exclusive_group.add_argument('--probe', action='store_true', help='read-only HP EliteBook 830 G6 bring-up probe')
    exclusive_group.add_argument(
        '--monitor',
        metavar='update_rate',
        const=1.0,
        type=positive_finite_float,
        nargs='?',
        help='realtime monitoring of throttling causes (default 1s)',
    )
    parser.add_argument('--json', action='store_true', help='with --probe: print JSON output')
    parser.add_argument('--snapshot', metavar='PATH', help='with --probe: save a read-only stock snapshot')
    parser.add_argument('--config', default='/etc/throttled.conf', help='override default config file path')
    parser.add_argument('--force', action='store_true', help='bypass compatibility checks (EXPERTS only)')
    parser.add_argument('--log', metavar='/path/to/file', help='log to file instead of stdout')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return parser


def main():
    """Daemon entrypoint: parse args, validate platform, start power thread."""
    global args

    parser = build_arg_parser()
    args = parser.parse_args()

    if getattr(args, 'probe', False):
        from throttled_probe import main as probe_main

        probe_args = []
        if getattr(args, 'json', False):
            probe_args.append('--json')
        snapshot_path = getattr(args, 'snapshot', None)
        if snapshot_path:
            probe_args.extend(('--snapshot', snapshot_path))
        return probe_main(probe_args)

    if getattr(args, 'json', False) or getattr(args, 'snapshot', None):
        parser.error('--json/--snapshot are valid only together with --probe')

    if args.log:
        try:
            args.log = open(args.log, 'w')
        except OSError as e:
            args.log = None
            fatal(f'Unable to write to the log file: {e}')

    log('[I] Loading config file.')
    config = load_config()
    if not config_is_enabled(config):
        log('[I] Throttled is disabled in config file... Quitting. :(')
        return

    if os.geteuid() != 0:
        fatal('No root no party. Try again with sudo.')

    cpuid = None
    if not args.force:
        check_kernel()
        cpuid = check_cpu()

    acquire_daemon_lock()

    set_msr_allow_writes()

    test_msr_rw_capabilities(config)

    power['source'] = 'BATTERY' if is_on_battery(config) else 'AC'

    platform_info = get_cpu_platform_info()
    if args.debug:
        for key, value in platform_info.items():
            log(f'[D] cpu platform info: {key.replace("_", " ")} = {value}')
    regs = calc_reg_values(platform_info, config)

    undervolt(config)
    set_icc_max(config)
    set_hwp_epp(_hwp_epp(config, power['source']))
    set_turbo(_turbo_setting(config, power['source']))
    set_bdprochot(_bdprochot_setting(config, power['source']))

    state = {'config': config, 'regs': regs}

    exit_event = Event()
    thread = Thread(target=power_thread, args=(state, exit_event))
    thread.daemon = True
    thread.start()

    log('[I] Starting main loop.')

    monitor_thread = None
    if args.monitor is not None:
        monitor_thread = Thread(target=monitor, args=(exit_event, args.monitor, cpuid, config))
        monitor_thread.daemon = True
        monitor_thread.start()

    try:
        asyncio.run(run_dbus_loop(state))
    except KeyboardInterrupt:
        pass
    finally:
        exit_event.set()
        thread.join(timeout=1)
        if monitor_thread is not None:
            monitor_thread.join(timeout=0.1)


if __name__ == '__main__':
    raise SystemExit(main())
