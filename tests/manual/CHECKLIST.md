# Save Sync — real USB / Windows validation checklist

Everything that the automated suite cannot prove: a physical USB stick, real
Windows APIs, real games. Run it before a release, on **two Windows PCs** (A and
B) and one USB drive. Record the result of every step (✅ / ❌ + note) and keep the
`events.jsonl` of both PCs (`%APPDATA%\SaveSync\`) with the report.

## 0. Preparation

- [ ] Build: `powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Ludusavi C:\path\ludusavi.exe`
- [ ] Copy `dist\usb-layout\SaveSync\` to the root of the USB drive (gives `X:\SaveSync\ludusavi\ludusavi.exe`).
- [ ] Copy `dist\SaveSync\` to each PC (any folder) and start `SaveSync.exe`.
- [ ] Pick a test game **with a small save** that both PCs have installed (a Steam game with local saves is ideal). Note its save folder (Ludusavi GUI → Backup preview, or the game detail in Save Sync after a sync). Call it *G*.
- [ ] Optional second game *H* whose saves are in the Windows Registry.
- [ ] On each PC, make a manual copy of *G*'s save folder somewhere outside Save Sync (the tester's own safety net).
- [ ] Run the automated Windows-only tests on each PC: `pytest tests/windows tests/integration -v --ludusavi-path=X:\SaveSync\ludusavi\ludusavi.exe`

## 1. First run and registration (PC A)

- [ ] First start opens the window and Settings (initial configuration). Later starts with `--background` do **not** open the window.
- [ ] Tray icon is ⚪ (USB absent) before the USB is plugged in.
- [ ] Plug in the USB: it appears in Settings → USB drive as "empty" (or "Save Sync drive"). Press **Use this drive**. `X:\SaveSync\savesync-identity.json` exists.
- [ ] Tray turns 🟢/🟡; a "USB connected" notification appears.
- [ ] Settings → Start with Windows ✔ → sign out/in: Save Sync is in the tray, window closed. Untick → it no longer starts.
- [ ] Start `SaveSync.exe` a second time: no second tray icon; the first instance's window comes to the front.

## 2. The 17 scenarios (plan §26.7)

1. [ ] **PC A → USB.** Play *G* on A, save, quit. Sync Now. *G* shows *Synchronized*; History shows `BACKUP … PC → USB … SUCCESS`. `X:\SaveSync\backups\<G>\` exists.
2. [ ] **USB → PC B.** Plug the USB into B (never synced). *G* is offered in the **First synchronization** wizard (or "Pending configuration" if B's path is invalid). Choose *Use USB*. B loads A's save in the game.
3. [ ] **PC B modifies a save.** Play on B, save, quit, Sync Now → *PC → USB* then *Synchronized*.
4. [ ] **USB → PC A.** Plug into A. With "Synchronize when the USB is connected" on, A restores B's save automatically; the game loads B's progress. A safety snapshot exists in `%APPDATA%\SaveSync\safety\`.
5. [ ] **PC A modifies it again.** Play/save on A, Sync Now → uploaded.
6. [ ] **Both PCs modify the same game.** Without syncing in between, save on B and on A. Connect to A → *G* is a **Conflict**, the tray is 🔴, the window opens on Conflicts. Nothing changed on either side (check the save files' timestamps and the USB versions in the game detail).
7. [ ] **Remove USB during sync.** Start a sync with several changed games and pull the USB out mid-way. Result: "USB was removed during synchronization", nothing reported as synchronized, no crash. Reconnect → the sync completes; USB versions of the interrupted game show no *incomplete* version used as newest.
8. [ ] **Connect USB while the game is open.** Launch *G*, connect the USB / Sync Now → "*G* is currently running" with [Wait and synchronize] [Skip this game] [Continue without synchronizing it]. Saves untouched. Choose *Wait and synchronize*, quit the game → it synchronizes within ~5 s.
9. [ ] **Test USB version.** From the conflict of step 6 choose *Explore both*. "⚠ Trial mode" window: the PC now has the USB version; launch the game and confirm.
10. [ ] **Test PC version.** *Test PC version* → the game shows A's version again (works with the USB unplugged).
11. [ ] **Return to original.** *Cancel trial / return to original* → A's version, conflict still pending.
12. [ ] **Choose USB.** Explore both again → *Keep USB* → *Synchronized*; the USB version is on the PC. The previous PC state is recoverable ("Recover previous PC state" in the game detail).
13. [ ] **Choose PC.** Recreate a conflict → *Use PC* → the PC version becomes the newest USB version; B restores it next time.
14. [ ] **Shut down with sync enabled.** Settings → "Synchronize saves when shutting down Windows" ✔. Change a save, keep the USB in, shut down. Windows shows "Synchronizing game saves with the USB…" briefly; after reboot the USB has the new version (History: `SHUTDOWN … SUCCESS`). Time it: must be well under 3 minutes.
15. [ ] **Shut down with sync disabled.** Untick the option, change a save, shut down: no delay; after reboot *G* is *PC → USB* pending.
16. [ ] **Disconnect USB with pending changes.** Change a save (the watcher marks it), unplug the USB: notification "USB removed with pending changes", tray ⚪, banner says it will be synchronized on reconnection.
17. [ ] **Reconnect later.** Plug the USB into a **different USB port** (new drive letter): recognized as the registered USB, pending change uploaded.

## 3. Windows-specific checks

- [ ] **Unknown USB:** plug in a second, unrelated stick → banner "Unknown USB", Sync Now refuses, nothing written to it.
- [ ] **Cloned marker:** copy `X:\SaveSync` to another stick → it is *not* trusted (volume serial differs).
- [ ] **Empty card reader / DVD drive** present: no "insert a disk" dialogs, no errors.
- [ ] **External USB SSD** (reports as a fixed disk): accepted only once it carries `SaveSync\`.
- [ ] **Registry saves (*H*):** changes in *H*'s registry values are detected (PC → USB) and restored on the other PC.
- [ ] **Different Windows user names** on A and B: *G* appears as "Pending configuration" on B → Manage → mapping `C:/Users/<A-user>` → `C:/Users/<B-user>` → the game becomes restorable; afterwards progress travels both ways.
- [ ] **Locked file:** keep a save file open exclusively (e.g. the game running while its process is not recognized) → the backup/restore reports an error, the previous USB version stays the synchronized one.
- [ ] **Offline PC:** disconnect the network on B before its first run → Ludusavi's game database is taken from the USB (`X:\SaveSync\ludusavi\manifest.yaml`, copied there by A).
- [ ] **Idle cost:** Task Manager shows ~0 % CPU with the app idle in the tray for 10 minutes.
- [ ] **Full library scan time** with the real manifest (dozens of installed games): note the "Synchronizing…" duration of a no-change Sync Now.
- [ ] **Notifications** appear as Windows toasts and respect the toggles in Settings.
- [ ] **Clean safety backups** in Settings shows a size and frees it; snapshots older than 7 days disappear on startup.
- [ ] **Open logs folder** (Settings and History) opens `%APPDATA%\SaveSync\`.

## 4. Recovery drills

- [ ] Break a restore on purpose (make the target save file read-only and keep the game's folder open in another program): Save Sync reports that the previous state was recovered, or offers "Recover previous PC state".
- [ ] Corrupt `X:\SaveSync\backups\<G>\mapping.yaml` (copy it first!): *G* shows an error "the backup index on the USB is unreadable" and nothing is uploaded over it. Restore the file afterwards.
