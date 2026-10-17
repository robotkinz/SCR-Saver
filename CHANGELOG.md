# Changelog

## 0.1.4-alpha — 2026-10-17

- ScreenTime for Flash savers (FLCL and similar) now play the companion projector, not just the Gainax logo
- Mouse and keyboard dismiss a screensaver during Test, not only after idle timeout

## 0.1.3-alpha — 2026-10-17

- Share Wine compatibility scripts: export a prefix as a recipe, import it into any bottle
- Scripts list Winetricks packages and screensaver settings; game scripts carry autoplay only
- Import downloads those packages into the prefix you pick (the highlighted bottle is the default)
- The Wine Prefix Wizard offers the same import after a new bottle is created

## 0.1.2-alpha — 2026-10-16

- Restarting or logging out is no longer treated as a crash

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
