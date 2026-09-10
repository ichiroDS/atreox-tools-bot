from __future__ import annotations

import uuid

import pytest

from app.utils.temp_files import job_workspace, safe_display_name, safe_extension


async def test_workspace_is_created_and_removed(tmp_path):
    job_id = uuid.uuid4()
    async with job_workspace(tmp_path, job_id) as workspace:
        assert workspace.path.exists()
        assert workspace.path.name == str(job_id)
        (workspace.path / "payload.bin").write_bytes(b"x" * 16)
    assert not workspace.path.exists()


async def test_workspace_is_removed_when_the_job_fails(tmp_path):
    captured = {}
    with pytest.raises(RuntimeError):
        async with job_workspace(tmp_path) as workspace:
            captured["path"] = workspace.path
            (workspace.path / "leftover.mp4").write_bytes(b"data")
            raise RuntimeError("processing blew up")
    assert not captured["path"].exists()


async def test_new_file_generates_unique_internal_names(tmp_path):
    async with job_workspace(tmp_path) as workspace:
        first = workspace.new_file(".mp4", prefix="src_")
        second = workspace.new_file(".mp4", prefix="src_")
        assert first != second
        assert first.parent == workspace.path
        assert first.name.startswith("src_") and first.suffix == ".mp4"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("holiday.MP4", ".mp4"),
        ("photo.jpeg", ".jpeg"),
        (None, ".bin"),
        ("noextension", ".bin"),
        ("evil.exe", ".bin"),
        ("archive.tar.gz", ".bin"),
        ("../../etc/passwd", ".bin"),
        ("C:\Windows\system32\cmd.exe", ".bin"),
        ("shell.mp4;rm -rf /", ".bin"),
    ],
)
def test_safe_extension_rejects_untrusted_input(raw, expected):
    assert safe_extension(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("clip.mp4", "clip.mp4"),
        ("../../etc/passwd", "passwd"),
        ("C:\Windows\evil name.jpg", "evil_name.jpg"),
        ("my file (1).jpg", "my_file__1_.jpg"),
        (None, "fallback.jpg"),
        ("...", "fallback.jpg"),
    ],
)
def test_safe_display_name(raw, expected):
    assert safe_display_name(raw, "fallback.jpg") == expected
