from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import passagen.stages.scanning.service
from passagen.stages.scanning import ScanDirectoryError, scan_directory
from passagen.storage.repository import list_papers


def write_pdf(path: Path, content: bytes = b"content") -> bytes:
    pdf = b"%PDF-1.7\n" + content + b"\n%%EOF\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf)
    return pdf


def test_imports_pdf_into_content_addressed_storage(tmp_path: Path) -> None:
    source_path = tmp_path / "inbox" / "Paper.PDF"
    pdf = write_pdf(source_path)
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"
    sha256 = hashlib.sha256(pdf).hexdigest()

    result = scan_directory(source_path.parent, data_dir=data_dir, database_path=database_path)

    assert len(result.imported) == 1
    assert not result.skipped
    assert not result.failures
    record = result.imported[0]
    assert record.original_filename == "Paper.PDF"
    assert record.file_size_bytes == len(pdf)
    assert record.managed_pdf_path is not None
    assert record.managed_pdf_path == Path("pdfs") / sha256[:2] / f"{sha256}.pdf"
    assert not record.managed_pdf_path.is_absolute()
    assert record.imported_at

    managed_path = data_dir / record.managed_pdf_path
    source_path.unlink()
    assert managed_path.read_bytes() == pdf


def test_skips_duplicate_pdf_content(tmp_path: Path) -> None:
    source_dir = tmp_path / "inbox"
    write_pdf(source_dir / "a.pdf")
    write_pdf(source_dir / "b.pdf")
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"

    first = scan_directory(source_dir, data_dir=data_dir, database_path=database_path)
    second = scan_directory(source_dir, data_dir=data_dir, database_path=database_path)

    assert len(first.imported) == 1
    assert len(first.skipped) == 1
    assert not second.imported
    assert len(second.skipped) == 2
    assert len(list_papers(database_path)) == 1
    assert len(list((data_dir / "pdfs").glob("*/*.pdf"))) == 1


def test_respects_recursive_option(tmp_path: Path) -> None:
    source_dir = tmp_path / "inbox"
    write_pdf(source_dir / "nested" / "paper.pdf")
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"

    shallow = scan_directory(
        source_dir,
        data_dir=data_dir,
        database_path=database_path,
        recursive=False,
    )
    recursive = scan_directory(source_dir, data_dir=data_dir, database_path=database_path)

    assert not shallow.imported
    assert len(recursive.imported) == 1


def test_invalid_pdf_isolated_and_temp_file_removed(tmp_path: Path) -> None:
    source_dir = tmp_path / "inbox"
    source_dir.mkdir()
    (source_dir / "invalid.pdf").write_text("not a PDF")
    (source_dir / "ignored.txt").write_text("not a PDF")
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"

    result = scan_directory(source_dir, data_dir=data_dir, database_path=database_path)

    assert not result.imported
    assert len(result.failures) == 1
    assert result.failures[0].path.name == "invalid.pdf"
    assert not list_papers(database_path)
    assert not list((data_dir / "pdfs" / ".tmp").iterdir())


def test_rejects_truncated_pdf(tmp_path: Path) -> None:
    source_dir = tmp_path / "inbox"
    source_dir.mkdir()
    (source_dir / "truncated.pdf").write_bytes(b"%PDF-1.7\nmissing trailer")
    data_dir = tmp_path / "data"

    result = scan_directory(
        source_dir,
        data_dir=data_dir,
        database_path=data_dir / "passagen.db",
    )

    assert len(result.failures) == 1
    assert "PDF trailer" in result.failures[0].message


def test_database_failure_removes_unreferenced_managed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "inbox"
    pdf = write_pdf(source_dir / "paper.pdf")
    sha256 = hashlib.sha256(pdf).hexdigest()
    data_dir = tmp_path / "data"
    database_path = data_dir / "passagen.db"

    def fail_registration(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(passagen.stages.scanning.service, "register_pdf", fail_registration)

    result = scan_directory(source_dir, data_dir=data_dir, database_path=database_path)

    assert len(result.failures) == 1
    assert not (data_dir / "pdfs" / sha256[:2] / f"{sha256}.pdf").exists()


def test_rejects_missing_scan_directory(tmp_path: Path) -> None:
    with pytest.raises(ScanDirectoryError, match="does not exist"):
        scan_directory(
            tmp_path / "missing",
            data_dir=tmp_path / "data",
            database_path=tmp_path / "data" / "passagen.db",
        )
