# megaraid-console -- LSI / Broadcom MegaRAID monitoring console for fnOS.
# Copyright (C) 2026 emilzhang
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  It is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; see the LICENSE file for details.

#!/usr/bin/env python3
"""megaraid-console daemon.

Serves the RAID monitoring UI and a small JSON API over a Unix socket, which
is how fnOS applications are exposed: nginx forwards /app/<name> to
/var/run/trim_http_cgi.socket, which in turn talks to this process' socket.
No TCP port is ever opened.

Subcommands::

    raidwatchd.py run          start the HTTP server
    raidwatchd.py init-config  write a default config (detects storcli)
    raidwatchd.py status       print a one-line status

Needs root: storcli reaches the controller through an ioctl that requires it.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import signal
import socketserver
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import raidcore  # noqa: E402
import pam_verify  # noqa: E402
from raidcore import CTRL, PhySampler, StorcliError, TARGET_RE  # noqa: E402

APP_NAME = "megaraid-console"
VERSION = "1.0.6"
DEFAULT_BASE = "/app/megaraid-console"

DEFAULT_CONFIG = {
    "storcli": "",
    "poll_interval": 10,
    "phy_warn_rate": 5.0,
    "phy_bad_rate": 50.0,
    "temp_warn": 50,
    "temp_bad": 58,
    "event_limit": 60,
    # storcli.log cap in MB; rotated to storcli.log.1 when reached (one backup).
    "storcli_log_max_mb": 128,
}

# storcli commands are serialised behind one lock, so a slow command blocks the
# next one. Running them inside the request thread meant the browser sat on a
# dead connection with nothing to show; they are background jobs instead.
JOB_KEEP = 50          # finished jobs retained for the UI to reconcile against
JOB_TTL = 900.0        # seconds a finished job stays queryable
EVENT_CACHE_TTL = 20.0  # the controller event log is ~15 MB; don't re-read it per poll
# ...and when that read fails, back off too, or every poll retries 15 MB.
EVENT_ERROR_TTL = 60.0

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

BASE_PLACEHOLDER = "__RW_BASE__"

# Accept both addressing schemes: behind an enclosure (/c0/e252/s2) and
# direct-attached (/c0/s2, typical of an external JBOD without SES).
DRIVE_RE = re.compile(r"^/c0/(?:e\d+/)?s\d+$")
VD_RE = re.compile(r"^/c0/v\d+$")

# A request body for this API is a few hundred bytes at most; anything larger
# is either a bug or an attempt to make the daemon allocate on command.
MAX_BODY = 1 << 20
# Two browsers, or one impatient operator, must not multiply the number of
# storcli runs: everything collapses onto one collection per this many seconds.
MIN_COLLECT_GAP = 3.0
# When the controller is already running a background job, management traffic
# competes with it, so the UI is told to back off.
BUSY_POLL_MIN = 30
# Rotate the audit trail rather than growing a file in @appconf forever.
AUDIT_MAX_BYTES = 2 << 20
AUDIT_KEEP = 1

# C0 controls, DEL included, TAB kept (0x09) and -- critically -- newline
# (0x0a) NOT kept: a newline is exactly what lets a crafted request line forge
# an extra entry in the log.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def strip_control(text, limit=None):
    """Strip control characters so nothing can forge or mangle a log line.

    Covers C0 controls (except TAB) and DEL.  Newlines are included, which is
    the point: both the request line and storcli's own output reach the log.
    """
    cleaned = _CONTROL_CHARS.sub("", str(text if text is not None else ""))
    return cleaned[:limit] if limit else cleaned


# Drive/VD states that mean "an operation is in flight on the controller".
BUSY_DRIVE_STATES = {"rbld", "rec", "cpyb", "copyb", "init", "migr", "sntze"}
BUSY_VD_STATES = {"init", "rbld", "rec", "bksy", "migration", "migr"} 

# Vendor rule: never overlap a state change with a background operation.
# Starting an offline/jbod/foreign-import while a rebuild or copyback is running
# can take the array down for good, so these are refused while the controller
# is busy.  Stopping/pausing the running job stays available.
BG_CONFLICT_ACTIONS = {
    "drive_offline", "drive_missing", "drive_jbod", "drive_good",
    "foreign_import", "foreign_delete",
    "cc_start", "cc_resume", "rebuild_start",
}

# Removing a member from an already-degraded array destroys the last copy of
# the redundancy; these need an explicit target-named confirmation on top of
# the password.
LAST_REDUNDANCY_ACTIONS = {"drive_offline", "drive_missing", "drive_jbod"}


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self, etc_dir):
        self.etc_dir = etc_dir
        self.path = os.path.join(etc_dir, "config.json")
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            stored = {}
        with self._lock:
            merged = dict(DEFAULT_CONFIG)
            if isinstance(stored, dict):
                merged.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
            self._data = merged

    def snapshot(self):
        with self._lock:
            return dict(self._data)

    def update(self, changes):
        with self._lock:
            for key, value in changes.items():
                if key not in DEFAULT_CONFIG:
                    continue
                self._data[key] = value
            data = dict(self._data)
        os.makedirs(self.etc_dir, exist_ok=True)
        # Write-then-rename: a crash or a concurrent update mid-write would
        # otherwise leave a truncated file, and Config.load() silently falls
        # back to defaults when the JSON will not parse -- i.e. settings would
        # quietly revert instead of failing loudly.
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        return data


# --------------------------------------------------------------------------- #
# action catalogue
# --------------------------------------------------------------------------- #
#
# risk:
#   write  -- changes controller state but will not take data offline
#   danger -- can take a drive/array offline or trigger a multi-hour rebuild

def _c0(*args):
    return [CTRL] + list(args)


ACTION_TABLE = (
    # --- alarm ----------------------------------------------------------- #
    {"name": "alarm_on", "label": "开启蜂鸣器", "risk": "write",
     "args": lambda t, v: _c0("set", "alarm=on")},
    {"name": "alarm_off", "label": "关闭蜂鸣器", "risk": "write",
     "args": lambda t, v: _c0("set", "alarm=off")},
    {"name": "alarm_silence", "label": "静音当前告警", "risk": "write",
     "args": lambda t, v: _c0("set", "alarm=silence")},

    # --- identify -------------------------------------------------------- #
    {"name": "locate_start", "label": "点亮定位灯", "risk": "write", "target": "drive",
     "args": lambda t, v: [t, "start", "locate"]},
    {"name": "locate_stop", "label": "熄灭定位灯", "risk": "write", "target": "drive",
     "args": lambda t, v: [t, "stop", "locate"]},

    # --- rebuild --------------------------------------------------------- #
    {"name": "rebuild_start", "label": "开始重建", "risk": "danger", "target": "drive",
     "args": lambda t, v: [t, "start", "rebuild"]},
    {"name": "rebuild_stop", "label": "停止重建", "risk": "danger", "target": "drive",
     "args": lambda t, v: [t, "stop", "rebuild"]},
    {"name": "rebuild_pause", "label": "暂停重建", "risk": "danger", "target": "drive",
     "args": lambda t, v: [t, "pause", "rebuild"]},
    {"name": "rebuild_resume", "label": "恢复重建", "risk": "danger", "target": "drive",
     "args": lambda t, v: [t, "resume", "rebuild"]},

    # --- drive state ----------------------------------------------------- #
    {"name": "drive_online", "label": "盘置为上线", "risk": "danger", "target": "drive",
     "args": lambda t, v: [t, "set", "online"]},
    {"name": "drive_offline", "label": "盘置为离线", "risk": "danger", "target": "drive",
     "args": lambda t, v: [t, "set", "offline"]},
    {"name": "drive_good", "label": "盘置为 Unconfigured Good", "risk": "write",
     "target": "drive", "args": lambda t, v: [t, "set", "good"]},
    {"name": "drive_missing", "label": "标记盘缺失（会触发重建）", "risk": "danger",
     "target": "drive", "args": lambda t, v: [t, "set", "missing"]},
    {"name": "drive_jbod", "label": "盘置为 JBOD 直通", "risk": "danger",
     "target": "drive", "args": lambda t, v: [t, "set", "jbod"]},

    # --- consistency check ----------------------------------------------- #
    {"name": "cc_start", "label": "开始一致性校验", "risk": "write", "target": "vd",
     "args": lambda t, v: [t, "start", "cc", "force"]},
    {"name": "cc_stop", "label": "停止一致性校验", "risk": "write", "target": "vd",
     "args": lambda t, v: [t, "stop", "cc"]},
    {"name": "cc_pause", "label": "暂停一致性校验", "risk": "write", "target": "vd",
     "args": lambda t, v: [t, "pause", "cc"]},
    {"name": "cc_resume", "label": "恢复一致性校验", "risk": "write", "target": "vd",
     "args": lambda t, v: [t, "resume", "cc"]},

    # --- foreign configuration ------------------------------------------- #
    {"name": "foreign_import", "label": "导入外来配置", "risk": "danger",
     "args": lambda t, v: _c0("fall", "import")},
    {"name": "foreign_delete", "label": "删除外来配置", "risk": "danger",
     "args": lambda t, v: _c0("fall", "delete")},

    # --- controller settings --------------------------------------------- #
    {"name": "set_rebuildrate", "label": "设置重建速率", "risk": "write",
     "value": "percent", "args": lambda t, v: _c0("set", "rebuildrate=%d" % v)},
    {"name": "set_ccrate", "label": "设置校验速率", "risk": "write",
     "value": "percent", "args": lambda t, v: _c0("set", "ccrate=%d" % v)},
    {"name": "set_prrate", "label": "设置巡检速率", "risk": "write",
     "value": "percent", "args": lambda t, v: _c0("set", "prrate=%d" % v)},
    {"name": "set_autorebuild", "label": "自动重建开关", "risk": "write",
     "value": "toggle", "args": lambda t, v: _c0("set", "autorebuild=%s" % v)},
    {"name": "set_maintainpdfailhistory", "label": "PD 失效历史开关", "risk": "write",
     "value": "toggle",
     "args": lambda t, v: _c0("set", "maintainpdfailhistory=%s" % v)},
    {"name": "set_activityforlocate", "label": "用活动灯定位开关", "risk": "write",
     "value": "toggle", "args": lambda t, v: _c0("set", "activityforlocate=%s" % v)},
    {"name": "set_patrolread", "label": "巡检开关", "risk": "write",
     "value": "toggle", "args": lambda t, v: _c0("set", "patrolread=%s" % v)},
)

ACTIONS = {entry["name"]: entry for entry in ACTION_TABLE}


# Actions that need hardware the controller may not have.  A PERC H840 has no
# buzzer at all (storcli: "Alarm ABSENT"), and a direct-attached drive on a card
# without an SES enclosure cannot be given a locate blink (storcli:
# "No Enclosure Found!" / "Start Drive Locate Failed.").  Sending those commands
# anyway produces a scary red failure for something that cannot ever work.
ALARM_ACTIONS = {"alarm_on", "alarm_off", "alarm_silence"}
LOCATE_ACTIONS = {"locate_start", "locate_stop"}

LOCATE_UNSUPPORTED = (
    "当前磁盘为直连拓扑（/c0/sN，控制器未挂 SES 机框，storcli 报 "
    "No Enclosure Found），控制器无法驱动定位灯。定位灯需要背板/机框支持。")
ALARM_UNSUPPORTED = (
    "该控制器没有蜂鸣器（storcli 报 Alarm ABSENT），开启、关闭与静音都无对象可操作。")


def controller_capabilities(alarm_state, enclosures, drive_paths):
    """What the card can physically do, from what storcli already reported.

    ``alarm_state`` is the `show alarm` token, ``enclosures`` the Enclosure LIST
    rows and ``drive_paths`` the slot paths from the drive list.
    """
    alarm = str(alarm_state or "-").strip().upper()
    return {
        # "Alarm ABSENT" is storcli saying there is no buzzer to talk to.
        "alarm": alarm not in ("ABSENT", "N/A", "NA", "-", ""),
        # A locate LED is driven through the SES enclosure; direct-attached
        # drives (paths without /eN) have no such channel.
        "locate": bool(enclosures) or any(
            "/e" in str(path or "") for path in (drive_paths or [])),
    }


def action_unavailable(name, capabilities):
    """Why `name` cannot run on this controller, or "" when it can.

    `capabilities` is None before the first collection: unknown is not the same
    as unsupported, so nothing is blocked until the hardware has been probed.
    """
    if not capabilities:
        return ""
    if name in ALARM_ACTIONS and not capabilities.get("alarm"):
        return ALARM_UNSUPPORTED
    if name in LOCATE_ACTIONS and not capabilities.get("locate"):
        return LOCATE_UNSUPPORTED
    return ""


class ActionError(ValueError):
    """The request was rejected before anything was executed."""


# Accepted range per tunable, as (min, max, type, human label).
CONFIG_RANGES = {
    # Floor of 3s: each poll costs four controller round-trips, and storcli's
    # own `show all` is a heavyweight command.
    "poll_interval": (3, 3600, int, "刷新间隔（秒）"),
    "temp_warn": (0, 120, int, "温度预警阈值（°C）"),
    "temp_bad": (0, 120, int, "温度告警阈值（°C）"),
    "event_limit": (1, 500, int, "事件条数上限"),
    "phy_warn_rate": (0.0, 1e6, float, "链路误码预警阈值"),
    "phy_bad_rate": (0.0, 1e6, float, "链路误码告警阈值"),
    "storcli_log_max_mb": (1, 4096, int, "storcli 日志上限（MB）"),
}


def _as_number(value, default, cast=float):
    """Tolerant numeric coercion.

    Values coming out of config.json are used as divisors/thresholds all over
    the summary; a hand-edited file must not be able to take the whole endpoint
    down with a ValueError.
    """
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def validate_storcli_path(raw):
    """Only accept a root-owned executable that others cannot write to.

    The daemon runs as root and executes whatever this path points at, so
    accepting something like /tmp/storcli64 would hand root code execution to
    anyone able to drop a file there -- including a non-root member of the
    fnOS administrators group, who can reach this endpoint.
    """
    path = str(raw or "").strip()
    if not path:
        return ""                      # empty means "auto-detect"
    resolved = os.path.realpath(path)
    if not os.path.isabs(resolved) or not os.path.isfile(resolved):
        raise ActionError("storcli 路径不存在：%s" % path)
    if not os.access(resolved, os.X_OK):
        raise ActionError("storcli 路径不可执行：%s" % resolved)
    try:
        info = os.stat(resolved)
    except OSError as exc:
        raise ActionError("无法读取 storcli 路径：%s" % exc)
    if info.st_uid != 0:
        raise ActionError("为安全起见，storcli 必须是 root 拥有的文件：%s" % resolved)
    if info.st_mode & 0o022:
        raise ActionError("为安全起见，storcli 不能被同组或其他用户写入：%s" % resolved)
    return resolved


def coerce_config(changes, current):
    """Validate and type-check a config patch.

    Raises ActionError with a message the UI can show.  Keys outside
    DEFAULT_CONFIG are dropped: paths and thresholds stay server-side, so a
    crafted request cannot widen the trust boundary.
    """
    cleaned = {}
    for key, raw in changes.items():
        if key == "storcli":
            cleaned["storcli"] = validate_storcli_path(raw)
            continue
        spec = CONFIG_RANGES.get(key)
        if spec is None:
            continue
        low, high, cast, label = spec
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            raise ActionError("%s 必须是%s" % (label, "整数" if cast is int else "数字"))
        if not low <= value <= high:
            raise ActionError("%s 必须在 %s-%s 之间" % (label, low, high))
        cleaned[key] = value

    merged = dict(current)
    merged.update(cleaned)
    if merged["phy_warn_rate"] > merged["phy_bad_rate"]:
        raise ActionError("链路误码的预警阈值不能大于告警阈值")
    if merged["temp_warn"] > merged["temp_bad"]:
        raise ActionError("温度的预警阈值不能大于告警阈值")
    return cleaned


def action_catalogue():
    """Describe every action to the UI, including the guard rails.

    The flags are advisory for rendering only -- the same rules are enforced
    again server-side in prepare_action(), because a client is not a boundary.
    """
    out = []
    for entry in ACTION_TABLE:
        name = entry["name"]
        out.append({
            "name": name,
            "label": entry["label"],
            "risk": entry["risk"],
            "target": entry.get("target", "none"),
            "value": entry.get("value", "none"),
            "needs_password": entry["risk"] == "danger",
            "requires": "password" if entry["risk"] == "danger" else "none",
            # Blocked while the controller already has a background job running.
            "blocked_when_busy": name in BG_CONFLICT_ACTIONS,
            # Needs the target name typed out when the array is degraded.
            "last_redundancy_risk": name in LAST_REDUNDANCY_ACTIONS,
        })
    return out


def build_action(name, target, value, password):
    """Validate and translate a UI action into a storcli argv.

    Returns (argv, entry).  Raises ActionError for anything that does not pass
    validation -- nothing reaches a subprocess from here on.

    For `danger` actions this only checks that a password was supplied; the
    actual verification against PAM happens in RaidWatch.run_action.
    """
    entry = ACTIONS.get(name)
    if entry is None:
        raise ActionError("未知操作：%s" % name)

    needs_target = entry.get("target", "none")
    if needs_target == "none":
        if target:
            raise ActionError("该操作不接受目标参数")
        target = CTRL
    else:
        target = (target or "").strip()
        if not TARGET_RE.match(target):
            raise ActionError("非法的目标路径：%s" % (target or "(空)"))
        if needs_target == "drive" and not DRIVE_RE.match(target):
            raise ActionError("该操作需要指定物理盘（/c0/eN/sN）")
        if needs_target == "vd" and not VD_RE.match(target):
            raise ActionError("该操作需要指定虚拟盘（/c0/vN）")

    value_kind = entry.get("value", "none")
    if value_kind == "percent":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ActionError("参数必须是整数")
        if not 0 <= value <= 100:
            raise ActionError("参数必须在 0-100 之间")
    elif value_kind == "toggle":
        value = str(value or "").strip().lower()
        if value not in ("on", "off"):
            raise ActionError("参数必须是 on 或 off")
    else:
        value = None

    if entry["risk"] == "danger" and not password:
        raise ActionError("该操作需要输入账户密码")

    return entry["args"](target, value), entry


# --------------------------------------------------------------------------- #
# application object
# --------------------------------------------------------------------------- #

def merge_power_backup(base, detail):
    """Fold the detailed probe into the one-line summary from `show all`.

    The summary is often the only place that knows the *kind* (CacheVault vs
    BBU), while the probe knows the electrical numbers; neither is complete on
    its own.  Placeholder values never overwrite a real one.
    """
    merged = dict(base or {})
    for key, value in (detail or {}).items():
        if value in (None, "", "-"):
            continue
        merged[key] = value
    return merged


def power_backup_advisories(cv):
    """Warnings derived from the power-backup object, if the card reports one.

    Pure function so the wording rules can be tested without a controller:
    the caller merges these into the summary's advisory list.
    """
    cv = cv or {}
    is_bbu = str(cv.get("kind") or "").strip().lower() == "bbu"
    backup_name = "电池（BBU）" if is_bbu else "CacheVault"
    backup_part = "电池" if is_bbu else "超级电容"
    state = str(cv.get("state") or "-").strip()
    # A battery in its learn cycle holds no reserve; that is one advisory, not
    # a "not Optimal" warning plus a learn notice stacked on top of it.
    learning = is_bbu and state.lower() in ("learning", "relearn", "in progress")
    out = []
    if state not in ("Optimal", "-", "") and not learning:
        out.append({
            "level": "warn",
            "title": "%s 非 Optimal（%s）" % (backup_name, state),
            "body": "写回缓存中的数据在掉电时不再受保护。请尽快处理%s。" % backup_part,
        })
    if str(cv.get("replacement_required") or "").strip().lower() in ("yes", "true"):
        out.append({
            "level": "bad",
            "title": "%s 提示需要更换" % backup_name,
            "body": "模块已标记 Replacement required = Yes，掉电时写缓存数据有丢失风险。",
        })
    # During a learn cycle a battery holds no usable reserve, so write-back is
    # unprotected even though the controller reports no fault.
    if learning:
        out.append({
            "level": "info",
            "title": "电池正在学习周期（%s）" % state,
            "body": "学习期间电池不提供掉电保护；此时依赖写回缓存并不安全。",
        })
    return out


class RaidWatch:
    def __init__(self, config, require_admin):
        self.config = config
        self.require_admin = require_admin
        self.sampler = PhySampler()
        self._storcli_cache = (None, None)  # (configured, resolved)
        self._last_identity = {}
        self._state_lock = threading.Lock()

        # Background job registry (guarded by _state_lock).
        self._jobs = {}
        self._job_order = []
        self._job_seq = 0

        # Controller event log cache: {ts, ok, fetched, rows} or None.
        self._events_cache = None
        self._events_lock = threading.Lock()

        # Last collection, so concurrent/frequent polls share one storcli run.
        self._summary_lock = threading.Lock()
        self._summary_cache = None
        self._summary_ts = 0.0

        # Filled by _collect_summary(); read by the action guards.
        self._bg_active = False
        self._critical_slots = {}
        # None until the first collection: the guards stay out of the way
        # rather than refusing an action whose hardware support is unknown.
        self._capabilities = None
        raidcore.set_log_limit_mb(
            self.config.snapshot().get("storcli_log_max_mb"))

        # Create the dedicated PAM service definition (needs root, which the
        # daemon has). Danger actions are refused when PAM is unusable, so this
        # is a hard dependency rather than an optional nicety.
        ok, message = pam_verify.ensure_service_file()
        self.pam_ready = pam_verify.available() and ok
        if not self.pam_ready:
            sys.stderr.write("警告：二次密码验证不可用 —— %s%s\n" % (
                message,
                "" if pam_verify.available() else "（%s）" % pam_verify.unavailable_reason()))
        else:
            sys.stderr.write("二次密码验证已就绪（PAM 服务：%s）\n" % pam_verify.DEFAULT_SERVICE)

    def apply_log_limit(self):
        """Push the configured storcli.log cap into the collector."""
        return raidcore.set_log_limit_mb(
            self.config.snapshot().get("storcli_log_max_mb"))

    # -- storcli resolution ------------------------------------------------ #
    def storcli(self):
        configured = (self.config.snapshot().get("storcli") or "").strip()
        with self._state_lock:
            cached_conf, cached_path = self._storcli_cache
            if cached_path and cached_conf == configured:
                return cached_path
        resolved = raidcore.find_storcli(configured or None)
        with self._state_lock:
            self._storcli_cache = (configured, resolved)
        return resolved

    def require_storcli(self):
        binary = self.storcli()
        if not binary:
            raise StorcliError(
                "未找到 storcli。请先安装 Broadcom StorCLI "
                "（例如解包 storcli 包后将其放到 /opt/MegaRAID/storcli/storcli64），"
                "或在应用设置中指定路径。")
        return binary

    # -- identity / audit -------------------------------------------------- #
    def note_identity(self, identity):
        with self._state_lock:
            self._last_identity = dict(identity)

    def audit(self, identity, action, target, value, ok, detail):
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user": strip_control(identity.get("username") or "-", 80) or "-",
            "userid": strip_control(identity.get("userid") or "-", 40) or "-",
            "action": strip_control(action, 60),
            "target": strip_control(target or "-", 60) or "-",
            "value": "-" if value is None else strip_control(value, 60),
            "result": "ok" if ok else "fail",
            # storcli output is multi-line; the JSON wrapper would escape the
            # newlines anyway, but dropping them keeps the file readable.
            "detail": strip_control((detail or "").strip(), 500),
        }
        path = os.path.join(self.config.etc_dir, "audit.log")
        try:
            os.makedirs(self.config.etc_dir, exist_ok=True)
            self._rotate_audit(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                # The audit trail is the evidence that a dangerous action was
                # authorised; make sure it survives a power cut.
                os.fsync(handle.fileno())
        except OSError:
            pass

    @staticmethod
    def _rotate_audit(path):
        """Keep the trail bounded: audit.log -> audit.log.1 -> gone."""
        try:
            if os.path.getsize(path) < AUDIT_MAX_BYTES:
                return
        except OSError:
            return
        for index in range(AUDIT_KEEP, 0, -1):
            older = "%s.%d" % (path, index)
            newer = path if index == 1 else "%s.%d" % (path, index - 1)
            try:
                if os.path.exists(older):
                    os.unlink(older)
                if os.path.exists(newer):
                    os.rename(newer, older)
            except OSError:
                pass

    def recent_audit(self, limit=100):
        """Read just the tail of the trail instead of the whole file."""
        path = os.path.join(self.config.etc_dir, "audit.log")
        limit = max(1, int(limit or 100))
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        chunk = 64 * 1024
        data = b""
        try:
            with open(path, "rb") as handle:
                while size > 0 and data.count(b"\n") <= limit:
                    step = min(chunk, size)
                    size -= step
                    handle.seek(size)
                    data = handle.read(step) + data
        except OSError:
            return []
        lines = data.decode("utf-8", "replace").splitlines()
        return [line for line in lines if line.strip()][-limit:]

    # -- event log cache --------------------------------------------------- #
    def events(self, limit=60, allow_refresh=True):
        """Controller events, with both positive and negative caching.

        The event log is a ~15 MB blob that storcli re-renders on every call, so
        reading it per poll was a large slice of the refresh latency.  Failures
        are cached too: without that, a controller that is slow to answer would
        be handed this whole read again on every single poll.

        ``allow_refresh=False`` is used while a background job is running, so
        the log read never competes with a rebuild.
        """
        want = max(1, min(int(limit or 60), 500))
        now = time.monotonic()
        with self._events_lock:
            entry = self._events_cache
            if entry:
                age = now - entry["ts"]
                if entry["ok"] and age < EVENT_CACHE_TTL and entry["fetched"] >= want:
                    return entry["rows"][:want]
                if not entry["ok"] and age < EVENT_ERROR_TTL:
                    return entry["rows"][:want]

        if not allow_refresh:
            return entry["rows"][:want] if entry else []

        binary = self.require_storcli()
        fetch = max(want, 60)
        try:
            rows = raidcore.gather_events(binary, limit=fetch)
        except StorcliError:
            with self._events_lock:
                stale = (self._events_cache or {}).get("rows") or []
                self._events_cache = {"ts": now, "ok": False,
                                      "fetched": fetch, "rows": stale}
            if stale:
                # Serve the last good copy rather than blanking the panel and
                # re-reading 15 MB on the next poll.
                return stale[:want]
            raise
        with self._events_lock:
            self._events_cache = {"ts": now, "ok": True,
                                  "fetched": fetch, "rows": rows}
        return rows[:want]

    def invalidate_events(self):
        with self._events_lock:
            self._events_cache = None

    # -- background jobs --------------------------------------------------- #
    def _prune_jobs(self):
        """Drop old finished jobs. Caller must hold _state_lock."""
        cutoff = time.time() - JOB_TTL
        keep = []
        for job_id in self._job_order:
            job = self._jobs.get(job_id)
            if job is None:
                continue
            finished = job.get("finished")
            settled = job["state"] in ("done", "failed")
            if settled and finished and finished < cutoff:
                self._jobs.pop(job_id, None)
                continue
            keep.append(job_id)
        # Trim the oldest entries, but never evict a queued/running job: its
        # worker looks the record up again when it finishes and would silently
        # drop the result.  Evicting by position alone could do exactly that
        # once more than JOB_KEEP jobs exist.
        overflow = len(keep) - JOB_KEEP
        if overflow > 0:
            for job_id in list(keep):          # oldest first
                if overflow <= 0:
                    break
                job = self._jobs.get(job_id)
                if job and job["state"] in ("done", "failed"):
                    self._jobs.pop(job_id, None)
                    overflow -= 1
            keep = [job_id for job_id in keep if job_id in self._jobs]
        self._job_order = keep

    @staticmethod
    def _job_view(job):
        end = job.get("finished") or time.time()
        view = dict(job)
        view["elapsed"] = round(max(0.0, end - job["started"]), 1)
        view["started_at"] = time.strftime("%H:%M:%S", time.localtime(job["started"]))
        view["argv"] = list(job.get("argv") or [])
        return view

    def job_view(self, job_id):
        with self._state_lock:
            job = self._jobs.get(job_id)
            return self._job_view(job) if job else None

    def jobs_snapshot(self, limit=12):
        with self._state_lock:
            ids = [i for i in self._job_order if i in self._jobs][-limit:]
            return [self._job_view(self._jobs[i]) for i in reversed(ids)]

    def _new_job(self, identity, entry, target, value, argv):
        label = entry.get("label", entry["name"])
        if target and target != CTRL:
            label = "%s %s" % (label, raidcore.slot_shortname(target) or target)
        with self._state_lock:
            self._job_seq += 1
            job_id = "j%d" % self._job_seq
            job = {
                "id": job_id,
                "name": entry["name"],
                "label": entry.get("label", entry["name"]),
                "summary": label,
                "target": target,
                "value": value,
                "user": identity.get("username") or "-",
                "state": "queued",
                "started": time.time(),
                "finished": None,
                "elapsed": 0.0,
                "result": None,
                "error": None,
                "argv": [os.path.basename(argv[0])] + list(argv[1:]) if argv else [],
            }
            self._jobs[job_id] = job
            self._job_order.append(job_id)
            self._prune_jobs()
            return self._job_view(job)

    def _job_worker(self, job_id, identity, entry, argv, target, value, note):
        with self._state_lock:
            job = self._jobs.get(job_id)
            if job:
                job["state"] = "running"
        try:
            result = self._execute(identity, entry, argv, target, value, note)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as job.error
            with self._state_lock:
                job = self._jobs.get(job_id)
                if job:
                    job["state"] = "failed"
                    job["error"] = str(exc)
                    job["finished"] = time.time()
            self.invalidate_events()
            return
        with self._state_lock:
            job = self._jobs.get(job_id)
            if job:
                job["state"] = "done"
                job["result"] = result
                job["finished"] = time.time()
        self.invalidate_events()

    def prepare_action(self, identity, name, target, value, password, confirm=None):
        """Validate the request and verify the password, before anything runs.

        Returns ``(entry, argv, verified_note)``.  Raises ActionError for
        anything that must never reach a subprocess.
        """
        entry = ACTIONS.get(name)
        try:
            argv, entry = build_action(name, target, value, password)
        except ActionError as exc:
            self.audit(identity, name, target, value, False, str(exc))
            raise

        # --- hardware the controller simply does not have -------------------
        # Refuse before anything reaches storcli: a card without a buzzer, or a
        # direct-attached drive with no SES channel, can never satisfy these,
        # and letting storcli fail produces a red error that reads like a bug.
        with self._state_lock:
            capabilities = self._capabilities
        unavailable = action_unavailable(entry["name"], capabilities)
        if unavailable:
            self.audit(identity, name, target, value, False, unavailable)
            raise ActionError(unavailable)

        # --- vendor rule: never overlap state changes with a background job ---
        # Broadcom's guidance is not to issue further configuration changes while
        # a rebuild / copyback / consistency check is running: on a degraded
        # array that is how a recoverable incident turns into data loss.
        # Stopping or pausing the running job stays available.
        if entry["name"] in BG_CONFLICT_ACTIONS and self.background_active():
            self.audit(identity, name, target, value, False, "后台任务进行中，已拒绝")
            raise ActionError(
                "控制器当前正在执行后台任务（重建 / 回拷 / 一致性校验等）。"
                "按控制器厂商规范，此时下发状态变更可能与后台任务冲突，"
                "导致阵列丢失或数据不可恢复，已拒绝执行。"
                "请等任务结束，或先暂停/停止该任务（停止重建、暂停重建等操作仍然可用）。")

        # --- last redundancy: removing a member of an already degraded array ---
        if entry["name"] in LAST_REDUNDANCY_ACTIONS:
            with self._state_lock:
                critical = dict(self._critical_slots)
            disk_group = critical.get(target)
            if disk_group is not None:
                expect = raidcore.slot_shortname(target) or target
                if str(confirm or "").strip().lower() != expect.lower():
                    self.audit(identity, name, target, value, False,
                               "二次确认缺失（阵列已降级）")
                    raise ActionError(
                        "阵列已经降级（磁盘组 %s）。%s 是该阵列的成员盘，"
                        "移除它会让阵列彻底失去冗余、数据不可恢复。\n"
                        "如果确实要继续，请在“确认”框中输入 %s 后再次提交。"
                        % (disk_group, expect, expect))

        # Danger actions are gated on a fresh password check, every single time.
        verified_note = ""
        if entry["risk"] == "danger":
            username = str(identity.get("username") or "").strip()
            if not username:
                self.audit(identity, name, target, value, False, "无法确定当前登录用户")
                raise ActionError("无法确定当前登录用户，已拒绝执行危险操作")
            ok, message = self.verify_password(username, password)
            if not ok:
                self.audit(identity, name, target, value, False, "密码验证失败：%s" % message)
                raise ActionError("密码验证失败：%s" % message)
            verified_note = "密码已验证(%s); " % username
        return entry, argv, verified_note

    def _execute(self, identity, entry, argv, target, value, note):
        """Run the storcli command and audit the outcome."""
        binary = self.require_storcli()
        rc, out = raidcore.run_storcli(binary, argv)
        success = "status = success" in out.lower()

        description = ""
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("description ="):
                description = stripped.split("=", 1)[1].strip()

        detail = description
        if not detail:
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            detail = lines[0] if lines else ""
        self.audit(identity, entry["name"], target, value, success, (note or "") + detail)

        if not success:
            raise StorcliError(detail or "storcli 执行失败")
        return {
            "action": entry["name"],
            "label": entry.get("label", entry["name"]),
            "target": target,
            "requested_value": value,
            "description": description,
        }

    def run_action(self, identity, name, target, value, password, confirm=None):
        """Synchronous execution (kept for internal callers)."""
        entry, argv, note = self.prepare_action(identity, name, target, value,
                                                password, confirm)
        return self._execute(identity, entry, argv, target, value, note)

    def start_job(self, identity, name, target, value, password, confirm=None):
        """Validate + verify, then run the command in a background thread.

        Returns the job record straight away so the UI can show progress
        instead of holding a request open for the length of a rebuild stop.
        """
        entry, argv, note = self.prepare_action(identity, name, target, value,
                                                password, confirm)
        # Fail fast (and synchronously) on a missing storcli.
        self.require_storcli()

        job = self._new_job(identity, entry, target, value, argv)
        worker = threading.Thread(
            target=self._job_worker,
            args=(job["id"], identity, entry, argv, target, value, note),
            name="rw-%s" % job["id"],
            daemon=True,
        )
        worker.start()
        return job

    # -- composite collectors --------------------------------------------- #
    #
    # Only these states can be mid-operation; `show rebuild` answers "Not in
    # progress" for everything else, so polling every drive would just add
    # pointless controller round-trips.  Copyback/recovery are included so their
    # progress is visible too, not only a full rebuild.
    REBUILD_WATCH_STATES = {"rbld", "rebuild", "cpyb", "copyb", "rec"}

    def _rebuild_targets(self, drives):
        targets = []
        for slot_path, drive in drives.items():
            state = str(drive.get("State", "")).strip().lower()
            if state in self.REBUILD_WATCH_STATES:
                targets.append(slot_path)
        return targets

    def summary(self):
        """One collection, shared by every caller in a short window.

        Two open tabs, or an impatient operator hammering refresh, must not each
        run four storcli commands: the controller is the bottleneck, and
        management traffic competes with whatever the array is already doing.
        Callers arriving during a collection simply wait and get its result.
        """
        now = time.monotonic()
        with self._summary_lock:
            cached = self._summary_cache
            if cached is not None and (now - self._summary_ts) < MIN_COLLECT_GAP:
                return dict(cached)
            data = self._collect_summary()
            self._summary_cache = data
            self._summary_ts = time.monotonic()
            # Shallow copy: the request handler decorates the top level with the
            # caller's identity, which must not leak into the next caller.
            return dict(data)

    def background_active(self):
        """True when the controller already has an operation in flight."""
        with self._state_lock:
            return self._bg_active

    def _collect_summary(self):
        binary = self.require_storcli()
        settings = self.config.snapshot()

        overview = raidcore.gather_overview(binary)
        # The buzzer state lives outside `show all`; one extra cheap call.
        try:
            alarm = raidcore.gather_alarm(binary)
            if alarm and alarm != "-":
                overview["controller"]["alarm"] = alarm
        except StorcliError:
            pass
        # `show all` only carries a one-line summary; the energy, capacitance,
        # module firmware and FRU numbers need their own call.  The collector
        # probes /cN/cv (CacheVault) first and falls back to /cN/bbu, so a
        # battery-backed card such as the H840 reports here too.
        try:
            detail = raidcore.gather_battery(binary)
            if detail:
                overview["cachevault"] = merge_power_backup(
                    overview["cachevault"], detail)
        except StorcliError:
            pass
        drives = raidcore.gather_drives(binary)
        # PHY counters and rebuild progress are auxiliary: if either call fails
        # the page should still render controller and drive state, so these
        # degrade to empty rather than failing the whole collection.
        warnings = []
        try:
            phy = raidcore.gather_phy(binary)
        except StorcliError as exc:
            phy = {}
            warnings.append("链路误码计数暂不可用：%s" % exc)
        rates = self.sampler.update(phy)

        try:
            rebuild = raidcore.gather_rebuild(binary, self._rebuild_targets(drives))
        except StorcliError as exc:
            rebuild = {}
            warnings.append("重建进度暂不可用：%s" % exc)

        # Merge per-drive detail, PHY counters, PHY rates and rebuild progress
        # into one row per physical drive.
        merged = {}
        for slot_path, drive in drives.items():
            short = raidcore.slot_shortname(slot_path) or slot_path
            counters = phy.get(slot_path, {})
            rate = rates.get(slot_path)
            row = dict(drive)
            row["slot_path"] = slot_path
            row["short"] = short
            row["phy"] = {
                "inval": counters.get("inval", 0),
                "disp": counters.get("disp", 0),
                "los": counters.get("los", 0),
                "rst": counters.get("rst", 0),
                "inval_per_s": (rate or {}).get("inval_per_s"),
                "disp_per_s": (rate or {}).get("disp_per_s"),
                "los_per_s": (rate or {}).get("los_per_s"),
                "los_delta": (rate or {}).get("los_delta"),
                "interval": (rate or {}).get("interval"),
            }
            if slot_path in rebuild:
                row["rebuild"] = rebuild[slot_path]
            merged[short] = row

        # Health verdict per drive, mirroring the thresholds the UI colours by.
        # Read defensively: config.json can be edited by hand.
        warn_rate = _as_number(settings.get("phy_warn_rate"), 5.0, float)
        bad_rate = _as_number(settings.get("phy_bad_rate"), 50.0, float)
        temp_warn = _as_number(settings.get("temp_warn"), 50, int)
        temp_bad = _as_number(settings.get("temp_bad"), 58, int)
        for short, row in merged.items():
            issues = []
            state = str(row.get("State", "")).strip().lower()
            if state not in ("onln", "rbld"):
                issues.append("state:%s" % row.get("State"))
            rate_s = row["phy"].get("inval_per_s")
            if rate_s is not None and rate_s >= bad_rate:
                issues.append("phy:bad")
            elif rate_s is not None and rate_s >= warn_rate:
                issues.append("phy:warn")
            if row["phy"].get("los_delta"):
                issues.append("phy:lostsync")
            temp = row.get("temperature") or 0
            if temp >= temp_bad:
                issues.append("temp:bad")
            elif temp >= temp_warn:
                issues.append("temp:warn")
            if row.get("media_errors"):
                issues.append("media")
            if str(row.get("smart_alert", "")).strip().lower() in ("yes", "true"):
                issues.append("smart")
            bad = [i for i in issues if i.endswith(":bad")]
            row["issues"] = issues
            row["health"] = "bad" if bad else ("warn" if issues else "ok")

        # Array level verdict.  VD LIST has no member count, so derive it from
        # the disk-group column of PD LIST.
        dg_counts = {}
        for pd_row in overview["pds"]:
            disk_group = str(raidcore.clean(pd_row.get("DG"))).strip()
            if disk_group not in ("-", ""):
                dg_counts[disk_group] = dg_counts.get(disk_group, 0) + 1

        vds = []
        for vd in overview["vds"]:
            row = dict(vd)
            state = str(row.get("State", "")).strip().lower()
            row["degraded"] = state not in ("optl", "optimal")
            disk_group = str(row.get("DG/VD", "")).split("/")[0].strip()
            row["drive_count"] = dg_counts.get(disk_group, "-")
            row["strip"] = overview["controller"].get("default_strip", "-")
            vds.append(row)
        overview["vds"] = vds

        degraded = [v for v in vds if v["degraded"]]
        failing = [d for d in merged.values() if d["health"] == "bad"]
        warning = [d for d in merged.values() if d["health"] == "warn"]

        if degraded or failing:
            overall = "bad"
        elif warning:
            overall = "warn"
        else:
            overall = "ok"

        # --- is anything already running on the controller? ------------- #
        busy_slots = sorted(
            d["short"] for d in merged.values()
            if str(d.get("State", "")).strip().lower() in BUSY_DRIVE_STATES)
        busy_vds = sorted(
            str(v.get("DG/VD") or "?") for v in vds
            if str(v.get("State", "")).strip().lower() in BUSY_VD_STATES)
        background_active = bool(busy_slots or busy_vds)

        # --- drives whose removal would cost the last redundancy -------- #
        degraded_dgs = set()
        for vd in degraded:
            degraded_dgs.add(str(vd.get("DG/VD", "")).split("/")[0].strip())
        degraded_dgs.discard("")
        critical_slots = {}
        for slot_path, drive in drives.items():
            disk_group = str(raidcore.clean(drive.get("DG"))).strip()
            if disk_group in degraded_dgs:
                critical_slots[slot_path] = disk_group

        # --- consequences the operator should not have to infer -------- #
        # Wording follows what the card actually reports: a supercapacitor on a
        # 9361-8i, a battery on a PERC H840.  Telling an H840 operator to
        # "处理超级电容" for a battery fault is exactly the kind of noise that
        # gets a real warning ignored.
        advisories = power_backup_advisories(overview["cachevault"])
        for vd in degraded:
            policy = str(vd.get("Cache") or "").upper()
            if policy and policy != "-" and "WT" not in policy and "W" in policy:
                advisories.append({
                    "level": "warn",
                    "title": "虚拟盘 %s 降级且启用写回缓存" % vd.get("DG/VD"),
                    "body": "降级阵列 + 写回缓存（%s）在掉电时可能丢失数据，"
                            "维修期间可临时改为 Write-Through。" % policy,
                })
        for name in busy_slots:
            advisories.append({
                "level": "info",
                "title": "磁盘 %s 正在执行后台操作" % name,
                "body": "期间已暂停下发状态变更类操作，避免与厂商规范冲突。",
            })

        # --- how often the UI may poll --------------------------------- #
        base_poll = _as_number(settings.get("poll_interval"), 10, int)
        poll_hint = max(base_poll, BUSY_POLL_MIN) if background_active else base_poll

        # --- what this card can physically do --------------------------- #
        capabilities = controller_capabilities(
            overview["controller"].get("alarm"),
            overview["enclosures"],
            [d.get("slot_path") for d in drives.values()],
        )

        with self._state_lock:
            self._bg_active = background_active
            self._critical_slots = dict(critical_slots)
            self._capabilities = capabilities

        return {
            "ok": True,
            "version": VERSION,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "storcli": binary,
            "interval": self.sampler.peek_interval(),
            "overall": overall,
            "capabilities": capabilities,
            "storcli_log": raidcore.log_stats(),
            "controller": overview["controller"],
            "settings": overview["settings"],
            "cachevault": overview["cachevault"],
            "schedule": overview["schedule"],
            "topology": overview["topology"],
            "vds": overview["vds"],
            "pds": overview["pds"],
            "enclosures": overview["enclosures"],
            "drives": list(merged.values()),
            "thresholds": {
                "phy_warn_rate": warn_rate,
                "phy_bad_rate": bad_rate,
                "temp_warn": temp_warn,
                "temp_bad": temp_bad,
            },
            "poll_hint": poll_hint,
            "background_active": background_active,
            "busy_slots": busy_slots,
            "redundancy": {
                "degraded": bool(degraded_dgs),
                "critical_slots": sorted(critical_slots),
            },
            "advisories": advisories,
            "warnings": warnings,
        }

    def verify_password(self, username, password):
        """Re-authenticate a user against the system PAM stack.

        Fails closed: if PAM cannot be used the caller refuses the action
        rather than silently downgrading the protection.
        """
        ok, message = pam_verify.ensure_service_file()
        if not ok:
            return False, "PAM 服务定义不可用：%s" % message
        if not pam_verify.available():
            return False, "系统 PAM 不可用（%s）" % pam_verify.unavailable_reason()
        return pam_verify.authenticate(pam_verify.DEFAULT_SERVICE, username, password)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "megaraid-console/%s" % VERSION
    protocol_version = "HTTP/1.1"

    # -- logging ----------------------------------------------------------- #
    def address_string(self):
        """Client address, tolerant of Unix sockets.

        Over an AF_UNIX connection ``client_address`` is a bare ``''`` rather
        than the ``(host, port)`` tuple TCP uses, and the base implementation
        indexes into it -- which raises IndexError inside send_response() and
        therefore breaks *every* request. fnOS only ever reaches us over a
        Unix socket, so this is the normal path, not an edge case.
        """
        addr = self.client_address
        if isinstance(addr, tuple) and addr:
            return str(addr[0])
        return str(addr) if addr else "-"

    def log_message(self, fmt, *args):
        # `args` carries the raw request line, which the client controls; without
        # stripping control characters a crafted request could inject fake lines
        # into the app log.
        try:
            sys.stderr.write("%s - %s\n"
                             % (strip_control(self.address_string()),
                                strip_control(fmt % args, 400)))
        except Exception:  # noqa: BLE001 - logging must never break a request
            pass

    # -- helpers ----------------------------------------------------------- #
    @property
    def app(self):
        return self.server.app

    @property
    def base_path(self):
        return self.server.base_path.rstrip("/")

    def identity(self):
        return {
            "userid": self.headers.get("X-Trim-Userid"),
            "username": self.headers.get("X-Trim-Username"),
            "isadmin": self.headers.get("X-Trim-Isadmin"),
        }

    def is_admin(self, identity):
        return str(identity.get("isadmin", "")).strip().lower() in ("1", "true", "yes")

    def _respond(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_out(self, payload, code=200):
        self._respond(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def error_out(self, message, code=400):
        self.json_out({"ok": False, "error": str(message)}, code=code)

    def read_body(self):
        """Read the request body, but never on the client's terms.

        Content-Length is attacker-controlled: passing a huge value straight to
        BufferedReader.read() would make the daemon allocate that much before a
        single byte arrives.  Oversized bodies are drained (so keep-alive stays
        in sync) and then rejected.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._body_oversized = True
            return b""
        if length <= 0:
            return b""
        if length > MAX_BODY:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._body_oversized = True
            return b""
        return self.rfile.read(length)

    def read_json(self):
        raw = getattr(self, "_body", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def relative_path(self):
        from urllib.parse import unquote, urlparse
        path = urlparse(self.path).path
        prefix = self.base_path
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        path = unquote(path)
        if not path.startswith("/"):
            path = "/" + path
        return path

    # -- authorisation ----------------------------------------------------- #
    def authorise(self, write=False):
        """Return an identity dict, or None after having sent a rejection."""
        identity = self.identity()
        self.app.note_identity(identity)
        if identity.get("userid") is None:
            # Hit the socket directly (local debugging); the socket is root-only.
            identity.setdefault("username", "local")
            identity["isadmin"] = "true"
            return identity
        if self.app.require_admin and not self.is_admin(identity):
            self.error_out("需要管理员权限", code=403)
            return None
        if write and not self.is_admin(identity):
            self.error_out("该操作需要管理员权限", code=403)
            return None
        return identity

    # -- static ------------------------------------------------------------ #
    def serve_static(self, rel):
        if rel in ("/", ""):
            rel = "/index.html"
        root = os.path.realpath(self.server.ui_dir)
        target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        if target != root and not target.startswith(root + os.sep):
            self.error_out("forbidden", code=403)
            return
        if not os.path.isfile(target):
            self.error_out("not found", code=404)
            return
        ext = os.path.splitext(target)[1].lower()
        ctype = MIME_TYPES.get(ext, "application/octet-stream")
        if ext in (".html", ".js", ".css"):
            try:
                with open(target, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                self.error_out(str(exc), code=500)
                return
            body = text.replace(BASE_PLACEHOLDER, self.base_path)
            self._respond(200, body, ctype)
            return
        try:
            with open(target, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            self.error_out(str(exc), code=500)
            return
        self._respond(200, data, ctype)

    # -- GET --------------------------------------------------------------- #
    def do_GET(self):
        path = self.relative_path()

        if path.startswith("/api/"):
            identity = self.authorise()
            if identity is None:
                return
            try:
                self.handle_api_get(path, identity)
            except StorcliError as exc:
                self.error_out(exc, code=503)
            except Exception as exc:  # noqa: BLE001 - never leak a traceback to the UI
                self.error_out("内部错误：%s" % exc, code=500)
            return

        self.serve_static(path)

    def handle_api_get(self, path, identity):
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.path).query)

        if path == "/api/ping":
            self.json_out({
                "ok": True,
                "version": VERSION,
                "user": identity.get("username"),
                "isadmin": self.is_admin(identity),
                "require_admin": self.app.require_admin,
            })
            return

        if path == "/api/summary":
            payload = self.app.summary()
            payload["user"] = identity.get("username")
            payload["isadmin"] = self.is_admin(identity)
            self.json_out(payload)
            return

        if path == "/api/actions":
            self.json_out({"ok": True, "actions": action_catalogue()})
            return

        if path == "/api/events":
            limit = raidcore.to_int((query.get("limit") or ["60"])[0], 60)
            limit = max(1, min(limit, 500))
            # While the controller is busy, serve the cached log instead of
            # making it render a 15 MB blob alongside a running rebuild.
            events = self.app.events(limit,
                                     allow_refresh=not self.app.background_active())
            self.json_out({"ok": True, "events": events, "count": len(events)})
            return

        if path == "/api/jobs":
            self.json_out({"ok": True, "jobs": self.app.jobs_snapshot()})
            return

        if path == "/api/job":
            job_id = (query.get("id") or [""])[0].strip()
            if not job_id:
                self.error_out("缺少任务 id", code=400)
                return
            job = self.app.job_view(job_id)
            if job is None:
                # The daemon was restarted, or the job aged out.
                self.error_out("任务不存在（服务可能已重启）", code=404)
                return
            self.json_out({"ok": True, "job": job})
            return

        if path == "/api/foreign":
            binary = self.app.require_storcli()
            self.json_out({"ok": True, "foreign": raidcore.gather_foreign(binary)})
            return

        if path == "/api/audit":
            limit = raidcore.to_int((query.get("limit") or ["100"])[0], 100)
            limit = max(1, min(limit, 1000))
            records = []
            for line in self.app.recent_audit(limit):
                try:
                    records.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
            records.reverse()
            self.json_out({"ok": True, "audit": records})
            return

        if path == "/api/config":
            self.json_out({"ok": True, "config": self.app.config.snapshot(),
                           "resolved_storcli": self.app.storcli()})
            return

        self.error_out("未知接口：%s" % path, code=404)

    # -- POST -------------------------------------------------------------- #
    def do_POST(self):
        # Drain the body before anything can return early: on a keep-alive
        # connection an unread body would be parsed as the next request line.
        self._body_oversized = False
        try:
            self._body = self.read_body()
        except (OSError, ValueError):
            self._body = b""

        path = self.relative_path()
        if not path.startswith("/api/"):
            self.error_out("not found", code=404)
            return

        identity = self.authorise(write=True)
        if identity is None:
            return

        if getattr(self, "_body_oversized", False):
            self.error_out("请求体过大（上限 %d 字节）" % MAX_BODY, code=413)
            return

        try:
            payload = self.read_json()
        except ValueError as exc:
            self.error_out(exc, code=400)
            return

        try:
            if path == "/api/action":
                # Validation and the password check happen synchronously so a
                # bad request still fails immediately; only the storcli call
                # itself is handed to a worker thread.
                job = self.app.start_job(
                    identity,
                    str(payload.get("action") or "").strip(),
                    str(payload.get("target") or "").strip(),
                    payload.get("value"),
                    payload.get("password"),
                    payload.get("confirm"),
                )
                self.json_out({"ok": True, "job": job}, code=202)
                return

            if path == "/api/config":
                changes = payload.get("config")
                if not isinstance(changes, dict):
                    self.error_out("config 必须是对象", code=400)
                    return
                # Type, range and cross-field checks live in coerce_config; a
                # bad value must come back as 400 with a readable message
                # rather than blowing up as an internal error.
                allowed = coerce_config(changes, self.app.config.snapshot())
                updated = self.app.config.update(allowed)
                if "storcli_log_max_mb" in allowed:
                    self.app.apply_log_limit()
                self.app.audit(identity, "set_config", "-", None, True,
                               json.dumps(allowed, ensure_ascii=False))
                self.json_out({"ok": True, "config": updated})
                return

        except ActionError as exc:
            self.error_out(exc, code=400)
            return
        except StorcliError as exc:
            self.error_out(exc, code=503)
            return
        except Exception as exc:  # noqa: BLE001
            self.error_out("内部错误：%s" % exc, code=500)
            return

        self.error_out("未知接口：%s" % path, code=404)


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", None)

if _UnixStreamServer is not None:
    class UnixHTTPServer(socketserver.ThreadingMixIn, _UnixStreamServer):
        daemon_threads = True

        def __init__(self, socket_path, handler, app, ui_dir, base_path):
            self.app = app
            self.ui_dir = ui_dir
            self.base_path = base_path
            super().__init__(socket_path, handler)
else:
    # Windows has no AF_UNIX; TCP mode is used for local development there.
    UnixHTTPServer = None


class TcpHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, app, ui_dir, base_path):
        self.app = app
        self.ui_dir = ui_dir
        self.base_path = base_path
        super().__init__(address, handler)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def resolve_paths(args):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    appdest = args.appdest or os.path.dirname(script_dir)
    etc = args.etc or os.path.join(appdest, "etc")
    ui = args.ui or os.path.join(appdest, "ui")
    sock = args.socket or os.path.join(appdest, "app.sock")
    return appdest, etc, ui, sock


def write_default_config(etc_dir):
    os.makedirs(etc_dir, exist_ok=True)
    path = os.path.join(etc_dir, "config.json")

    if os.path.isfile(path):
        # An existing configuration always wins, even across reinstalls.
        print("config already exists: %s" % path)
        return 0

    detected = raidcore.find_storcli() or ""
    data = dict(DEFAULT_CONFIG)
    data["storcli"] = detected
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=4, ensure_ascii=False)
    print("storcli: %s" % (detected or "(未找到，请在界面中设置路径)"))
    print("wrote %s" % path)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="raidwatchd")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "init-config", "status"])
    parser.add_argument("--appdest")
    parser.add_argument("--etc")
    parser.add_argument("--socket")
    parser.add_argument("--ui")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--require-admin", action="store_true")
    args = parser.parse_args(argv)

    appdest, etc, ui, sock = resolve_paths(args)

    if args.command == "init-config":
        return write_default_config(etc)

    config = Config(etc)
    app = RaidWatch(config, require_admin=args.require_admin)

    if args.command == "status":
        binary = app.storcli()
        print(json.dumps({
            "app": APP_NAME,
            "version": VERSION,
            "storcli": binary,
            "config": config.snapshot(),
            "require_admin": app.require_admin,
        }, ensure_ascii=False))
        return 0

    # --- run ------------------------------------------------------------- #
    if args.host:
        server = TcpHTTPServer((args.host, args.port), Handler, app, ui, args.base)
        address = "%s:%s" % (args.host, args.port)
    else:
        directory = os.path.dirname(sock)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(sock):
            os.unlink(sock)
        # Bind with a restrictive umask so the socket is never briefly
        # world-connectable between bind() and the chmod below.
        old_umask = os.umask(0o177)
        try:
            server = UnixHTTPServer(sock, Handler, app, ui, args.base)
        finally:
            os.umask(old_umask)
        try:
            os.chmod(sock, 0o600)
        except OSError:
            pass
        address = sock

    stop = threading.Event()

    def handle_signal(signum, frame):  # noqa: ARG001
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    binary = app.storcli()
    sys.stderr.write(
        "%s %s listening on %s (storcli=%s, require_admin=%s)\n"
        % (APP_NAME, VERSION, address, binary or "missing", app.require_admin))
    sys.stderr.flush()

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        if not args.host and os.path.exists(sock):
            try:
                os.unlink(sock)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
