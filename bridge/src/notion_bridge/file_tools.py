"""Local write and image-preview helpers, independent of the MCP server.

The caller supplies write roots, extension allowlists and size limits. Render
scene resolution must remain separate from these more general write helpers.
"""

import errno
import hashlib
import math
import os
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from io import BytesIO

from PIL import Image, ImageOps

# Keep caller-facing validation failures catchable with a single ValueError.
# ruff: noqa: TRY004


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_DIRECTORY_FLAGS |= getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS |= getattr(os, "O_CLOEXEC", 0)


def _path_string(path: str | os.PathLike[str]) -> str:
    try:
        text = os.fspath(path)
    except TypeError as exc:
        raise ValueError("path must be a string or a text PathLike") from exc
    if not isinstance(text, str) or not text or "\0" in text:
        raise ValueError("path must be a nonempty string without NUL bytes")
    # Check before expanduser/abspath/normpath can hide traversal components.
    if ".." in text.split(os.sep):
        raise ValueError("'..' path components are not allowed")
    return text


def _expand_path(path: str | os.PathLike[str]) -> str:
    text = _path_string(path)
    expanded = os.path.expanduser(text)
    if text.startswith("~") and not os.path.isabs(expanded):
        raise ValueError("could not expand home directory in path")
    return _path_string(expanded)


def _root_path(path: str | os.PathLike[str]) -> str:
    expanded = _expand_path(path)
    if not os.path.isabs(expanded):
        raise ValueError("write roots must be absolute paths")
    return os.path.normpath(expanded)


def _write_path(
    path: str | os.PathLike[str],
    scenes_dir: str | os.PathLike[str],
    workspace_dir: str | os.PathLike[str],
    extensions: Iterable[str],
) -> str:
    expanded = _expand_path(path)
    if expanded.endswith(os.sep):
        raise ValueError("path must name a file")
    scenes_root = _root_path(scenes_dir)
    workspace_root = _root_path(workspace_dir)
    target = os.path.normpath(
        expanded if os.path.isabs(expanded) else os.path.join(scenes_root, expanded)
    )
    roots = (scenes_root, workspace_root) if os.path.isabs(expanded) else (scenes_root,)
    if not any(
        target != root and os.path.commonpath((root, target)) == root
        for root in roots
    ):
        raise ValueError("path is not a file under an allowed write root")
    if any(
        part.casefold().startswith(".env")
        or part.casefold() in {"audit.log", ".git"}
        for part in target.split(os.sep)
    ):
        raise ValueError("sensitive path blocked")
    if isinstance(extensions, (str, bytes)):
        raise ValueError("extensions must be an iterable of dotted suffixes")
    allowed = set()
    for extension in extensions:
        if not isinstance(extension, str) or not extension.startswith("."):
            raise ValueError("extensions must contain dotted suffixes")
        allowed.add(extension.lower())
    if os.path.splitext(target)[1].lower() not in allowed:
        raise ValueError("extension not allowed")
    return target


def _open_directory(name: str, parent_fd: int) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("symlinks and non-directory parents are not allowed") from exc
        raise


@contextmanager
def _parent_directory(path: str, *, create: bool) -> Iterator[int | None]:
    """Walk every ancestor without following symlinks; retain the final fd.

    Missing ancestors are allowed during read-only resolution. During writing,
    each mkdir/open pair is relative to the previously verified directory fd.
    """
    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for component in path.split(os.sep)[1:-1]:
            try:
                child = _open_directory(component, descriptor)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(component, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    # Another writer may have created it. The no-follow open
                    # below still rejects a concurrently installed symlink.
                    pass
                child = _open_directory(component, descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _check_target(parent_fd: int, filename: str) -> None:
    try:
        entry = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(entry.st_mode):
        raise ValueError("target must be a regular file, not a symlink or directory")


def resolve_write_path(
    path: str | os.PathLike[str],
    scenes_dir: str | os.PathLike[str],
    workspace_dir: str | os.PathLike[str],
    extensions: Iterable[str],
) -> str:
    """Validate a write destination without creating anything.

    Relative paths are rooted in scenes_dir. Absolute and expanded '~' paths
    may be under either supplied root. All symlink components are rejected,
    including symlinks in the supplied root's ancestors; pass physical roots.
    The extension is compared case-insensitively but the name is preserved.
    Resolution is a preflight check, not authorization for a later plain open;
    use write_bytes for descriptor-based, atomic writing.
    """
    target = _write_path(path, scenes_dir, workspace_dir, extensions)
    with _parent_directory(target, create=False) as parent_fd:
        if parent_fd is not None:
            _check_target(parent_fd, os.path.basename(target))
    return target


def _temporary_file(parent_fd: int) -> tuple[str, int]:
    for _ in range(10):
        name = f".notion-bridge-{secrets.token_hex(12)}.tmp"
        try:
            return name, os.open(name, _FILE_FLAGS, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate an exclusive temporary file")


def write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    scenes_dir: str | os.PathLike[str],
    workspace_dir: str | os.PathLike[str],
    extensions: Iterable[str],
    max_bytes: int,
) -> dict:
    """Atomically write bytes and return {path, bytes, sha256}.

    Text callers should encode UTF-8 before calling, and binary callers should
    validate/decode base64 first. Invalid input and over-limit payloads create
    no directories and leave existing files intact. I/O failures before replace
    also leave existing files intact and remove the temporary file.
    """
    if not isinstance(data, bytes):
        raise ValueError("data must be bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    size = len(data)
    if size > max_bytes:
        raise ValueError(f"too large ({size} > {max_bytes} bytes)")
    digest = hashlib.sha256(data).hexdigest()
    target = resolve_write_path(path, scenes_dir, workspace_dir, extensions)
    with _parent_directory(target, create=True) as parent_fd:
        assert parent_fd is not None
        filename = os.path.basename(target)
        _check_target(parent_fd, filename)
        temporary, descriptor = _temporary_file(parent_fd)
        try:
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("temporary file write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _check_target(parent_fd, filename)
            os.replace(
                temporary, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    return {"path": target, "bytes": size, "sha256": digest}


def inspect_image(path: str | os.PathLike[str]) -> dict:
    """Read image header metadata without encoding or decoding a preview."""
    with Image.open(path) as image:
        width, height = image.size
        if image.getexif().get(274) in (5, 6, 7, 8):
            width, height = height, width
        return {
            "path": os.fspath(path), "width": width, "height": height,
            "format": (image.format or "unknown").lower(),
            "bytes": os.path.getsize(path), "frames": getattr(image, "n_frames", 1),
        }


def _encode_image(image: Image.Image, format: str, quality: int) -> bytes:
    output = BytesIO()
    if format == "jpeg":
        image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    elif format == "png":
        image.save(output, format="PNG", optimize=True, compress_level=9)
    else:
        image.save(output, format="WEBP", quality=quality, method=4)
    return output.getvalue()


def encode_preview(
    path: str | os.PathLike[str],
    max_width: int = 1024,
    format: str = "jpeg",
    max_bytes: int = 300_000,
) -> dict:
    """Return encoded preview bytes and metadata without modifying the input.

    Supported formats are jpeg (jpg alias), png and webp. Apply EXIF orientation,
    select the first GIF frame, flatten transparency onto white, and never
    upscale. Pillow's default decompression-bomb limits remain enabled. The
    caller controls read-root authorization and any compressed input byte cap.
    Lossy encoders start at quality 80 and progressively reduce quality; all
    formats shrink further if needed. Very small, impossible byte budgets raise
    ValueError rather than returning an oversized or different-format preview.
    """
    if isinstance(max_width, bool) or not isinstance(max_width, int) or not 1 <= max_width <= 8192:
        raise ValueError("max_width must be an integer from 1 to 8192")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(format, str):
        raise ValueError("format must be jpeg, png or webp")
    format = format.lower()
    if format == "jpg":
        format = "jpeg"
    if format not in {"jpeg", "png", "webp"}:
        raise ValueError("format must be jpeg, png or webp")

    with Image.open(path) as original:
        original.seek(0)
        oriented = ImageOps.exif_transpose(original)
        try:
            if "A" in oriented.getbands() or "transparency" in oriented.info:
                with oriented.convert("RGBA") as rgba:
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = oriented.convert("RGB")
        finally:
            oriented.close()
    # Avoid carrying large EXIF/ICC/text metadata into the preview.
    image.info.clear()
    try:
        if image.width > max_width:
            size = (max_width, max(1, round(image.height * max_width / image.width)))
            resized = image.resize(size, Image.Resampling.LANCZOS)
            image.close()
            image = resized
        qualities = (80, 65, 50, 35, 20) if format != "png" else (80,)
        while True:
            for quality in qualities:
                data = _encode_image(image, format, quality)
                if len(data) <= max_bytes:
                    return {
                        "data": data,
                        "mime_type": f"image/{format}",
                        "width": image.width,
                        "height": image.height,
                        "bytes": len(data),
                        "format": format,
                    }
            if image.size == (1, 1):
                raise ValueError("image cannot fit max_bytes even at 1x1")
            scale = min(0.85, math.sqrt(max_bytes / len(data)) * 0.9)
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            resized = image.resize(size, Image.Resampling.LANCZOS)
            image.close()
            image = resized
    finally:
        image.close()
