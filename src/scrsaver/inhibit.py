from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class IdleInhibit:
    """Keep the session from blanking or sleeping while a screensaver is on screen."""

    def __init__(self) -> None:
        self._ss_cookie: int | None = None
        self._pm_cookie: int | None = None

    def acquire(self) -> None:
        self.release()
        try:
            import dbus
        except ImportError:
            return
        try:
            bus = dbus.SessionBus()
        except Exception as exc:
            log.debug("No session bus for inhibit: %s", exc)
            return
        try:
            saver = dbus.Interface(
                bus.get_object("org.freedesktop.ScreenSaver", "/ScreenSaver"),
                "org.freedesktop.ScreenSaver",
            )
            self._ss_cookie = int(saver.Inhibit("SCR Saver", "Playing a Windows screensaver"))
        except Exception as exc:
            log.debug("ScreenSaver inhibit failed: %s", exc)
        try:
            power = dbus.Interface(
                bus.get_object(
                    "org.freedesktop.PowerManagement.Inhibit",
                    "/org/freedesktop/PowerManagement/Inhibit",
                ),
                "org.freedesktop.PowerManagement.Inhibit",
            )
            self._pm_cookie = int(power.Inhibit("SCR Saver", "Playing a Windows screensaver"))
        except Exception as exc:
            log.debug("PowerManagement inhibit failed: %s", exc)

    def release(self) -> None:
        try:
            import dbus
        except ImportError:
            self._ss_cookie = None
            self._pm_cookie = None
            return
        try:
            bus = dbus.SessionBus()
        except Exception:
            self._ss_cookie = None
            self._pm_cookie = None
            return
        if self._ss_cookie is not None:
            try:
                saver = dbus.Interface(
                    bus.get_object("org.freedesktop.ScreenSaver", "/ScreenSaver"),
                    "org.freedesktop.ScreenSaver",
                )
                saver.UnInhibit(self._ss_cookie)
            except Exception as exc:
                log.debug("ScreenSaver uninhibit failed: %s", exc)
            self._ss_cookie = None
        if self._pm_cookie is not None:
            try:
                power = dbus.Interface(
                    bus.get_object(
                        "org.freedesktop.PowerManagement.Inhibit",
                        "/org/freedesktop/PowerManagement/Inhibit",
                    ),
                    "org.freedesktop.PowerManagement.Inhibit",
                )
                power.UnInhibit(self._pm_cookie)
            except Exception as exc:
                log.debug("PowerManagement uninhibit failed: %s", exc)
            self._pm_cookie = None
