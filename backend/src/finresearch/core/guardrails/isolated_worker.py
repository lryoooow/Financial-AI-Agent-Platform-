"""Bounded child processes for parsing untrusted files and rendering exports."""

import asyncio
import io
import multiprocessing
import os
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

MAX_BYTES = 12 * 1024 * 1024
MAX_TEXT = 240_000
_SLOTS = asyncio.Semaphore(2)


def check_zip(data: bytes) -> None:
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 1024 or sum(entry.file_size for entry in entries) > 40 * 1024 * 1024:
                raise ValueError("压缩文档展开后超过大小限制。")
            for entry in entries:
                path = PurePosixPath(entry.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
                    raise ValueError("压缩文档含非法路径。")
                if entry.flag_bits & 1 or entry.file_size > 100 * max(1, entry.compress_size):
                    raise ValueError("压缩文档加密或压缩比例超过限制。")
    except BadZipFile as exc:
        raise ValueError("压缩文档格式无效。") from exc


def _read(ext: str, data: bytes) -> str:
    if ext in {".txt", ".md", ".csv"}:
        try:
            return data.decode("utf-8-sig")[:MAX_TEXT]
        except UnicodeDecodeError:
            return data.decode("gb18030", errors="replace")[:MAX_TEXT]
    if ext == ".xlsx":
        check_zip(data)
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        try:
            rows = []
            for sheet in book.worksheets[:10]:
                for row in sheet.iter_rows(max_row=1000, max_col=24, values_only=True):
                    cells = [str(cell)[:200] for cell in row if cell is not None]
                    if cells:
                        rows.append("\t".join(cells))
            return "\n".join(rows)[:MAX_TEXT]
        finally:
            book.close()
    if ext == ".docx":
        check_zip(data)
        from docx import Document

        doc = Document(io.BytesIO(data))
        paragraphs = [paragraph.text for paragraph in doc.paragraphs]
        for table in doc.tables:
            paragraphs.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
        return "\n".join(paragraphs)[:MAX_TEXT]
    if ext == ".pdf":
        if not data.lstrip().startswith(b"%PDF-"):
            raise ValueError("PDF 文件头无效。")
        from pypdf import PdfReader

        pdf = PdfReader(io.BytesIO(data), strict=True)
        if pdf.is_encrypted:
            raise ValueError("请先解密 PDF。")
        if len(pdf.pages) > 150:
            raise ValueError("PDF 最多可导入 150 页。")
        pieces, size = [], 0
        for page in pdf.pages:
            text = (page.extract_text() or "")[:MAX_TEXT - size]
            pieces.append(text)
            size += len(text)
            if size >= MAX_TEXT:
                break
        return "\n".join(pieces)[:MAX_TEXT]
    raise ValueError("支持 PDF、DOCX、XLSX、CSV、Markdown 和 TXT。")


def _child(pipe, operation: str, args: tuple) -> None:
    try:
        if os.name != "nt":
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (15, 18))
        if operation == "parse":
            result = _read(*args)
        else:
            from ...tools import export

            result = {"pdf": export.render_pdf, "docx": export.render_docx}[operation](*args)
        pipe.send((True, result))
    except ValueError as exc:
        pipe.send((False, str(exc) if operation == "parse" else "报告导出失败。"))
    except Exception:
        pipe.send((False, "文件无法解析。" if operation == "parse" else "报告导出失败。"))
    finally:
        pipe.close()


def _run(operation: str, args: tuple) -> bytes | str:
    ctx = multiprocessing.get_context("spawn")
    recv, send = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_child, args=(send, operation, args), daemon=True)
    try:
        worker.start()
        send.close()
        if not recv.poll(20):
            raise ValueError("文档处理超时。")
        try:
            ok, result = recv.recv()
        except EOFError as exc:
            raise ValueError("文档处理进程提前退出。") from exc
        if not ok:
            raise ValueError(result)
        return result
    finally:
        recv.close()
        send.close()
        if worker.pid is not None:
            worker.join(timeout=0.2)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1)
            if worker.is_alive():
                worker.kill()
                worker.join()
            worker.close()


async def _isolated(operation: str, args: tuple) -> bytes | str:
    async with _SLOTS:
        work = asyncio.create_task(asyncio.to_thread(_run, operation, args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # Keep the slot until the child has exited or hit its hard deadline.
            try:
                await asyncio.shield(work)
            except Exception:
                pass
            raise


async def parse_document(ext: str, data: bytes) -> str:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("文档为空或超过 12 MiB。")
    result = await _isolated("parse", (ext, data))
    return str(result).replace("\x00", "")


async def render_export(format: str, doc: dict) -> bytes:
    return await _isolated(format, (doc,))
