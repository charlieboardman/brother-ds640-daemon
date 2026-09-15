"""Bounded paper-sensor worker for the bundled brscan5 1.5.1-0 driver.

No scan is started here. The parent releases this process before scanimage opens
the device. Keep ctypes and a possibly blocked driver outside the daemon process.
"""
from __future__ import annotations

import argparse
import ctypes as C
import sys
import time


class Device(C.Structure):
    _fields_ = [(name, C.c_char_p) for name in ("name", "vendor", "model", "type")]


def is_present(state: int) -> bool:
    # Verified by a live DS-640 insertion and the driver's IsAdfInserted wrapper.
    if state not in (2, 3):
        raise RuntimeError(f"Brother paper query failed: state={state}")
    return state == 2


class PaperSensor:
    def __enter__(self):
        self.lib = C.CDLL("libsane-brother5.so.1.0.7", mode=C.RTLD_GLOBAL)
        self.core = C.CDLL("libLxBsScanCoreApi.so.3.2.1")
        self.lib.sane_init.argtypes = [C.POINTER(C.c_int), C.c_void_p]
        self.lib.sane_get_devices.argtypes = [C.POINTER(C.POINTER(C.POINTER(Device))), C.c_int]
        self.lib.sane_open.argtypes = [C.c_char_p, C.POINTER(C.c_void_p)]
        self.lib.sane_close.argtypes = [C.c_void_p]
        self.lib.sane_close.restype = None
        self.lib.sane_exit.argtypes = []
        self.lib.sane_exit.restype = None
        self.query = getattr(self.core, "_ZN6bscore9CScanCore13QueryAdfStateEv")
        self.query.argtypes = [C.c_void_p]
        self.query.restype = C.c_int
        self.handle = C.c_void_p()
        self._check(self.lib.sane_init(C.byref(C.c_int()), None))
        try:
            devices = C.POINTER(C.POINTER(Device))()
            self._check(self.lib.sane_get_devices(C.byref(devices), 1))
            if not devices or not devices[0] or devices[1]:
                raise RuntimeError("Expected exactly one Brother scanner")
            if devices[0].contents.model != b"DS-640":
                raise RuntimeError("Paper sensor supports the DS-640 only")
            self._check(self.lib.sane_open(devices[0].contents.name, C.byref(self.handle)))
            self.instance = C.c_void_p.in_dll(self.lib, "pp")
            if not self.instance.value:
                raise RuntimeError("Brother driver did not initialize its scanner core")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    @staticmethod
    def _check(status: int) -> None:
        if status:
            raise RuntimeError(f"SANE setup failed: status={status}")

    def present(self) -> bool:
        return is_present(self.query(self.instance))

    def __exit__(self, *_):
        if self.handle:
            self.lib.sane_close(self.handle)
        self.lib.sane_exit()


def wait_for_state(sensor, *, empty=False, interval=0.5, timeout=30.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        if sensor.present() == (not empty):
            return 0
        if time.monotonic() >= deadline:
            return 2  # Healthy idle timeout, distinct from a driver error.
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--empty", action="store_true")
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    try:
        with PaperSensor() as sensor:
            return wait_for_state(sensor, empty=args.empty, interval=max(0.1, args.interval))
    except Exception as exc:
        print(f"paper_sensor_error={exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
