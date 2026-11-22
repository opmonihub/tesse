"""Nonblocking byte-range locks compatible with SQLite's native OS locks."""

import errno
import os


def lock_bytes(fd, offset, length=1, *, exclusive=False):
    if os.name != "nt":
        import fcntl

        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.lockf(fd, mode | fcntl.LOCK_NB, length, offset)
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                    ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD),
                    ("event", wintypes.HANDLE)]

    lock = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
    lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                     wintypes.DWORD, ctypes.POINTER(Overlapped)]
    lock.restype = wintypes.BOOL
    location = Overlapped(offset=offset & 0xffffffff, offset_high=offset >> 32)
    flags = 1 | (2 if exclusive else 0)  # FAIL_IMMEDIATELY | optional EXCLUSIVE_LOCK
    if not lock(msvcrt.get_osfhandle(fd), flags, 0, length, 0, ctypes.byref(location)):
        code = ctypes.get_last_error()
        if code in {32, 33, 158}:  # sharing/lock violations, segment already locked
            raise BlockingIOError(errno.EAGAIN, "byte-range lock busy")
        raise ctypes.WinError(code)
