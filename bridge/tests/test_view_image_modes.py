import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from notion_bridge import server
from notion_bridge.file_tools import inspect_image


@pytest.fixture
def image_env(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    workspace = root / 'workspace'
    workspace.mkdir()
    scenes = root / 'scenes'
    scenes.mkdir()
    source = root / 'original.png'
    with Image.new('RGB', (1600, 1000), 'teal') as image:
        image.save(source)
    monkeypatch.setattr(server, 'ALLOWED_ROOTS', [str(root)])
    monkeypatch.setattr(server, 'WORKSPACE_DIR', str(workspace))
    monkeypatch.setattr(server, 'SCENES_DIR', str(scenes))
    monkeypatch.setattr(server, 'audit', lambda *args: None)
    return source, workspace


def test_info_returns_only_small_metadata_without_encoding(image_env, monkeypatch):
    source, workspace = image_env
    monkeypatch.setattr(server, 'encode_preview', lambda *args, **kwargs: pytest.fail('info must not encode'))
    result = server.view_image(str(source), mode='info')
    assert len(result) == 1 and result[0].type == 'text'
    text = result[0].text
    metadata = json.loads(text)
    assert metadata == {'mode': 'info', 'path': str(source), 'width': 1600,
                        'height': 1000, 'format': 'png', 'bytes': source.stat().st_size,
                        'frames': 1}
    assert len(text.encode()) < 1000 and not (workspace / 'artifacts').exists()


@pytest.mark.parametrize('format,suffix', [('jpeg', '.jpg'), ('png', '.png'), ('webp', '.webp')])
def test_file_creates_preview_without_image_content_or_source_changes(image_env, format, suffix):
    source, workspace = image_env
    original_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    result = server.view_image(str(source), max_width=384, format=format, mode='file')
    assert len(result) == 1 and result[0].type == 'text'
    metadata = json.loads(result[0].text)
    preview = metadata['preview']
    path = Path(preview['path'])
    assert path.parent == workspace / 'artifacts' / 'image-previews'
    assert path.suffix == suffix
    assert preview['bytes'] == path.stat().st_size <= 300000
    assert preview['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    with Image.open(path) as image:
        assert image.size == (384, 240)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha
    assert len(result[0].text.encode()) < 1500
    assert 'data' not in preview and 'base64' not in result[0].text


def test_metadata_applies_exif_orientation(tmp_path):
    source = tmp_path / 'rotated.jpg'
    with Image.new('RGB', (80, 40), 'red') as image:
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, exif=exif)
    metadata = inspect_image(source)
    assert (metadata['width'], metadata['height']) == (40, 80)


def test_old_default_still_returns_image_and_text(image_env):
    source, _ = image_env
    result = server.view_image(str(source))
    assert len(result) == 2 and result[1].type == 'text'
    assert 'preview 1024x640' in result[1].text


def test_file_mode_rejects_symlink_output_directory(image_env, tmp_path):
    source, workspace = image_env
    outside = tmp_path.resolve() / 'outside'
    outside.mkdir()
    (workspace / 'artifacts').symlink_to(outside, target_is_directory=True)
    result = server.view_image(str(source), mode='file')
    assert result[0].text.startswith('[rejected]')
    assert not list(outside.iterdir())


def test_modes_keep_read_allowlist_and_reject_unknown_mode(image_env, monkeypatch):
    source, _ = image_env
    assert server.view_image(str(source), mode='unknown')[0].text.startswith('[rejected]')
    monkeypatch.setattr(server, 'ALLOWED_ROOTS', [])
    for mode in ('info', 'file'):
        assert server.view_image(str(source), mode=mode)[0].text.startswith('[rejected]')
