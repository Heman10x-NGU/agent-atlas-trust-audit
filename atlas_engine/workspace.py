"""Local document workspace indexing and search.

Phase 2 keeps SQLite/FTS as the default local-first store, but adds richer
document records, hashing, and conversion hooks so memo outputs can be replayed
against exact source versions.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree


SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx", ".pptx", ".xlsx", ".csv"}
IGNORED_DIRS = {".atlas", ".git", "__pycache__"}
DEFAULT_INDEX_DIR = ".atlas"
DEFAULT_INDEX_FILE = "index.sqlite"


def index_workspace(workspace_path: str | Path, chunk_chars: int = 1200) -> dict:
    """Index supported local documents under a workspace."""
    workspace = Path(workspace_path).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {workspace}")

    index_path = get_index_path(workspace)
    index_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(index_path)
    try:
        has_fts = _init_schema(conn)
        files = list(_iter_supported_files(workspace))
        existing_docs = _existing_documents(conn)
        seen_paths: set[str] = set()
        inserted_files = 0
        skipped_files = 0
        removed_files = 0
        for file_path in files:
            rel_path = file_path.relative_to(workspace).as_posix()
            document = convert_document(file_path, workspace)
            seen_paths.add(rel_path)
            previous = existing_docs.get(rel_path)
            if previous and previous["content_hash"] == document["content_hash"] and previous["converted_hash"] == document["converted_hash"]:
                skipped_files += 1
                continue
            conn.execute("DELETE FROM chunks WHERE source_path = ?", (rel_path,))
            if has_fts:
                conn.execute("DELETE FROM chunks_fts WHERE source_path = ?", (rel_path,))
            conn.execute("DELETE FROM documents WHERE source_path = ?", (rel_path,))
            conn.execute(
                """
                INSERT INTO documents (
                    source_id, source_path, source_uri, title, extension, content_hash,
                    converted_hash, byte_size, modified_time, converter, indexed_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["source_id"],
                    document["source_path"],
                    document["source_uri"],
                    document["title"],
                    document["extension"],
                    document["content_hash"],
                    document["converted_hash"],
                    document["byte_size"],
                    document["modified_time"],
                    document["converter"],
                    document["indexed_at"],
                    json.dumps(document.get("metadata", {}), sort_keys=True),
                ),
            )
            for chunk in _chunks_for_file(rel_path, document["text"], file_path.suffix, chunk_chars):
                conn.execute(
                    """
                    INSERT INTO chunks (chunk_id, source_id, source_path, section, chunk_index, text, locator_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk["chunk_id"],
                        document["source_id"],
                        chunk["source_path"],
                        chunk["section"],
                        chunk["chunk_index"],
                        chunk["text"],
                        json.dumps(chunk.get("locator", {}), sort_keys=True),
                    ),
                )
                if has_fts:
                    conn.execute(
                        """
                        INSERT INTO chunks_fts (chunk_id, source_path, section, text)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            chunk["chunk_id"],
                            chunk["source_path"],
                            chunk["section"],
                            chunk["text"],
                        ),
                    )
            inserted_files += 1

        removed_paths = set(existing_docs) - seen_paths
        for rel_path in removed_paths:
            conn.execute("DELETE FROM chunks WHERE source_path = ?", (rel_path,))
            if has_fts:
                conn.execute("DELETE FROM chunks_fts WHERE source_path = ?", (rel_path,))
            conn.execute("DELETE FROM documents WHERE source_path = ?", (rel_path,))
            removed_files += 1

        conn.execute("DELETE FROM metadata")
        conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", ("workspace", str(workspace)))
        conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", ("fts5", "1" if has_fts else "0"))
        conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", ("indexed_at", _utc_now()))
        conn.commit()
    finally:
        conn.close()

    manifest = source_manifest(workspace)
    chunk_count = _count_rows(index_path, "chunks")
    return {
        "workspace": str(workspace),
        "index_path": str(index_path),
        "indexed_files": inserted_files,
        "skipped_files": skipped_files,
        "removed_files": removed_files,
        "chunks": chunk_count,
        "fts5": has_fts,
        "documents": len(manifest.get("documents", [])),
        "source_manifest": manifest,
    }


def search_workspace(query: str, workspace_path: str | Path, limit: int = 8) -> list[dict]:
    """Search indexed workspace chunks and return citation-ready results."""
    workspace = Path(workspace_path).expanduser().resolve()
    index_path = get_index_path(workspace)
    if not index_path.exists():
        return []

    conn = sqlite3.connect(index_path)
    conn.row_factory = sqlite3.Row
    try:
        has_fts = _has_fts(conn)
        rows = _search_fts(conn, query, limit) if has_fts else []
        if not rows:
            rows = _search_fallback(conn, query, limit)
    finally:
        conn.close()

    return [_row_to_result(row, workspace) for row in rows]


def get_index_path(workspace_path: str | Path) -> Path:
    """Return the default workspace index path."""
    return Path(workspace_path).expanduser().resolve() / DEFAULT_INDEX_DIR / DEFAULT_INDEX_FILE


def source_manifest(workspace_path: str | Path) -> dict:
    """Return the current indexed source manifest for replayability."""
    workspace = Path(workspace_path).expanduser().resolve()
    index_path = get_index_path(workspace)
    documents: list[dict] = []
    generated_at = _utc_now()
    if index_path.exists():
        conn = sqlite3.connect(index_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT source_id, source_path, source_uri, title, extension, content_hash,
                       converted_hash, byte_size, modified_time, converter, indexed_at, metadata_json
                FROM documents
                ORDER BY source_path
                """
            ).fetchall()
            for row in rows:
                record = dict(row)
                metadata_json = record.pop("metadata_json", "{}")
                try:
                    metadata = json.loads(metadata_json or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                record["metadata"] = metadata
                documents.append(record)
        except sqlite3.OperationalError:
            documents = []
        finally:
            conn.close()
    return {
        "workspace": str(workspace),
        "index_path": str(index_path),
        "generated_at": generated_at,
        "documents": documents,
    }


def source_manifest_hash(manifest: dict) -> str:
    """Stable hash of a source manifest's replay-critical fields."""
    replay = {
        "workspace": manifest.get("workspace", ""),
        "documents": [
            {
                "source_path": doc.get("source_path"),
                "content_hash": doc.get("content_hash"),
                "converted_hash": doc.get("converted_hash"),
                "converter": doc.get("converter"),
            }
            for doc in manifest.get("documents", [])
        ],
    }
    payload = json.dumps(replay, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def convert_document(file_path: str | Path, workspace_path: str | Path | None = None) -> dict:
    """Convert a supported document into indexable text plus source metadata."""
    path = Path(file_path).expanduser().resolve()
    workspace = Path(workspace_path).expanduser().resolve() if workspace_path else path.parent
    rel_path = path.relative_to(workspace).as_posix() if path.is_relative_to(workspace) else path.name
    raw = path.read_bytes()
    extension = path.suffix.lower()
    text, converter, metadata = _extract_text(path, raw)
    converted_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    stat = path.stat()
    source_id = "src_" + hashlib.sha1(f"{rel_path}|{hashlib.sha256(raw).hexdigest()}".encode("utf-8")).hexdigest()[:12]
    return {
        "source_id": source_id,
        "source_path": rel_path,
        "source_uri": f"local://{rel_path}",
        "title": path.stem.replace("_", " ").replace("-", " ").title(),
        "extension": extension,
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "converted_hash": converted_hash,
        "byte_size": stat.st_size,
        "modified_time": stat.st_mtime,
        "converter": converter,
        "indexed_at": _utc_now(),
        "metadata": metadata,
        "text": text,
    }


def _init_schema(conn: sqlite3.Connection) -> bool:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            source_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            source_uri TEXT NOT NULL,
            title TEXT NOT NULL,
            extension TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            converted_hash TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            modified_time REAL NOT NULL,
            converter TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id TEXT NOT NULL UNIQUE,
            source_id TEXT,
            source_path TEXT NOT NULL,
            section TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            locator_json TEXT DEFAULT '{}'
        )
        """
    )
    _ensure_column(conn, "chunks", "source_id", "TEXT")
    _ensure_column(conn, "chunks", "locator_json", "TEXT DEFAULT '{}'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(chunk_id, source_path, section, text)
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def _has_fts(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM chunks_fts LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


def _iter_supported_files(workspace: Path) -> Iterable[Path]:
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(workspace).parts):
            continue
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def _chunks_for_file(rel_path: str, text: str, suffix: str, chunk_chars: int) -> list[dict]:
    sections = _markdown_sections(text) if suffix.lower() == ".md" else [("section 1", text)]
    chunks = []
    for section_index, (section, section_text) in enumerate(sections, start=1):
        for chunk_index, chunk_text in enumerate(_split_text(section_text, chunk_chars), start=1):
            chunk_id = f"{rel_path}::section-{section_index}::chunk-{chunk_index}"
            chunks.append({
                "chunk_id": chunk_id,
                "source_path": rel_path,
                "section": section,
                "chunk_index": chunk_index,
                "text": chunk_text,
                "locator": {
                    "source_path": rel_path,
                    "section": section,
                    "chunk_id": chunk_id,
                    "chunk_index": chunk_index,
                },
            })
    return chunks


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    current_title = "section 1"
    current_lines: list[str] = []

    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = heading.group(2).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_title, current_lines))

    return [(title, "\n".join(lines).strip()) for title, lines in sections if "\n".join(lines).strip()]


def _split_text(text: str, chunk_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= chunk_chars:
            current = paragraph
        else:
            chunks.extend(_split_long_text(paragraph, chunk_chars))
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _split_long_text(text: str, chunk_chars: int) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        next_len = current_len + len(word) + (1 if current else 0)
        if current and next_len > chunk_chars:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len = next_len
    if current:
        chunks.append(" ".join(current))
    return chunks


def _search_fts(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    fts_query = " ".join(_tokens(query))
    if not fts_query:
        return []
    try:
        return conn.execute(
            """
            SELECT c.chunk_id, c.source_id, c.source_path, c.section, c.chunk_index, c.text
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ?
            ORDER BY bm25(chunks_fts)
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _search_fallback(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    query_tokens = set(_tokens(query))
    rows = conn.execute(
        """
        SELECT chunk_id, source_id, source_path, section, chunk_index, text
        FROM chunks
        """
    ).fetchall()
    if not query_tokens:
        return rows[:limit]

    def score(row: sqlite3.Row) -> tuple[int, str]:
        haystack = f"{row['source_path']} {row['section']} {row['text']}".lower()
        return (sum(haystack.count(token) for token in query_tokens), row["chunk_id"])

    ranked = sorted(rows, key=score, reverse=True)
    return [row for row in ranked if score(row)[0] > 0][:limit]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_]+", text.lower())


def _row_to_result(row: sqlite3.Row, workspace: Path) -> dict:
    citation = f"local://{row['source_path']}#{row['chunk_id']}"
    source_id = row["source_id"] if "source_id" in row.keys() else ""
    return {
        "title": f"{row['source_path']} — {row['section']}",
        "url": citation,
        "snippet": row["text"][:700],
        "source": "workspace",
        "evidence_class": "private_document",
        "source_id": source_id,
        "source_path": row["source_path"],
        "section": row["section"],
        "chunk_id": row["chunk_id"],
        "chunk_index": row["chunk_index"],
        "workspace_path": str(workspace),
        "citation": {
            "source_path": row["source_path"],
            "section": row["section"],
            "chunk_id": row["chunk_id"],
        },
    }


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _existing_documents(conn: sqlite3.Connection) -> dict[str, dict]:
    try:
        rows = conn.execute(
            """
            SELECT source_path, source_id, content_hash, converted_hash
            FROM documents
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: {"source_id": row[1], "content_hash": row[2], "converted_hash": row[3]} for row in rows}


def _extract_text(path: Path, raw: bytes) -> tuple[str, str, dict]:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace"), "native_text", {}
    if suffix == ".csv":
        return _extract_csv(path), "csv", {}

    markitdown_text = _try_markitdown(path)
    if markitdown_text:
        return markitdown_text, "markitdown", {}

    if suffix == ".docx":
        return _extract_docx(raw), "docx_zip_xml", {"markitdown": "not_used_or_unavailable"}
    if suffix == ".pptx":
        return _extract_pptx(raw), "pptx_zip_xml", {"markitdown": "not_used_or_unavailable"}
    if suffix == ".xlsx":
        return _extract_xlsx(raw), "xlsx_zip_xml", {"markitdown": "not_used_or_unavailable"}
    if suffix == ".pdf":
        return _extract_pdf_fallback(raw), "pdf_binary_fallback", {"markitdown": "not_used_or_unavailable"}
    return path.read_text(encoding="utf-8", errors="replace"), "fallback_text", {}


def _try_markitdown(path: Path) -> str:
    try:
        from markitdown import MarkItDown  # type: ignore
    except Exception:
        return ""
    try:
        converted = MarkItDown().convert(str(path))
    except Exception:
        return ""
    text = getattr(converted, "text_content", None) or getattr(converted, "markdown", None) or str(converted)
    return str(text or "").strip()


def _extract_csv(path: Path) -> str:
    rows = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            rows.append(" | ".join(cell.strip() for cell in row))
    return "\n".join(rows)


def _extract_docx(raw: bytes) -> str:
    import zipfile
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            texts: list[str] = []
            for name in sorted(n for n in archive.namelist() if n.endswith(".xml")):
                data = archive.read(name).decode("utf-8", errors="ignore")
                texts.extend(re.findall(r"<w:t[^>]*>(.*?)</w:t>", data, flags=re.S))
            return _clean_xml_text(texts)
    except Exception:
        return ""


def _extract_pptx(raw: bytes) -> str:
    import zipfile
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            texts: list[str] = []
            for name in sorted(n for n in archive.namelist() if n.endswith(".xml")):
                data = archive.read(name).decode("utf-8", errors="ignore")
                texts.extend(re.findall(r"<a:t[^>]*>(.*?)</a:t>", data, flags=re.S))
            return _clean_xml_text(texts)
    except Exception:
        return ""


def _extract_xlsx(raw: bytes) -> str:
    import zipfile
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            shared_strings = _xlsx_shared_strings(archive)
            lines = []
            for name in sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")):
                data = archive.read(name).decode("utf-8", errors="ignore")
                row_values = []
                for cell_match in re.finditer(r"<c([^>]*)>(.*?)</c>", data, flags=re.S):
                    attrs = cell_match.group(1)
                    body = cell_match.group(2)
                    value_match = re.search(r"<v>(.*?)</v>", body, flags=re.S)
                    inline_match = re.search(r"<is>.*?<t>(.*?)</t>.*?</is>", body, flags=re.S)
                    if inline_match:
                        row_values.append(inline_match.group(1).strip())
                        continue
                    if not value_match:
                        continue
                    cell_type_match = re.search(r't="([^"]+)"', attrs)
                    cell_type = cell_type_match.group(1) if cell_type_match else ""
                    value = value_match.group(1).strip()
                    if cell_type == "s":
                        try:
                            row_values.append(shared_strings[int(value)])
                        except (ValueError, IndexError):
                            row_values.append(value)
                    else:
                        row_values.append(value)
                if row_values:
                    lines.append(" | ".join(row_values))
            return "\n".join(lines)
    except Exception:
        return ""


def _extract_office_xml(raw: bytes, prefixes: list[str], text_path: str) -> str:
    import zipfile
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            names = [
                name for name in archive.namelist()
                if any(name == prefix or name.startswith(prefix) for prefix in prefixes) and name.endswith(".xml")
            ]
            lines = []
            for name in sorted(names):
                root = ElementTree.fromstring(archive.read(name))
                texts = [node.text for node in root.findall(text_path) if node.text]
                if texts:
                    lines.append(" ".join(texts))
            return "\n\n".join(lines)
    except Exception:
        return ""


def _xlsx_shared_strings(archive) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except Exception:
        return []
    strings = []
    ns_path = ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
    for item in root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
        strings.append(" ".join(node.text or "" for node in item.findall(ns_path)).strip())
    return strings


def _extract_pdf_fallback(raw: bytes) -> str:
    text = raw.decode("latin-1", errors="ignore")
    strings = re.findall(r"\(([^()]{3,})\)", text)
    if strings:
        return "\n".join(s.replace("\\n", "\n").replace("\\r", " ") for s in strings)
    return " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9 ,.;:%$+/_-]{4,}", text))


def _clean_xml_text(parts: list[str]) -> str:
    cleaned = [
        re.sub(r"\s+", " ", part.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip())
        for part in parts
        if part and part.strip()
    ]
    return "\n".join(cleaned)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_rows(index_path: Path, table: str) -> int:
    conn = sqlite3.connect(index_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()
