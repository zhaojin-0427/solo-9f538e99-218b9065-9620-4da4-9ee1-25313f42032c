"""档案馆检索索引核对页（Flask）。

上传同册的旧 OCR 导出（页×词）与新软件导出（词×页）两套数据，
核对能否重建同一检索索引；一致时提供旧格式 ZIP 与单文件阅览包下载。
另可单独把一套页×词数据制成阅览包，并在阅览页上传阅览包按词检索扫描页。
"""

import os
import re
import secrets
import shutil
import tempfile
import time

from flask import Flask, abort, render_template, request, send_file, url_for

import matrixio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.environ.get("WORK_DIR", os.path.join(BASE_DIR, "work"))
DOWNLOAD_DIR = os.path.join(WORK_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DOWNLOAD_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_SECONDS", "3600"))
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
# 阅览包上传上限：manifest 与查询所需的块都要整体读入内存。
MAX_PACKAGE_BYTES = int(
    os.environ.get("MAX_PACKAGE_BYTES", str(512 * 1024 * 1024)))
QUERY_LIMIT = 100

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_PACKAGE_BYTES

FIELDS = [
    ("old_matrix", "旧导出 · 矩阵（页×词）"),
    ("old_pages", "旧导出 · 页键 CSV"),
    ("old_words", "旧导出 · 词表 CSV"),
    ("new_matrix", "新导出 · 矩阵（词×页）"),
    ("new_pages", "新导出 · 页键 CSV"),
    ("new_words", "新导出 · 词表 CSV"),
]

BROWSE_FIELDS = [
    ("matrix", "矩阵 matrix.mtx（页×词）"),
    ("pages", "页键 CSV"),
    ("words", "词表 CSV"),
]


def _cleanup_downloads():
    """惰性清理过期的 ZIP（避免下载目录无限增长）。"""
    deadline = time.time() - DOWNLOAD_TTL_SECONDS
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < deadline:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def _count_text(v):
    return "未写出该坐标（按 0 计）" if v == 0 else str(v)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/build")
def build_page():
    """制包页：把一套 页×词 数据制成单文件阅览包。"""
    return render_template("browse_build.html")


@app.get("/browse")
def browse_page():
    """阅览页：上传阅览包并按词检索扫描页。"""
    return render_template("browse_query.html")


@app.post("/verify")
def verify():
    _cleanup_downloads()
    uploads = {}
    missing = []
    for key, label in FIELDS:
        fs = request.files.get(key)
        if fs is None or fs.filename == "":
            missing.append(label)
        else:
            uploads[key] = fs
    if missing:
        return (
            render_template("result.html", errors=["缺少文件：" + "、".join(missing)]),
            400,
        )

    job = tempfile.mkdtemp(prefix="job-", dir=WORK_DIR)
    try:
        old_ds, old_errors = matrixio.ingest_dataset(
            "旧导出", "old",
            uploads["old_matrix"].stream,
            uploads["old_pages"].stream,
            uploads["old_words"].stream,
            job,
        )
        new_ds, new_errors = matrixio.ingest_dataset(
            "新导出", "new",
            uploads["new_matrix"].stream,
            uploads["new_pages"].stream,
            uploads["new_words"].stream,
            job,
        )
        errors = old_errors + new_errors
        if errors:
            return render_template("result.html", errors=errors)

        check = matrixio.verify_datasets(old_ds, new_ds, job)
        diff = check["diff"]
        if diff is not None:
            if diff["kind"] == "count":
                diff["old_text"] = _count_text(diff["old"])
                diff["new_text"] = _count_text(diff["new"])
            return render_template(
                "result.html", diff=diff, old_ds=old_ds, new_ds=new_ds
            )

        token = secrets.token_urlsafe(16)
        zip_path = os.path.join(DOWNLOAD_DIR, token + ".zip")
        matrixio.build_zip(
            zip_path, check["pages"], check["words"], check["nnz"],
            check["entries_path"],
        )
        token_browse = secrets.token_urlsafe(16)
        browse_path = os.path.join(DOWNLOAD_DIR, token_browse + ".zip")
        browse = matrixio.build_browse_package(
            browse_path, check["pages"], check["words"], check["nnz"],
            check["entries_path"], job,
        )
        return render_template(
            "result.html",
            success=True,
            old_ds=old_ds,
            new_ds=new_ds,
            nnz=check["nnz"],
            pages_count=len(check["pages"]),
            words_count=len(check["words"]),
            download_url=url_for("download_zip", token=token),
            browse_url=url_for("download_browse", token=token_browse),
            browse_blocks=browse["blocks"],
        )
    finally:
        shutil.rmtree(job, ignore_errors=True)


# ---------------------------------------------------------------- 阅览包


def _publish_browse_package(job, pages, words, nnz, entries_path):
    """在临时目录生成阅览包，成功后放到下载目录；返回 (token, blocks)。"""
    token = secrets.token_urlsafe(16)
    path = os.path.join(DOWNLOAD_DIR, token + ".zip")
    try:
        info = matrixio.build_browse_package(
            path, pages, words, nnz, entries_path, job)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return token, info["blocks"]


@app.post("/browse/build")
def browse_build():
    """把一套 页×词 数据（矩阵 + 页键 + 词表）制成单文件阅览包。"""
    _cleanup_downloads()
    uploads = {}
    missing = []
    for key, label in BROWSE_FIELDS:
        fs = request.files.get(key)
        if fs is None or fs.filename == "":
            missing.append(label)
        else:
            uploads[key] = fs
    if missing:
        return (
            render_template(
                "browse_build.html", errors=["缺少文件：" + "、".join(missing)]),
            400,
        )

    job = tempfile.mkdtemp(prefix="build-", dir=WORK_DIR)
    try:
        ds, errors = matrixio.ingest_dataset(
            "数据", "old",
            uploads["matrix"].stream,
            uploads["pages"].stream,
            uploads["words"].stream,
            job,
        )
        if errors:
            return render_template("browse_build.html", errors=errors)
        canon = matrixio.canonicalize_dataset(ds, job)
        token, blocks = _publish_browse_package(
            job, canon["pages"], canon["words"], canon["nnz"],
            canon["entries_path"])
        return render_template(
            "browse_build.html",
            success=True,
            pages_count=len(canon["pages"]),
            words_count=len(canon["words"]),
            nnz=canon["nnz"],
            blocks=blocks,
            download_url=url_for("download_browse", token=token),
        )
    finally:
        shutil.rmtree(job, ignore_errors=True)


@app.errorhandler(413)
def too_large(_exc):
    return (
        render_template(
            "browse_query.html",
            errors=[f"上传内容过大：单个请求不得超过 "
                    f"{MAX_PACKAGE_BYTES // (1024 * 1024)} MB"]),
        413,
    )


@app.post("/browse/query")
def browse_query():
    """上传阅览包 + 词（可选起始页键），列出前 100 个非零页及计数。"""
    upload = request.files.get("package")
    word = (request.form.get("word") or "").strip()
    start_key = (request.form.get("start_page") or "").strip()
    if upload is None or upload.filename == "":
        return (
            render_template(
                "browse_query.html", errors=["请上传阅览包（.zip）"],
                word=word, start_key=start_key),
            400,
        )
    if not word:
        return (
            render_template(
                "browse_query.html", errors=["请输入要查的词"],
                word=word, start_key=start_key),
            400,
        )
    try:
        result = matrixio.query_browse_package(
            upload.stream, word, start_key or None, QUERY_LIMIT)
    except matrixio.PackageError as exc:
        # 所需块缺失或完整性校验失败：整页报错，不返回部分结果。
        return (
            render_template(
                "browse_query.html", errors=[str(exc)],
                word=word, start_key=start_key),
            422,
        )
    return render_template(
        "browse_query.html", result=result, word=word, start_key=start_key)


@app.get("/download/<token>")
def download_zip(token):
    if not _TOKEN_RE.fullmatch(token):
        abort(404)
    path = os.path.join(DOWNLOAD_DIR, token + ".zip")
    if not os.path.isfile(path):
        abort(404)
    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="pages-x-words.zip",
    )


@app.get("/download/browse/<token>")
def download_browse(token):
    if not _TOKEN_RE.fullmatch(token):
        abort(404)
    path = os.path.join(DOWNLOAD_DIR, token + ".zip")
    if not os.path.isfile(path):
        abort(404)
    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="archive-browse.zip",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
