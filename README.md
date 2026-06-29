# SnipClicker

SnipClicker is a Windows desktop automation tool that clicks screen elements by matching image targets you save.

It is built for workflows where the same buttons, icons, or visual targets appear repeatedly. Add an image target, choose where SnipClicker should search for it, then start scanning with the app button or global hotkey.

## Download

Download the latest beta from the Releases page:

[Download SnipClicker](https://github.com/NickCampbell91/SnipClicker/releases/latest)

Each release includes `SnipClicker.exe` as a downloadable asset.

## Beta Warning

SnipClicker is beta software. It is currently unsigned, and it uses screen capture, a global hotkey, and mouse control. Windows SmartScreen or antivirus tools may warn about the executable.

Only download SnipClicker from this repository:

```text
https://github.com/NickCampbell91/SnipClicker
```

## Features

- Add targets from a screen snip, clipboard image, or image file.
- Enable, disable, rename, and reorder saved targets.
- Bind searches to a selected window, or set the window to `None` to search the whole screen.
- Select per-target search areas that move with the bound window.
- Clear a search area to return a target to full-window or full-screen scanning.
- Choose left click, double click, or right click per target.
- Set the exact click location inside a target image.
- Crop target images with the built-in crop editor.
- Tune match threshold per target.
- Start or stop scanning with a customizable global hotkey.
- Delay clicks while the cursor is moving, then click once the cursor stops.
- Return the cursor to its previous position after automated clicks.

## Basic Use

1. Open `SnipClicker.exe`.
2. Click `+ Add Target`.
3. Add a target by snipping the screen, pasting an image, or choosing an image file.
4. Select the target and adjust its details if needed.
5. Use `Window` to choose the window SnipClicker should follow, or leave it as `None` for whole-screen scanning.
6. Use `Set Area` if the target should only be searched in one part of the window.
7. Click `Start`, or press the configured hotkey.

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

## Making a Release

GitHub Actions builds the Windows executable automatically. Pushing a version tag creates a GitHub Release and attaches `SnipClicker.exe`.

```powershell
git tag v0.1.2-beta
git push origin v0.1.2-beta
```
