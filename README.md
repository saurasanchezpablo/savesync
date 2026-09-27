# Save Sync

A resident Windows tray application that keeps game saves in sync between a
**USB drive — the single source of truth —** and the PC, with
[Ludusavi](https://github.com/mtkennerly/ludusavi) underneath as the discovery,
backup, restore, versioning and validation engine. It feels like a smart memory
card for PC game saves: plug the USB in, Save Sync analyzes, restores or uploads
what is needed, and shows an aggregate status. You never open Ludusavi's GUI.

```
      💾 USB  (SaveSync\backups — the only permanent copy of the save library)
        ↕
     Save Sync  (tray · status window · Ludusavi CLI)
        ↕
   real PC saves
```

## Safety model

Unknown is never success, and a conflict is never resolved automatically.

| Rule | How it is enforced |
|---|---|
| 1. Unknown error → never "synchronized" | A game's baseline (USB backup identity + local fingerprint) moves only after Ludusavi's JSON confirms success **and** a re-check passes. Exit codes alone are never trusted. |
| 2. Conflict → never overwrite | Both sides changed ⇒ `CONFLICT`; only *Use USB*, *Use PC* or trial mode act. |
| 3. Unknown USB → never used | Marker (`savesync-identity.json`) + volume serial + folder structure must all match; drive letters are irrelevant. |
| 4. Running game → untouched | One process snapshot per cycle (install folder / exe names from Ludusavi's manifest, plus your own names); rechecked right before each operation. |
| 5. USB removed mid-operation | The running Ludusavi command is cancelled, the operation is recorded as pending, versions left behind are marked `savesync:incomplete`. |
| 6. Failed restore → recoverable | Every USB → PC restore is preceded by a local safety snapshot and rolled back automatically on failure. |
| 7. Previous version kept | The last validated USB version is **locked** in Ludusavi before a new backup; the new one is locked only after validation. |

Which USB version counts as "the newest": Ludusavi's own listing order (never
the timestamps, which come from each PC's clock), and — once Save Sync has
validated a version of a game — only validated versions. An unmarked version
after it may be what an upload interrupted on another PC left behind.

Measured Ludusavi 0.31 behaviors that shaped this design (each has a test):
failed backups still become versions and count toward retention; a corrupt
`mapping.yaml` looks like an empty USB; a single unknown title fails a whole
call; one retained full backup is written in place; two full backups in the same
second overwrite each other; restores never delete files.

## Using it

1. Put the portable Ludusavi (**0.30 or newer** — Save Sync needs
   `backups edit`) on the USB: `X:\SaveSync\ludusavi\ludusavi.exe`
   (or set an executable in Settings).
2. Start `SaveSync.exe`. On the first run Settings opens: choose the USB and
   press **Use this drive** — this creates `X:\SaveSync\` and the marker.
3. From then on Save Sync lives in the tray:
   🟢 connected and synchronized · 🟡 pending changes · 🔴 conflict/error ·
   ⚪ USB absent. Left-click opens the status window.
4. On connect (optional) or with **Sync Now**, every game is classified:

   | State | What happens |
   |---|---|
   | Synchronized | nothing |
   | PC → USB | new USB version, validated, then baseline |
   | USB → PC | safety snapshot → restore → validate → baseline |
   | Conflict | nothing — *Use USB* / *Use PC* / *Explore both* |
   | First synchronization | this PC has no baseline yet — the wizard asks |
   | Pending configuration | game on the USB, no valid local path — *Manage* (e.g. map another Windows user's folder) |
   | Game running | nothing — *Wait and synchronize* / *Skip* / *Continue* |
   | Error / Unknown | nothing — logged and shown |

5. **Explore both** (trial mode) lets you test the USB version in the game,
   switch to the PC version, and keep one — or return to the original. Both
   versions are kept locally, so switching works without the USB.

Other features: USB version list per game (restore an older version), history
view of every operation, automatic cleanup of safety snapshots older than 7
days, optional sync at Windows shutdown (off by default, bounded by a deadline),
Start with Windows, notification preferences, optional read-only Playnite
metadata (from a JSON export). Save Sync watches only the known save folders to
notice changes while the USB is away; it never monitors games permanently.

State lives in `%APPDATA%\SaveSync\` (`config.json`, `games.json`,
`events.jsonl`, `safety\`, a private Ludusavi configuration in `ludusavi\`).
None of it contains save contents except the temporary safety snapshots.

## Development

Python ≥ 3.11, PySide6.

```
python -m venv .venv && .venv\Scripts\activate        # or: uv venv
pip install -e ".[dev]"
python -m savesync                                     # tray + window
python -m savesync --fake-usb D:\scratch\usb --data-dir D:\scratch\state
```

`--fake-usb` turns any folder into the USB drive (see
`tests/fixtures/fake_usb/README.md`).

### Tests

| Command | Layer |
|---|---|
| `pytest tests/unit -v` | decision table, engine parsing and lock protocol, fingerprints, registry, orchestrator, trial mode |
| `pytest tests/ui -v` | pytest-qt over the complete app (fake USB + simulated Ludusavi), incl. a threaded end-to-end Sync Now |
| `pytest tests/windows -v` | Windows adapters with fake OS backends; real Win32 checks run on Windows |
| `pytest tests/integration -v --ludusavi-path="C:\path\to\ludusavi.exe"` | real Ludusavi against offline fixtures, safety rules, watcher, app-level round trips |
| `pytest tests/performance -v --ludusavi-path=…` | 10/50/100/200+ games, shutdown sync vs. the 3-minute target |
| `pytest tests -v` | everything (real-Ludusavi tests are skipped without the path) |

Hardware validation: [tests/manual/CHECKLIST.md](tests/manual/CHECKLIST.md).

### Layout

```
src/savesync/
  core/       decisions, state, engine (Ludusavi), fingerprint, registry, sync,
              trial, safety, usb identity, log, messages, notifications
  platform/   interfaces + windows/ (media, process, watcher, startup, shutdown,
              notify, instance), fake.py (tests / --fake-usb), fallback.py (dev on non-Windows)
  metadata/   Ludusavi manifest provider, optional Playnite export provider
  ui/         tray, main window, game detail, conflict, trial, first sync,
              settings, history
```

### Packaging

`packaging\build.ps1` runs the tests and builds a portable one-folder app with
PyInstaller (`dist\SaveSync\SaveSync.exe`) plus a ready USB layout
(`dist\usb-layout\SaveSync\`).

## License

MIT (see `LICENSE`). The synchronization engine is derived from
[decky-nonsteam-sync](https://github.com/JoseArkadio/decky-nonsteam-sync)
(`py_modules/sdsync`), BSD-3-Clause — see `LICENSE-UPSTREAM`; derived files carry
a header naming their origin. Nothing of the Decky frontend is reused.
