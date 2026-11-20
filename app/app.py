"""档案馆检索索引核对页（Flask）。

上传同册的旧 OCR 导出（页×词）与新软件导出（词×页）两套数据，
核对能否重建同一检索索引；一致时提供旧格式 ZIP 下载。
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

app = Flask(__name__)

FIELDS = [
    ("old_matrix", "旧导出 · 矩阵（页×词）"),
    ("old_pages", "旧导出 · 页键 CSV"),
    ("old_words", "旧导出 · 词表 CSV"),
    ("new_matrix", "新导出 · 矩阵（词×页）"),
    ("new_pages", "新导出 · 页键 CSV"),
    ("new_words", "新导出 · 词表 CSV"),
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
        return render_template(
            "result.html",
            success=True,
            old_ds=old_ds,
            new_ds=new_ds,
            nnz=check["nnz"],
            pages_count=len(check["pages"]),
            words_count=len(check["words"]),
            download_url=url_for("download", token=token),
        )
    finally:
        shutil.rmtree(job, ignore_errors=True)


@app.get("/download/<token>")
def download(token):
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
