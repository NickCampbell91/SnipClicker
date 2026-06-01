# SnipClicker

SnipClicker is a Windows desktop automation tool that clicks screen elements based on saved image targets.

You can add targets by snipping part of the screen, pasting an image, or loading an image file. Each target can be enabled or disabled, assigned a click type, limited to a selected search area, reordered by priority, cropped after creation, and tuned with a match threshold.

## Features

- Add targets from a screen snip, clipboard image, or image file.
- Bind searches to a selected window, or set the window to `None` to search the whole screen.
- Select per-target search areas that move with the bound window.
- Clear a target's search area to return it to full-window or full-screen scanning.
- Choose left click, double click, or right click per target.
- Set a custom click location inside the target image.
- Crop target images with a built-in editor.
- Tune match threshold per target.
- Reorder target priority by dragging rows.
- Start or stop scanning with a customizable global hotkey.
- Delay clicks while the cursor is moving, then click once the cursor stops.
- Return the cursor to its previous position after automated clicks.
- Save diagnostics when repeated clicks suggest a target may not be clicking correctly.

## Run From Source

```powershell
py -m pip install -r requirements.txt
py app.py
```

## Build Locally

```powershell
.\build.bat
```

The executable is created at:

```text
dist\SnipClicker.exe
```

## Download

Download the latest Windows executable from the GitHub Releases page:

```text
https://github.com/NickCampbell91/SnipClicker/releases
```

## GitHub Build

This repository includes a GitHub Actions workflow that builds a Windows executable automatically.

Regular branch builds upload the executable as an Actions artifact. Version tags such as `v0.1.0` create a GitHub Release and attach `SnipClicker.exe` directly to that release.

To make a release:

```powershell
git tag v0.1.0
git push origin v0.1.0
```

## Beta Notes

SnipClicker is beta software. Because it screenshots the screen, listens for a global hotkey, and controls the mouse, Windows SmartScreen or antivirus software may warn about the executable unless it is code-signed.
