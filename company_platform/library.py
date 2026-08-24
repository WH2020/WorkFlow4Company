from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree


DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 2_000_000
MAX_ZIP_MEMBERS = 5_000
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_RELATIONSHIPS_BYTES = 2 * 1024 * 1024

ALLOWED_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".pptx", ".xlsx"})
CATEGORIES = frozenset({"bp", "patent", "development", "general"})
CONFIDENTIALITY_LEVELS = {"internal": 0, "confidential": 1, "highly_confidential": 2}
ITEM_STATUSES = frozenset({"current", "archived"})

AuditCallback = Callable[..., None]


class LibraryError(RuntimeError):
    """公司资料库错误基类。"""


class LibraryValidationError(LibraryError):
    pass


class LibraryNotFoundError(LibraryError):
    pass


class LibraryConflictError(LibraryError):
    pass


class OwnerConfirmationRequired(LibraryError):
    pass


class BlobIntegrityError(LibraryError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clean_text(value: str | None, *, field: str, required: bool = False, maximum: int = 500) -> str:
    cleaned = (value or "").strip()
    if required and not cleaned:
        raise LibraryValidationError(f"{field}不能为空")
    if len(cleaned) > maximum:
        raise LibraryValidationError(f"{field}不能超过 {maximum} 个字符")
    if any(ord(character) < 32 and character not in "\t\r\n" for character in cleaned):
        raise LibraryValidationError(f"{field}包含控制字符")
    return cleaned


def _normalize_tags(tags: Sequence[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags or ():
        cleaned = _clean_text(str(tag), field="标签", maximum=40)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(cleaned)
    if len(normalized) > 30:
        raise LibraryValidationError("标签不能超过 30 个")
    return tuple(normalized)


def _validate_filename(filename: str) -> tuple[str, str]:
    cleaned = _clean_text(filename, field="文件名", required=True, maximum=240)
    if any(ord(character) < 32 for character in cleaned):
        raise LibraryValidationError("文件名不能包含控制字符")
    if cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned or ":" in cleaned:
        raise LibraryValidationError("文件名不能包含路径或盘符")
    if cleaned != Path(cleaned).name:
        raise LibraryValidationError("文件名不能包含路径")
    extension = Path(cleaned).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise LibraryValidationError(f"不支持的文件类型：{extension or '无扩展名'}")
    windows_stem = Path(cleaned).stem.rstrip(" .").upper()
    if (
        windows_stem in {"CON", "PRN", "AUX", "NUL"}
        or re.fullmatch(r"(?:COM|LPT)[1-9]", windows_stem)
        or cleaned.endswith((" ", "."))
    ):
        raise LibraryValidationError("文件名在 Windows 中无效")
    return cleaned, extension


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise LibraryValidationError("文本文件必须使用 UTF-8 或 GB18030 编码")


def _validate_zip(content: bytes, extension: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        members = archive.infolist()
    except (OSError, zipfile.BadZipFile) as error:
        raise LibraryValidationError(f"{extension} 文件结构无效") from error
    if len(members) > MAX_ZIP_MEMBERS:
        archive.close()
        raise LibraryValidationError("Office 文件包含过多内部文件")
    total_size = 0
    for member in members:
        normalized = member.filename.replace("\\", "/")
        if normalized.startswith("/") or "../" in f"/{normalized}":
            archive.close()
            raise LibraryValidationError("Office 文件包含不安全的内部路径")
        if member.file_size > MAX_ZIP_MEMBER_BYTES:
            archive.close()
            raise LibraryValidationError("Office 文件包含超大内部文件")
        total_size += member.file_size
        if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
            archive.close()
            raise LibraryValidationError("Office 文件解压后大小超出限制")
        if member.file_size > 1_000_000 and member.compress_size > 0 and member.file_size / member.compress_size > 200:
            archive.close()
            raise LibraryValidationError("Office 文件压缩比异常")
    names = {member.filename for member in members}
    lower_names = {name.replace("\\", "/").lower() for name in names}
    dangerous_members = sorted(
        name
        for name in lower_names
        if name.endswith("vbaproject.bin")
        or "/activex/" in f"/{name}"
        or "/embeddings/" in f"/{name}"
        or "/externallinks/" in f"/{name}"
    )
    if dangerous_members:
        archive.close()
        raise LibraryValidationError("Office 文件包含宏、ActiveX、嵌入对象或外部数据链接")
    for member in members:
        normalized = member.filename.replace("\\", "/")
        if not normalized.lower().endswith(".rels"):
            continue
        if member.file_size > MAX_RELATIONSHIPS_BYTES:
            archive.close()
            raise LibraryValidationError("Office 文件关系定义过大")
        try:
            relationships = ElementTree.fromstring(archive.read(member))
        except (KeyError, ElementTree.ParseError) as error:
            archive.close()
            raise LibraryValidationError("Office 文件关系定义无效") from error
        for relation in relationships.iter():
            if (
                relation.tag.rsplit("}", 1)[-1] != "Relationship"
                or relation.attrib.get("TargetMode", "").casefold() != "external"
            ):
                continue
            relationship_type = relation.attrib.get("Type", "").rsplit("/", 1)[-1].casefold()
            target = relation.attrib.get("Target", "")
            parsed_target = urlsplit(target)
            safe_hyperlink = relationship_type == "hyperlink" and (
                (
                    parsed_target.scheme.casefold() in {"http", "https"}
                    and bool(parsed_target.netloc)
                )
                or (
                    parsed_target.scheme.casefold() == "mailto"
                    and bool(parsed_target.path)
                )
            )
            if not safe_hyperlink or len(target) > 2_048 or any(ord(char) < 32 for char in target):
                archive.close()
                raise LibraryValidationError("Office 文件包含不受支持的外部关系，已拒绝导入")
    expected = {
        ".docx": "word/document.xml",
        ".pptx": "ppt/presentation.xml",
        ".xlsx": "xl/workbook.xml",
    }[extension]
    if expected not in names or "[Content_Types].xml" not in names:
        archive.close()
        raise LibraryValidationError(f"文件内容与 {extension} 扩展名不匹配")
    return archive


def _validate_content(filename: str, extension: str, content: bytes, maximum: int) -> None:
    if not content:
        raise LibraryValidationError("不接受空文件")
    if len(content) > maximum:
        raise LibraryValidationError(f"文件大小不能超过 {maximum // (1024 * 1024)} MB")
    if content.startswith((b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe")):
        raise LibraryValidationError(f"{filename} 包含可执行文件特征，已拒绝导入")
    if extension == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise LibraryValidationError("文件内容与 PDF 扩展名不匹配")
        _load_pdf_reader(content)
        return
    if extension in {".docx", ".pptx", ".xlsx"}:
        with _validate_zip(content, extension):
            return
    if b"\x00" in content[:8192]:
        raise LibraryValidationError(f"{filename} 看起来是二进制文件，不能作为文本导入")
    text = _decode_text(content)
    if extension == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as error:
            raise LibraryValidationError("JSON 文件内容无效") from error


def _load_pdf_reader(content: bytes) -> Any:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("运行环境缺少固定 PDF 依赖 pypdf，请重新执行安装脚本") from error
    logger = logging.getLogger("pypdf")
    previous_level = logger.level
    try:
        logger.setLevel(logging.ERROR)
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise LibraryValidationError("暂不支持加密 PDF，请解密后再导入")
        if reader.trailer.get("/Root") is None:
            raise LibraryValidationError("PDF 缺少文档目录")
        len(reader.pages)
        return reader
    except LibraryValidationError:
        raise
    except Exception as error:  # pypdf exposes several parser-specific exception classes
        raise LibraryValidationError("PDF 文件结构无效") from error
    finally:
        logger.setLevel(previous_level)


def _xml_text(
    data: bytes,
    accepted_tags: frozenset[str],
    container_tags: frozenset[str],
    maximum_characters: int,
) -> list[str]:
    if maximum_characters <= 0:
        return []
    result: list[str] = []
    active_container_depth = 0
    current_parts: list[str] = []
    current_length = 0
    consumed_characters = 0
    try:
        for event, element in ElementTree.iterparse(
            io.BytesIO(data), events=("start", "end")
        ):
            local_name = element.tag.rsplit("}", 1)[-1]
            if event == "start" and local_name in container_tags:
                if active_container_depth == 0:
                    current_parts = []
                    current_length = 0
                active_container_depth += 1
                continue
            if event != "end":
                continue
            if (
                active_container_depth > 0
                and local_name in accepted_tags
                and element.text
            ):
                remaining = maximum_characters - consumed_characters - current_length
                if remaining > 0:
                    value = element.text[:remaining]
                    current_parts.append(value)
                    current_length += len(value)
            if local_name in container_tags and active_container_depth > 0:
                active_container_depth -= 1
                if active_container_depth == 0:
                    text = "".join(current_parts).strip()
                    if text:
                        result.append(text)
                        consumed_characters += len(text) + 1
            element.clear()
            if consumed_characters >= maximum_characters:
                break
    except ElementTree.ParseError as error:
        raise LibraryValidationError("Office 文件正文 XML 无效") from error
    return result


def _extract_office(content: bytes, extension: str) -> tuple[str, str, str | None]:
    pieces: list[str] = []
    with _validate_zip(content, extension) as archive:
        names = archive.namelist()
        if extension == ".docx":
            selected = sorted(name for name in names if re.fullmatch(r"word/(document|header\d*|footer\d*)\.xml", name))
            tags = frozenset({"t"})
            containers = frozenset({"p"})
        elif extension == ".pptx":
            selected = sorted(
                (name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=lambda value: int(re.search(r"(\d+)", Path(value).stem).group(1)),  # type: ignore[union-attr]
            )
            tags = frozenset({"t"})
            containers = frozenset({"p"})
        else:
            selected = [name for name in names if name == "xl/sharedStrings.xml" or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)]
            tags = frozenset({"t", "v"})
            containers = frozenset({"si", "c"})
        uncompressed_consumed = 0
        extracted_characters = 0
        for name in selected:
            member = archive.getinfo(name)
            if uncompressed_consumed + member.file_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                break
            uncompressed_consumed += member.file_size
            extracted = _xml_text(
                archive.read(name),
                tags,
                containers,
                MAX_EXTRACTED_CHARACTERS - extracted_characters,
            )
            pieces.extend(extracted)
            extracted_characters += sum(len(piece) + 1 for piece in extracted)
            if extracted_characters >= MAX_EXTRACTED_CHARACTERS:
                break
    text = "\n".join(pieces)[:MAX_EXTRACTED_CHARACTERS]
    if text:
        return text, "ready", None
    return "", "unavailable", "未从文件中提取到可检索文字"


def _extract_pdf(content: bytes) -> tuple[str, str, str | None]:
    logger = logging.getLogger("pypdf")
    previous_level = logger.level
    try:
        logger.setLevel(logging.ERROR)
        reader = _load_pdf_reader(content)
        pieces: list[str] = []
        total = 0
        for page in reader.pages[:500]:
            page_text = page.extract_text() or ""
            if page_text:
                remaining = MAX_EXTRACTED_CHARACTERS - total
                pieces.append(page_text[:remaining])
                total += min(len(page_text), remaining)
            if total >= MAX_EXTRACTED_CHARACTERS:
                break
        text = "\n".join(pieces)
        if text:
            return text, "ready", None
        return "", "unavailable", "PDF 不含可提取文字，可能是扫描件"
    except Exception as error:  # pypdf exposes several parser-specific exception classes
        return "", "unavailable", f"PDF 文字提取失败：{type(error).__name__}"
    finally:
        logger.setLevel(previous_level)


def _extract_content(content: bytes, extension: str) -> tuple[str, str, str | None]:
    if extension in {".txt", ".md", ".csv", ".json"}:
        return _decode_text(content)[:MAX_EXTRACTED_CHARACTERS], "ready", None
    if extension in {".docx", ".pptx", ".xlsx"}:
        return _extract_office(content, extension)
    if extension == ".pdf":
        return _extract_pdf(content)
    return "", "unavailable", "当前文件类型不支持文字提取"


def search_library_read_only(
    database_path: Path | str,
    query: str,
    *,
    company_id: str = "company-local",
    confidentiality: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search the current library through a SQLite read-only connection.

    This entry point is used by Pi so a nominally read-only tool cannot create a database,
    schema, directory, index, or audit record as a side effect.
    """
    cleaned_query = _clean_text(query, field="搜索词", required=True, maximum=200)
    if confidentiality is not None and confidentiality not in CONFIDENTIALITY_LEVELS:
        raise LibraryValidationError("资料密级筛选无效")
    source = Path(database_path)
    if not source.exists():
        return []
    if source.is_symlink() or not source.is_file():
        raise LibraryValidationError("资料库数据库不是受限普通文件")
    safe_limit = max(1, min(int(limit), 100))
    escaped = cleaned_query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    connection = sqlite3.connect(source.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('library_items', 'library_versions')"
            ).fetchall()
        }
        if tables != {"library_items", "library_versions"}:
            return []
        confidentiality_clause = " AND i.confidentiality = ?" if confidentiality else ""
        parameters: list[Any] = [company_id]
        if confidentiality:
            parameters.append(confidentiality)
        parameters.extend((pattern, pattern, pattern, pattern, safe_limit))
        rows = connection.execute(
            f"""
            SELECT i.item_id, i.title, i.category, i.confidentiality,
                   v.version_id, v.version_number, v.original_filename,
                   v.content_sha256, v.extracted_text
            FROM library_items i
            JOIN library_versions v ON v.version_id = i.current_version_id
            WHERE i.company_id = ? AND i.status = 'current'
              {confidentiality_clause}
              AND (lower(i.title) LIKE ? ESCAPE '\\' OR lower(v.original_filename) LIKE ? ESCAPE '\\'
                   OR lower(i.tags_json) LIKE ? ESCAPE '\\' OR lower(v.extracted_text) LIKE ? ESCAPE '\\')
            ORDER BY i.updated_at DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    finally:
        connection.close()
    results: list[dict[str, Any]] = []
    folded_query = cleaned_query.casefold()
    for row in rows:
        body = row["extracted_text"] or ""
        position = body.casefold().find(folded_query)
        if position >= 0:
            start = max(0, position - 80)
            end = min(len(body), position + len(cleaned_query) + 120)
            snippet = ("…" if start else "") + body[start:end].replace("\n", " ").strip() + ("…" if end < len(body) else "")
            locator = f"正文字符 {position + 1}–{position + len(cleaned_query)}"
        else:
            snippet = row["title"]
            locator = "标题、文件名或标签"
        results.append(
            {
                "item_id": row["item_id"],
                "version_id": row["version_id"],
                "version_number": row["version_number"],
                "title": row["title"],
                "category": row["category"],
                "confidentiality": row["confidentiality"],
                "original_filename": row["original_filename"],
                "content_sha256": row["content_sha256"],
                "locator": locator,
                "snippet": snippet,
                "evidence": {
                    "item_id": row["item_id"],
                    "version_id": row["version_id"],
                    "content_sha256": row["content_sha256"],
                    "locator": locator,
                },
            }
        )
    return results


class LibraryStore:
    """本地优先、内容寻址的单用户公司资料库。

    ``database_path`` 可以与平台 RuntimeStore 指向同一个 SQLite 文件；本类仅创建
    ``library_*`` 表。审计不另建事实表，而是通过 ``audit_callback`` 注入现有平台审计。
    """

    def __init__(
        self,
        database_path: Path | str,
        storage_root: Path | str,
        *,
        company_id: str = "company-local",
        actor_id: str = "local-admin",
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        audit_callback: AuditCallback | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.storage_root = Path(storage_root)
        self.blob_root = self.storage_root / "blobs"
        self.staging_root = self.storage_root / "staging"
        self.company_id = company_id
        self.actor_id = actor_id
        self.max_file_bytes = max_file_bytes
        self.audit_callback = audit_callback
        self._lock = threading.RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._fts_available = False
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS library_items (
                    item_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidentiality TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_version_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS library_items_scope_status
                    ON library_items(company_id, status, updated_at);

                CREATE TABLE IF NOT EXISTS library_versions (
                    version_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES library_items(item_id) ON DELETE RESTRICT,
                    version_number INTEGER NOT NULL,
                    original_filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    blob_relpath TEXT NOT NULL,
                    extraction_status TEXT NOT NULL,
                    extraction_error TEXT,
                    extracted_text TEXT NOT NULL,
                    version_note TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, version_number),
                    UNIQUE(item_id, content_sha256)
                );
                CREATE INDEX IF NOT EXISTS library_versions_item_time
                    ON library_versions(item_id, version_number DESC);
                CREATE INDEX IF NOT EXISTS library_versions_sha256
                    ON library_versions(content_sha256);

                CREATE TRIGGER IF NOT EXISTS library_versions_immutable_update
                BEFORE UPDATE ON library_versions
                BEGIN
                    SELECT RAISE(ABORT, 'library versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS library_versions_immutable_delete
                BEFORE DELETE ON library_versions
                BEGIN
                    SELECT RAISE(ABORT, 'library versions are immutable');
                END;
                """
            )
            version_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(library_versions)").fetchall()
            }
            if "version_note" not in version_columns:
                connection.execute(
                    "ALTER TABLE library_versions ADD COLUMN version_note TEXT NOT NULL DEFAULT ''"
                )
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(
                        version_id UNINDEXED,
                        item_id UNINDEXED,
                        title,
                        filename,
                        category,
                        tags,
                        body,
                        tokenize='trigram'
                    )
                    """
                )
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False

    def _audit(
        self,
        connection: sqlite3.Connection,
        action: str,
        result: str,
        details: Mapping[str, Any],
        payload: Any | None = None,
    ) -> None:
        if self.audit_callback is not None:
            self.audit_callback(
                action,
                result,
                dict(details),
                payload_sha256=_payload_sha256(payload) if payload is not None else None,
                connection=connection,
            )

    @staticmethod
    def _validate_category(category: str) -> str:
        if category not in CATEGORIES:
            raise LibraryValidationError(f"未知资料分类：{category}")
        return category

    @staticmethod
    def _validate_confidentiality(confidentiality: str) -> str:
        if confidentiality not in CONFIDENTIALITY_LEVELS:
            raise LibraryValidationError(f"未知密级：{confidentiality}")
        return confidentiality

    @staticmethod
    def default_confidentiality(category: str) -> str:
        return "highly_confidential" if category == "patent" else "confidential" if category == "bp" else "internal"

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _prepare_blob(self, content: bytes, digest: str) -> tuple[str, Path | None]:
        """Write and fsync a staging file without holding the shared SQLite write lock."""
        relative = Path(digest[:2]) / digest[2:4] / digest
        destination = self.blob_root / relative
        if self.blob_root.is_symlink() or self.staging_root.is_symlink():
            raise BlobIntegrityError("资料存储根目录不能是符号链接")
        current = self.blob_root
        for part in relative.parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise BlobIntegrityError("资料 Blob 目录包含符号链接")
            current.mkdir(exist_ok=True)
        if destination.exists() or destination.is_symlink():
            existing = self._checked_blob_path(relative.as_posix())
            if existing.stat().st_size != len(content) or self._file_sha256(existing) != digest:
                raise BlobIntegrityError("内容寻址存储中已有同名损坏文件")
            return relative.as_posix(), None
        staging = self.staging_root / f"{uuid4().hex}.part"
        try:
            with staging.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if staging.stat().st_size != len(content) or self._file_sha256(staging) != digest:
                raise BlobIntegrityError("暂存文件哈希校验失败")
            return relative.as_posix(), staging
        except Exception:
            staging.unlink(missing_ok=True)
            raise

    def _activate_blob(
        self,
        staging: Path | None,
        blob_relpath: str,
        digest: str,
        expected_size: int,
    ) -> bool:
        """Atomically expose a prepared Blob; return whether this call created it."""
        if staging is None:
            return False
        destination = self.blob_root / Path(blob_relpath)
        try:
            try:
                os.link(staging, destination)
            except FileExistsError:
                existing = self._checked_blob_path(blob_relpath)
                if existing.stat().st_size != expected_size or self._file_sha256(existing) != digest:
                    raise BlobIntegrityError("并发写入的内容寻址 Blob 完整性异常")
                return False
            except OSError:
                if destination.exists() or destination.is_symlink():
                    existing = self._checked_blob_path(blob_relpath)
                    if existing.stat().st_size != expected_size or self._file_sha256(existing) != digest:
                        raise BlobIntegrityError("并发写入的内容寻址 Blob 完整性异常")
                    return False
                os.replace(staging, destination)
            return True
        except OSError as error:
            raise BlobIntegrityError("资料 Blob 原子激活失败") from error

    def _checked_blob_path(self, blob_relpath: str) -> Path:
        relative = Path(blob_relpath)
        if relative.is_absolute() or len(relative.parts) != 3 or ".." in relative.parts:
            raise BlobIntegrityError("资料 Blob 路径无效")
        if self.blob_root.is_symlink():
            raise BlobIntegrityError("资料 Blob 根目录不能是符号链接")
        root = self.blob_root.resolve(strict=True)
        candidate = self.blob_root / relative
        current = self.blob_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise BlobIntegrityError("资料 Blob 路径包含符号链接")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise BlobIntegrityError("资料原文件缺失或无法读取") from error
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise BlobIntegrityError("资料 Blob 路径越界或不是普通文件")
        return resolved

    def _index_version(self, connection: sqlite3.Connection, item: sqlite3.Row, version: sqlite3.Row) -> None:
        if not self._fts_available:
            return
        connection.execute("DELETE FROM library_fts WHERE version_id = ?", (version["version_id"],))
        connection.execute(
            "INSERT INTO library_fts(version_id, item_id, title, filename, category, tags, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                version["version_id"],
                item["item_id"],
                item["title"],
                version["original_filename"],
                item["category"],
                " ".join(json.loads(item["tags_json"])),
                version["extracted_text"],
            ),
        )

    @staticmethod
    def _row_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "item_id": row["item_id"],
            "company_id": row["company_id"],
            "title": row["title"],
            "category": row["category"],
            "confidentiality": row["confidentiality"],
            "tags": json.loads(row["tags_json"]),
            "description": row["description"],
            "status": row["status"],
            "current_version_id": row["current_version_id"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    @staticmethod
    def _row_version(row: sqlite3.Row, *, include_text: bool = False) -> dict[str, Any]:
        value = {
            "version_id": row["version_id"],
            "item_id": row["item_id"],
            "version_number": row["version_number"],
            "original_filename": row["original_filename"],
            "extension": row["extension"],
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
            "content_sha256": row["content_sha256"],
            "extraction_status": row["extraction_status"],
            "extraction_error": row["extraction_error"],
            "version_note": row["version_note"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        if include_text:
            value["extracted_text"] = row["extracted_text"]
        return value

    def import_document(
        self,
        filename: str,
        content: bytes,
        *,
        title: str | None = None,
        category: str = "general",
        confidentiality: str | None = None,
        tags: Sequence[str] | None = None,
        description: str | None = None,
        version_note: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        category = self._validate_category(category)
        confidentiality = self._validate_confidentiality(confidentiality or self.default_confidentiality(category))
        safe_filename, extension = _validate_filename(filename)
        if not isinstance(content, bytes):
            raise LibraryValidationError("文件内容必须是 bytes")
        _validate_content(safe_filename, extension, content, self.max_file_bytes)
        item_title = _clean_text(title or Path(safe_filename).stem, field="资料标题", required=True, maximum=200)
        item_tags = _normalize_tags(tags)
        item_description = _clean_text(description, field="资料说明", maximum=2_000)
        cleaned_version_note = _clean_text(version_note, field="版本说明", maximum=500)
        digest = hashlib.sha256(content).hexdigest()
        extracted_text, extraction_status, extraction_error = _extract_content(content, extension)
        now = utc_now()
        item_id = f"lib_{uuid4().hex}"
        version_id = f"libv_{uuid4().hex}"
        actor = actor_id or self.actor_id
        media_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
        blob_relpath, staging = self._prepare_blob(content, digest)
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._activate_blob(staging, blob_relpath, digest, len(content))
                connection.execute(
                "INSERT INTO library_items VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, ?, ?, NULL)",
                (item_id, self.company_id, item_title, category, confidentiality, _canonical_json(item_tags), item_description, version_id, actor, now, now),
                )
                connection.execute(
                """
                INSERT INTO library_versions (
                    version_id, item_id, version_number, original_filename, extension,
                    media_type, size_bytes, content_sha256, blob_relpath, extraction_status,
                    extraction_error, extracted_text, version_note, created_by, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id, item_id, safe_filename, extension, media_type, len(content),
                    digest, blob_relpath, extraction_status, extraction_error, extracted_text,
                    cleaned_version_note, actor, now,
                ),
                )
                item_row = connection.execute("SELECT * FROM library_items WHERE item_id = ?", (item_id,)).fetchone()
                version_row = connection.execute("SELECT * FROM library_versions WHERE version_id = ?", (version_id,)).fetchone()
                self._index_version(connection, item_row, version_row)
                details = {"item_id": item_id, "version_id": version_id, "category": category, "confidentiality": confidentiality, "content_sha256": digest, "size_bytes": len(content)}
                self._audit(connection, "library.item.imported", "accepted", details, details)
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
        return self.get_item(item_id)

    def add_version(
        self,
        item_id: str,
        filename: str,
        content: bytes,
        *,
        make_current: bool = False,
        owner_confirmed: bool = False,
        version_note: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        safe_filename, extension = _validate_filename(filename)
        if not isinstance(content, bytes):
            raise LibraryValidationError("文件内容必须是 bytes")
        _validate_content(safe_filename, extension, content, self.max_file_bytes)
        if make_current and not owner_confirmed:
            raise OwnerConfirmationRequired("将新版本设为当前版本需要本人明确确认")
        digest = hashlib.sha256(content).hexdigest()
        cleaned_version_note = _clean_text(version_note, field="版本说明", maximum=500)
        extracted_text, extraction_status, extraction_error = _extract_content(content, extension)
        now = utc_now()
        version_id = f"libv_{uuid4().hex}"
        actor = actor_id or self.actor_id
        media_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
        with self._connection() as connection:
            item_row = connection.execute(
                "SELECT item_id FROM library_items WHERE item_id = ? AND company_id = ?",
                (item_id, self.company_id),
            ).fetchone()
            if item_row is None:
                raise LibraryNotFoundError("资料不存在")
            duplicate = connection.execute(
                "SELECT version_id FROM library_versions WHERE item_id = ? AND content_sha256 = ?",
                (item_id, digest),
            ).fetchone()
            if duplicate is not None:
                raise LibraryConflictError("相同内容已存在于该资料的版本历史中")
        blob_relpath, staging = self._prepare_blob(content, digest)
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                item_row = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ? AND company_id = ?", (item_id, self.company_id)
                ).fetchone()
                if item_row is None:
                    raise LibraryNotFoundError("资料不存在")
                duplicate = connection.execute(
                "SELECT version_id FROM library_versions WHERE item_id = ? AND content_sha256 = ?", (item_id, digest)
                ).fetchone()
                if duplicate is not None:
                    raise LibraryConflictError("相同内容已存在于该资料的版本历史中")
                self._activate_blob(staging, blob_relpath, digest, len(content))
                next_number = connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM library_versions WHERE item_id = ?", (item_id,)
                ).fetchone()[0]
                connection.execute(
                """
                INSERT INTO library_versions (
                    version_id, item_id, version_number, original_filename, extension,
                    media_type, size_bytes, content_sha256, blob_relpath, extraction_status,
                    extraction_error, extracted_text, version_note, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id, item_id, next_number, safe_filename, extension, media_type,
                    len(content), digest, blob_relpath, extraction_status, extraction_error,
                    extracted_text, cleaned_version_note, actor, now,
                ),
                )
                if make_current:
                    connection.execute(
                    "UPDATE library_items SET current_version_id = ?, updated_at = ? WHERE item_id = ?", (version_id, now, item_id)
                    )
                version_row = connection.execute("SELECT * FROM library_versions WHERE version_id = ?", (version_id,)).fetchone()
                latest_item = connection.execute("SELECT * FROM library_items WHERE item_id = ?", (item_id,)).fetchone()
                self._index_version(connection, latest_item, version_row)
                details = {"item_id": item_id, "version_id": version_id, "version_number": next_number, "content_sha256": digest, "made_current": make_current}
                self._audit(connection, "library.version.added", "accepted", details, details)
                if make_current:
                    self._audit(connection, "library.current_version.changed", "accepted", {**details, "confirmed_by": actor}, details)
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
        return self.get_version(version_id, include_text=True)

    def set_current_version(
        self,
        item_id: str,
        version_id: str,
        *,
        owner_confirmed: bool = False,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        if not owner_confirmed:
            raise OwnerConfirmationRequired("切换当前版本需要本人明确确认")
        now = utc_now()
        actor = actor_id or self.actor_id
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ? AND company_id = ?", (item_id, self.company_id)
            ).fetchone()
            if item is None:
                raise LibraryNotFoundError("资料不存在")
            version = connection.execute(
                "SELECT * FROM library_versions WHERE version_id = ? AND item_id = ?", (version_id, item_id)
            ).fetchone()
            if version is None:
                raise LibraryNotFoundError("资料版本不存在或不属于该资料")
            previous = item["current_version_id"]
            if previous == version_id:
                raise LibraryConflictError("该版本已经是当前版本")
            connection.execute(
                "UPDATE library_items SET current_version_id = ?, updated_at = ? WHERE item_id = ?", (version_id, now, item_id)
            )
            details = {
                "item_id": item_id,
                "previous_version_id": previous,
                "current_version_id": version_id,
                "target_version_number": version["version_number"],
                "target_content_sha256": version["content_sha256"],
                "confirmed_by": actor,
            }
            self._audit(connection, "library.current_version.changed", "accepted", details, details)
        return self.get_item(item_id)

    def update_item(
        self,
        item_id: str,
        *,
        title: str | None = None,
        category: str | None = None,
        confidentiality: str | None = None,
        tags: Sequence[str] | None = None,
        description: str | None = None,
        owner_confirmed: bool = False,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ? AND company_id = ?", (item_id, self.company_id)
            ).fetchone()
            if item is None:
                raise LibraryNotFoundError("资料不存在")
            new_title = _clean_text(title if title is not None else item["title"], field="资料标题", required=True, maximum=200)
            new_category = self._validate_category(category if category is not None else item["category"])
            new_confidentiality = self._validate_confidentiality(confidentiality if confidentiality is not None else item["confidentiality"])
            if CONFIDENTIALITY_LEVELS[new_confidentiality] < CONFIDENTIALITY_LEVELS[item["confidentiality"]] and not owner_confirmed:
                raise OwnerConfirmationRequired("降低资料密级需要本人明确确认")
            new_tags = _normalize_tags(tags) if tags is not None else tuple(json.loads(item["tags_json"]))
            new_description = _clean_text(description if description is not None else item["description"], field="资料说明", maximum=2_000)
            previous_metadata = {
                "title": item["title"],
                "category": item["category"],
                "confidentiality": item["confidentiality"],
                "tags": tuple(json.loads(item["tags_json"])),
                "description": item["description"],
            }
            next_metadata = {
                "title": new_title,
                "category": new_category,
                "confidentiality": new_confidentiality,
                "tags": new_tags,
                "description": new_description,
            }
            changed_fields = [
                field
                for field in next_metadata
                if previous_metadata[field] != next_metadata[field]
            ]
            if not changed_fields:
                raise LibraryConflictError("资料信息没有变化")
            now = utc_now()
            connection.execute(
                "UPDATE library_items SET title = ?, category = ?, confidentiality = ?, tags_json = ?, description = ?, updated_at = ? WHERE item_id = ?",
                (new_title, new_category, new_confidentiality, _canonical_json(new_tags), new_description, now, item_id),
            )
            updated = connection.execute("SELECT * FROM library_items WHERE item_id = ?", (item_id,)).fetchone()
            current_version = connection.execute(
                "SELECT * FROM library_versions WHERE version_id = ?",
                (updated["current_version_id"],),
            ).fetchone()
            self._index_version(connection, updated, current_version)
            actor = actor_id or self.actor_id
            details = {
                "item_id": item_id,
                "actor_id": actor,
                "changed_fields": changed_fields,
                "scope_before": {
                    "category": item["category"],
                    "confidentiality": item["confidentiality"],
                },
                "scope_after": {
                    "category": new_category,
                    "confidentiality": new_confidentiality,
                },
                "metadata_sha256_before": {
                    field: _payload_sha256(previous_metadata[field])
                    for field in ("title", "tags", "description")
                },
                "metadata_sha256_after": {
                    field: _payload_sha256(next_metadata[field])
                    for field in ("title", "tags", "description")
                },
            }
            self._audit(connection, "library.item.updated", "accepted", details, details)
        return self.get_item(item_id)

    def archive_item(
        self,
        item_id: str,
        *,
        owner_confirmed: bool = False,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        if not owner_confirmed:
            raise OwnerConfirmationRequired("归档资料需要本人明确确认")
        return self._set_item_status(item_id, "archived", actor_id=actor_id)

    def restore_item(
        self,
        item_id: str,
        *,
        owner_confirmed: bool = False,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        if not owner_confirmed:
            raise OwnerConfirmationRequired("恢复资料需要本人明确确认")
        return self._set_item_status(item_id, "current", actor_id=actor_id)

    def _set_item_status(self, item_id: str, status: str, *, actor_id: str | None) -> dict[str, Any]:
        if status not in ITEM_STATUSES:
            raise LibraryValidationError("资料状态无效")
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT status FROM library_items WHERE item_id = ? AND company_id = ?", (item_id, self.company_id)
            ).fetchone()
            if item is None:
                raise LibraryNotFoundError("资料不存在")
            if item["status"] == status:
                raise LibraryConflictError("资料已经处于目标状态")
            archived_at = now if status == "archived" else None
            connection.execute(
                "UPDATE library_items SET status = ?, archived_at = ?, updated_at = ? WHERE item_id = ?", (status, archived_at, now, item_id)
            )
            action = "library.item.archived" if status == "archived" else "library.item.restored"
            details = {"item_id": item_id, "actor_id": actor_id or self.actor_id, "status": status}
            self._audit(connection, action, "accepted", details, details)
        return self.get_item(item_id)

    def get_item(self, item_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            item = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ? AND company_id = ?", (item_id, self.company_id)
            ).fetchone()
            if item is None:
                raise LibraryNotFoundError("资料不存在")
            versions = connection.execute(
                "SELECT * FROM library_versions WHERE item_id = ? ORDER BY version_number DESC", (item_id,)
            ).fetchall()
        value = self._row_item(item)
        value["versions"] = [self._row_version(version) for version in versions]
        value["current_version"] = next(
            (version for version in value["versions"] if version["version_id"] == value["current_version_id"]), None
        )
        return value

    def get_version(self, version_id: str, *, include_text: bool = False) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT v.* FROM library_versions v
                JOIN library_items i ON i.item_id = v.item_id
                WHERE v.version_id = ? AND i.company_id = ?
                """,
                (version_id, self.company_id),
            ).fetchone()
        if row is None:
            raise LibraryNotFoundError("资料版本不存在")
        return self._row_version(row, include_text=include_text)

    def list_items(
        self,
        *,
        status: str = "current",
        category: str | None = None,
        confidentiality: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if status not in ITEM_STATUSES:
            raise LibraryValidationError("资料状态无效")
        if category is not None:
            self._validate_category(category)
        if confidentiality is not None:
            self._validate_confidentiality(confidentiality)
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        clauses = ["company_id = ?", "status = ?"]
        parameters: list[Any] = [self.company_id, status]
        if category:
            clauses.append("category = ?")
            parameters.append(category)
        if confidentiality:
            clauses.append("confidentiality = ?")
            parameters.append(confidentiality)
        parameters.extend((limit, offset))
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM library_items WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        return [self._row_item(row) for row in rows]

    def search(self, query: str, *, include_archived: bool = False, limit: int = 20) -> list[dict[str, Any]]:
        query = _clean_text(query, field="搜索词", required=True, maximum=200)
        limit = max(1, min(int(limit), 100))
        escaped_query = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_query}%"
        status_clause = "" if include_archived else "AND i.status = 'current'"
        # Small, single-user libraries favor deterministic evidence snippets. FTS narrows candidates
        # when possible; LIKE remains the safe fallback for two-character Chinese terms.
        with self._connection() as connection:
            candidate_ids: list[str] = []
            if self._fts_available and len(query) >= 3:
                fts_query = '"' + query.replace('"', '""') + '"'
                try:
                    candidate_ids = [
                        row[0]
                        for row in connection.execute(
                            "SELECT version_id FROM library_fts WHERE library_fts MATCH ? LIMIT ?", (fts_query, limit * 5)
                        ).fetchall()
                    ]
                except sqlite3.OperationalError:
                    candidate_ids = []
            rows = connection.execute(
                f"""
                SELECT i.*, v.*
                FROM library_items i
                JOIN library_versions v ON v.version_id = i.current_version_id
                WHERE i.company_id = ? {status_clause}
                  AND (lower(i.title) LIKE ? ESCAPE '\\' OR lower(v.original_filename) LIKE ? ESCAPE '\\'
                       OR lower(i.tags_json) LIKE ? ESCAPE '\\' OR lower(v.extracted_text) LIKE ? ESCAPE '\\')
                ORDER BY i.updated_at DESC
                LIMIT ?
                """,
                (self.company_id, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
            if candidate_ids:
                candidate_set = set(candidate_ids)
                rows = sorted(rows, key=lambda row: row["version_id"] not in candidate_set)
        results: list[dict[str, Any]] = []
        folded_query = query.casefold()
        for row in rows[:limit]:
            body = row["extracted_text"] or ""
            folded = body.casefold()
            position = folded.find(folded_query)
            if position >= 0:
                start = max(0, position - 80)
                end = min(len(body), position + len(query) + 120)
                snippet = ("…" if start else "") + body[start:end].replace("\n", " ").strip() + ("…" if end < len(body) else "")
                locator = f"正文字符 {position + 1}–{position + len(query)}"
            else:
                snippet = row["title"]
                locator = "标题、文件名或标签"
            results.append(
                {
                    "item_id": row["item_id"],
                    "version_id": row["version_id"],
                    "version_number": row["version_number"],
                    "title": row["title"],
                    "category": row["category"],
                    "confidentiality": row["confidentiality"],
                    "original_filename": row["original_filename"],
                    "content_sha256": row["content_sha256"],
                    "locator": locator,
                    "snippet": snippet,
                    "evidence": {
                        "item_id": row["item_id"],
                        "version_id": row["version_id"],
                        "content_sha256": row["content_sha256"],
                        "locator": locator,
                    },
                }
            )
        return results

    def read_content(self, version_id: str) -> bytes:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT v.blob_relpath, v.content_sha256, v.size_bytes
                FROM library_versions v JOIN library_items i ON i.item_id = v.item_id
                WHERE v.version_id = ? AND i.company_id = ?
                """,
                (version_id, self.company_id),
            ).fetchone()
        if row is None:
            raise LibraryNotFoundError("资料版本不存在")
        path = self._checked_blob_path(row["blob_relpath"])
        try:
            content = path.read_bytes()
        except OSError as error:
            raise BlobIntegrityError("资料原文件缺失或无法读取") from error
        if len(content) != row["size_bytes"] or hashlib.sha256(content).hexdigest() != row["content_sha256"]:
            raise BlobIntegrityError("资料原文件完整性校验失败")
        return content

    def verify_blob(self, version_id: str) -> dict[str, Any]:
        content = self.read_content(version_id)
        version = self.get_version(version_id)
        return {"version_id": version_id, "valid": True, "size_bytes": len(content), "content_sha256": version["content_sha256"]}

    def statistics(self) -> dict[str, Any]:
        with self._connection() as connection:
            item_counts = connection.execute(
                "SELECT status, COUNT(*) AS count FROM library_items WHERE company_id = ? GROUP BY status", (self.company_id,)
            ).fetchall()
            category_counts = connection.execute(
                "SELECT category, COUNT(*) AS count FROM library_items WHERE company_id = ? AND status = 'current' GROUP BY category", (self.company_id,)
            ).fetchall()
            versions = connection.execute(
                "SELECT COUNT(*) FROM library_versions v JOIN library_items i ON i.item_id = v.item_id WHERE i.company_id = ?", (self.company_id,)
            ).fetchone()[0]
        return {
            "items": {row["status"]: row["count"] for row in item_counts},
            "categories": {row["category"]: row["count"] for row in category_counts},
            "versions": versions,
            "fts_available": self._fts_available,
        }
