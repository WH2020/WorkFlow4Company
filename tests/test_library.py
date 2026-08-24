from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from contextlib import redirect_stdout

from company_platform.cli import command_library_search
from company_platform.library import (
    BlobIntegrityError,
    LibraryConflictError,
    LibraryNotFoundError,
    LibraryStore,
    LibraryValidationError,
    OwnerConfirmationRequired,
)
from company_platform.plugin_registry import load_registry
from company_platform.runtime import RuntimeStore


ROOT = Path(__file__).resolve().parents[1]


def make_docx(text: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return output.getvalue()


def make_split_run_docx(first: str, second: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{first}</w:t></w:r>"
                f"<w:r><w:rPr><w:b/></w:rPr><w:t>{second}</w:t></w:r></w:p>"
                "</w:body></w:document>"
            ),
        )
    return output.getvalue()


def make_dangerous_docx(text: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
        archive.writestr("word/vbaProject.bin", b"macro payload")
    return output.getvalue()


def make_external_relationship_docx(text: str) -> bytes:
    output = io.BytesIO(make_docx(text))
    with zipfile.ZipFile(output, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/_rels/document.xml.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://example.invalid/template" '
                'Target="https://example.invalid/template.dotx" TargetMode="External"/>'
                "</Relationships>"
            ),
        )
    return output.getvalue()


def make_safe_hyperlink_docx(text: str) -> bytes:
    output = io.BytesIO(make_docx(text))
    with zipfile.ZipFile(output, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/_rels/document.xml.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                'Target="https://company.example/" TargetMode="External"/>'
                "</Relationships>"
            ),
        )
    return output.getvalue()


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 16 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


class LibraryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "platform.db"
        self.storage = root / "library"
        self.audit_events: list[tuple[str, str, dict[str, object], str | None]] = []

        def audit(
            action: str,
            result: str,
            details: object,
            payload_sha256: str | None,
            connection: sqlite3.Connection,
        ) -> None:
            self.audit_events.append((action, result, dict(details), payload_sha256))  # type: ignore[arg-type]

        self.store = LibraryStore(self.database, self.storage, audit_callback=audit)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_import_applies_startup_defaults_and_content_addressing(self) -> None:
        item = self.store.import_document(
            "融资BP.md",
            "# 公司商业计划\n解决工业研发协作问题。".encode(),
            category="bp",
            tags=["融资", "2026", "融资"],
        )
        self.assertEqual("confidential", item["confidentiality"])
        self.assertEqual(["融资", "2026"], item["tags"])
        self.assertEqual(item["current_version_id"], item["versions"][0]["version_id"])
        digest = item["versions"][0]["content_sha256"]
        self.assertTrue((self.storage / "blobs" / digest[:2] / digest[2:4] / digest).is_file())
        self.assertEqual("library.item.imported", self.audit_events[-1][0])
        self.assertEqual(64, len(self.audit_events[-1][3] or ""))

    def test_version_note_is_kept_with_immutable_version(self) -> None:
        item = self.store.import_document(
            "融资BP.md",
            "第一版".encode(),
            category="bp",
            version_note="用于首次投资人沟通",
        )
        version = self.store.get_version(item["current_version_id"])
        self.assertEqual("用于首次投资人沟通", version["version_note"])

    def test_patent_defaults_highly_confidential(self) -> None:
        item = self.store.import_document("发明专利.txt", "权利要求一".encode(), category="patent")
        self.assertEqual("highly_confidential", item["confidentiality"])

    def test_version_is_immutable_and_current_switch_requires_confirmation(self) -> None:
        item = self.store.import_document("设计说明.md", "旧架构说明".encode(), category="development")
        first_version = item["current_version_id"]
        second = self.store.add_version(item["item_id"], "设计说明-v2.md", "新架构说明".encode())
        self.assertEqual(first_version, self.store.get_item(item["item_id"])["current_version_id"])
        with self.assertRaises(OwnerConfirmationRequired):
            self.store.set_current_version(item["item_id"], second["version_id"])
        changed = self.store.set_current_version(
            item["item_id"], second["version_id"], owner_confirmed=True
        )
        self.assertEqual(second["version_id"], changed["current_version_id"])
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE library_versions SET original_filename = 'changed.md' WHERE version_id = ?",
                    (first_version,),
                )
        finally:
            connection.close()
        actions = [event[0] for event in self.audit_events]
        self.assertIn("library.version.added", actions)
        self.assertIn("library.current_version.changed", actions)
        current_event = next(
            event
            for event in reversed(self.audit_events)
            if event[0] == "library.current_version.changed"
        )
        self.assertEqual(second["content_sha256"], current_event[2]["target_content_sha256"])
        self.assertEqual(2, current_event[2]["target_version_number"])

    def test_chinese_search_returns_current_version_evidence(self) -> None:
        item = self.store.import_document(
            "开发文档.md",
            "启动流程先校验配置，再建立本地数据库连接，最后启动桌面窗口。".encode(),
            category="development",
        )
        results = self.store.search("本地数据库")
        self.assertEqual(1, len(results))
        self.assertEqual(item["item_id"], results[0]["item_id"])
        self.assertIn("本地数据库", results[0]["snippet"])
        self.assertEqual(results[0]["version_id"], results[0]["evidence"]["version_id"])
        self.assertEqual(64, len(results[0]["evidence"]["content_sha256"]))

    def test_archive_hides_search_and_restore_recovers_it(self) -> None:
        item = self.store.import_document("过期BP.txt", "专注机器人控制器".encode(), category="bp")
        with self.assertRaises(OwnerConfirmationRequired):
            self.store.archive_item(item["item_id"])
        self.store.archive_item(item["item_id"], owner_confirmed=True)
        self.assertEqual([], self.store.search("机器人控制器"))
        self.assertEqual(1, len(self.store.search("机器人控制器", include_archived=True)))
        with self.assertRaises(OwnerConfirmationRequired):
            self.store.restore_item(item["item_id"])
        restored = self.store.restore_item(item["item_id"], owner_confirmed=True)
        self.assertEqual("current", restored["status"])
        self.assertEqual(1, len(self.store.search("机器人控制器")))

    def test_confidentiality_downgrade_requires_confirmation(self) -> None:
        item = self.store.import_document("专利.txt", "未公开技术方案".encode(), category="patent")
        with self.assertRaises(OwnerConfirmationRequired):
            self.store.update_item(item["item_id"], confidentiality="internal")
        updated = self.store.update_item(
            item["item_id"],
            title="专利申请草案",
            confidentiality="internal",
            owner_confirmed=True,
        )
        self.assertEqual("internal", updated["confidentiality"])
        event = next(
            event for event in reversed(self.audit_events) if event[0] == "library.item.updated"
        )
        self.assertEqual(["title", "confidentiality"], event[2]["changed_fields"])
        self.assertEqual(
            {"category": "patent", "confidentiality": "highly_confidential"},
            event[2]["scope_before"],
        )
        self.assertEqual(
            {"category": "patent", "confidentiality": "internal"},
            event[2]["scope_after"],
        )
        self.assertNotEqual(
            event[2]["metadata_sha256_before"]["title"],
            event[2]["metadata_sha256_after"]["title"],
        )

    def test_rejects_unsafe_filename_extension_magic_and_size(self) -> None:
        cases = (
            ("../secret.md", b"safe"),
            ("bad\nname.txt", b"safe"),
            ("CON.txt", b"safe"),
            ("secret.exe", b"MZ"),
            ("fake.pdf", b"not a pdf"),
            ("fake-header.pdf", b"%PDF-1.7\nthis is not a PDF\nMZfake"),
            ("fake.docx", b"PK not a zip"),
            ("binary.txt", b"hello\x00world"),
            ("renamed-executable.txt", b"MZfake executable"),
            ("invalid.json", b"{not-json}"),
        )
        for filename, content in cases:
            with self.subTest(filename=filename), self.assertRaises(LibraryValidationError):
                self.store.import_document(filename, content)
        limited = LibraryStore(
            Path(self.temporary.name) / "limited.db",
            Path(self.temporary.name) / "limited-library",
            max_file_bytes=4,
        )
        with self.assertRaisesRegex(LibraryValidationError, "文件大小"):
            limited.import_document("large.txt", b"12345")

    def test_search_treats_sql_wildcards_as_literal_text(self) -> None:
        self.store.import_document("普通文档.txt", "没有特殊符号".encode())
        self.assertEqual([], self.store.search("%"))
        item = self.store.import_document("指标说明.txt", "毛利率_目标为60%".encode())
        self.assertEqual(item["item_id"], self.store.search("60%")[0]["item_id"])

    def test_office_text_is_extracted_without_third_party_dependencies(self) -> None:
        item = self.store.import_document(
            "技术方案.docx", make_docx("电机控制算法设计"), category="development"
        )
        version = self.store.get_version(item["current_version_id"], include_text=True)
        self.assertEqual("ready", version["extraction_status"])
        self.assertIn("电机控制算法设计", version["extracted_text"])
        self.assertEqual(1, len(self.store.search("控制算法")))

        split = self.store.import_document(
            "跨样式文本.docx",
            make_split_run_docx("电机", "控制"),
            category="development",
        )
        self.assertEqual(split["item_id"], self.store.search("电机控制")[0]["item_id"])

    def test_office_container_rejects_macro_and_active_content(self) -> None:
        with self.assertRaisesRegex(LibraryValidationError, "宏、ActiveX"):
            self.store.import_document("危险方案.docx", make_dangerous_docx("不可执行"))
        with self.assertRaisesRegex(LibraryValidationError, "外部关系"):
            self.store.import_document(
                "外部模板.docx", make_external_relationship_docx("不应连接外部模板")
            )
        safe_link = self.store.import_document(
            "公司官网.docx", make_safe_hyperlink_docx("公司官网")
        )
        self.assertEqual("ready", safe_link["versions"][0]["extraction_status"])

    def test_pdf_text_is_extracted_by_pinned_dependency(self) -> None:
        item = self.store.import_document(
            "patent-roadmap.pdf",
            make_pdf("Patent roadmap and filing strategy"),
            category="patent",
        )
        version = self.store.get_version(item["current_version_id"], include_text=True)
        self.assertEqual("ready", version["extraction_status"])
        self.assertIn("Patent roadmap", version["extracted_text"])
        self.assertEqual(1, len(self.store.search("filing strategy")))

    def test_duplicate_blobs_are_deduplicated_but_duplicate_item_version_is_rejected(self) -> None:
        content = "通用公司介绍".encode()
        first = self.store.import_document("介绍一.txt", content)
        second = self.store.import_document("介绍二.txt", content)
        self.assertEqual(
            first["versions"][0]["content_sha256"], second["versions"][0]["content_sha256"]
        )
        self.assertEqual(1, len(list((self.storage / "blobs").rglob(first["versions"][0]["content_sha256"]))))
        with self.assertRaises(LibraryConflictError):
            self.store.add_version(first["item_id"], "重复.txt", content)

    def test_download_verifies_blob_integrity(self) -> None:
        original = b"immutable content"
        item = self.store.import_document("evidence.txt", original)
        version_id = item["current_version_id"]
        self.assertEqual(original, self.store.read_content(version_id))
        digest = item["versions"][0]["content_sha256"]
        blob = self.storage / "blobs" / digest[:2] / digest[2:4] / digest
        blob.write_bytes(b"tampered")
        with self.assertRaises(BlobIntegrityError):
            self.store.read_content(version_id)

    def test_download_rejects_symbolic_link_blob_path(self) -> None:
        original = b"do not follow symbolic links"
        item = self.store.import_document("link-test.txt", original)
        digest = item["versions"][0]["content_sha256"]
        blob = self.storage / "blobs" / digest[:2] / digest[2:4] / digest
        outside = Path(self.temporary.name) / "outside.bin"
        outside.write_bytes(original)
        blob.unlink()
        try:
            blob.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"当前 Windows 环境不允许创建符号链接：{error}")
        with self.assertRaisesRegex(BlobIntegrityError, "符号链接"):
            self.store.read_content(item["current_version_id"])

    def test_failed_write_rolls_back_metadata_without_online_cas_deletion(self) -> None:
        rollback_root = Path(self.temporary.name) / "rollback"
        database = rollback_root / "platform.db"
        storage = rollback_root / "library"

        def reject_audit(*_args, **_kwargs) -> None:
            raise RuntimeError("audit unavailable")

        store = LibraryStore(database, storage, audit_callback=reject_audit)
        content = b"transactional document"
        digest = hashlib.sha256(content).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            store.import_document("rollback.txt", content)
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM library_items").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM library_versions").fetchone()[0])
        finally:
            connection.close()
        # A failed transaction may leave an unreachable CAS Blob. Online deletion is
        # deliberately forbidden because another transaction may be adopting the same digest.
        self.assertTrue((storage / "blobs" / digest[:2] / digest[2:4] / digest).is_file())
        self.assertEqual([], store.list_items())

    def test_failed_import_cannot_delete_blob_adopted_by_concurrent_import(self) -> None:
        root = Path(self.temporary.name) / "cas-race"
        database = root / "platform.db"
        storage = root / "library"
        activated = threading.Event()
        second_prepared = threading.Event()
        content = b"shared concurrent CAS content"

        def reject_after_activation(*_args, **_kwargs) -> None:
            activated.set()
            if not second_prepared.wait(timeout=3):
                raise TimeoutError("second import did not prepare")
            raise RuntimeError("forced audit rollback")

        first = LibraryStore(database, storage, audit_callback=reject_after_activation)
        second = LibraryStore(database, storage)
        original_prepare = second._prepare_blob

        def observe_prepare(payload: bytes, digest: str):
            if not activated.wait(timeout=3):
                raise TimeoutError("first import did not activate")
            prepared = original_prepare(payload, digest)
            second_prepared.set()
            return prepared

        second._prepare_blob = observe_prepare  # type: ignore[method-assign]
        first_errors: list[BaseException] = []
        second_items: list[dict[str, object]] = []
        second_errors: list[BaseException] = []

        def first_import() -> None:
            try:
                first.import_document("first.txt", content)
            except BaseException as error:
                first_errors.append(error)

        def second_import() -> None:
            try:
                second_items.append(second.import_document("second.txt", content))
            except BaseException as error:
                second_errors.append(error)

        first_thread = threading.Thread(target=first_import)
        second_thread = threading.Thread(target=second_import)
        first_thread.start()
        second_thread.start()
        first_thread.join(timeout=6)
        second_thread.join(timeout=6)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(1, len(first_errors))
        self.assertIsInstance(first_errors[0], RuntimeError)
        self.assertEqual([], second_errors)
        self.assertEqual(1, len(second_items))
        version_id = str(second_items[0]["current_version_id"])
        self.assertEqual(content, second.read_content(version_id))

    def test_missing_item_version_does_not_leave_orphan_blob(self) -> None:
        content = b"must never become an orphan"
        digest = hashlib.sha256(content).hexdigest()
        with self.assertRaisesRegex(LibraryNotFoundError, "资料不存在"):
            self.store.add_version("lib_missing", "missing.txt", content)
        self.assertFalse((self.storage / "blobs" / digest[:2] / digest[2:4] / digest).exists())

    def test_version_staging_does_not_hold_shared_database_write_lock(self) -> None:
        shared_database = Path(self.temporary.name) / "concurrent.db"
        runtime = RuntimeStore(shared_database, load_registry(ROOT), enabled_domains=())
        library = LibraryStore(
            shared_database,
            Path(self.temporary.name) / "concurrent-library",
            audit_callback=runtime.record_platform_audit,
        )
        item = library.import_document("base.txt", b"base version")
        entered_staging = threading.Event()
        release_staging = threading.Event()
        original_prepare = library._prepare_blob

        def delayed_prepare(content: bytes, digest: str):
            entered_staging.set()
            if not release_staging.wait(timeout=3):
                raise TimeoutError("staging test timed out")
            return original_prepare(content, digest)

        library._prepare_blob = delayed_prepare  # type: ignore[method-assign]
        errors: list[BaseException] = []

        def add_version() -> None:
            try:
                library.add_version(item["item_id"], "v2.txt", b"second version")
            except BaseException as error:  # captured and asserted in the parent test thread
                errors.append(error)

        worker = threading.Thread(target=add_version)
        worker.start()
        try:
            self.assertTrue(entered_staging.wait(timeout=2))
            runtime.record_platform_audit(
                "library.concurrent.probe",
                "accepted",
                {"purpose": "verify short database lock"},
            )
        finally:
            release_staging.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)

    def test_does_not_create_parallel_audit_table(self) -> None:
        self.store.import_document("审计.txt", "写操作进入统一审计回调".encode())
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertNotIn("library_audit_events", tables)
        self.assertTrue(self.audit_events)

    def test_runtime_callback_writes_to_unified_platform_audit(self) -> None:
        shared_database = Path(self.temporary.name) / "shared.db"
        runtime = RuntimeStore(shared_database, load_registry(ROOT), enabled_domains=())
        library = LibraryStore(
            shared_database,
            Path(self.temporary.name) / "shared-library",
            audit_callback=runtime.record_platform_audit,
        )
        item = library.import_document("统一审计.txt", "公司资料操作".encode())
        event = runtime.list_audit_events(1)[0]
        self.assertEqual("library.item.imported", event["action"])
        self.assertEqual("accepted", event["result"])
        self.assertEqual(item["item_id"], event["details"]["item_id"])
        self.assertEqual("platform.library", event["node_id"])

    def test_agent_search_returns_internal_evidence_and_excludes_confidential_material(self) -> None:
        runtime_root = Path(self.temporary.name) / "agent-runtime"
        database = runtime_root / "company-platform.db"
        library = LibraryStore(database, runtime_root / "library")
        internal = library.import_document(
            "development.md",
            "Desktop safety roadmap uses immutable versions.".encode(),
            category="development",
        )
        library.import_document(
            "patent.txt",
            "Patent safety roadmap is confidential.".encode(),
            category="patent",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                command_library_search(
                    "safety roadmap",
                    limit=8,
                    runtime_dir=runtime_root,
                ),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual([internal["item_id"]], [item["item_id"] for item in payload["results"]])
        self.assertNotIn("excluded_confidential_count", payload)
        self.assertEqual(64, len(payload["results"][0]["evidence"]["content_sha256"]))

    def test_agent_search_is_not_starved_by_newer_confidential_matches(self) -> None:
        runtime_root = Path(self.temporary.name) / "agent-starvation-runtime"
        database = runtime_root / "company-platform.db"
        library = LibraryStore(database, runtime_root / "library")
        internal = library.import_document(
            "old-internal.txt",
            b"shared agent search phrase",
            category="development",
        )
        for index in range(40):
            library.import_document(
                f"new-confidential-{index}.txt",
                f"shared agent search phrase {index}".encode(),
                category="bp",
            )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                command_library_search(
                    "shared agent search phrase",
                    limit=1,
                    runtime_dir=runtime_root,
                ),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual([internal["item_id"]], [item["item_id"] for item in payload["results"]])
        self.assertNotIn("excluded_confidential_count", payload)


if __name__ == "__main__":
    unittest.main()
