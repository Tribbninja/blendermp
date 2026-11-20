# Platforms

Blendermp uses the same Tauri shell and Blender addon on Windows, macOS and
Linux. Window **docking** is Windows-only; on other platforms the app launches
a normal Blender window and hides the dock controls.

## Windows

- Installer: `dist/Blendermp-<version>-Setup.exe` (NSIS).
- Addon: `dist/blender_multiplayer-<version>.zip`.
- TLS vendor: the installer places the cp313 `cryptography` tree in
  `%LOCALAPPDATA%\Blendermp\python`.
- Docking uses an owned top-level window and the Win32 bridge.
- The detached dashboard is served by the local directory listener and opened in the system browser, so it works on every supported platform.
- The NSIS installer registers the current user as the handler for `blender://` and `blendermp://` invite links. A portable copy needs those registry entries added manually.

## macOS

- Build the app with `cargo tauri build` (or the project's release tooling).
- Install the addon with:
  `scripts/install_extension.sh` or
  `Blender.app/Contents/MacOS/Blender --command extension install-file ...`.
- The addon searches `/Applications/Blender.app` and `/Applications/Blender*`.
- Provide a matching `cryptography` wheel for the Python version bundled with
  Blender, or run the app from a build that bundles it. The Windows vendor tree
  is not valid on macOS.
- Code signing/notarization and an updater manifest are release requirements;
  they are not performed by `scripts/package.ps1`.

## Linux

- Build with Tauri's WebKitGTK dependencies (see `.github/workflows/ci.yml`).
- Addon install: `scripts/install_extension.sh`.
- Blender is detected at `/usr/bin/blender`, `/usr/local/bin/blender`,
  `/snap/bin/blender` and the Flatpak export.
- Docking is disabled; Blender runs as a separate window.
- Bundle a platform `cryptography` wheel for Blender's Python.

## Blender compatibility

The addon declares Blender 4.2 LTS or newer. Blender 5.x APIs that changed
(slotted actions, layered F-curves) are feature-detected with a legacy fallback.
CI runs the addon tests on the supported Python versions and the headless
benchmark against Blender 5.3.
