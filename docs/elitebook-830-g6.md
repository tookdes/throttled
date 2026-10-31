# HP EliteBook 830 G6 ThrottleStop replacement

This document defines the narrow hardware target for this fork. It is not a
claim of generic Intel laptop support.

## Target

- HP EliteBook 830 G6
- Intel Whiskey Lake-U target CPUID: family 6, model 142, stepping 11
- Linux with the `msr` driver
- direct register writes require root and must not be blocked by kernel lockdown

The implementation is deliberately capability-driven. Unsupported or locked
interfaces must fail closed instead of pretending a setting was accepted.

## ThrottleStop feature map

| ThrottleStop area | Linux implementation | Current status |
| --- | --- | --- |
| Speed Shift EPP | IA32_HWP_REQUEST + package request when active | implemented |
| Turbo checkbox | IA32_MISC_ENABLE bit 38 | implemented |
| SpeedStep | IA32_MISC_ENABLE bit 16 | implemented |
| BD PROCHOT | MSR_POWER_CTL bit 0 | implemented, reversible |
| Clock Mod 100% | IA32_CLOCK_MODULATION enable bit | implemented |
| Set Multiplier | IA32_PERF_CTL target ratio | implemented |
| Turbo Ratio Limits | MSR_TURBO_RATIO_LIMIT | implemented |
| PL1 / PL2 | MSR_PKG_POWER_LIMIT | implemented |
| PL1 / PL2 Enable | MSR_PKG_POWER_LIMIT | implemented |
| Clamp | MSR_PKG_POWER_LIMIT | implemented |
| Tau | RAPL time-window fields | implemented |
| MMIO power limits | kernel intel-rapl-mmio first | implemented |
| direct MCHBAR | validated PCI allow-list only | fallback only |
| FIVR voltage offsets | OC mailbox MSR 0x150 | implemented in explicit control CLI |
| FIVR IccMax | OC mailbox MSR 0x150 | implemented in explicit control CLI |
| Temperature target | MSR_TEMPERATURE_TARGET | daemon profile support |
| Limits | Whiskey/SKL performance-limit reasons + thermal state | implemented |
| Ratio / MHz | IA32_PERF_STATUS | implemented |
| Effective MHz | APERF / MPERF deltas | implemented |
| C0% | MPERF vs reference-rate elapsed time | implemented |
| Package power | RAPL energy delta | implemented |
| Stock reset | raw MSR snapshot + verified restore | implemented for non-mailbox state |
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

MSR_PKG_POWER_LIMIT bit 63 is treated as a hard lock. The code never sets that
bit and refuses direct TPL writes when it is already set.

For the chipset copy of PL1/PL2, the preferred order is:

1. kernel `intel-rapl-mmio` powercap zone;
2. existing direct MCHBAR implementation only for host bridges already in the
   validated allow-list;
3. no MMIO write if neither interface is available.

This is intentional. A guessed Whiskey Lake PCI ID / MCHBAR offset must not be
added merely to make the feature appear supported.

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
Disable_BDPROCHOT: False
```

`HWP_EPP` is preferred over the historical boolean `HWP_Mode`; the old
option remains for compatibility.

Profile controls are reapplied on AC/battery transitions, periodically where
firmware or another power manager may overwrite them, and immediately after
resume.

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
sudo throttlectl speedstep on
sudo throttlectl clock-mod
sudo throttlectl multiplier 38
sudo throttlectl turbo-ratio --1c 42 --2c 41 --3c 39 --4c 39

sudo throttlectl fivr read
sudo throttlectl fivr set --core -80 --cache -80
sudo throttlectl iccmax read
sudo throttlectl iccmax set --core 100 --cache 100
```

Values in these examples demonstrate syntax only; they are not recommended
settings for every 830 G6 CPU or cooling configuration.

## Stock snapshot

The strict snapshot captures directly readable control state:

- MSR_PKG_POWER_LIMIT
- MSR_TEMPERATURE_TARGET
- MSR_POWER_CTL
- MSR_CONFIG_TDP_CONTROL
- MSR_TURBO_RATIO_LIMIT
- IA32_HWP_REQUEST_PKG
- per-CPU IA32_HWP_REQUEST
- per-CPU IA32_MISC_ENABLE
- per-CPU IA32_CLOCK_MODULATION
- per-CPU IA32_PERF_CTL

FIVR and IccMax are excluded because reading their current state requires an
OC-mailbox write. MMIO powercap state is also not yet stored in the strict raw
MSR snapshot.

## Remaining hardware-validation gates

The implementation is intentionally not declaring these items complete until
they are observed on the physical 830 G6:

- exact DMI product string and CPUID stepping;
- whether `intel-rapl-mmio` is exposed by the installed kernel;
- host-bridge PCI ID and whether direct MCHBAR fallback is needed at all;
- MSR_PKG_POWER_LIMIT lock state;
- whether HWP package control is active;
- whether Turbo Ratio writes are accepted or firmware-locked;
- OC mailbox availability and firmware/microcode voltage lock behavior;
- IccMax read/write acceptance;
- whether firmware/EC rewrites any control and at what cadence;
- which performance-limit reason bits appear under the 830 G6 workload.

Until those are measured, the code should remain conservative: no guessed PCI
allow-list entries, no lock-bit manipulation, and no automatic undervolt.
