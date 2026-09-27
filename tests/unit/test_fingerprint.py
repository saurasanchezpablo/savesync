import os

import pytest

from savesync.core import fingerprint as fpmod
from savesync.core.fingerprint import Fingerprint, normalize
from support import write


@pytest.fixture(params=["xxh3_128", "sha256"])
def fp(request):
    if request.param == "xxh3_128" and fpmod.xxhash is None:
        pytest.skip("xxhash not installed")
    return Fingerprint(request.param)


def test_unchanged_files_are_unchanged(tmp_path, fp):
    a = write(str(tmp_path / "s" / "a.sav"), "one")
    b = write(str(tmp_path / "s" / "b.sav"), "two")
    base = fp.compute([a, b])
    assert fp.has_changed(base, [a, b]) is False
    assert fp.has_changed(base, [b, a]) is False, "order does not matter"


def test_content_change_with_same_size_and_timestamp_is_detected(tmp_path, fp):
    a = write(str(tmp_path / "a.sav"), "AAAA")
    stat = os.stat(a)
    base = fp.compute([a])
    write(a, "BBBB")
    os.utime(a, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert os.stat(a).st_size == stat.st_size and os.stat(a).st_mtime_ns == stat.st_mtime_ns
    assert fp.has_changed(base, [a]) is True


def test_touch_without_content_change_is_not_a_change(tmp_path, fp):
    a = write(str(tmp_path / "a.sav"), "same")
    base = fp.compute([a])
    os.utime(a, (1, 1))
    assert fp.has_changed(base, [a]) is False


def test_size_change_short_circuits_without_hashing(tmp_path, fp, monkeypatch):
    a = write(str(tmp_path / "a.sav"), "1")
    base = fp.compute([a])
    write(a, "1234")
    monkeypatch.setattr(Fingerprint, "_digest_file", lambda *a, **k: pytest.fail("hashed"))
    assert fp.has_changed(base, [a]) is True


def test_new_and_removed_paths_are_changes(tmp_path, fp):
    a = write(str(tmp_path / "a.sav"), "1")
    b = write(str(tmp_path / "b.sav"), "2")
    base = fp.compute([a])
    assert fp.has_changed(base, [a, b]) is True
    assert fp.has_changed(fp.compute([a, b]), [a]) is True


def test_missing_file_is_part_of_the_state_not_an_error(tmp_path, fp):
    a = write(str(tmp_path / "a.sav"), "1")
    base = fp.compute([a])
    os.remove(a)
    assert fp.has_changed(base, [a]) is True
    gone = fp.compute([a])
    assert fp.has_changed(gone, [a]) is False


def test_path_change_is_a_change(tmp_path, fp):
    a = write(str(tmp_path / "old" / "a.sav"), "1")
    b = write(str(tmp_path / "new" / "a.sav"), "1")
    assert fp.has_changed(fp.compute([a]), [b]) is True


def test_directories_are_expanded(tmp_path, fp):
    write(str(tmp_path / "d" / "x.sav"), "1")
    base = fp.compute([str(tmp_path / "d")])
    write(str(tmp_path / "d" / "sub" / "y.sav"), "2")
    assert fp.has_changed(base, [str(tmp_path / "d")]) is True


def test_extra_state_such_as_registry_dump_counts(tmp_path, fp):
    a = write(str(tmp_path / "a.sav"), "1")
    base = fp.compute([a], extra={"registry": "HKCU\\X=1"})
    assert fp.has_changed(base, [a], extra={"registry": "HKCU\\X=1"}) is False
    assert fp.has_changed(base, [a], extra={"registry": "HKCU\\X=2"}) is True
    assert fp.has_changed(base, [a]) is True


@pytest.mark.parametrize("previous", ["", None, "garbage", "sfp1:{", 'sfp1:{"alg":"md4"}'])
def test_unusable_previous_is_unknown_never_unchanged(tmp_path, fp, previous):
    a = write(str(tmp_path / "a.sav"), "1")
    assert fp.has_changed(previous, [a]) is None


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_unreadable_file_is_unknown(tmp_path, fp):
    a = write(str(tmp_path / "a.sav"), "1")
    base = fp.compute([a])
    os.chmod(a, 0)
    try:
        assert fp.has_changed(base, [a]) is None
        with pytest.raises(fpmod.FingerprintError):
            fp.compute([a])
    finally:
        os.chmod(a, 0o644)


def test_fingerprint_stores_no_save_content(tmp_path, fp):
    secret = "MY-SECRET-SAVE-CONTENT-123"
    a = write(str(tmp_path / "a.sav"), secret)
    assert secret not in fp.compute([a])


def test_sha256_fingerprint_is_readable_with_xxhash_installed(tmp_path):
    a = write(str(tmp_path / "a.sav"), "1")
    base = Fingerprint("sha256").compute([a])
    assert Fingerprint().has_changed(base, [a]) is False


def test_paths_are_normalized(tmp_path, fp):
    a = write(str(tmp_path / "a.sav"), "1")
    messy = os.path.join(str(tmp_path), ".", "a.sav")
    assert fp.has_changed(fp.compute([a]), [messy]) is False
    assert normalize(messy) == normalize(a)
