# Fake USB medium

An ordinary directory with the layout of a registered Save Sync drive:

```
fake_usb/
└── SaveSync/
    ├── savesync-identity.json   marker: usb_id + volume serial
    ├── backups/                 Ludusavi backup target
    └── ludusavi/                portable ludusavi.exe goes here
```

Tests copy it to a temporary folder and expose it through
`FakeRemovableMediaDetector` (see `src/savesync/platform/fake.py`) as a
`DriveInfo` with serial `FAKE-0001`. Most tests build a fresh one with the
`make_usb` fixture instead; this copy documents the layout and is used by
`tests/integration/test_usb.py::test_static_fixture_is_a_valid_medium`.

To try the application by hand without hardware:

```
python -m savesync --fake-usb <copy of this folder> --data-dir <scratch folder>
```
