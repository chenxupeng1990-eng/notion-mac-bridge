import hashlib
import os
import random
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from notion_bridge import file_tools
from notion_bridge.file_tools import encode_preview, resolve_write_path, write_bytes
from PIL import Image

TEXT_EXTENSIONS = {
    ".html", ".htm", ".js", ".mjs", ".json", ".css", ".glsl", ".vert", ".frag",
    ".md", ".txt", ".py", ".sh", ".yaml", ".yml", ".toml", ".csv",
}
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".glb", ".gltf", ".mp3",
    ".wav", ".ogg", ".m4a", ".aac", ".flac", ".ttf", ".otf", ".woff", ".woff2",
    ".bin", ".tgz", ".tar", ".gz", ".zip", ".pdf",
}


@pytest.fixture
def roots(tmp_path):
    # macOS /var and /tmp are symlinks; use the physical pytest temp root.
    base = tmp_path.resolve()
    scenes = base / "bridge" / "renderer" / "scenes"
    workspace = base / "notion-workspace"
    scenes.mkdir(parents=True)
    workspace.mkdir()
    return scenes, workspace


def test_legacy_relative_scene_write_and_side_effect_free_resolution(roots):
    scenes, workspace = roots
    target = scenes / "fx" / "scene.html"
    assert resolve_write_path("fx/scene.html", scenes, workspace, TEXT_EXTENSIONS) == str(target)
    assert not target.parent.exists()
    result = write_bytes("fx/scene.html", b"<html></html>", scenes, workspace, TEXT_EXTENSIONS, 200_000)
    assert result == {
        "path": str(target), "bytes": 13,
        "sha256": hashlib.sha256(b"<html></html>").hexdigest(),
    }
    assert target.read_bytes() == b"<html></html>"
    assert not (workspace / "fx").exists()


@pytest.mark.parametrize("extension", sorted(TEXT_EXTENSIONS | BINARY_EXTENSIONS))
def test_allowlisted_extensions_preserved_for_absolute_workspace_paths(roots, extension):
    scenes, workspace = roots
    extensions = TEXT_EXTENSIONS if extension in TEXT_EXTENSIONS else BINARY_EXTENSIONS
    target = workspace / "nested" / ("File" + extension.upper())
    result = write_bytes(target, b"payload", scenes, workspace, extensions, 100)
    assert result["path"] == str(target)
    assert target.read_bytes() == b"payload"
    assert target.suffix == extension.upper()


def test_text_utf8_byte_count_and_shasum(roots):
    scenes, workspace = roots
    target = workspace / "Text" / "a.py"
    data = 'print("你好，世界 🌍")\n'.encode()
    result = write_bytes(target, data, scenes, workspace, TEXT_EXTENSIONS, len(data))
    checksum = subprocess.run(
        ["shasum", "-a", "256", str(target)], check=True, capture_output=True, text=True
    ).stdout.split()[0]
    assert target.read_bytes() == data
    assert result["bytes"] == len(data) > len(data.decode("utf-8"))
    assert result["sha256"] == checksum == hashlib.sha256(data).hexdigest()
    assert len(result["sha256"]) == 64
    assert result["sha256"] == result["sha256"].lower()


@pytest.mark.parametrize("name", ["asset.tgz", "asset.tar.gz", "asset.zip", "asset.pdf"])
def test_binary_byte_count_and_shasum(roots, name):
    scenes, workspace = roots
    target = workspace / name
    data = bytes(range(256)) + b"\0\xff\x1f\x8b"
    result = write_bytes(target, data, scenes, workspace, BINARY_EXTENSIONS, len(data))
    checksum = subprocess.run(
        ["shasum", "-a", "256", str(target)], check=True, capture_output=True, text=True
    ).stdout.split()[0]
    assert target.read_bytes() == data
    assert result == {"path": str(target), "bytes": len(data), "sha256": checksum}


def test_home_expansion_and_absolute_scene_path(roots, monkeypatch):
    scenes, workspace = roots
    monkeypatch.setenv("HOME", str(workspace.parent))
    target = workspace / "Text" / "a.py"
    result = write_bytes("~/notion-workspace/Text/a.py", b"pass\n", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert result["path"] == str(target)
    assert target.read_bytes() == b"pass\n"
    scene = scenes / "scene.js"
    assert write_bytes(scene, b"scene", scenes, workspace, TEXT_EXTENSIONS, 100)["path"] == str(scene)


@pytest.mark.parametrize("path", [
    "../outside.py", "sub/../inside.py", "./../escape.py", "sub/../../escape.py",
    "~/../etc/payload.py", "~/notion-workspace/sub/../inside.py",
])
def test_any_dotdot_component_rejected_before_normalization(roots, monkeypatch, path):
    scenes, workspace = roots
    monkeypatch.setenv("HOME", str(workspace.parent))
    with pytest.raises(ValueError, match="components"):
        write_bytes(path, b"bad", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert list(scenes.iterdir()) == list(workspace.iterdir()) == []


def test_absolute_dotdot_rejected_even_when_normalization_would_stay_inside(roots):
    scenes, workspace = roots
    path = f"{workspace}/missing/../valid.py"
    with pytest.raises(ValueError, match="components"):
        resolve_write_path(path, scenes, workspace, TEXT_EXTENSIONS)
    assert not (workspace / "missing").exists()


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.backup.py", ".ENV.production.py", "audit.log", "AUDIT.LOG", ".git/config.py", ".GIT/config.py", ".env-dir/a.py", "audit.log/a.py"])
def test_sensitive_files_and_directory_components_blocked(roots, name):
    scenes, workspace = roots
    # Explicitly allow these suffixes so the sensitive-path rule is exercised.
    extensions = TEXT_EXTENSIONS | {".log", ".env", ".local"}
    with pytest.raises(ValueError, match="sensitive"):
        write_bytes(str(workspace / name), b"bad", scenes, workspace, extensions, 100)
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("relative", ["version..2.py", "..notes.py", "sub/./a.py"])
def test_dotdot_substrings_are_not_traversal_components(roots, relative):
    scenes, workspace = roots
    result = write_bytes(relative, b"safe", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert Path(result["path"]).read_bytes() == b"safe"


@pytest.mark.parametrize("suffix", ["-other", "2"])
def test_absolute_root_prefix_collisions_rejected(roots, suffix):
    scenes, workspace = roots
    outside = Path(str(workspace) + suffix) / "a.py"
    with pytest.raises(ValueError, match="allowed write root"):
        write_bytes(outside, b"bad", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert not outside.parent.exists()


@pytest.mark.parametrize("path", ["", "\0a.py", "directory/", "file.exe", "file", b"file.py", None])
def test_invalid_paths_rejected_without_creating_parents(roots, path):
    scenes, workspace = roots
    with pytest.raises(ValueError):
        write_bytes(path, b"bad", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert list(scenes.iterdir()) == list(workspace.iterdir()) == []


def test_caller_controls_extensions_and_extension_sets_are_not_mutated(roots):
    scenes, workspace = roots
    original = TEXT_EXTENSIONS.copy()
    with pytest.raises(ValueError, match="extension"):
        write_bytes(workspace / "asset.gz", b"bad", scenes, workspace, TEXT_EXTENSIONS, 100)
    with pytest.raises(ValueError, match="extension"):
        write_bytes(workspace / "a.py", b"bad", scenes, workspace, BINARY_EXTENSIONS, 100)
    assert TEXT_EXTENSIONS == original
    assert resolve_write_path("a.PY", scenes, workspace, iter([".py"])) == str(scenes / "a.PY")


@pytest.mark.parametrize("extensions", [".py", ["py"], [1]])
def test_invalid_extension_allowlists_rejected(roots, extensions):
    scenes, workspace = roots
    with pytest.raises(ValueError, match="extensions"):
        resolve_write_path("a.py", scenes, workspace, extensions)


@pytest.mark.parametrize("root_name", ["scenes", "workspace"])
def test_write_root_itself_is_not_a_file_target(roots, root_name):
    scenes, workspace = roots
    target = scenes if root_name == "scenes" else workspace
    with pytest.raises(ValueError, match="allowed write root"):
        resolve_write_path(target, scenes, workspace, {""})


@pytest.mark.parametrize("data,max_bytes", [
    (b"too large", 2), ("not bytes", 100), (bytearray(b"mutable"), 100),
    (b"x", -1), (b"x", True), (b"x", 1.5),
])
def test_invalid_data_and_limits_leave_existing_files_intact(roots, data, max_bytes):
    scenes, workspace = roots
    target = workspace / "existing.py"
    target.write_bytes(b"original")
    with pytest.raises(ValueError):
        write_bytes(target, data, scenes, workspace, TEXT_EXTENSIONS, max_bytes)
    assert target.read_bytes() == b"original"
    assert list(workspace.iterdir()) == [target]
    with pytest.raises(ValueError):
        write_bytes(workspace / "new" / "a.py", data, scenes, workspace, TEXT_EXTENSIONS, max_bytes)
    assert not (workspace / "new").exists()


def test_empty_payload_and_atomic_regular_file_overwrite(roots):
    scenes, workspace = roots
    target = workspace / "a.py"
    target.write_bytes(b"original")
    result = write_bytes(target, b"", scenes, workspace, TEXT_EXTENSIONS, 0)
    assert target.read_bytes() == b""
    assert result["bytes"] == 0
    assert result["sha256"] == hashlib.sha256(b"").hexdigest()
    assert list(workspace.iterdir()) == [target]


@pytest.mark.parametrize("kind", ["directory", "file", "dangling", "inside"])
def test_symlink_escapes_and_in_root_symlinks_rejected(roots, kind):
    scenes, workspace = roots
    outside = workspace.parent / "outside"
    outside.mkdir()
    original = outside / "a.py"
    original.write_bytes(b"untouched")
    link = workspace / ("link" if kind == "directory" else "a.py")
    if kind == "directory":
        link.symlink_to(outside, target_is_directory=True)
        target = link / "a.py"
    elif kind == "dangling":
        link.symlink_to(outside / "new.py")
        target = link
    elif kind == "inside":
        other = workspace / "safe.py"
        other.write_bytes(b"inside untouched")
        link.symlink_to(other)
        target = link
    else:
        link.symlink_to(original)
        target = link
    for operation in (
        lambda: resolve_write_path(target, scenes, workspace, TEXT_EXTENSIONS),
        lambda: write_bytes(target, b"bad", scenes, workspace, TEXT_EXTENSIONS, 100),
    ):
        with pytest.raises(ValueError, match="symlink"):
            operation()
    assert original.read_bytes() == b"untouched"
    assert not (outside / "new.py").exists()
    assert link.is_symlink()
    if kind == "inside":
        assert other.read_bytes() == b"inside untouched"


def test_symlink_in_configured_root_rejected(roots):
    scenes, workspace = roots
    alias = workspace.parent / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_bytes(alias / "a.py", b"bad", scenes, alias, TEXT_EXTENSIONS, 100)
    assert list(workspace.iterdir()) == []


def test_non_regular_targets_rejected(roots):
    scenes, workspace = roots
    target = workspace / "directory.py"
    target.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        write_bytes(target, b"bad", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert target.is_dir()


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_io_failure_preserves_existing_file_and_cleans_temporary(roots, monkeypatch, failure):
    scenes, workspace = roots
    target = workspace / "existing.py"
    target.write_bytes(b"original")
    real_write = os.write
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "write" and calls == 1:
            return real_write(args[0], args[1][:2])
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(file_tools.os, failure, fail)
    with pytest.raises(OSError, match="simulated"):
        write_bytes(target, b"new content", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert target.read_bytes() == b"original"
    assert list(workspace.iterdir()) == [target]


def test_directory_swapped_to_symlink_after_preflight_is_rejected(roots, monkeypatch):
    scenes, workspace = roots
    parent = workspace / "parent"
    parent.mkdir()
    outside = workspace.parent / "outside"
    outside.mkdir()
    victim = outside / "a.py"
    victim.write_bytes(b"untouched")
    real_resolve = file_tools.resolve_write_path

    def swap(*args, **kwargs):
        target = real_resolve(*args, **kwargs)
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
        return target

    monkeypatch.setattr(file_tools, "resolve_write_path", swap)
    with pytest.raises(ValueError, match="symlink"):
        write_bytes(parent / "a.py", b"bad", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert victim.read_bytes() == b"untouched"
    assert list(outside.iterdir()) == [victim]


def test_symlink_installed_during_mkdir_race_is_rejected(roots, monkeypatch):
    scenes, workspace = roots
    outside = workspace.parent / "outside"
    outside.mkdir()
    real_mkdir = os.mkdir

    def raced_mkdir(name, *args, **kwargs):
        if name == "new-parent" and kwargs.get("dir_fd") is not None:
            os.symlink(str(outside), name, dir_fd=kwargs["dir_fd"])
            raise FileExistsError("concurrent symlink")
        return real_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(file_tools.os, "mkdir", raced_mkdir)
    with pytest.raises(ValueError, match="symlink"):
        write_bytes(workspace / "new-parent" / "a.py", b"bad", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert list(outside.iterdir()) == []


def test_final_symlink_swap_before_replace_is_rejected(roots, monkeypatch):
    scenes, workspace = roots
    target = workspace / "a.py"
    target.write_bytes(b"original")
    outside = workspace.parent / "outside.py"
    outside.write_bytes(b"untouched")
    real_fsync = os.fsync

    def swap(descriptor):
        real_fsync(descriptor)
        target.unlink()
        target.symlink_to(outside)

    monkeypatch.setattr(file_tools.os, "fsync", swap)
    with pytest.raises(ValueError, match="symlink"):
        write_bytes(target, b"bad", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert outside.read_bytes() == b"untouched"
    assert target.is_symlink()
    assert list(workspace.iterdir()) == [target]


def test_replace_uses_verified_directory_descriptors(roots, monkeypatch):
    scenes, workspace = roots
    target = workspace / "a.py"
    real_replace = os.replace
    calls = []

    def replace(src, dst, **kwargs):
        assert not os.path.isabs(src)
        assert dst == "a.py"
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        assert os.fstat(kwargs["dst_dir_fd"]).st_ino == workspace.stat().st_ino
        calls.append((src, dst))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(file_tools.os, "replace", replace)
    write_bytes(target, b"safe", scenes, workspace, TEXT_EXTENSIONS, 100)
    assert len(calls) == 1
    assert target.read_bytes() == b"safe"


def assert_preview(preview, format, max_width=1024, max_bytes=300_000):
    assert preview["format"] == format
    assert preview["mime_type"] == f"image/{format}"
    assert isinstance(preview["data"], bytes)
    assert preview["bytes"] == len(preview["data"]) <= max_bytes
    assert 1 <= preview["width"] <= max_width
    assert preview["height"] >= 1
    with Image.open(BytesIO(preview["data"])) as decoded:
        assert decoded.format.lower() == format
        assert decoded.size == (preview["width"], preview["height"])
        decoded.load()


def test_complex_2600x2000_png_jpeg_preview_and_original_sha(tmp_path):
    original = tmp_path / "noisy.png"
    noise = random.Random(42).randbytes(2600 * 2000 * 3)
    with Image.frombytes("RGB", (2600, 2000), noise) as image:
        image.save(original, format="PNG")
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    # This real input exceeds main's previous 10 MiB compressed-input cap.
    assert original.stat().st_size > 10 * 1024 * 1024
    preview = encode_preview(original)
    assert_preview(preview, "jpeg")
    assert abs(preview["width"] / preview["height"] - 2600 / 2000) < 0.01
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("format", ["png", "webp", "jpeg", "jpg", "JPEG"])
def test_requested_format_and_no_upscale(tmp_path, format):
    original = tmp_path / "small.png"
    with Image.new("RGB", (80, 60), "red") as image:
        image.save(original)
    preview = encode_preview(original, max_width=1024, format=format)
    assert_preview(preview, "jpeg" if format.lower() in {"jpeg", "jpg"} else format)
    assert (preview["width"], preview["height"]) == (80, 60)


@pytest.mark.parametrize("format", ["png", "webp", "jpeg"])
def test_complex_preview_byte_budget_preserves_format_and_aspect(tmp_path, format):
    original = tmp_path / "noise.png"
    with Image.frombytes("RGB", (900, 600), random.Random(0).randbytes(900 * 600 * 3)) as image:
        image.save(original)
    before = original.read_bytes()
    preview = encode_preview(original, max_width=900, format=format, max_bytes=30_000)
    assert_preview(preview, format, max_width=900, max_bytes=30_000)
    assert abs(preview["width"] / preview["height"] - 1.5) < 0.02
    if format == "png":
        assert preview["width"] < 900
    assert original.read_bytes() == before


@pytest.mark.parametrize("format", ["png", "webp", "jpeg"])
def test_transparency_is_flattened_to_white(tmp_path, format):
    original = tmp_path / "transparent.png"
    with Image.new("RGBA", (100, 80), (255, 0, 0, 0)) as image:
        image.save(original)
    preview = encode_preview(original, format=format)
    with Image.open(BytesIO(preview["data"])) as decoded:
        assert decoded.convert("RGB").getpixel((50, 40)) == (255, 255, 255)
        assert "A" not in decoded.getbands()


def test_palette_transparency_flattened_to_white(tmp_path):
    original = tmp_path / "palette.png"
    with Image.new("P", (20, 10), 0) as image:
        image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
        image.save(original, transparency=0)
    preview = encode_preview(original, format="png")
    with Image.open(BytesIO(preview["data"])) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 255, 255)


def test_exif_transpose_before_width_limit_and_original_unchanged(tmp_path):
    original = tmp_path / "oriented.jpg"
    with Image.new("RGB", (40, 80), "blue") as image:
        exif = Image.Exif()
        exif[274] = 6
        image.save(original, exif=exif)
    before = original.read_bytes()
    preview = encode_preview(original, max_width=60, format="png")
    assert (preview["width"], preview["height"]) == (60, 30)
    assert original.read_bytes() == before
    with Image.open(BytesIO(preview["data"])) as decoded:
        assert decoded.getexif().get(274, 1) == 1


def test_gif_uses_first_frame(tmp_path):
    original = tmp_path / "animation.gif"
    with Image.new("RGB", (40, 30), "red") as first, Image.new("RGB", (40, 30), "blue") as second:
        first.save(original, save_all=True, append_images=[second], duration=100, loop=0)
    before = original.read_bytes()
    preview = encode_preview(original, format="png")
    with Image.open(BytesIO(preview["data"])) as decoded:
        assert decoded.getpixel((20, 15)) == (255, 0, 0)
    assert original.read_bytes() == before


@pytest.mark.parametrize("width", [0, -1, 8193, 1.2, True, "1024", None])
def test_invalid_preview_width_rejected_before_read(tmp_path, width):
    with pytest.raises(ValueError, match="max_width"):
        encode_preview(tmp_path / "does-not-exist.png", max_width=width)


@pytest.mark.parametrize("budget", [0, -1, 1.2, True, "300000", None])
def test_invalid_preview_byte_limit_rejected_before_read(tmp_path, budget):
    with pytest.raises(ValueError, match="max_bytes"):
        encode_preview(tmp_path / "does-not-exist.png", max_bytes=budget)


@pytest.mark.parametrize("format", ["gif", "tiff", "bmp", "", None, 1])
def test_unsupported_preview_formats_rejected_before_read(tmp_path, format):
    with pytest.raises(ValueError, match="format"):
        encode_preview(tmp_path / "does-not-exist.png", format=format)


@pytest.mark.parametrize("width", [1, 8192])
def test_preview_width_boundaries(tmp_path, width):
    original = tmp_path / "small.png"
    with Image.new("RGB", (2, 2), "green") as image:
        image.save(original)
    preview = encode_preview(original, max_width=width)
    assert_preview(preview, "jpeg", max_width=width)
    assert preview["width"] == min(2, width)


@pytest.mark.parametrize("format", ["jpeg", "png", "webp"])
def test_impossible_preview_budget_raises_and_preserves_original(tmp_path, format):
    original = tmp_path / "small.png"
    with Image.new("RGB", (20, 10), "green") as image:
        image.save(original)
    before = original.read_bytes()
    with pytest.raises(ValueError, match="cannot fit"):
        encode_preview(original, format=format, max_bytes=1)
    assert original.read_bytes() == before


def test_pillow_decompression_bomb_protection_remains_enabled(tmp_path, monkeypatch):
    original = tmp_path / "bomb.png"
    with Image.new("RGB", (16, 16), "white") as image:
        image.save(original)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(Image.DecompressionBombError):
        encode_preview(original)
    assert Image.MAX_IMAGE_PIXELS == 10
