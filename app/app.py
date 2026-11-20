"""档案词频阅览包（Flask）。

首页把一套 页×词 词频矩阵及配套的页键、词表 CSV 制成单文件阅览包；
阅览页上传阅览包后输入词（可选填起始页键），按页键顺序列出该词的
非零扫描页及计数，每页 100 条并标明有无后续。
"""

import os
import re
import secrets
import shutil
import tempfile
import time

from flask import (Flask, abort, redirect, render_template, request,
                   send_file, url_for)

import packio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.environ.get("WORK_DIR", os.path.join(BASE_DIR, "work"))
DOWNLOAD_DIR = os.path.join(WORK_DIR, "downloads")
PACKAGE_DIR = os.path.join(WORK_DIR, "packages")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(PACKAGE_DIR, exist_ok=True)

FILE_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_SECONDS", "3600"))
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")

app = Flask(__name__)

FIELDS = [
    ("matrix", "矩阵 matrix.mtx（页×词）"),
    ("pages", "页键 CSV"),
    ("words", "词表 CSV"),
]


def _cleanup(dir_path):
    """惰性清理过期的下载包 / 已上传阅览包（避免目录无限增长）。"""
    deadline = time.time() - FILE_TTL_SECONDS
    try:
        for name in os.listdir(dir_path):
            path = os.path.join(dir_path, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < deadline:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def _pack_path(token):
    """已上传阅览包的路径；token 非法或文件不存在时返回 None。"""
    if not _TOKEN_RE.fullmatch(token or ""):
        return None
    path = os.path.join(PACKAGE_DIR, token + ".zip")
    return path if os.path.isfile(path) else None


# ---------------------------------------------------------------- 制包

@app.get("/")
def index():
    return render_template("index.html")


@app.post("/pack")
def pack():
    _cleanup(DOWNLOAD_DIR)
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
            render_template("pack_result.html",
                            errors=["缺少文件：" + "、".join(missing)]),
            400,
        )

    job = tempfile.mkdtemp(prefix="job-", dir=WORK_DIR)
    try:
        ds, errors = packio.ingest_dataset(
            uploads["matrix"].stream,
            uploads["pages"].stream,
            uploads["words"].stream,
            job,
        )
        if errors:
            return render_template("pack_result.html", errors=errors)

        token = secrets.token_urlsafe(16)
        zip_path = os.path.join(DOWNLOAD_DIR, token + ".zip")
        stats = packio.build_pack(zip_path, ds, job)
        return render_template(
            "pack_result.html",
            success=True,
            ds=ds,
            stats=stats,
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
        download_name="wordfreq-pack.zip",
    )


# ---------------------------------------------------------------- 阅览

@app.get("/viewer")
def viewer():
    token = request.args.get("pack", "")
    info = None
    if token:
        path = _pack_path(token)
        if path is None:
            return render_template(
                "viewer_error.html",
                message="阅览包不存在或已过期，请重新上传",
            ), 404
        try:
            info = packio.pack_summary(path)
        except packio.PackError as exc:
            return render_template("viewer_error.html", message=str(exc))
        info["token"] = token
    return render_template("viewer.html", pack=info)


@app.post("/viewer/upload")
def viewer_upload():
    _cleanup(PACKAGE_DIR)
    fs = request.files.get("pack")
    if fs is None or fs.filename == "":
        return render_template(
            "viewer_error.html", message="缺少文件：请上传阅览包 ZIP"
        ), 400
    token = secrets.token_urlsafe(16)
    path = os.path.join(PACKAGE_DIR, token + ".zip")
    tmp_path = path + ".part"
    with open(tmp_path, "wb") as out:
        shutil.copyfileobj(fs.stream, out, 1 << 20)
    os.replace(tmp_path, path)  # 完整落盘后才可被查询
    try:
        packio.pack_summary(path)  # 上传即校验，坏包不留存
    except packio.PackError as exc:
        os.remove(path)
        return render_template("viewer_error.html", message=str(exc))
    return redirect(url_for("viewer", pack=token))


@app.get("/viewer/query")
def viewer_query():
    token = request.args.get("pack", "")
    word = request.args.get("word", "")
    start = request.args.get("start", "") or None
    if not word:
        return render_template(
            "viewer_error.html", message="请填写要查询的词"
        ), 400
    path = _pack_path(token)
    if path is None:
        return render_template(
            "viewer_error.html",
            message="阅览包不存在或已过期，请重新上传",
        ), 404
    _cleanup(PACKAGE_DIR)
    try:
        result = packio.query_pack(path, word, start)
    except packio.PackError as exc:
        # 所需块缺失或完整性校验失败：整页报错
        return render_template("viewer_error.html", message=str(exc))
    return render_template(
        "viewer_result.html", token=token, word=word, start=start, **result
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
