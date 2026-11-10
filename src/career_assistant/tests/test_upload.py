"""B 组：PDF 上传与简历存储的边界测试。

覆盖分工表模块 4「PDF上传与简历存储」列出的每一种失败与重复场景：
空文件、扫描件、加密文件、解析失败、同名上传、重复请求，以及
10 MB 限制、PDF 真实性校验、随机文件名与私有存储。
"""
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from career_assistant.resume import ResumeError, extract_pdf


def make_pdf(text=None, encrypted=False) -> bytes:
    """构造一份 PDF：text=None 表示没有文字（扫描件），encrypted=True 表示加密。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text is not None:
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode("ascii"))
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("secret-pass")
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload(client, csrf, payload, filename="resume.pdf"):
    """走真实的 multipart 上传通道。"""
    return client.post(
        "/resumes",
        data={"csrf": csrf, "resume": (BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )


# ---------------------------------------------------------------- 文本提取边界

def test_extract_pdf_reads_text():
    assert "Hello Resume" in extract_pdf(make_pdf("Hello Resume 2026"))


def test_extract_pdf_rejects_empty_file():
    with pytest.raises(ResumeError, match="文件为空"):
        extract_pdf(b"")


def test_extract_pdf_rejects_renamed_non_pdf():
    with pytest.raises(ResumeError, match="不是 PDF"):
        extract_pdf(b"plain text pretending to be a pdf")


def test_extract_pdf_rejects_scanned_image_only_document():
    """扫描件没有文字层，本项目不提供 OCR，必须明确拒绝。"""
    with pytest.raises(ResumeError, match="扫描件"):
        extract_pdf(make_pdf())


def test_extract_pdf_rejects_encrypted_document():
    with pytest.raises(ResumeError, match="加密"):
        extract_pdf(make_pdf("Hello Resume", encrypted=True))


def test_extract_pdf_rejects_broken_document():
    with pytest.raises(ResumeError, match="损坏"):
        extract_pdf(b"%PDF-1.4\nbroken body without xref")


def test_extract_pdf_rejects_oversized_document():
    with pytest.raises(ResumeError, match="不能超过 10 MB"):
        extract_pdf(b"%PDF-" + b"0" * (10 * 1024 * 1024))


# ---------------------------------------------------------------- 上传通道边界

def test_upload_requires_login(app, store, csrf):
    """未登录时即使 CSRF 正确也进不去上传通道。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess["csrf"]
    response = upload(client, token, make_pdf("Hello Resume"))
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert not store.list("resumes")


def test_missing_file_is_rejected(auth_client, csrf):
    response = auth_client.post("/resumes", data={"csrf": csrf},
                                content_type="multipart/form-data")
    assert response.status_code == 400
    assert "请选择文本型 PDF" in response.get_data(as_text=True)


def test_empty_file_is_rejected(auth_client, csrf):
    response = upload(auth_client, csrf, b"")
    assert response.status_code == 400
    assert "不是 PDF" in response.get_data(as_text=True)


def test_non_pdf_content_is_rejected(auth_client, csrf):
    """改扩展名不能把文本文件变成 PDF。"""
    response = upload(auth_client, csrf, b"not a pdf at all", filename="resume.pdf")
    assert response.status_code == 400
    assert "不是 PDF" in response.get_data(as_text=True)


def test_oversized_upload_is_rejected(auth_client, csrf):
    payload = b"%PDF-" + b"0" * (10 * 1024 * 1024)
    response = upload(auth_client, csrf, payload)
    assert response.status_code == 413
    assert "10 MB" in response.get_data(as_text=True)


def test_upload_stores_a_random_private_filename(auth_client, store, csrf, app):
    response = upload(auth_client, csrf, make_pdf("Hello Resume"), filename="我的简历.pdf")
    assert response.status_code == 302
    resumes = store.list("resumes")
    assert len(resumes) == 1
    stored = resumes[0]
    assert stored["filename"] == "我的简历.pdf"
    saved = Path(stored["path"])
    assert saved.suffix == ".pdf"
    assert saved.name != "我的简历.pdf"
    assert saved.parent == app.config["UPLOAD_DIR"].resolve()
    assert saved.exists()


def test_same_name_upload_keeps_both_records(auth_client, store, csrf):
    """同名上传不覆盖旧文件，两份记录各自独立。"""
    first = make_pdf("First Resume")
    second = make_pdf("Second Resume")
    upload(auth_client, csrf, first, filename="resume.pdf")
    upload(auth_client, csrf, second, filename="resume.pdf")
    resumes = sorted(store.list("resumes"), key=lambda item: item["id"])
    assert len(resumes) == 2
    assert resumes[0]["id"] != resumes[1]["id"]
    assert resumes[0]["path"] != resumes[1]["path"]
    assert Path(resumes[0]["path"]).read_bytes() == first
    assert Path(resumes[1]["path"]).read_bytes() == second


def test_repeated_request_creates_separate_records_and_tasks(auth_client, store, csrf):
    """重复上传同样内容：各自成为独立记录与独立任务，谁也不覆盖谁。"""
    payload = make_pdf("Same Resume")
    upload(auth_client, csrf, payload)
    upload(auth_client, csrf, payload)
    assert len(store.list("resumes")) == 2
    tasks = store.list("tasks", kind="analyze")
    assert len(tasks) == 2
    assert len({task["payload"]["resume_id"] for task in tasks}) == 2


def test_task_idempotency_key_is_reused(store):
    """同一幂等键重复入队只得到一个任务。"""
    first = store.enqueue("analyze", {"resume_id": 1}, "analyze:1", max_attempts=2)
    second = store.enqueue("analyze", {"resume_id": 1}, "analyze:1", max_attempts=2)
    assert first == second
    assert len(store.list("tasks", kind="analyze")) == 1


def test_stored_pdf_is_only_reachable_after_login(app, store, auth_client, csrf):
    upload(auth_client, csrf, make_pdf("Hello Resume"))
    resume_id = store.list("resumes")[0]["id"]
    anonymous = app.test_client().get(f"/resumes/{resume_id}/pdf")
    assert anonymous.status_code == 302
    assert "/login" in anonymous.headers["Location"]
    signed_in = auth_client.get(f"/resumes/{resume_id}/pdf")
    assert signed_in.status_code == 200
    assert signed_in.mimetype == "application/pdf"
