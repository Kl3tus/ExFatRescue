# ExFatRescue 🛡️

**ExFatRescue** is a lightweight, open-source raw forensic file & project recovery tool specifically engineered to recover data from **exFAT storage devices (microSD cards, flash drives, external HDDs)** whose partition tables, boot records, or root directories were destroyed (such as by Windows Media Creation Tool or accidental formatting).

Unlike traditional raw carvers (like PhotoRec) which destroy folder structures and drop nameless files (`f123456.txt`), **ExFatRescue** parses surviving **exFAT directory records (`0x85`, `0xC0`, `0xC1`)** directly from unallocated sectors, restoring:
- **Real UTF-16 Unicode Filenames**
- **Exact File Sizes & Cluster Chains**
- **Full Godot Game Projects** (`.godot`, `.gd`, `.tscn`, `.tres`)
- **3D Assets & Project Workspaces** (`.blend`, `.fbx`, `.obj`)
- **Source Code & Developer Repositories** (`.java`, `.py`, `.cpp`, `.cs`, etc.)
- **Media & Compressed Archives** (`.zip`, `.7z`, `.png`, `.jpg`, `.mp4`)

---

## ✨ Features

- 🖥️ **Modern Dark-Themed GUI:** Built with Python/Tkinter with zero bloat.
- 🎯 **Pre-Recovery Inspection & Selection:** Preview discovered files and folders before extracting. Choose what you want, leave what you don't.
- 🔍 **Instant Search & Category Filters:** Quickly isolate Godot projects, 3D models, code files, or archives.
- ⚡ **Zero External Dependencies:** Built using Python's standard library.
- 🚀 **Standalone Executable:** Ready to run with embedded Windows UAC administrator elevation.

---

## 🛠️ Usage

### Running from Pre-Compiled Executable:
1. Download `ExFatRescue.exe` from Releases.
2. Double-click to launch (approve Windows UAC prompt).
3. Select your target device (e.g., `\\.\PhysicalDrive1`).
4. Set your output destination folder.
5. Click **🔍 Start Deep Scan**.
6. Select the files you want to recover, then click **🚀 Recover Selected Files**.

### Running from Source:
```bash
python exfat_rescue.py
```
*(Note: Requires Administrator privileges to open `\\.\PhysicalDriveX` on Windows).*

---

## 📦 Building Standalone `.exe`

Run `build.bat` or compile with PyInstaller:
```cmd
pyinstaller --onefile --uac-admin --noconsole --add-data "paypal_qr.png;." --name "ExFatRescue" exfat_rescue.py
```

---

## 💖 Support & Donations

If **ExFatRescue** saved your projects, code, or personal data and you would like to support future open-source development, donations are deeply appreciated!

<div align="center">
  <img src="paypal_qr.png" alt="PayPal QR Code" width="200" />
  <br/><br/>
  <a href="https://paypal.me/Kl3tus">
    <img src="https://img.shields.io/badge/Donate-PayPal-blue.svg?style=for-the-badge&logo=paypal" alt="Donate with PayPal" />
  </a>
  <br/>
  <b><a href="https://paypal.me/Kl3tus">paypal.me/Kl3tus</a></b>
</div>

---

## 📄 License
MIT License. Free and open source for the community. Copyright (c) 2026 Mikel Bryan (Kl3tus).
