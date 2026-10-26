# megaraid-console -- LSI / Broadcom MegaRAID monitoring console for fnOS.
# Copyright (C) 2026 emilzhang
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  It is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; see the LICENSE file for details.

"""pam_verify -- re-authenticate a local account through the system PAM stack.

Used to confirm the logged-in user's password before any operation that can
take a drive or array offline. Pure ctypes: fnOS ships libpam (1.5.2) and this
daemon deliberately has no third-party dependencies.

Two things learned the hard way and encoded below:

*   **Never reuse an existing service name.**  /etc/pam.d/su and /etc/pam.d/sudo
    start with ``auth sufficient pam_rootok.so``; this daemon runs as root, so
    that module short-circuits the whole stack and *every password passes*.
    A dedicated service file is created instead.

*   **The reply buffer must come from libc malloc.**  PAM takes ownership of the
    responses and frees them itself. Handing it memory owned by the Python
    interpreter aborts the process with "free(): invalid pointer".
"""

from __future__ import annotations

import ctypes
import os
import threading

PAM_SUCCESS = 0
PAM_BUF_ERR = 5
PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2

DEFAULT_SERVICE = "megaraid-console"
SERVICE_PATH = "/etc/pam.d/" + DEFAULT_SERVICE

SERVICE_TEMPLATE = """\
# megaraid-console -- 应用内二次密码验证（由应用自动创建，可安全删除）
#
# 关键：这里绝不能出现 pam_rootok.so。
# 本应用以 root 身份运行，若改用 su / sudo 之类已包含 pam_rootok 的服务名，
# 该模块会直接返回成功并短路整条认证链 —— 任何密码都会被放行。
auth     include  common-auth
account  include  common-account
"""

_lock = threading.Lock()
_load_error = None


class _PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class _PamResponse(ctypes.Structure):
    # void* rather than c_char_p so a raw malloc'd address can be stored.
    _fields_ = [("resp", ctypes.c_void_p), ("resp_retcode", ctypes.c_int)]


_CONV_FUNC = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(_PamMessage)),
    ctypes.POINTER(ctypes.POINTER(_PamResponse)),
    ctypes.c_void_p,
)


class _PamConv(ctypes.Structure):
    _fields_ = [("conv", _CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]


_RESPONSE_SIZE = ctypes.sizeof(_PamResponse)

try:
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _libc.malloc.restype = ctypes.c_void_p
    _libc.malloc.argtypes = [ctypes.c_size_t]
    _libc.strdup.argtypes = [ctypes.c_char_p]
    _libc.strdup.restype = ctypes.c_void_p
    _libc.memset.restype = ctypes.c_void_p
    _libc.memset.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]

    _libpam = ctypes.CDLL("libpam.so.0", use_errno=True)
    _libpam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                  ctypes.POINTER(_PamConv), ctypes.POINTER(ctypes.c_void_p)]
    _libpam.pam_start.restype = ctypes.c_int
    _libpam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpam.pam_authenticate.restype = ctypes.c_int
    _libpam.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpam.pam_acct_mgmt.restype = ctypes.c_int
    _libpam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpam.pam_end.restype = ctypes.c_int
    _libpam.pam_strerror.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpam.pam_strerror.restype = ctypes.c_char_p
except OSError as exc:  # pragma: no cover - depends on the appliance
    _libc = None
    _libpam = None
    _load_error = str(exc)


def available():
    """True when the PAM library could be loaded."""
    return _libpam is not None


def unavailable_reason():
    return _load_error or "libpam 不可用"


def ensure_service_file(path=SERVICE_PATH):
    """Create the dedicated PAM service definition if it is missing.

    Returns (ok, message). Needs root, which the daemon already has.
    """
    if os.path.isfile(path):
        return True, "已存在"
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(SERVICE_TEMPLATE)
        os.chmod(path, 0o644)
        return True, "已创建 %s" % path
    except OSError as exc:
        return False, "无法创建 %s：%s" % (path, exc)


def authenticate(service, username, password):
    """Verify username/password against PAM.

    Returns (ok, message):
      (True,  "认证通过")
      (False, "Authentication failure")   -- wrong password
      (False, "..." )                     -- library / account / config problem
    """
    if _libpam is None:
        return False, "libpam 不可用：%s" % unavailable_reason()
    if not username:
        return False, "未提供用户名"
    if password is None or password == "":
        return False, "未提供密码"

    secret = password.encode("utf-8")
    holder = []          # keeps the callback object alive for the call

    def conversation(count, msg_pp, resp_pp, _appdata):
        messages = ctypes.cast(msg_pp, ctypes.POINTER(ctypes.POINTER(_PamMessage)))

        # PAM frees this buffer itself -- it must come from libc malloc.
        block = _libc.malloc(_RESPONSE_SIZE * count)
        if not block:
            return PAM_BUF_ERR
        _libc.memset(block, 0, _RESPONSE_SIZE * count)
        replies = ctypes.cast(block, ctypes.POINTER(_PamResponse))

        for index in range(count):
            style = messages[index].contents.msg_style
            if style in (PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON):
                replies[index].resp = _libc.strdup(secret)
            else:
                replies[index].resp = None
            replies[index].resp_retcode = 0

        resp_pp[0] = replies
        return PAM_SUCCESS

    callback = _CONV_FUNC(conversation)
    holder.append(callback)
    conv = _PamConv(callback, None)
    handle = ctypes.c_void_p()

    with _lock:
        rc = _libpam.pam_start(service.encode(), username.encode(),
                               ctypes.byref(conv), ctypes.byref(handle))
        if rc != PAM_SUCCESS:
            return False, "pam_start 失败（rc=%d，PAM 服务 %s 是否存在？）" % (rc, service)

        status = rc
        try:
            rc = _libpam.pam_authenticate(handle, 0)
            status = rc
            if rc != PAM_SUCCESS:
                text = _libpam.pam_strerror(handle, rc)
                return False, (text.decode() if text else "认证失败")
            rc = _libpam.pam_acct_mgmt(handle, 0)
            status = rc
            if rc != PAM_SUCCESS:
                text = _libpam.pam_strerror(handle, rc)
                return False, "账户校验未通过：%s" % (text.decode() if text else "?")
            return True, "认证通过"
        finally:
            _libpam.pam_end(handle, status)


if __name__ == "__main__":  # tiny self-test
    import sys
    if len(sys.argv) < 3:
        raise SystemExit("usage: pam_verify.py <username> <password> [service]")
    print(ensure_service_file())
    print(authenticate(sys.argv[3] if len(sys.argv) > 3 else DEFAULT_SERVICE,
                       sys.argv[1], sys.argv[2]))
