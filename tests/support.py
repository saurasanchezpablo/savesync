"""Test doubles shared by every test layer.

`FakeRunner` replays canned (code, stdout, stderr) replies — the upstream pattern,
kept for parsing edge cases. `FakeLudusavi` is a small simulation of the Ludusavi
CLI subset Save Sync uses, working on real directories: a fake local save tree and
a fake USB. Its behavior mirrors what was MEASURED on Ludusavi 0.31 (versions per
backup, retention that spares locked versions, failed entries with exit code 0,
unknown games, restores that never delete extra files).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil

from savesync.core.engine import backup_dir_name


class FakeRunner:
    """Replies are (code, stdout, stderr), matched by substring of the argv."""

    def __init__(self, replies=None, default=(0, "{}", "")):
        self.replies = replies or {}
        self.default = default
        self.calls = []
        self.timeouts = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        joined = " ".join(argv)
        for needle, reply in self.replies.items():
            if needle in joined:
                return reply(argv) if callable(reply) else reply
        return self.default


def _sha1(path):
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


_CLOCK = 0  # version counter shared by every simulated PC


class FakeLudusavi:
    """Callable runner: `engine = LudusaviEngine(["ludusavi"], ..., runner=fake)`."""

    def __init__(self, games=None):
        # {title: [file or directory paths on this "PC"]}
        self.games = dict(games or {})
        self.calls = []
        self.fail_paths = set()      # files that fail during a real backup/restore
        self.fail_preview_paths = set()  # files that fail even in a preview
        self.fail_restore_paths = set()  # fail only when restored from the USB
        self.unreadable_usb = False  # every call touching the USB fails
        self.hooks = []              # callables(argv) -> reply|None, run first
        self.version_ok = True

    # --- helpers ---

    def _tick(self):
        # real time (like Ludusavi) — never drifting ahead when a test writes
        # many versions per second; a global counter keeps names unique across
        # the simulated PCs
        global _CLOCK
        import time as _t
        _CLOCK += 1
        now = _t.time()
        stamp = "%s-%06d" % (_t.strftime("%Y%m%dT%H%M%SZ", _t.gmtime(now)), _CLOCK)
        when = "%s.%09dZ" % (_t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(now)), _CLOCK % 10**9)
        return stamp, when

    def local_files(self, title):
        out = []
        for path in self.games.get(title, []):
            if os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    out.extend(os.path.join(root, f) for f in files)
            elif os.path.isfile(path):
                out.append(path)
        return sorted(out)

    @staticmethod
    def _mapping_path(root, title):
        return os.path.join(root, backup_dir_name(title), "mapping.yaml")

    def read_mapping(self, root, title):
        path = self._mapping_path(root, title)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)  # JSON is valid YAML
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return "corrupt"
        return data

    def _write_mapping(self, root, title, data):
        path = self._mapping_path(root, title)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)

    def latest(self, root, title):
        data = self.read_mapping(root, title)
        if not isinstance(data, dict) or not data.get("backups"):
            return None
        return data["backups"][-1]

    def _backup_state(self, root, title, name=None):
        """{path: sha1} of a backup version (the newest when name is None)."""
        data = self.read_mapping(root, title)
        if not isinstance(data, dict):
            return None
        backups = data.get("backups") or []
        if name:
            backups = [b for b in backups if b["name"] == name]
        return dict(backups[-1]["files"]) if backups else None

    # --- CLI ---

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        for hook in list(self.hooks):
            reply = hook(argv)
            if reply is not None:
                return reply
        args = list(argv[1:])
        # global options
        while args and args[0] in ("--try-manifest-update", "--no-manifest-update", "--config"):
            if args.pop(0) == "--config":
                args.pop(0)
        if args[:1] == ["--version"]:
            return (0, "ludusavi 0.31.0-fake\n", "") if self.version_ok else (-1, "", "not found")
        command = args.pop(0) if args else ""
        sub = None
        if command == "backups" and args[:1] == ["edit"]:
            sub = args.pop(0)
        opts, titles, flags = {}, [], set()
        valued = {"--path", "--backup", "--full-limit", "--differential-limit", "--comment"}
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                titles.extend(args[i + 1:])
                break
            if a in valued:
                opts[a] = args[i + 1]
                i += 2
                continue
            if a.startswith("--"):
                flags.add(a)
            else:
                titles.append(a)
            i += 1
        path = opts.get("--path", "")
        if self.unreadable_usb and path and "safety" not in path:
            return -1, "", "I/O error on %s" % path
        if command == "backup":
            return self._backup(path, titles, opts, flags)
        if command == "restore":
            return self._restore(path, titles, opts, flags)
        if command == "backups" and sub == "edit":
            return self._edit(path, titles, opts, flags)
        if command == "backups":
            return self._list(path, titles)
        return 2, "", "unsupported command %s" % command

    def _unknown(self, titles):
        return [t for t in titles if t not in self.games]

    def _result(self, games, unknown, failed=False, all_unknown=False):
        # MEASURED on 0.31: any unknown title fails the whole call (exit 1, no data
        # even for known titles); failed entries exit 1 with the reason in the JSON.
        errors = {}
        if unknown:
            errors["unknownGames"] = unknown
            games = {}
        if failed:
            errors["someGamesFailed"] = True
        out = {"overall": {}, "games": games}
        if errors:
            out["errors"] = errors
        return (1 if (unknown or failed) else 0), json.dumps(out), ""

    def _backup(self, root, titles, opts, flags):
        unknown = self._unknown(titles)
        wanted = [t for t in (titles or self.games) if t in self.games]
        preview = "--preview" in flags
        games, any_failed = {}, False
        for title in wanted:
            files = self.local_files(title)
            if not files:
                continue
            previous = self._backup_state(root, title)
            entries, game_change, failed_here = {}, "Same", False
            current = {}
            failing = self.fail_preview_paths | (set() if preview else self.fail_paths)
            for f in files:
                if f in failing:
                    entries[f] = {"change": "Different", "bytes": 0, "failed": True,
                                  "error": {"message": "Permission denied (os error 13)"}}
                    failed_here = True
                    game_change = "Different"
                    continue
                digest = _sha1(f)
                current[f] = digest
                if previous is None:
                    change = "New"
                elif f not in previous:
                    change = "New"
                elif previous[f] != digest:
                    change = "Different"
                else:
                    change = "Same"
                entries[f] = {"change": change, "bytes": os.path.getsize(f)}
                if change != "Same":
                    game_change = "Different" if previous is not None else "New"
            for f in (previous or {}):
                if f not in current and f not in entries:
                    entries[f] = {"change": "Removed", "bytes": 0}
                    game_change = "Different"
            games[title] = {"decision": "Processed", "change": game_change,
                            "files": entries, "registry": {}}
            if "--dump-registry" in flags:
                games[title]["dump"] = {"registry": None}
            any_failed = any_failed or failed_here
            if not preview and game_change != "Same":
                self._write_version(root, title, current, int(opts.get("--full-limit", 1)),
                                    files_all=files)
        return self._result(games, unknown, failed=any_failed and not preview,
                            all_unknown=bool(titles) and len(unknown) == len(titles))

    def _write_version(self, root, title, current, full_limit, files_all):
        data = self.read_mapping(root, title)
        if not isinstance(data, dict):
            data = {"name": title, "backups": []}
        stamp, when = self._tick()
        name = "backup-%s" % stamp if full_limit > 1 else "."
        vdir = os.path.join(root, backup_dir_name(title), name)
        if name == ".":
            data["backups"] = []
        os.makedirs(vdir, exist_ok=True)
        for src, _digest in current.items():
            dst = os.path.join(vdir, "files", hashlib.sha1(src.encode()).hexdigest())
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        data["backups"].append({"name": name, "when": when, "files": current,
                                "locked": False, "comment": None})
        # retention: unlocked versions beyond the limit go, oldest first
        unlocked = [b for b in data["backups"] if not b.get("locked")]
        while len(unlocked) > full_limit:
            gone = unlocked.pop(0)
            data["backups"].remove(gone)
            shutil.rmtree(os.path.join(root, backup_dir_name(title), gone["name"]),
                          ignore_errors=True)
        self._write_mapping(root, title, data)

    def _list(self, root, titles):
        games = {}
        base = root
        candidates = titles or []
        if not titles and os.path.isdir(base):
            for entry in sorted(os.listdir(base)):
                data = self.read_mapping(base, entry) if os.path.isfile(
                    os.path.join(base, entry, "mapping.yaml")) else None
                if isinstance(data, dict):
                    candidates.append(data.get("name") or entry)
        for title in candidates:
            data = self.read_mapping(base, title)
            if not isinstance(data, dict):
                continue  # corrupt or missing → silently absent (MEASURED)
            games[title] = {"backupPath": os.path.join(base, backup_dir_name(title)),
                            "backups": [{"name": b["name"], "when": b["when"],
                                         "locked": bool(b.get("locked")),
                                         "comment": b.get("comment"), "os": "windows"}
                                        for b in data.get("backups") or []]}
        return 0, json.dumps({"games": games}), ""

    def _edit(self, root, titles, opts, flags):
        title = titles[0] if titles else ""
        data = self.read_mapping(root, title)
        if not isinstance(data, dict):
            return 1, "", "No backups for %s" % title
        name = opts.get("--backup")
        target = [b for b in data["backups"] if b["name"] == name] if name else data["backups"][-1:]
        if not target:
            return 1, "", "Invalid backup ID."
        if "--lock" in flags:
            target[0]["locked"] = True
        if "--unlock" in flags:
            target[0]["locked"] = False
        if "--comment" in opts:
            target[0]["comment"] = opts["--comment"]
        self._write_mapping(root, title, data)
        return 0, "", ""

    def _restore(self, root, titles, opts, flags):
        unknown = [t for t in titles if not isinstance(self.read_mapping(root, t), dict)]
        preview = "--preview" in flags
        games, any_failed = {}, False
        for title in titles:
            data = self.read_mapping(root, title)
            if not isinstance(data, dict) or not data.get("backups"):
                continue
            versions = data["backups"]
            if opts.get("--backup"):
                versions = [b for b in versions if b["name"] == opts["--backup"]]
                if not versions:
                    return 1, "", "Invalid backup ID."
            version = versions[-1]
            vdir = os.path.join(root, backup_dir_name(title), version["name"])
            entries = {}
            for target, digest in version["files"].items():
                stored = os.path.join(vdir, "files", hashlib.sha1(target.encode()).hexdigest())
                if not os.path.exists(target):
                    change = "New"
                elif _sha1(target) != digest:
                    change = "Different"
                else:
                    change = "Same"
                entry = {"change": change, "bytes": 0}
                if ((target in self.fail_paths or (target in self.fail_restore_paths
                                                    and "safety" not in root)) and not preview) \
                        or not os.path.isfile(stored):
                    entry.update(failed=True, error={"message": "No such file or directory (os error 2)"})
                    any_failed = True
                elif not preview and change != "Same":
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(stored, target)
                entries[target] = entry
            games[title] = {"decision": "Processed",
                            "change": "Same" if all(e["change"] == "Same" for e in entries.values()) else "Different",
                            "files": entries, "registry": {}}
        return self._result(games, unknown, failed=any_failed,
                            all_unknown=bool(titles) and len(unknown) == len(titles))


def write(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(content)
    return path


def read(path):
    with open(path) as fh:
        return fh.read()
