# -*- coding: utf-8 -*-
"""档案馆核对工具的端到端测试。

运行：python3 -I tests/run_tests.py
（-I 隔离模式下运行，脚本自行把 app 目录加入 sys.path。）
"""

import io
import json
import os
import re
import site
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "app")
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples")
sys.path.insert(0, APP_DIR)
# -I 模式不含用户级 site-packages，显式加回（Flask 安装在那里）
sys.path.append(site.getusersitepackages())

os.environ["WORK_DIR"] = tempfile.mkdtemp(prefix="verify-test-")

import matrixio  # noqa: E402
from app import app  # noqa: E402

# ---------------------------------------------------------------- 数据构造
# 规范数据：p4 为全零页，cherry 为全零词
# counts: (p1,apple)=3 (p1,banana)=1 (p2,apple)=2 (p3,banana)=5

EXPECTED_PAGES = ["p1", "p2", "p3", "p4"]
EXPECTED_WORDS = ["apple", "banana", "cherry"]
EXPECTED_ENTRIES = [(1, 1, 3), (1, 2, 1), (2, 1, 2), (3, 2, 5)]


def mtx(rows, cols, entries, comments=()):
    lines = ["%%MatrixMarket matrix coordinate integer general"]
    lines += ["% " + c for c in comments]
    lines.append(f"{rows} {cols} {len(entries)}")
    lines += [f"{r} {c} {v}" for r, c, v in entries]
    return ("\n".join(lines) + "\n").encode("utf-8")


def csv_bytes(items):
    return ("\n".join(items) + "\n").encode("utf-8")


def build_files(fault=None):
    # 旧导出（页×词）：词表含重复词 apple（第 1、3 列），含重复坐标与显式 0
    old_pages = ["p1", "p2", "p3", "p4"]
    old_words = ["apple", "banana", "apple", "cherry"]
    old_entries = [
        (3, 2, 2), (1, 1, 1), (4, 4, 0), (1, 3, 2),
        (2, 3, 2), (3, 2, 3), (1, 2, 1), (2, 2, 0),
    ]
    # 新导出（词×页）：页顺序不同，词 apple 占第 3、4 行
    new_pages = ["p4", "p2", "p1", "p3"]
    new_words = ["banana", "cherry", "apple", "apple"]
    new_entries = [
        (1, 4, 5), (3, 3, 2), (2, 1, 0), (4, 3, 1), (1, 3, 1), (3, 2, 2),
    ]
    if fault == "count":
        new_entries[0] = (1, 4, 4)  # (p3, banana)：5 -> 4
    elif fault == "missing_page":
        new_pages = ["p2", "p1", "p3"]
        remap = {2: 1, 3: 2, 4: 3}
        new_entries = [(r, remap[c], v) for (r, c, v) in new_entries if c != 1]
    elif fault == "extra_page":
        new_pages = ["p4", "p2", "p1", "p3", "p5"]  # 多一个全零页
    elif fault == "missing_word":
        old_words = ["apple", "banana", "apple"]  # 去掉全零词 cherry
        old_entries = [e for e in old_entries if e[1] != 4]
    return {
        "old_matrix": mtx(len(old_pages), len(old_words), old_entries, ["old"]),
        "old_pages": csv_bytes(old_pages),
        "old_words": csv_bytes(old_words),
        "new_matrix": mtx(len(new_words), len(new_pages), new_entries, ["new"]),
        "new_pages": csv_bytes(new_pages),
        "new_words": csv_bytes(new_words),
    }


def post_verify(client, files):
    data = {k: (io.BytesIO(v), k + ".dat") for k, v in files.items()}
    return client.post("/verify", data=data, content_type="multipart/form-data")


def download_zip(client, html):
    m = re.search(r'/download/([A-Za-z0-9_-]+)', html)
    assert m, "页面缺少下载链接"
    resp = client.get("/download/" + m.group(1))
    assert resp.status_code == 200, resp.status_code
    return zipfile.ZipFile(io.BytesIO(resp.data))


PASSED = []


def check(name, fn):
    fn()
    PASSED.append(name)
    print(f"  ok - {name}")


# ---------------------------------------------------------------- 用例

def t_success_and_zip():
    client = app.test_client()
    resp = post_verify(client, build_files())
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200, resp.status_code
    assert "核对一致" in html, html
    assert ">4</strong> 页" in html and ">3</strong> 个不同的词" in html
    assert ">4</strong> 个非零计数" in html
    zf = download_zip(client, html)
    assert sorted(zf.namelist()) == ["matrix.mtx", "pages.csv", "words.csv"]
    assert zf.read("pages.csv").decode() == "p1\np2\np3\np4\n"
    assert zf.read("words.csv").decode() == "apple\nbanana\ncherry\n"
    lines = zf.read("matrix.mtx").decode().splitlines()
    assert lines[0] == "%%MatrixMarket matrix coordinate integer general"
    dims = [l for l in lines if l and not l.startswith("%")][0]
    assert dims == "4 3 4", dims
    entries = [tuple(map(int, l.split())) for l in lines
               if l and not l.startswith("%")][1:]
    assert entries == EXPECTED_ENTRIES, entries
    # 回环：ZIP 解压后可作为旧格式再次核对
    files = build_files()
    files["old_matrix"] = zf.read("matrix.mtx")
    files["old_pages"] = zf.read("pages.csv")
    files["old_words"] = zf.read("words.csv")
    resp2 = post_verify(client, files)
    assert "核对一致" in resp2.get_data(as_text=True)


def t_count_diff():
    client = app.test_client()
    html = post_verify(client, build_files("count")).get_data(as_text=True)
    assert "计数不同" in html
    assert "p3" in html and "banana" in html
    assert "<td>旧导出</td><td>5</td>" in html
    assert "<td>新导出</td><td>4</td>" in html
    assert "下载" not in html or "下载 ZIP" not in html


def t_missing_page():
    client = app.test_client()
    html = post_verify(client, build_files("missing_page")).get_data(as_text=True)
    assert "缺页" in html and "p4" in html
    assert "旧导出" in html and "新导出" in html


def t_extra_page():
    client = app.test_client()
    html = post_verify(client, build_files("extra_page")).get_data(as_text=True)
    assert "缺页" in html and "p5" in html


def t_missing_word():
    client = app.test_client()
    html = post_verify(client, build_files("missing_word")).get_data(as_text=True)
    assert "缺词" in html and "cherry" in html


def _expect_error(name, files, *fragments):
    client = app.test_client()
    resp = post_verify(client, files)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200, (name, resp.status_code)
    assert "数据格式有误" in html, (name, html)
    for frag in fragments:
        assert frag in html, (name, frag, html)


def t_format_errors():
    base = build_files()

    f = dict(base); f["old_matrix"] = b"%MatrixMarket matrix coordinate integer general\n4 4 0\n"
    _expect_error("banner", f, "首行应为")

    f = dict(base); f["old_matrix"] = mtx(5, 4, [])
    _expect_error("dims-old", f, "与 CSV 行列数不符", "5×4", "页数×词数 = 4×4")

    f = dict(base); f["new_matrix"] = mtx(4, 5, [])
    _expect_error("dims-new", f, "词数×页数 = 4×4")

    f = dict(base); f["old_matrix"] = mtx(4, 4, [(1, 1, -2)])
    _expect_error("negative", f, "为负")

    f = dict(base); f["old_matrix"] = mtx(4, 4, [(1, 9, 1)])
    _expect_error("range", f, "超出范围 1..4")

    f = dict(base)
    f["old_matrix"] = b"%%MatrixMarket matrix coordinate integer general\n4 4 3\n1 1 1\n"
    _expect_error("nnz-short", f, "声明 3 个非零项，实际只读到 1 个")

    f = dict(base)
    f["old_matrix"] = b"%%MatrixMarket matrix coordinate integer general\n4 4 1\n1 1 1\n2 2 2\n"
    _expect_error("nnz-extra", f, "多余内容")

    f = dict(base); f["old_pages"] = b"p1\np2\np1\np4\n"
    _expect_error("dup-page", f, "重复", "页键必须唯一")

    f = dict(base); f["old_words"] = b"apple,x\nbanana,y\napple,z\ncherry,w\n"
    _expect_error("two-cols", f, "单列 CSV")

    f = dict(base); f["old_matrix"] = mtx(4, 4, [(1, 1, 1)]) .replace(b"1 1 1", b"1 1 2.5")
    _expect_error("non-integer", f, "三个整数")

    f = dict(base); f["new_matrix"] = b"%%MatrixMarket matrix coordinate real general\n4 4 0\n"
    _expect_error("field", f, "字段类型", "integer")

    f = dict(base); f["old_matrix"] = b"\xff\xfe%%MatrixMarket"
    _expect_error("encoding", f, "UTF-8")


def t_missing_field():
    client = app.test_client()
    files = build_files()
    del files["new_words"]
    resp = client.post(
        "/verify",
        data={k: (io.BytesIO(v), k) for k, v in files.items()},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "缺少文件" in resp.get_data(as_text=True)


def t_download_guards():
    client = app.test_client()
    assert client.get("/download/nonexistent-token").status_code == 404
    assert client.get("/download/..%2F..%2Fetc").status_code == 404
    assert client.get("/download/browse/nonexistent-token").status_code == 404
    assert client.get("/download/browse/..%2F..%2Fetc").status_code == 404


# ---------------------------------------------------------------- 阅览包

def build_browse(client, files=None):
    """调用制包页，返回 (zipfile.ZipFile, html)。"""
    files = files or {
        "matrix": open(os.path.join(EXAMPLES, "old", "matrix.mtx"), "rb").read(),
        "pages": open(os.path.join(EXAMPLES, "old", "pages.csv"), "rb").read(),
        "words": open(os.path.join(EXAMPLES, "old", "words.csv"), "rb").read(),
    }
    data = {k: (io.BytesIO(v), k + ".dat") for k, v in files.items()}
    resp = client.post("/browse/build", data=data,
                       content_type="multipart/form-data")
    return resp


def post_query(client, raw, word, start=""):
    data = {"package": (io.BytesIO(raw), "pkg.zip"), "word": word,
            "start_page": start}
    return client.post("/browse/query", data=data,
                       content_type="multipart/form-data")


def t_browse_build_and_query():
    client = app.test_client()
    resp = build_browse(client)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "阅览包已生成" in html, html
    m = re.search(r'/download/browse/([A-Za-z0-9_-]+)', html)
    assert m, html
    raw = client.get("/download/browse/" + m.group(1)).data
    zf = zipfile.ZipFile(io.BytesIO(raw))
    assert sorted(zf.namelist()) == [
        "blocks/1.csv", "manifest.json", "pages.csv", "words.csv"], zf.namelist()
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == matrixio.BROWSE_FORMAT
    assert manifest["nnz"] == 4 and manifest["pages_count"] == 4
    assert zf.read("pages.csv").decode() == "p1\np2\np3\np4\n"
    assert zf.read("words.csv").decode() == "apple\nbanana\ncherry\n"
    # 同词 apple 的两列（例中第 1、3 列）合计：p1=3, p2=2
    lines = zf.read("blocks/1.csv").decode().splitlines()
    assert lines == ["1,1,3", "1,2,2", "2,1,1", "2,3,5"], lines

    html = post_query(client, raw, "apple").get_data(as_text=True)
    assert "查询结果" in html and ">2</strong> 个非零页" in html, html
    assert "<code>p1</code>" in html and "<code>p2</code>" in html
    body = html.split("<table>")[1].split("</table>")[0]
    assert body.index("p1") < body.index("p2")
    assert "<td>3</td>" in body and "<td>2</td>" in body
    assert "已到末尾，没有后续" in html
    assert "1 / 1 块" in html


def t_browse_query_start_and_limits():
    """起始页键之后开始，最多 100 个，并标明有无后续。"""
    client = app.test_client()
    raw = _browse_raw(client)
    html = post_query(client, raw, "apple", "p1").get_data(as_text=True)
    assert "起始页键 <code>p1</code> 之后" in html
    body = html.split("<table>")[1].split("</table>")[0]
    assert "<code>p2</code>" in body and "<code>p1</code>" not in body
    # 起始页键在词的所有出现之后
    html = post_query(client, raw, "apple", "p2").get_data(as_text=True)
    assert "起始页键之后没有该词" in html
    assert "2</strong> 个页" in html
    # 起始页键不在包中
    html = post_query(client, raw, "apple", "p9").get_data(as_text=True)
    assert "不在本包的页键表中" in html
    # 分页：造一个 130 页都含该词的包
    P = 130
    mtx = mtx_bytes(P, 1, [(i, 1, i) for i in range(1, P + 1)])
    pages = [f"page-{i:03d}" for i in range(1, P + 1)]
    files = {
        "matrix": mtx,
        "pages": ("\n".join(pages) + "\n").encode(),
        "words": b"target\n",
    }
    raw = _browse_raw(client, files)
    html = post_query(client, raw, "target").get_data(as_text=True)
    body = html.split("<table>")[1].split("</table>")[0]
    assert body.count("<tr>") == 101, body.count("<tr>")  # 含表头行
    assert "还有后续页" in html
    assert "page-100" in body and "page-101" not in body
    # 从第 100 页之后继续
    html = post_query(client, raw, "target", "page-100").get_data(as_text=True)
    body = html.split("<table>")[1].split("</table>")[0]
    assert body.count("<tr>") == 31, body.count("<tr>")  # 30 行 + 表头
    assert "page-101" in body and "page-130" in body
    assert "已到末尾，没有后续" in html


def t_browse_not_in_vocab_vs_zero_total():
    client = app.test_client()
    raw = _browse_raw(client)
    html = post_query(client, raw, "zebra").get_data(as_text=True)
    assert "词表中没有这个词" in html
    assert "zebra" in html
    html = post_query(client, raw, "cherry").get_data(as_text=True)
    assert "词在全卷计数为零" in html
    assert "词表中没有这个词" not in html
    assert "cherry" in html


def t_browse_block_spans_words():
    """每块至多 512 条；一个词可以跨块，查询合并各块。"""
    old = matrixio.BLOCK_MAX_ROWS
    matrixio.BLOCK_MAX_ROWS = 2
    try:
        client = app.test_client()
        files = {
            "matrix": mtx_bytes(3, 2, [(1, 1, 1), (2, 1, 2), (3, 2, 3),
                                       (1, 2, 4), (3, 1, 5)]),
            "pages": b"p1\np2\np3\n",
            "words": b"w0\nw1\n",
        }
        resp = client.post(
            "/browse/build",
            data={k: (io.BytesIO(v), k) for k, v in files.items()},
            content_type="multipart/form-data")
        html = resp.get_data(as_text=True)
        m = re.search(r'/download/browse/([A-Za-z0-9_-]+)', html)
        raw = client.get("/download/browse/" + m.group(1)).data
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(raw))
                              .read("manifest.json"))
        assert len(manifest["blocks"]) == 3, manifest["blocks"]
        assert manifest["blocks"][0]["last_word"] == 1 and \
            manifest["blocks"][1]["first_word"] == 1, manifest["blocks"]
        assert all(b["rows"] <= 2 for b in manifest["blocks"])
        # w0 覆盖块 1、2（跨块），合并后 p1=1 p2=2 p3=5
        html = post_query(client, raw, "w0").get_data(as_text=True)
        body = html.split("<table>")[1].split("</table>")[0]
        assert "<code>p1</code>" in body and "<code>p2</code>" in body
        assert "<td>1</td>" in body and "<td>2</td>" in body
        assert "<td>5</td>" in body
        assert "2 / 3 块" in html
    finally:
        matrixio.BLOCK_MAX_ROWS = old


def t_browse_corrupt_blocks():
    """所需块损坏 -> 整页报错；无关块损坏 -> 仍可查询。"""
    client = app.test_client()
    raw = _browse_raw(client)
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("manifest.json"))
    block_name = manifest["blocks"][0]["name"]

    def broken(name, mutate):
        out = io.BytesIO()
        src = zipfile.ZipFile(io.BytesIO(raw))
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == name:
                    data = mutate(data)
                zf.writestr(info.filename, data)
        return out.getvalue()

    # 所需块内容被改坏
    bad = broken(block_name, lambda d: d.replace(b"3", b"9", 1))
    resp = post_query(client, bad, "apple")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 422 and "完整性校验失败" in html, html
    assert "<table>" not in html
    # 所需块被删掉
    out = io.BytesIO()
    src = zipfile.ZipFile(io.BytesIO(raw))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for info in src.infolist():
            if info.filename != block_name:
                zf.writestr(info.filename, src.read(info.filename))
    resp = post_query(client, out.getvalue(), "apple")
    assert resp.status_code == 422
    assert "包不完整" in resp.get_data(as_text=True)
    # 无关块损坏（本例只有一个块，另造一个多块包）
    big = {"matrix": mtx_bytes(3, 2, [(1, 1, 1), (2, 1, 2), (3, 2, 3),
                                      (1, 2, 4)]),
           "pages": b"p1\np2\np3\n", "words": b"w0\nw1\n"}
    old = matrixio.BLOCK_MAX_ROWS
    matrixio.BLOCK_MAX_ROWS = 2
    try:
        raw2 = _browse_raw(client, big)
    finally:
        matrixio.BLOCK_MAX_ROWS = old
    m2 = json.loads(zipfile.ZipFile(io.BytesIO(raw2)).read("manifest.json"))
    # 改坏最后一块（w1 所在），查询 w0 不受影响
    bad2 = broken_call(raw2, m2["blocks"][-1]["name"])
    html = post_query(client, bad2, "w0").get_data(as_text=True)
    assert "查询结果" in html and "<code>p1</code>" in html, html
    # 而查询 w1 时整页报错
    resp = post_query(client, bad2, "w1")
    assert resp.status_code == 422
    # 非 ZIP
    resp = post_query(client, b"not a zip at all", "apple")
    assert resp.status_code == 422
    assert "不是有效的 ZIP" in resp.get_data(as_text=True)


def broken_call(raw, name):
    out = io.BytesIO()
    src = zipfile.ZipFile(io.BytesIO(raw))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == name:
                data = data.replace(b",", b";", 1)
            zf.writestr(info.filename, data)
    return out.getvalue()


def t_browse_manifest_guards():
    client = app.test_client()
    raw = _browse_raw(client)
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("manifest.json"))

    def rewrap(m):
        out = io.BytesIO()
        src = zipfile.ZipFile(io.BytesIO(raw))
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == "manifest.json":
                    data = json.dumps(m, ensure_ascii=False).encode()
                zf.writestr(info.filename, data)
        return out.getvalue()

    m = dict(manifest); m["format"] = "other"
    assert "不是本工具的阅览包" in post_query(
        client, rewrap(m), "apple").get_data(as_text=True)
    m = dict(manifest); m["version"] = 99
    assert "不支持的阅览包版本" in post_query(
        client, rewrap(m), "apple").get_data(as_text=True)
    m = dict(manifest); m["nnz"] = 999
    assert "包不完整" in post_query(
        client, rewrap(m), "apple").get_data(as_text=True)
    m = dict(manifest); m["words_sha256"] = "0" * 64
    assert "完整性校验失败" in post_query(
        client, rewrap(m), "apple").get_data(as_text=True)


def t_browse_build_errors():
    client = app.test_client()
    files = {
        "matrix": mtx_bytes(4, 4, [(1, 1, -1)]),
        "pages": b"p1\np2\np3\np4\n",
        "words": b"a\nb\nc\nd\n",
    }
    resp = client.post("/browse/build",
                       data={k: (io.BytesIO(v), k) for k, v in files.items()},
                       content_type="multipart/form-data")
    assert "为负" in resp.get_data(as_text=True)
    files = {"matrix": mtx_bytes(4, 4, []), "words": b"a\nb\nc\nd\n"}
    resp = client.post("/browse/build",
                       data={k: (io.BytesIO(v), k) for k, v in files.items()},
                       content_type="multipart/form-data")
    assert "缺少文件" in resp.get_data(as_text=True)
    resp = client.post("/browse/build", data={},
                       content_type="multipart/form-data")
    assert "缺少文件" in resp.get_data(as_text=True)
    # 查询缺词 / 缺包
    resp = post_query(client, b"x", "")
    assert resp.status_code == 400 and "输入要查的词" in resp.get_data(as_text=True)
    resp = client.post("/browse/query", data={},
                       content_type="multipart/form-data")
    assert resp.status_code == 400 and "上传阅览包" in resp.get_data(as_text=True)


def t_browse_memory_bounded():
    """制包与查询的内存不随非零项数增长。"""
    import gc
    import random
    import tracemalloc

    random.seed(11)
    P, W, N = 400, 300, 600_000
    work = tempfile.mkdtemp(prefix="browse-big-")
    path = os.path.join(work, "big.mtx")
    with open(path, "w") as f:
        f.write("%%MatrixMarket matrix coordinate integer general\n")
        f.write(f"{P} {W} {N}\n")
        for _ in range(N):
            f.write(f"{random.randint(1, P)} {random.randint(1, W)} "
                    f"{random.randint(0, 5)}\n")
    pages = [f"page-{i:04d}" for i in range(P)]
    words = [f"w-{i:04d}" for i in range(W)]
    gc.collect()

    tracemalloc.start()
    try:
        ds, errs = matrixio.ingest_dataset(
            "数据", "old", open(path, "rb"),
            io.BytesIO(("\n".join(pages) + "\n").encode()),
            io.BytesIO(("\n".join(words) + "\n").encode()), work)
        assert not errs, errs
        canon = matrixio.canonicalize_dataset(ds, work)
        pkg = os.path.join(work, "big.zip")
        info = matrixio.build_browse_package(
            pkg, canon["pages"], canon["words"], canon["nnz"],
            canon["entries_path"], work)
        assert info["blocks"] >= 1
        with open(pkg, "rb") as f:
            result = matrixio.query_browse_package(f, words[0])
        assert result["status"] in ("ok", "zero_total")
        _, peak = tracemalloc.get_traced_memory()
        # 外包一层读取用的内存不计：build 期间在 tracemalloc 监控下
    finally:
        tracemalloc.stop()
    peak_mb = peak / 1e6
    print(f"    [nnz≈{N:,} 条输入、{info['blocks']} 块，Python 侧内存峰值 "
          f"{peak_mb:.1f} MB]")
    assert peak_mb < 64, f"内存峰值 {peak_mb:.1f} MB 超出 64 MB 上限"
    # 块数符合每块至多 512 条
    zf = zipfile.ZipFile(pkg)
    manifest = json.loads(zf.read("manifest.json"))
    assert all(b["rows"] <= matrixio.BLOCK_MAX_ROWS for b in manifest["blocks"])
    assert sum(b["rows"] for b in manifest["blocks"]) == canon["nnz"]


def _browse_raw(client, files=None):
    resp = build_browse(client, files)
    html = resp.get_data(as_text=True)
    m = re.search(r'/download/browse/([A-Za-z0-9_-]+)', html)
    assert m, html
    return client.get("/download/browse/" + m.group(1)).data


def mtx_bytes(rows, cols, entries):
    return mtx(rows, cols, entries)


def t_spooled_stream():
    """werkzeug 的 SpooledTemporaryFile 也能正确包装读取。"""
    sp = tempfile.SpooledTemporaryFile(max_size=4)  # 强制落盘
    sp.write(b"%%MatrixMarket matrix coordinate integer general\n1 1 1\n1 1 5\n")
    sp.seek(0)
    work = tempfile.mkdtemp(prefix="verify-unit-")
    ds, errors = matrixio.ingest_dataset(
        "旧导出", "old", sp, io.BytesIO(b"p1\n"), io.BytesIO(b"w1\n"), work
    )
    assert not errors, errors
    assert ds.nnz == 1 and ds.rows == 1 and ds.cols == 1


def t_memory_bounded():
    """非零项约 200 万时，Python 侧内存峰值保持有界（不随 nnz 增长）。"""
    import gc
    import random
    import tracemalloc

    random.seed(7)
    P, W, N = 300, 200, 1_000_000
    pages = [f"page-{i:04d}" for i in range(P)]
    words = [f"w-{i:04d}" for i in range(W)]

    work = tempfile.mkdtemp(prefix="verify-big-")
    old_path = os.path.join(work, "old.mtx")
    with open(old_path, "w") as f:
        f.write("%%MatrixMarket matrix coordinate integer general\n")
        f.write(f"{P} {W} {N}\n")
        for _ in range(N):
            f.write(f"{random.randint(1, P)} {random.randint(1, W)} "
                    f"{random.randint(0, 5)}\n")

    # 由旧文件聚合出规范计数，再生成等价的新导出（词×页，含拆分重复行）
    counts = {}
    with open(old_path) as f:
        next(f)
        dims = next(f)
        for line in f:
            r, c, v = line.split()
            counts[(int(r), int(c))] = counts.get((int(r), int(c)), 0) + int(v)
    counts = {k: v for k, v in counts.items() if v}
    new_entries = []
    for (r, c), v in counts.items():
        left = v
        while left > 0:
            part = random.randint(1, left)
            new_entries.append((c, r, part))
            left -= part
    random.shuffle(new_entries)
    new_path = os.path.join(work, "new.mtx")
    with open(new_path, "w") as f:
        f.write("%%MatrixMarket matrix coordinate integer general\n")
        f.write(f"{W} {P} {len(new_entries)}\n")
        for r, c, v in new_entries:
            f.write(f"{r} {c} {v}\n")
    del counts, new_entries
    gc.collect()

    pages_b = ("\n".join(pages) + "\n").encode()
    words_b = ("\n".join(words) + "\n").encode()

    tracemalloc.start()
    try:
        old_ds, e1 = matrixio.ingest_dataset(
            "旧导出", "old", open(old_path, "rb"),
            io.BytesIO(pages_b), io.BytesIO(words_b), work)
        new_ds, e2 = matrixio.ingest_dataset(
            "新导出", "new", open(new_path, "rb"),
            io.BytesIO(pages_b), io.BytesIO(words_b), work)
        assert not e1 and not e2, (e1, e2)
        check = matrixio.verify_datasets(old_ds, new_ds, work)
        assert check["diff"] is None, check["diff"]
        zip_path = os.path.join(work, "out.zip")
        matrixio.build_zip(zip_path, check["pages"], check["words"],
                           check["nnz"], check["entries_path"])
        zf = zipfile.ZipFile(zip_path)
        dims = [l for l in zf.read("matrix.mtx").decode().splitlines()
                if l and not l.startswith("%")][0]
        assert dims == f"{P} {W} {check['nnz']}", dims
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    peak_mb = peak / 1e6
    print(f"    [nnz≈{N + N:,} 条输入，Python 侧内存峰值 {peak_mb:.1f} MB]")
    assert peak_mb < 64, f"内存峰值 {peak_mb:.1f} MB 超出 64 MB 上限"


def main():
    print("运行测试…")
    check("一致核对 + ZIP 内容 + 回环再核对", t_success_and_zip)
    check("计数不同（指出页/词与两边计数）", t_count_diff)
    check("缺页（新导出少全零页）", t_missing_page)
    check("缺页（新导出多全零页）", t_extra_page)
    check("缺词（旧导出少全零词）", t_missing_word)
    check("各类格式错误提示", t_format_errors)
    check("缺少上传字段", t_missing_field)
    check("下载链接防护", t_download_guards)
    check("SpooledTemporaryFile 流包装", t_spooled_stream)
    check("内存不随非零项数增长", t_memory_bounded)
    check("制作阅览包 + 按词查询", t_browse_build_and_query)
    check("起始页键 / 前 100 个 / 有无后续", t_browse_query_start_and_limits)
    check("词表无此词 与 全卷计数为零 分开提示", t_browse_not_in_vocab_vs_zero_total)
    check("一词可跨块 / 每块至多 512 条", t_browse_block_spans_words)
    check("块损坏：所需块整页报错，无关块仍可查", t_browse_corrupt_blocks)
    check("manifest 校验", t_browse_manifest_guards)
    check("制包输入与查询参数错误", t_browse_build_errors)
    check("制包与查询内存有界", t_browse_memory_bounded)
    print(f"\n全部通过：{len(PASSED)} 项")


if __name__ == "__main__":
    main()
