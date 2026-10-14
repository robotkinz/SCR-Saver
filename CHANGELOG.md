# Changelog

## 0.1.1-alpha — 2026-10-14

- Tile the screensaver into empty space instead of filling the whole screen
- Option to hide KDE title bars and borders on the tiled window
- Option to scale the full picture down so the whole saver still fits
- KDE panels stay visible — the saver will not cover them
- Window detection now sees windows on every monitor, including the middle and right
- Crash reports: if it dies, a log is saved and the next start tells you where to find it

## 0.1.0-alpha — 2026-10-07

First public alpha.

- Play Windows `.scr` files with Wine when the session is idle
- Import `.scr`, installer `.exe`, zip archives, and folders
- Tray UI with Test, idle timeout, and per-display targeting
- Wine/Proton runner picker (system Wine, Steam Proton, Proton-GE, Lutris, Heroic, Bottles, PlayOnLinux)
- Prefix manager: extra bottles, CPU slowdown, FPS cap, winecfg, Winetricks
- Import a Windows EXE (including Heroic layouts) or an installed Steam game
- Record and replay autoplay keys, mouse movement, and clicks
- Audio duck/mute, optional mute while the frame cap is on, skip fullscreen video
- systemd user service for login autostart
- Portable Linux tarball via `./package.sh`
