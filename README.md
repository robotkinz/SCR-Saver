# SCR Saver

**Alpha** — Linux tray app that plays Windows `.scr` screensavers with Wine when the computer is idle. It leaves the session unlocked.

This is the first public alpha. Expect rough edges, especially around autoplay, Proton games, and odd `.scr` files.

## Features

- Import `.scr` files, installer `.exe` files, zip archives, and folders
- Play on idle (evdev, so it works on Wayland) or with **Test**
- Pick a display, or play on all of them
- Wine and Proton runners: system Wine, Steam Proton, Proton-GE, Lutris, Heroic, Bottles, PlayOnLinux
- Extra Wine prefixes with CPU slowdown and a global frame cap
- Import a Windows program by EXE (Heroic layouts included) or an installed Steam game
- Record keys, mouse movement, and clicks, then replay them when a demo starts
- Audio duck or mute, optional mute while the frame cap is on, skip a screen that is showing fullscreen video
- systemd user service so it can start with the graphical session

SCR Saver uses its own Wine prefixes under `~/.local/share/scrsaver/`. It does not use `~/.wine`.

## Requirements

- Linux (tested on KDE Plasma 6 Wayland)
- Python 3 with PyQt6 and python-evdev
- Wine
- `wmctrl` and `xdotool`
- Membership in the `input` group (idle detection and autoplay recording)
- Optional: `7z` for zip/exe import, distro `unshield` if you skip the bundled x86_64 helper

Arch / Garuda:

```bash
sudo pacman -S --needed python python-pyqt6 python-evdev wine-staging \
    wmctrl xdotool 7zip
sudo usermod -aG input "$USER"
```

Debian / Ubuntu:

```bash
sudo apt install python3 python3-pyqt6 python3-evdev wine wmctrl xdotool p7zip-full
sudo usermod -aG input "$USER"
```

Log out and back in after the group change.

## Install from source

```bash
./install.sh
scrsaver
```

That puts a launcher in `~/.local/bin`, a desktop entry, and enables the systemd **user** service (`scrsaver.service`).

```bash
systemctl --user status scrsaver.service
systemctl --user restart scrsaver.service
journalctl --user -u scrsaver.service -f
```

**Start SCR Saver when I log in** in the app enables or disables that unit.

Data lives under:

- config: `~/.config/scrsaver/config.json`
- imported files: `~/.local/share/scrsaver/library/`
- private Wine prefix: `~/.local/share/scrsaver/wineprefix/`

Rebuild the frame-pacing helper if you change `native/scrsaver_pace.c`:

```bash
make
```

## Portable archive

```bash
./package.sh
```

Writes `dist/scrsaver-*-linux.tar.gz` with source, the installer, and the x86_64 native helpers. It does **not** include your library or config.

To pack this machine’s imported savers for a USB copy to another Linux PC:

```bash
./package.sh --with-user-data
```

On the other PC:

```bash
tar -xzf scrsaver-*-linux.tar.gz
cd scrsaver-*-linux
./install.sh
```

Unpack onto a Linux filesystem. A new Wine prefix is created there.

## Use

1. **Import .scr** (or drop files on the list). Installer `.exe` files, zip archives, and folders work too.
2. **Test** the selected item. Move the mouse or press a key to stop, unless it is an interactive demo.
3. Enable **Run a screensaver after idle timeout** and pick a timeout.
4. Close the window if you want — it stays in the tray.

Right-click a list item for Wine/Proton, winecfg, Winetricks, Wine uninstaller, interactive demo, and **Record autoplay input…**.

Right-click the tray icon for **Additional Settings**: audio, fullscreen-video skip, interactive exit key, Wine Prefix Wizard, **Manage prefixes**, and a Windows installer into a chosen bottle.

**Manage prefixes** can import a Windows EXE (Heroic games under `~/Games/Heroic` bring their Wine/Proton bottle) or an installed Steam game. After import you can record keys and mouse input; Escape finishes recording and is not replayed. A later dialog can loop the take and choose whether a real key, mouse move, or click stops playback.

The default Wine prefix cannot be removed. Bottles created under `~/.local/share/scrsaver/` can be deleted; imported and Steam bottles are delisted only.

## Command line

```bash
scrsaver                  # window + tray
scrsaver --background     # tray only
scrsaver import FILE.scr
scrsaver --play "GA Saver"
```

## How `.scr` playback works

A `.scr` file is a Windows PE executable. Playback copies it into a private Wine prefix and runs:

```text
wine explorer /desktop=SCRSAVER-0,1920x1080 C:\scrsaver\FILE.SCR /s
```

Each screen gets its own Wine virtual desktop, then the window is placed with `wmctrl` and made fullscreen.

Old Win32 / GDI savers are the expected case. Direct3D or .NET savers may need extra Wine components (`winetricks d3dx9`, Mono, and so on). Configure dialogs use `wine FILE.scr /c` when the saver implements `/c`.

Installer `.exe` files (InstallShield, zip SFX, and similar) are unpacked and every `.scr` inside is added. Microsoft Plus! 98 titles such as Mystery and Inside Your Computer are launchers: they need `WL32DLL.DLL` and the theme data from a real Plus! 98 install. Drop those extra files next to the `.scr`.

## Limits

- Alpha quality. Please report bugs with distro, session (X11/Wayland), Wine/Proton version, and the saver or game involved.
- No session lock and no password prompt.
- Multi-monitor playback is one Wine instance per screen.
- Some savers ignore `/s` or expect a real Windows display driver.
- Idle detection and autoplay recording need the `input` group.
- Autoplay mouse playback is still experimental, especially inside Wine virtual desktops and FPS titles.
- If XScreenSaver is also running, both can fire. Turn it off or give SCR Saver a shorter timeout.

While a saver is on screen, SCR Saver inhibits idle sleep/blanking so the picture is not immediately blanked.

## License

MIT. See [LICENSE](LICENSE).

`native/unshield` is an optional x86_64 build of [unshield](https://github.com/twogood/unshield) for InstallShield cabinets. Other architectures can use the distro `unshield` package.
