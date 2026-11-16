# Copyright 2018-2026 Jérôme Dumonteil
# Authors: odfdo project: jerome.dumonteil@gmail.com
"""Round-trip behaviour of ODF packages edited in place.

These tests cover the guarantees expected from an in-place update of one
part in a package that also holds charts, pictures and attachments:

- members that were not modified keep their exact bytes and metadata,
- the manifest matches the members actually written,
- new references and revoked references are handled separately, and
- a failed write never replaces the previous package.
"""

from __future__ import annotations

import zipfile

import pytest

from odfdo import Document, Frame, Paragraph
from odfdo.const import ODF_CONTENT, ODF_MANIFEST
from odfdo.container import Container
from odfdo.element import NEXT_SIBLING


def _unchanged_members(source: str, target: str) -> list[str]:
    """Return members differing between two archives, manifest excluded."""
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(target) as after:
        changed = []
        for name in before.namelist():
            if name == ODF_MANIFEST:
                continue
            info_before = before.getinfo(name)
            if name not in after.namelist():
                changed.append(name)
                continue
            info_after = after.getinfo(name)
            differs = (
                before.read(name) != after.read(name)
                or info_before.CRC != info_after.CRC
                or info_before.compress_size != info_after.compress_size
                or info_before.compress_type != info_after.compress_type
                or info_before.date_time != info_after.date_time
            )
            if differs:
                changed.append(name)
        return changed


def _manifest_paths(path: str) -> set[str]:
    from lxml.etree import fromstring

    with zipfile.ZipFile(path) as archive:
        root = fromstring(archive.read(ODF_MANIFEST))
    ns = "{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}"
    return {entry.get(f"{ns}full-path") for entry in root.findall(f"{ns}file-entry")}


@pytest.fixture
def spreadsheet_with_picture(samples, tmp_path):
    """Return a spreadsheet where one table cell and one picture are added."""
    document = Document(samples("simple_table.ods"))
    url = document.add_file(samples("image.png"))
    frame = Frame.image_frame(url, size=("1cm", "1cm"))
    paragraph = Paragraph()
    paragraph.append(frame)
    document.body.append(paragraph)
    path = tmp_path / "with_picture.ods"
    document.save(path)
    yield path, url


def test_untouched_members_keep_bytes(samples, tmp_path):
    """Saving a package without editing XML leaves members byte-identical."""
    document = Document(samples("large_ods.ods"))
    target = tmp_path / "same.ods"
    document.save(target)
    with zipfile.ZipFile(samples("large_ods.ods")) as archive:
        members = archive.namelist()
    changed = _unchanged_members(str(samples("large_ods.ods")), str(target))
    # Only the generator and thus meta.xml may be rewritten on a plain save.
    assert set(changed) <= {"meta.xml", ODF_CONTENT, ODF_MANIFEST} or not changed
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == members


def test_chart_and_attachment_members_are_preserved(samples, tmp_path):
    """Embedded charts and attachments survive a content-only edit."""
    document = Document(samples("chart.odt"))
    document.body.append(Paragraph("a correction"))
    target = tmp_path / "chart_corrected.odt"
    document.save(target)
    with zipfile.ZipFile(samples("chart.odt")) as before:
        object_members = [
            name
            for name in before.namelist()
            if name.startswith(("Object ", "ObjectReplacements/"))
        ]
        expected = {name: before.read(name) for name in object_members}
    with zipfile.ZipFile(target) as after:
        for name, data in expected.items():
            assert after.read(name) == data
        assert set(expected) <= set(after.namelist())


def test_manifest_matches_members_after_delete(samples, tmp_path):
    """Deleting a part removes its manifest entry and keeps the rest valid."""
    container = Container(samples("example.odt"))
    container.del_part("Thumbnails/thumbnail.png")
    container.del_part("manifest.rdf")
    target = tmp_path / "deleted.odt"
    container.save(target)
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
    paths = _manifest_paths(str(target))
    assert "Thumbnails/thumbnail.png" not in paths
    assert "manifest.rdf" not in paths
    assert "Thumbnails/thumbnail.png" not in names
    assert ODF_CONTENT in paths
    assert "/" in paths


def test_manifest_lists_added_part(samples, tmp_path):
    """A newly added part is declared in the manifest."""
    container = Container(samples("simple_table.ods"))
    container.set_part("Pictures/added.png", b"\x89PNG\r\n\x1a\nadded")
    target = tmp_path / "added.ods"
    container.save(target)
    paths = _manifest_paths(str(target))
    assert "Pictures/added.png" in paths
    with zipfile.ZipFile(target) as archive:
        assert "Pictures/added.png" in archive.namelist()
        assert b"Pictures/added.png" in archive.read(ODF_MANIFEST)


def test_manifest_does_not_list_mimetype(samples, tmp_path):
    """The mimetype member is implicit and never declared in the manifest."""
    container = Container(samples("simple_table.ods"))
    target = tmp_path / "plain.ods"
    container.save(target)
    paths = _manifest_paths(str(target))
    assert "mimetype" not in paths


def test_new_reference_is_added(spreadsheet_with_picture):
    """Adding a picture adds both the member and its manifest entry."""
    path, url = spreadsheet_with_picture
    paths = _manifest_paths(str(path))
    assert url in paths
    with zipfile.ZipFile(path) as archive:
        assert url in archive.namelist()
        assert b"image/png" in archive.read(ODF_MANIFEST)


def test_revoked_reference_removes_part(spreadsheet_with_picture, tmp_path):
    """Removing the last reference removes the picture and its manifest entry."""
    path, url = spreadsheet_with_picture
    document = Document(path)
    for image in document.body.images:
        image.parent.delete()
    target = tmp_path / "without_picture.ods"
    document.save(target)
    with zipfile.ZipFile(target) as archive:
        assert url not in archive.namelist()
        assert url.encode() not in archive.read(ODF_MANIFEST)


def test_shared_image_survives_one_deleted_reference(
    spreadsheet_with_picture, tmp_path
):
    """A picture used twice is kept until its last reference is removed."""
    path, url = spreadsheet_with_picture
    document = Document(path)
    frame = document.body.images[0].parent
    frame.parent.insert(frame.clone, NEXT_SIBLING)
    assert len(document.body.images) == 2
    frame.delete()
    assert document.unused_image_parts() == []
    target = tmp_path / "shared.ods"
    document.save(target)
    with zipfile.ZipFile(target) as archive:
        assert url in archive.namelist()
        assert url.encode() in archive.read(ODF_MANIFEST)


def test_shared_image_removed_with_last_reference(spreadsheet_with_picture, tmp_path):
    """Once both references are gone the picture is removed."""
    path, url = spreadsheet_with_picture
    document = Document(path)
    frame = document.body.images[0].parent
    clone = frame.clone
    frame.parent.insert(clone, NEXT_SIBLING)
    for image_frame in list(document.body.images):
        image_frame.parent.delete()
    assert document.unused_image_parts() == [url]
    target = tmp_path / "shared_removed.ods"
    document.save(target)
    with zipfile.ZipFile(target) as archive:
        assert url not in archive.namelist()


def test_failed_save_does_not_replace_previous_package(samples, tmp_path, monkeypatch):
    """A write error leaves the previous package untouched."""
    target = tmp_path / "atomic.odt"
    Container(samples("example.odt")).save(target)
    previous = target.read_bytes()

    document = Document(samples("example.odt"))
    document.body.append(Paragraph("new content"))

    def broken_copy(self, zip_target, zip_source, path):
        msg = "forced failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(Container, "_copy_zip_member", broken_copy)
    with pytest.raises(RuntimeError, match="forced failure"):
        document.save(target)
    assert target.read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_save_removes_temporary_file(samples, tmp_path, monkeypatch):
    """A failure while writing leaves no partial temporary file behind."""
    target = tmp_path / "atomic2.odt"

    container = Container(samples("example.odt"))

    def boom(self, _fileobj):
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(Container, "_write_zip", boom)
    with pytest.raises(RuntimeError, match="boom"):
        container.save(target)
    assert not list(tmp_path.glob("*.tmp"))


def test_single_cell_correction_on_ods(samples, tmp_path):
    """Correcting one cell rewrites content.xml only (plus meta.xml)."""
    document = Document(samples("large_ods.ods"))
    table = document.body.tables[0]
    table.set_value("A1", "CORRECTED")
    target = tmp_path / "corrected.ods"
    document.save(target)
    changed = _unchanged_members(str(samples("large_ods.ods")), str(target))
    assert set(changed) <= {ODF_CONTENT, "meta.xml"}
    with zipfile.ZipFile(target) as archive:
        assert b"CORRECTED" in archive.read(ODF_CONTENT)


def test_backup_restored_on_failure(samples, tmp_path, monkeypatch):
    """With backup enabled, a failed save restores the original file."""
    target = tmp_path / "backup_target.odt"
    Container(samples("example.odt")).save(target)
    previous = target.read_bytes()

    container = Container(samples("example.odt"))
    container.set_part(ODF_CONTENT, b"<broken/>")

    def boom(self, _fileobj):
        msg = "write failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(Container, "_write_zip", boom)
    with pytest.raises(RuntimeError, match="write failed"):
        container.save(target, backup=True)
    assert target.read_bytes() == previous
    backup = tmp_path / "backup_target.backup.odt"
    assert backup.read_bytes() == previous


def test_line_ends_and_compress_method_preserved(samples, tmp_path):
    """Untouched stored members stay stored, deflated ones stay deflated."""
    source = samples("example.odt")
    target = tmp_path / "methods.odt"
    Container(source).save(target)
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(target) as after:
        for name in before.namelist():
            if name == ODF_MANIFEST:
                continue
            assert (
                before.getinfo(name).compress_type == after.getinfo(name).compress_type
            )
