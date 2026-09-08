"""Encrypt configuration backups for the current Windows account."""
import ctypes
import os
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _convert(data: bytes, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Encrypted configuration backups require Windows")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    destination = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise RuntimeError("Windows could not process the encrypted configuration backup")
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel32.LocalFree(destination.data)


def encrypt(data: bytes) -> bytes:
    return _convert(data, False)


def decrypt(data: bytes) -> bytes:
    return _convert(data, True)
