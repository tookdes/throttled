# HP EliteBook 830 G6 ThrottleStop replacement

This document defines the narrow hardware target for this fork. It is not a
claim of generic Intel laptop support.

## Target

- HP EliteBook 830 G6
- Intel Whiskey Lake-U target CPUIDs: family 6, model 142, stepping 11 (806EB) or 12 (806EC)
- Linux with the `msr` driver
- direct register writes require root and must not be blocked by kernel lockdown

The implementation is deliberately capability-driven. Unsupported or locked
interfaces must fail closed instead of pretending a setting was accepted.

## ThrottleStop feature map

| ThrottleStop area | Linux implementation | Current status |
| --- | --- | --- |
| Speed Shift EPP | IA32_HWP_REQUEST + package request when active | implemented |
| Turbo checkbox | IA32_MISC_ENABLE bit 38 | implemented |
| SpeedStep | IA32_MISC_ENABLE bit 16 | legacy-only; CLI refuses while HWP is enabled |
| BD PROCHOT | MSR_POWER_CTL bit 0 | implemented, reversible |
| Clock Mod 100% | IA32_CLOCK_MODULATION enable bit | implemented |
| Set Multiplier | IA32_PERF_CTL target ratio | legacy-only; CLI refuses while HWP is enabled |
| Turbo Ratio Limits | MSR_TURBO_RATIO_LIMIT | implemented |
| PL1 / PL2 | MSR_PKG_POWER_LIMIT | implemented |
| PL1 / PL2 Enable | MSR_PKG_POWER_LIMIT | implemented |
| Clamp | MSR_PKG_POWER_LIMIT | implemented |
| Tau | RAPL time-window fields | implemented |
| MMIO watts/Tau | kernel intel-rapl-mmio | implemented with bounds + readback |
| full TPL MMIO | direct MCHBAR | same-run MMIO==MSR preflight required |
| FIVR voltage offsets | OC mailbox MSR 0x150 | implemented in explicit control CLI |
| FIVR IccMax | OC mailbox MSR 0x150 | implemented in explicit control CLI |
| Temperature target | MSR_TEMPERATURE_TARGET | implemented in daemon and direct control |
| Limits | package MSR 0x64F Whiskey Lake layout + thermal state | implemented |
| Ratio / MHz | IA32_PERF_STATUS | implemented |
| Effective MHz | APERF / MPERF deltas | implemented |
| C0% | MPERF vs reference-rate elapsed time | implemented |
| Package power | RAPL energy delta | implemented |
| Stock reset | schema-v2 managed-field restore + captured TPL backends | implemented for non-OC-mailbox state |
| AC / battery profiles | existing throttled profile engine | implemented |
| resume reapply | login1 resume callback | implemented |

## Safety model

### Strict read-only probe

`sudo throttled --probe` performs RDMSR/sysfs/PCI reads only. It does not
query FIVR or IccMax because Intel's OC-mailbox query protocol requires a
selector WRMSR before reading the result.

### Explicit control writes

`throttlectl` is separate from the daemon. For write operations it:

1. requires root;
2. requires the HP 830 G6 + target Whiskey Lake identity unless
   `--force-target` is supplied;
3. refuses to race an active `throttled.service` unless
   `--allow-daemon-race` is supplied;
4. performs read-modify-write so unrelated MSR bits are retained;
5. verifies writable MSR state by reading it back;
6. attempts to restore pre-write state if a multi-register transaction fails.

FIVR/IccMax use their mailbox-specific rollback path because the mailbox cannot
be treated like a normal directly readable register.

### Package power

MSR_PKG_POWER_LIMIT bit 63 is a hard lock. Neither daemon nor CLI sets it.

The package-power control plane is intentionally fail-closed:

1. `intel-rapl-mmio` is used for watts/Tau and its kernel min/max bounds are
   enforced and read back;
2. direct MCHBAR is required for Enable/Clamp changes or when no kernel MMIO
   zone exists;
3. direct MCHBAR uses the shared allow-list (including Whiskey Lake-U
   `0x3E34/0x3E35`), bits 38:15 MCHBAR decoding and `+0x59A0` package-power
   mirror;
4. before any direct write, a separate read-only mapping must equal the current
   MSR 0x610 value; the RW mapping is then reopened and equality checked again;
5. kernel powercap rejection triggers rollback; it is never bypassed by falling
   through to raw `/dev/mem`;
6. if neither safe backend is available, persistent TPL updates are skipped.

## Persistent profiles

The existing `[AC]` and `[BATTERY]` profiles now accept the following
ThrottleStop-style controls in addition to the original power/temperature and
FIVR settings:

```ini
[AC]
Update_Rate_s: 5

PL1_Tdp_W: 25
PL1_Duration_s: 28
PL1_Enable: True
PL1_Clamp: True

PL2_Tdp_W: 44
PL2_Duration_s: 0.002
PL2_Enable: True
PL2_Clamp: False

HWP_EPP: 32
Disable_Turbo: False
BDPROCHOT_Enabled: True
```

`HWP_EPP` is preferred over the historical boolean `HWP_Mode`; the old
option remains for compatibility. `BDPROCHOT_Enabled` is the reversible
profile control. The historical `Disable_BDPROCHOT` keeps its original
one-way semantics: `True` disables BD PROCHOT, while `False` leaves the
current hardware state untouched.

Profile controls are serialized through a hardware-write lock. AC/battery
changes and config reloads apply one coherent profile; resume immediately
replays mailbox controls, configured HWP policy, TCC/cTDP and TPL instead of
waiting for the next polling interval. Legacy `HWP_Mode=False` is not
periodically reasserted, avoiding a new fight with desktop EPP managers.

## One-shot controls

Examples:

```sh
sudo systemctl stop throttled

sudo throttlectl status
sudo throttlectl limits
sudo throttlectl monitor --interval 0.5

sudo throttlectl epp 32
sudo throttlectl power --pl1 25 --pl2 44 --tau1 28
sudo throttlectl turbo on
sudo throttlectl bd-prochot on
# Only when HWP/Speed Shift is disabled:
sudo throttlectl speedstep on
sudo throttlectl multiplier 38

sudo throttlectl clock-mod
sudo throttlectl temp-target 90
sudo throttlectl turbo-ratio --1c 42 --2c 41 --3c 39 --4c 39

sudo throttlectl fivr read
sudo throttlectl fivr set --core -80 --cache -80
# The set command prints an automatic /var/lib/throttled/oc-backups/... path.
sudo throttlectl fivr restore /var/lib/throttled/oc-backups/fivr-....json

sudo throttlectl iccmax read
sudo throttlectl iccmax set --core 100 --cache 100
sudo throttlectl iccmax restore /var/lib/throttled/oc-backups/iccmax-....json
```

Values in these examples demonstrate syntax only; they are not recommended
settings for every 830 G6 CPU or cooling configuration.

## Stock snapshot

Snapshot schema v2 records full raw values for diagnostics but restores only
the fields this fork can change:

- package power: PL1/PL2 watts, Tau, Enable and Clamp (never the lock bit);
- temperature target: TCC offset only;
- cTDP target bits only;
- Turbo Ratio bytes when programmable;
- HWP package/per-CPU EPP only;
- BD PROCHOT bit only;
- Turbo/SpeedStep bits only;
- clock-modulation enable bit only;
- PERF_CTL ratio only, and only when HWP is disabled.

It additionally captures `intel-rapl-mmio` constraints when present and the
direct MCHBAR package-power raw value only when the read-only mirror equals MSR
0x610. Restore preflights all captured backends before the first write and
rolls back already restored state if a backend fails.

FIVR/IccMax stay outside this strict snapshot because querying them requires an
OC-mailbox WRMSR. Every explicit FIVR/IccMax set creates a root-only recovery
JSON before changing hardware, and dedicated restore subcommands consume those
records.

## Remaining hardware-validation gates

The implementation is intentionally not declaring these items complete until
they are observed on the physical 830 G6:

- exact DMI product string and CPUID stepping;
- whether `intel-rapl-mmio` is exposed by the installed kernel;
- whether the statically validated Whiskey Lake `0x3E34/0x3E35` direct MCHBAR fallback is actually needed on this BIOS/kernel;
- MSR_PKG_POWER_LIMIT lock state;
- whether HWP package control is active;
- whether Turbo Ratio writes are accepted or firmware-locked;
- OC mailbox availability and firmware/microcode voltage lock behavior;
- IccMax read/write acceptance;
- whether firmware/EC rewrites any control and at what cadence;
- which performance-limit reason bits appear under the 830 G6 workload.

Until those are measured, the code should remain conservative: no guessed PCI
allow-list entries, no lock-bit manipulation, and no automatic undervolt.
