# megaraid-console -- LSI / Broadcom MegaRAID monitoring console for fnOS.
# Copyright (C) 2026 emilzhang
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  It is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; see the LICENSE file for details.

"""raidcore -- controller access layer for megaraid-console.

Wraps the Broadcom StorCLI binary and turns its JSON output into the plain
dicts the web UI consumes.  Standard library only: fnOS does not guarantee any
third-party Python packages, and this daemon has to survive system upgrades.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time

APP_NAME = "megaraid-console"

CTRL = "/c0"

# Where storcli may live on a fnOS box.  The first hit wins.
STORCLI_CANDIDATES = (
    "/opt/MegaRAID/storcli/storcli64",
    "/opt/MegaRAID/storcli/storcli",
    "/usr/local/MegaRAID/storcli/storcli64",
    "/usr/local/bin/storcli64",
    "/usr/local/sbin/storcli64",
    "/usr/sbin/storcli64",
    "/usr/sbin/storcli",
    "/usr/bin/storcli64",
    "/sbin/storcli64",
)

# Only element paths matching this are ever handed to a subprocess.  This is the
# injection boundary: the web tier never passes a caller-supplied string
# straight through to the shell or to storcli's argv.
# A physical drive can be addressed two ways: through an enclosure
# (/c0/e252/s0) when the backplane answers SES/SGPIO, or directly
# (/c0/s0) when it does not -- an external JBOD without a management
# bridge is the common case.  Both must be accepted everywhere.
TARGET_RE = re.compile(r"^/c0$|^/c0/v\d+$|^/c0/e\d+/s\d+$|^/c0/s\d+$")

# storcli talks to the controller through an ioctl; concurrent invocations are
# serialised so a burst of UI polls cannot queue up behind each other.
_STORCLI_LOCK = threading.Lock()


class StorcliError(RuntimeError):
    """storcli is missing, failed, or returned something unparseable."""


# --------------------------------------------------------------------------- #
# process plumbing
# --------------------------------------------------------------------------- #

def find_storcli(explicit=None):
    """Return a usable storcli path, or None."""
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit
    for path in STORCLI_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in ("storcli64", "storcli"):
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


# storcli writes a storcli.log into its working directory. Run it from a scratch
# directory so runtime droppings never land in the application tree.
_WORKDIR = tempfile.gettempdir()

# storcli appends to ./storcli.log on every invocation, so with the daemon polling
# the controller this file grows for as long as the service runs (measured a bit
# over 2 MB/day at the default 10 s interval).  It is rotated rather than
# truncated, so a crash right after a command still leaves the previous log
# readable; exactly one backup is kept, which bounds the pair at twice the limit.
LOG_NAME = "storcli.log"
LOG_KEEP = 1
DEFAULT_LOG_LIMIT_MB = 128
_LOG_LIMIT_MB = DEFAULT_LOG_LIMIT_MB
_LOG_ROTATIONS = 0


def storcli_log_path():
    return os.path.join(_WORKDIR, LOG_NAME)


def set_log_limit_mb(value):
    """Bound for storcli.log in MB, clamped to a sane range."""
    global _LOG_LIMIT_MB
    try:
        megabytes = int(value)
    except (TypeError, ValueError):
        megabytes = DEFAULT_LOG_LIMIT_MB
    _LOG_LIMIT_MB = max(1, min(megabytes, 4096))
    return _LOG_LIMIT_MB


def rotate_log(path, limit_bytes, keep=LOG_KEEP):
    """Move `path` to `path.1` once it reaches `limit_bytes`.

    Returns True when a rotation happened.  `keep` exists for clarity: only one
    backup is retention-worthy here, and `os.replace` overwrites it atomically,
    so a rotation can never leave two half-written files behind.
    """
    if limit_bytes <= 0:
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size < limit_bytes:
        return False
    try:
        os.replace(path, path + ".1")
    except OSError:
        # Rename can fail on an odd mount; truncating in place still bounds the
        # file, which is the point of the exercise.
        try:
            with open(path, "w", encoding="utf-8"):
                pass
        except OSError:
            return False
    return True


def rotate_storcli_log():
    """Rotate the daemon's own storcli.log when it exceeds the configured cap."""
    global _LOG_ROTATIONS
    if rotate_log(storcli_log_path(), _LOG_LIMIT_MB * 1024 * 1024):
        _LOG_ROTATIONS += 1
        return True
    return False


def log_stats():
    """Current size / cap, surfaced in /api/summary for verification."""
    path = storcli_log_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {
        "path": path,
        "bytes": size,
        "limit_mb": _LOG_LIMIT_MB,
        "rotations": _LOG_ROTATIONS,
    }


def run_storcli(binary, args, timeout=60):
    """Run storcli with `args`; return (returncode, combined_output)."""
    if not binary:
        raise StorcliError("storcli 未安装")
    argv = [binary] + [str(a) for a in args]
    with _STORCLI_LOCK:
        # Bound the log before it grows past the cap; one stat() per call.
        rotate_storcli_log()
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                cwd=_WORKDIR,
            )
        except FileNotFoundError:
            raise StorcliError("storcli 未找到：%s" % binary)
        except PermissionError:
            raise StorcliError("没有权限执行 storcli（需要 root）")
        except subprocess.TimeoutExpired:
            raise StorcliError("storcli 执行超时（%s 秒）" % timeout)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _extract_json(text):
    start = text.find("{")
    if start < 0:
        raise StorcliError("storcli 输出中没有 JSON")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise StorcliError("storcli JSON 解析失败：%s" % exc)
    return obj


def run_storcli_json(binary, args, timeout=60):
    """Run storcli with the `J` flag appended and return the decoded JSON."""
    rc, out = run_storcli(binary, list(args) + ["J"], timeout=timeout)
    doc = _extract_json(out)
    status = controller_block(doc).get("Command Status") or {}
    if str(status.get("Status", "")).lower() not in ("success", ""):
        raise StorcliError(status.get("Description") or "storcli 命令失败")
    return doc


def controller_block(doc):
    controllers = doc.get("Controllers") or []
    if not controllers:
        raise StorcliError("storcli 输出中没有控制器")
    return controllers[0]


def response_data(doc):
    data = controller_block(doc).get("Response Data")
    return data if data is not None else {}


# --------------------------------------------------------------------------- #
# helpers for storcli's loose value types
# --------------------------------------------------------------------------- #

def to_int(value, default=0):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return default
    return default


def to_float(value, default=0.0):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(float(value)) else default
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            try:
                parsed = float(match.group(0))
            except (ValueError, OverflowError):
                return default
            # A very long digit string parses to inf rather than raising, and
            # inf would silently satisfy every ">= threshold" comparison.
            return parsed if math.isfinite(parsed) else default
    return default


def clean(value):
    """Trim storcli's padded strings; empty placeholders become '-'.
    """
    if value is None:
        return "-"
    text = str(value).strip()
    if not text or text in ("N/A", "NA"):
        return text or "-"
    return text


def temp_celsius(value):
    """' 45C (113.00 F)' -> 45"""
    return to_int(value, 0)


# --------------------------------------------------------------------------- #
# collectors
# --------------------------------------------------------------------------- #

def _controller_attrs(rd):
    """`show all` splits controller attributes across Basics / Version / Status,
    while plain `show` returns them flat. Merge whatever is present."""
    merged = {}
    for section in ("Basics", "Version", "Status"):
        value = rd.get(section)
        if isinstance(value, dict):
            merged.update(value)
    return merged or rd


def _find_key(mapping, *needles):
    """First value whose key contains every needle, case-insensitive."""
    for key, value in mapping.items():
        lowered = str(key).lower()
        if all(needle in lowered for needle in needles):
            return value
    return None


def _policies_map(rd):
    """Controller Policies is a mix of scalars and a 'Policies Table' list."""
    policies = rd.get("Policies")
    if not isinstance(policies, dict):
        return {}, {}
    scalars = {k: v for k, v in policies.items() if k != "Policies Table"}
    rates = {}
    table = policies.get("Policies Table")
    if isinstance(table, list):
        for row in rules_iter(table):
            if isinstance(row, dict) and "Policy" in row:
                rates[str(row["Policy"]).strip()] = clean(row.get("Current"))
    return scalars, rates


def rules_iter(value):
    """Iterate a storcli JSON list, tolerating a lone dict."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def hex_id(value):
    """storcli reports PCI IDs as decimals; `0x5D` is far easier to read.

    4096 -> '0x1000', 93 -> '0x5D'.  Non-numeric values pass through.
    """
    if value is None:
        return "-"
    text = str(value).strip()
    if not text or text == "-":
        return "-"
    if text.lower().startswith("0x"):
        return text
    try:
        return "0x%X" % int(text, 10)
    except (TypeError, ValueError):
        return text


def _section(rd, name):
    value = rd.get(name)
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _pick(mapping, *names):
    """Exact-ish lookup that tolerates storcli's padding and case drift."""
    if not isinstance(mapping, dict):
        return None
    wanted = [n.strip().lower() for n in names]
    for key, value in mapping.items():
        if str(key).strip().lower() in wanted:
            return value
    return None


def gather_alarm(binary, ctrl=CTRL):
    """Alarm/buzzer state. storcli does not expose it in `show all`, so this is
    a separate call -- it is cheap and the value matters to the UI."""
    rc, out = run_storcli(binary, [ctrl, "show", "alarm"])
    match = re.search(r"^\s*Alarm\s*[=:]?\s*(\S+)\s*$", out, re.MULTILINE)
    return match.group(1) if match else "-"


def gather_overview(binary, ctrl=CTRL):
    """Everything from a single `/c0 show all J`."""
    doc = run_storcli_json(binary, [ctrl, "show", "all"])
    rd = response_data(doc)
    attrs = _controller_attrs(rd)
    scalars, rates = _policies_map(rd)

    status = _section(rd, "Status")
    tasks = _section(rd, "Scheduled Tasks")
    hw = _section(rd, "HwCfg")
    bus = _section(rd, "Bus")
    caps = _section(rd, "Capabilities")
    defaults = _section(rd, "Defaults")

    # Power backup is either a CacheVault supercapacitor (`Cachevault_Info`) or
    # a plain battery (`BBU_Info`).  The 9361-8i class only reports the former;
    # battery-backed cards such as the Dell PERC H840 report the latter, and the
    # CacheVault key is then simply absent -- which is why such a card used to
    # show an empty battery panel.
    cv, cv_kind = {}, ""
    for section_name, kind in (("Cachevault_Info", "cachevault"),
                               ("BBU_Info", "bbu"),
                               ("Battery_Info", "bbu")):
        section = _section(rd, section_name)
        if section:
            cv, cv_kind = dict(section), kind
            break

    def table(name):
        value = rd.get(name)
        return value if isinstance(value, list) else []

    # Controller temperature: the 9361-8i usually only exposes the ROC sensor
    # ("Temperature Sensor for Controller = Absent"), so fall back to it and
    # remember which one the reading came from.
    temp_ctrl = to_int(_pick(hw, "Controller Temperature"), None)
    temp_roc = to_int(_pick(hw, "ROC temperature(Degree Celsius)"), None)
    if temp_ctrl is not None:
        temp_value, temp_source = temp_ctrl, "控制器"
    elif temp_roc is not None:
        temp_value, temp_source = temp_roc, "ROC"
    else:
        temp_value, temp_source = None, "-"

    return {
        "controller": {
            # --- 标识 ---
            "product": clean(attrs.get("Model") or attrs.get("Product Name")),
            "serial": clean(attrs.get("Serial Number")),
            "sas_address": clean(attrs.get("SAS Address")),
            "pci": clean(attrs.get("PCI Address")),
            "mfg_date": clean(attrs.get("Mfg Date") or attrs.get("Mfg. Date")),
            "rework_date": clean(attrs.get("Rework Date")),
            "revision": clean(attrs.get("Revision No") or attrs.get("Revision")),
            "vendor_id": hex_id(bus.get("Vendor Id")),
            "device_id": hex_id(bus.get("Device Id")),
            "subvendor_id": hex_id(bus.get("SubVendor Id")),
            "subdevice_id": hex_id(bus.get("SubDevice Id")),
            "bus_number": clean(bus.get("Bus Number")),
            "device_number": clean(bus.get("Device Number")),
            "function_number": clean(bus.get("Function Number")),
            "oem_id": clean(tasks.get("OEMID") or tasks.get("OEM ID")),
            "safe_id": clean(rd.get("Safe ID")),
            # --- 固件与驱动 ---
            "fw_version": clean(attrs.get("Firmware Version") or attrs.get("FW Version")),
            "fw_package": clean(attrs.get("Firmware Package Build")),
            "bios_version": clean(attrs.get("Bios Version") or attrs.get("BIOS Version")),
            "cpld_version": clean(attrs.get("CPLD Version")),
            "hii_version": clean(attrs.get("HII Version")),
            "ctlr_version": clean(attrs.get("Ctrl-R Version")),
            "preboot_cli": clean(attrs.get("Preboot CLI Version")),
            "nvdata_version": clean(attrs.get("NVDATA Version")),
            "boot_block_version": clean(attrs.get("Boot Block Version")),
            "driver": clean(attrs.get("Driver Name")),
            "driver_version": clean(attrs.get("Driver Version")),
            "chip_revision": clean(hw.get("ChipRevision")),
            # --- 硬件与缓存 ---
            "cache_size": clean(hw.get("On Board Memory Size") or attrs.get("Memory")),
            "nvram_size": clean(hw.get("NVRAM Size")),
            "flash_size": clean(hw.get("Flash Size")),
            "cv_flash_size": clean(hw.get("CacheVault Flash Size")),
            "fw_cache_size": clean(hw.get("Current Size of FW Cache (MB)")),
            "backup_units": clean(hw.get("BBU")),
            "tpm": clean(hw.get("TPM")),
            "onboard_expander": clean(hw.get("On Board Expander")),
            "front_ports": clean(hw.get("Front End Port Count")),
            "backend_ports": clean(hw.get("Backend Port Count")),
            "personality": clean(attrs.get("Current Personality")),
            # Host/Device Interface live in the Bus section, not Basics/Version/Status.
            "device_interface": clean(bus.get("Device Interface")
                                      or attrs.get("Device Interface")),
            "host_interface": clean(bus.get("Host Interface")
                                    or attrs.get("Host Interface")),
            "supported_drives": clean(caps.get("Supported Drives")),
            "raid_levels": clean(caps.get("RAID Level Supported")),
            "max_vds": clean(caps.get("Max Number of VDs")),
            "max_arrays": clean(caps.get("Max Arrays")),
            "max_parallel": clean(caps.get("Max Parallel Commands")),
            "jbod": clean(caps.get("Enable JBOD")),
            "min_strip": clean(caps.get("Min Strip Size")),
            "max_strip": clean(caps.get("Max Strip Size")),
            "default_strip": clean(defaults.get("Strip Size")),
            # --- 状态与温度 ---
            "status": clean(status.get("Controller Status")),
            "memory_correctable": to_int(status.get("Memory Correctable Errors")),
            "memory_uncorrectable": to_int(status.get("Memory Uncorrectable Errors")),
            "ecc_bucket": clean(status.get("ECC Bucket Count")),
            "cache_preserved": clean(status.get("Any Offline VD Cache Preserved")),
            "safe_mode": clean(status.get("Controller has booted into safe mode")),
            "temp": temp_value,
            "temp_roc": temp_roc,
            "temp_source": temp_source,
            "temp_sensor_roc": clean(hw.get("Temperature Sensor for ROC")),
            "temp_sensor_ctrl": clean(hw.get("Temperature Sensor for Controller")),
            "alarm": clean(_find_key(scalars, "alarm") or hw.get("Alarm")),
            "controller_time": clean(attrs.get("Current Controller Date/Time")),
            "system_time": clean(attrs.get("Current System Date/time")),
        },
        "settings": {
            "auto_rebuild": clean(scalars.get("Auto Rebuild")),
            "maintain_pd_fail_history": clean(scalars.get("Maintain PD Fail History")),
            "activity_for_locate": clean(scalars.get("Use drive activity for locate")),
            "rebuild_rate": rates.get("Rebuild Rate", "-"),
            "cc_rate": rates.get("Check Consistency Rate", "-"),
            "pr_rate": rates.get("PR Rate", "-"),
            "bgi_rate": rates.get("BGI Rate", "-"),
            "reconstruct_rate": rates.get("Reconstruction Rate", "-"),
            "expose_enclosure": clean(scalars.get("Expose Enclosure Devices")),
            "restore_hot_spare": clean(scalars.get("Restore Hot Spare on Insertion")),
            "auto_detect_backplane": clean(scalars.get("Auto detect BackPlane")),
        },
        "cachevault": {
            # `kind` tells the UI which wording to use: "cachevault" for a
            # supercapacitor, "bbu" for a battery.  Empty when neither is
            # reported, so a card without power backup renders as "-" rather
            # than pretending to be a degraded supercap.
            "kind": cv_kind,
            "model": clean(cv.get("Model") or cv.get("Type")
                           or cv.get("Product Name")),
            "state": clean(cv.get("State") or cv.get("Battery State")),
            "temp": clean(cv.get("Temp") or cv.get("Temperature")),
            "mfg_date": clean(cv.get("MfgDate")
                              or cv.get("Date of Manufacture")),
        },
        "schedule": {
            "cc_interval": clean(tasks.get("Consistency Check Reoccurrence")),
            "cc_next": clean(tasks.get("Next Consistency check launch")),
            "pr_interval": clean(tasks.get("Patrol Read Reoccurrence")),
            "pr_next": clean(tasks.get("Next Patrol Read launch")),
            "battery_next": clean(tasks.get("Next Battery Learn")),
        },
        "topology": [row for row in table("TOPOLOGY") if isinstance(row, dict)],
        "vds": [row for row in table("VD LIST") if isinstance(row, dict)],
        "pds": [row for row in table("PD LIST") if isinstance(row, dict)],
        "enclosures": [row for row in table("Enclosure LIST") if isinstance(row, dict)],
    }


def _flatten_pairs(value):
    """Property/Value pairs, a plain dict, or a list holding one of either.

    storcli has shipped all three shapes across versions; collapsing them here
    keeps the lookups below single-sourced.
    """
    out = {}
    for item in rules_iter(value):
        if not isinstance(item, dict):
            continue
        if "Property" in item:
            out[str(item["Property"]).strip()] = item.get("Value")
        else:
            # A summary row ({"Type": ..., "State": ...}) rather than pairs.
            out.update(item)
    return out


def _probe_power_backup(binary, ctrl, sub):
    """One `/cN/<sub> show all` call, flattened into a single mapping.

    storcli spreads the module description across several sections
    (Cachevault_Info / Firmware_Status / GasGaugeStatus / Design_Info /
    Properties, or BBU_Info for a battery).  Folding every section into one
    view keeps the lookups single-sourced and tolerant of whichever sections a
    given firmware decides to emit.
    """
    try:
        doc = run_storcli_json(binary, [ctrl + "/" + sub, "show", "all"])
    except StorcliError:
        return {}
    rd = response_data(doc)
    if not isinstance(rd, dict):
        return {}
    merged = {}
    for value in rd.values():
        merged.update(_flatten_pairs(value))
    for key, value in rd.items():
        if not isinstance(value, (dict, list)):
            merged[str(key)] = value
    return merged


def _normalise_power_backup(kind, fields):
    """Collapse a CacheVault or BBU mapping onto the keys the UI consumes.

    Both kinds share most meaningful fields; the aliases cover the wording
    differences storcli uses between the supercapacitor and battery objects
    (State vs Battery State, Module Version vs Firmware Version, ...).
    """
    def pick(*names):
        for name in names:
            text = clean(_pick(fields, name))
            if text not in ("-", ""):
                return text
        return "-"

    temp_raw = pick("Temperature", "Temp", "Module Temperature", "Battery Temperature")
    return {
        "kind": kind,
        # --- identity and state (both kinds) ---
        "model": pick("Type", "Model", "Product Name", "Device Name"),
        "state": pick("State", "Battery State", "Status"),
        "battery_state": pick("Battery State", "State"),
        "temp": temp_raw,
        "temp_value": to_int(temp_raw, None),
        "mfg_date": pick("Date of Manufacture", "MfgDate", "Mfg Date"),
        # --- energy / electrical readings ---
        "energy": pick("Pack Energy", "Energy"),
        "pack_energy": pick("Pack Energy", "Energy"),
        "design_capacity": pick("Design Capacity", "Capacity", "Design Capacity (mAh)"),
        "capacitance": pick("Capacitance"),
        "remaining_reserve": pick("Remaining Reserve Space"),
        "voltage": pick("Voltage", "Pack Voltage", "Voltage (mV)"),
        "current": pick("Current", "Current (mA)"),
        "retention": pick("Retention Time", "Battery Retention Time",
                          "Retention Time (Minutes)"),
        # --- health flags ---
        "nvcache_state": pick("NVCache State", "NVCache Status"),
        "replacement_required": pick("Replacement required", "Replacement Required",
                                     "Replace Battery", "Battery Replacement required"),
        "microcode_update": pick("Module microcode update required"),
        "no_space_offload": pick("No space to cache offload"),
        "voltage_state": pick("Voltage State"),
        # --- module identification ---
        "firmware": pick("Module Version", "Firmware Version", "FW Version"),
        "manufacturer": pick("Manufacture Name", "Manufacturer", "Vendor"),
        "serial": pick("Serial Number", "Battery Serial Number",
                       "tmmSerialNo", "scapSerialNo"),
        "flash_size": pick("CacheVault Flash Size"),
        "pcb_assy": pick("tmmPcbAssmNo"),
        "pcb_version": pick("tmmPCBversionNo"),
        "batpack_assy": pick("tmmBatPackAssmNo"),
        "fru": pick("tmmFru"),
        "cell_mfg_date": pick("tmm Date of Manufacture"),
        # --- learn cycle ---
        "learn_period": pick("Auto Learn Period", "Learn Cycle Period", "Learn Period"),
        "learn_mode": pick("Auto-Learn Mode", "Learn Mode"),
        "learn_delay": pick("Learn Delay Interval"),
        "next_learn": pick("Next Learn time", "Next Learn Time",
                           "Next Battery Learn", "Next Learn"),
    }


def gather_battery(binary, ctrl=CTRL):
    """Power-backup detail for supercapacitor *and* battery backed controllers.

    The 9361-8i class answers `/c0/cv` (CacheVault supercapacitor).  A
    battery-backed card such as the Dell PERC H840 answers `/c0/bbu` instead and
    often only there: its `show all` carries no `Cachevault_Info` at all, so the
    old collector came back empty and the panel showed nothing but dashes.
    Both sub-objects are probed -- a card reports at most one -- and collapsed
    onto one model, distinguished by `kind` ("cachevault" | "bbu").
    """
    for kind, sub in (("cachevault", "cv"), ("bbu", "bbu")):
        fields = _probe_power_backup(binary, ctrl, sub)
        if not fields:
            continue
        described = _normalise_power_backup(kind, fields)
        # An answering sub-object with neither a state nor a model is not a
        # power backup; keep looking rather than reporting an empty panel.
        if described["state"] in ("-", "") and described["model"] in ("-", ""):
            continue
        return described
    return {}


def gather_cachevault(binary, ctrl=CTRL):
    """Backwards-compatible name; the collector now covers battery cards too."""
    return gather_battery(binary, ctrl)


_DRIVE_KEY_RE = re.compile(r"^Drive (/c0/(?:e\d+/)?s\d+)$")
_DRIVE_DETAIL_RE = re.compile(r"^Drive (/c0/(?:e\d+/)?s\d+) - Detailed Information$")


def _drive_section(binary, ctrl, tail):
    """`Response Data` for a drive-wide query, whichever topology answers.

    Behind a backplane the drives are addressed as `eall/sall`; on a
    controller whose drives are direct-attached that path answers
    "No drive found!" and only `sall` works.  Try the enclosure path first
    (what a normal NAS backplane expects), then fall back.

    Note the element path must be one token: storcli rejects
    `eall sall` split into separate arguments.
    """
    last = None
    for path in (ctrl + "/eall/sall", ctrl + "/sall"):
        try:
            doc = run_storcli_json(binary, [path] + list(tail))
        except StorcliError as exc:
            last = exc
            continue
        section = response_data(doc)
        if isinstance(section, dict) and section:
            return section
    if last is not None:
        raise last
    return {}


def gather_drives(binary, ctrl=CTRL):
    """Per-drive detail from one `show all` call, on either topology."""
    rd = _drive_section(binary, ctrl, ["show", "all"])
    if not rd:
        return {}

    detail_by_slot = {}
    for key, value in rd.items():
        if not isinstance(key, str):
            continue
        match = _DRIVE_DETAIL_RE.match(key)
        if not match or not isinstance(value, dict):
            continue
        slot_path = match.group(1)
        # The nested section keys repeat the "Drive " prefix, e.g.
        #   "Drive /c0/e252/s0 - Detailed Information" -> { "Drive /c0/e252/s0 State": {...} }
        prefix = "Drive " + slot_path
        state = value.get(prefix + " State") or {}
        attrs = value.get(prefix + " Device attributes") or {}
        pol = value.get(prefix + " Policies/Settings") or {}
        if not isinstance(state, dict):
            state = {}
        if not isinstance(attrs, dict):
            attrs = {}
        if not isinstance(pol, dict):
            pol = {}
        detail_by_slot[slot_path] = {
            # --- 计数器 / 温度 ---
            "shield_counter": to_int(state.get("Shield Counter")),
            "media_errors": to_int(state.get("Media Error Count")),
            "other_errors": to_int(state.get("Other Error Count")),
            "predictive_failures": to_int(state.get("Predictive Failure Count")),
            "smart_alert": clean(state.get("S.M.A.R.T alert flagged by drive")),
            "temperature": temp_celsius(state.get("Drive Temperature")),
            "temperature_raw": clean(state.get("Drive Temperature")),
            # --- 设备属性 ---
            "sn": clean(attrs.get("SN")),
            "wwn": clean(attrs.get("WWN")),
            "model": clean(attrs.get("Model Number") or attrs.get("Model")),
            "manufacturer": clean(attrs.get("Manufacturer Id")),
            "nand_vendor": clean(attrs.get("NAND Vendor")),
            "firmware": clean(attrs.get("Firmware Revision")),
            "firmware_release": clean(attrs.get("Firmware Release Number")),
            "raw_size": clean(attrs.get("Raw size")),
            "coerced_size": clean(attrs.get("Coerced size")),
            "non_coerced_size": clean(attrs.get("Non Coerced size")),
            "sector_logical": clean(attrs.get("Logical Sector Size")),
            "sector_physical": clean(attrs.get("Physical Sector Size")),
            "write_cache": clean(attrs.get("Write Cache")),
            "device_speed": clean(attrs.get("Device Speed")),
            "link_speed": clean(attrs.get("Link Speed")),
            "connector": clean(attrs.get("Connector Name")),
            # --- 策略 / 设置 ---
            "position": clean(pol.get("Drive position")),
            "enclosure_position": clean(pol.get("Enclosure position")),
            "port": clean(pol.get("Connected Port Number")),
            "sequence": to_int(pol.get("Sequence Number")),
            "spare": clean(pol.get("Commissioned Spare")),
            "emergency_spare": clean(pol.get("Emergency Spare")),
            "last_predictive_seq": clean(
                pol.get("Last Predictive Failure Event Sequence Number")),
            "fde_type": clean(pol.get("FDE Type")),
            "sed_capable": clean(pol.get("SED Capable")),
            "sed_enabled": clean(pol.get("SED Enabled")),
            "secured": clean(pol.get("Secured")),
            "crypto_erase": clean(pol.get("Cryptographic Erase Capable")),
            "sanitize": clean(pol.get("Sanitize Support")),
            "locked": clean(pol.get("Locked")),
            "pi_eligible": clean(pol.get("PI Eligible")),
            "pi_formatted": clean(pol.get("Drive is formatted for PI")),
            "pi_type": clean(pol.get("PI type")),
            "certified": clean(pol.get("Certified")),
            "wide_port": clean(pol.get("Wide Port Capable")),
            "multipath": clean(pol.get("Multipath")),
            "port_info": pol.get("Port Information") or [],
        }

    drives = {}
    for key, value in rd.items():
        if not isinstance(key, str):
            continue
        match = _DRIVE_KEY_RE.match(key)
        if not match or not isinstance(value, list) or not value:
            continue
        row = value[0]
        if not isinstance(row, dict):
            continue
        slot_path = match.group(1)
        base = {k: (clean(v) if isinstance(v, str) else v) for k, v in row.items()}
        base["slot_path"] = slot_path
        base.update(detail_by_slot.get(slot_path, {}))
        drives[slot_path] = base
    return drives


def gather_phy(binary, ctrl=CTRL):
    """Cumulative PHY counters for every drive, one storcli call."""
    rd = _drive_section(binary, ctrl, ["show", "phyerrorcounters"])
    if not rd:
        return {}

    result = {}
    for drive, phys in rd.items():
        if not isinstance(phys, list):
            continue
        totals = {"inval": 0, "disp": 0, "los": 0, "rst": 0}
        ports = []
        for entry in phys:
            if not isinstance(entry, dict):
                continue
            ports.append(to_int(entry.get("PortNum")))
            totals["inval"] += to_int(entry.get("InvalDwdCnt"))
            totals["disp"] += to_int(entry.get("RungDispartyErrCnt"))
            totals["los"] += to_int(entry.get("LosOfDwrdSynCnt"))
            totals["rst"] += to_int(entry.get("PhyResetPrbCnt"))
        totals["ports"] = ports
        result[drive] = totals
    return result


def gather_rebuild(binary, slot_paths, ctrl=CTRL):
    """Rebuild progress for the given slot paths (caller passes the busy ones)."""
    out = {}
    for slot_path in slot_paths:
        if not TARGET_RE.match(slot_path):
            continue
        try:
            doc = run_storcli_json(binary, [slot_path, "show", "rebuild"])
        except StorcliError:
            continue
        rd = response_data(doc)
        rows = rd if isinstance(rd, list) else [rd]
        for row in rows:
            if not isinstance(row, dict):
                continue
            out[slot_path] = {
                "progress": clean(row.get("Progress%")),
                "status": clean(row.get("Status")),
                "eta": clean(row.get("Estimated Time Left")),
            }
            break
    return out


def parse_events(text, limit=60):
    """Parse the textual `/c0 show events` output into blocks."""
    events = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("seqNum:"):
            if current is not None:
                events.append(current)
            current = {"seq": line.split(":", 1)[1].strip()}
            continue
        if current is None:
            continue
        if line.startswith("Time:"):
            current["time"] = line.split(":", 1)[1].strip()
        elif line.startswith("Event Description:"):
            current["desc"] = line.split(":", 1)[1].strip()
        elif line.startswith("Code:"):
            current["code"] = line.split(":", 1)[1].strip()
        elif line.startswith("Class:"):
            current["class"] = line.split(":", 1)[1].strip()
    if current is not None:
        events.append(current)

    events = [e for e in events if e.get("desc")]
    events.reverse()  # newest first
    return events[:limit]


def gather_events(binary, ctrl=CTRL, limit=60):
    rc, out = run_storcli(binary, [ctrl, "show", "events"])
    return parse_events(out, limit=limit)


def gather_foreign(binary, ctrl=CTRL):
    """Foreign configuration summary (read-only)."""
    rc, out = run_storcli(binary, [ctrl, "fall", "show"])
    pds = None
    groups = None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Total Foreign PDs"):
            pds = to_int(stripped.split("=", 1)[-1])
        elif stripped.startswith("Total foreign Drive Groups"):
            groups = to_int(stripped.split("=", 1)[-1])
    return {"raw": out.strip(), "pd_count": pds, "drive_groups": groups}


# --------------------------------------------------------------------------- #
# PHY rate sampler
# --------------------------------------------------------------------------- #

class PhySampler:
    """Turns cumulative PHY counters into per-second rates.

    PHY error counters only ever increase and keep climbing even with a totally
    idle array, so the absolute number says very little.  The rate is the only
    signal that actually distinguishes a healthy link from a failing one.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._previous = {}
        self._previous_ts = None

    def update(self, counters):
        now = time.monotonic()
        with self._lock:
            elapsed = (now - self._previous_ts) if self._previous_ts else None
            rates = {}
            for drive, current in counters.items():
                previous = self._previous.get(drive)
                if previous and elapsed and elapsed > 0:
                    rates[drive] = {
                        "interval": round(elapsed, 1),
                        "inval_per_s": round(max(0, current["inval"] - previous["inval"]) / elapsed, 1),
                        "disp_per_s": round(max(0, current["disp"] - previous["disp"]) / elapsed, 1),
                        "los_per_s": round(max(0, current["los"] - previous["los"]) / elapsed, 1),
                        "rst_per_s": round(max(0, current["rst"] - previous["rst"]) / elapsed, 1),
                        "inval_delta": max(0, current["inval"] - previous["inval"]),
                        "los_delta": max(0, current["los"] - previous["los"]),
                    }
            self._previous = {d: dict(c) for d, c in counters.items()}
            self._previous_ts = now
            return rates

    def peek_interval(self):
        with self._lock:
            if not self._previous_ts:
                return None
            return round(time.monotonic() - self._previous_ts, 1)


# --------------------------------------------------------------------------- #
# slot helpers
# --------------------------------------------------------------------------- #

def slot_shortname(slot_path):
    """'/c0/e252/s2' or '/c0/s2' -> 's2'"""
    match = re.search(r"/s(\d+)$", slot_path or "")
    return "s%s" % match.group(1) if match else ""


def slot_label(pd_row):
    """'252:2' from a PD LIST row."""
    return clean(pd_row.get("EID:Slt"))
