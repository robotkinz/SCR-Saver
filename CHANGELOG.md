# Changelog

## 0.1.9-alpha — 2026-10-28

- After Dark savers play on every display; the second screen is no longer a white void
- Optional switch to another checkmarked screensaver after a set time (randomly, or down the list)

## 0.1.8-alpha — 2026-10-26

- After Dark 4 (and 3.x) `.ad` modules show up as their own screensavers; After Dark.scr is only the host
- Updating SCR Saver lists those modules from an After Dark install already in a Wine prefix — no reinstall
- After Dark 4 compatibility script: import `data/profiles/after-dark-4.scrsaver.json`, then run your own CD
- Configure on an After Dark module opens After Dark’s real settings page
- Compatibility scripts can name a Windows product the user installs from their own disc
- HTML After Dark wrappers (the CSS Flying Toasters remake) play in Chromium
- Wine DPI is set per prefix in Manage Wine Prefixes

## 0.1.7-alpha — 2026-10-21

- Hovering a screensaver in the list shows which Wine prefix it uses

## 0.1.6-alpha — 2026-10-21

- About window in the tray menu shows the installed version
- Wine no longer letterboxes old savers after the Graphics tab is used to emulate a virtual desktop

## 0.1.5-alpha — 2026-10-17

- Right-click a screensaver → Wine prefix to copy the .scr and companion files into another bottle
- Importing a compatibility script also copies matching library files into the prefix you pick

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
