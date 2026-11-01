"""Fail-closed OS restrictions applied before decoding untrusted media.

Model download happens in a separate process using operator-selected pinned models.
Linux requires Landlock and libseccomp; macOS uses the Seatbelt sandbox.
"""

import ctypes
import errno
import json
import math
import os
import resource
import socket
import stat
import sys
from pathlib import Path


def restrict_attempt(
    workspace: Path, cache: Path, *, memory_bytes: int, cpu_seconds: float
) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(cpu_seconds), math.ceil(cpu_seconds)))
    # Model downloads can leave pooled HTTP sockets open. Shutdown their connections
    # before restricting new sockets; do not close descriptors owned by libraries.
    for descriptor in range(3, 256):
        try:
            if stat.S_ISSOCK(os.fstat(descriptor).st_mode):
                connection = socket.socket(fileno=descriptor)
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                finally:
                    connection.detach()
        except OSError:
            continue
    # macOS does not implement an effective RLIMIT_AS; Compose also bounds RSS on Linux.
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        _linux(workspace, cache)
    elif sys.platform == "darwin":
        _macos(workspace, cache)
    else:
        raise RuntimeError("Unsupported inference sandbox platform")


def _read_paths(cache: Path) -> set[Path]:
    paths = {
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path(__file__).resolve().parents[1],
        cache.resolve(),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path("/lib"),
        Path("/lib64"),
        Path("/dev/null"),
        Path("/dev/urandom"),
        Path("/sys/devices/system/cpu"),
        Path("/proc/cpuinfo"),
        Path("/proc/meminfo"),
    }
    # Hugging Face snapshots use symlinks to content-addressed blobs. Allow those
    # files individually, without granting access to the cache's tokens or peers.
    paths.update(item.resolve() for item in cache.rglob("*") if item.is_file())
    return paths


def _macos(workspace: Path, cache: Path) -> None:
    paths = _read_paths(cache) | {Path("/System/Library"), Path("/private/var/db/dyld")}
    rules = ["(version 1)", "(deny default)", "(allow sysctl-read)"]
    for path in paths:
        if path.exists():
            rules.append(f"(allow file-read* (subpath {json.dumps(str(path.resolve()))}))")
    rules.append(f"(allow file-read* file-write* (subpath {json.dumps(str(workspace.resolve()))}))")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.c_void_p]
    library.sandbox_init.restype = ctypes.c_int
    if library.sandbox_init("\n".join(rules).encode(), 0, None) != 0:
        raise RuntimeError("Cannot activate macOS inference sandbox")


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _linux(workspace: Path, cache: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    # Landlock syscall numbers are shared by Linux x86_64 and aarch64.
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError("Landlock ABI 3 is required for inference (Linux 6.2+)")
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "Cannot set no_new_privs")
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13  # REFER: cross-directory rename/link
    if abi >= 3:
        handled |= 1 << 14  # TRUNCATE
    if abi >= 5:
        handled |= 1 << 15  # IOCTL_DEV
    ruleset = _Ruleset(handled)
    descriptor = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Cannot create Landlock ruleset")
    try:
        for path in _read_paths(cache) | {workspace.resolve()}:
            if not path.exists():
                continue
            allowed = handled if path == workspace.resolve() else (1 << 2) | (1 << 3)
            if path.is_file() or not path.is_dir():
                allowed &= (1 << 1) | (1 << 2) | (1 << 14) | (1 << 15)
            parent = os.open(path, 0x200000 | os.O_CLOEXEC)  # Linux O_PATH
            try:
                rule = _PathRule(allowed, parent)
                if libc.syscall(445, descriptor, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), "Cannot add Landlock path rule")
            finally:
                os.close(parent)
        if libc.syscall(446, descriptor, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot restrict filesystem")
    finally:
        os.close(descriptor)

    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Cannot create syscall filter")
    try:
        for name in (
            "socket",
            "socketpair",
            "connect",
            "bind",
            "listen",
            "accept",
            "accept4",
            "execve",
            "execveat",
            "fork",
            "vfork",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "pidfd_getfd",
            "pidfd_send_signal",
            "kill",
            "tkill",
            "tgkill",
            "rt_sigqueueinfo",
            "rt_tgsigqueueinfo",
            "bpf",
            "io_uring_setup",
            # Landlock does not mediate metadata mutation; block these separately.
            "chmod",
            "fchmod",
            "fchmodat",
            "fchmodat2",
            "chown",
            "fchown",
            "fchownat",
            "lchown",
            "utime",
            "utimes",
            "futimesat",
            "utimensat",
            "setxattr",
            "lsetxattr",
            "fsetxattr",
            "removexattr",
            "lremovexattr",
            "fremovexattr",
        ):
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and seccomp.seccomp_rule_add(context, 0x50000 | errno.EPERM, number, 0):
                raise RuntimeError(f"Cannot restrict syscall: {name}")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("Cannot activate syscall filter")
    finally:
        seccomp.seccomp_release(context)
