# -*- coding: utf-8 -*-
"""档案词频阅览包的端到端测试。

运行：python3 -I tests/run_tests.py
（-I 隔离模式下运行，脚本自行把 app 目录加入 sys.path。）
"""

import io
import json
import os
import re
import site
import struct
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, APP_DIR)
# -I 模式不含用户级 site-packages，显式加回（Flask 安装在那里）
sys.path.append(site.getusersitepackages())

os.environ["WORK_DIR"] = tempfile.mkdtemp(prefix="pack-test-")

import packio  # noqa: E402
from app import app  # noqa: E402

# ---------------------------------------------------------------- 数据构造
# 小样例：p4 为全零页，cherry 为全零词；apple 占两列（同词合计）
# 合计后：(p1,apple)=3 (p1,banana)=1 (p2,apple)=2 (p3,banana)=5


def mtx(rows, cols, entries, comments=()):
    lines = ["%%MatrixMarket matrix coordinate integer general"]
    lines += ["% " + c for c in comments]
    lines.append(f"{rows} {cols} {len(entries)}")
    lines += [f"{r} {c} {v}" for r, c, v in entries]
    return ("\n".join(lines) + "\n").encode("utf-8")


def csv_bytes(items):
    return ("\n".join(items) + "\n").encode("utf-8")


def small_files():
    pages = ["p1", "p2", "p3", "p4"]
    words = ["apple", "banana", "apple", "cherry"]
    entries = [
        (3, 2, 2), (1, 1, 1), (4, 4, 0), (1, 3, 2),
        (2, 3, 2), (3, 2, 3), (1, 2, 1), (2, 2, 0),
    ]
    return {"matrix": mtx(4, 4, entries), "pages": csv_bytes(pages),
            "words": csv_bytes(words)}


def big_files():
    """650 页 × 4 词：hot 出现 650 页（跨块、可翻页），zero 为全零词。"""
    pages = [f"p{i:04d}" for i in range(1, 651)]
    words = ["hot", "mid", "rare", "zero"]
    entries = []
    for i in range(1, 651):
        entries.append((i, 1, i % 7 + 1))
    for i in range(1, 201):
        entries.append((i, 2, 2))
    entries += [(1, 3, 9), (2, 3, 9), (3, 3, 9)]
    return {"matrix": mtx(650, 4, entries), "pages": csv_bytes(pages),
            "words": csv_bytes(words)}


# ---------------------------------------------------------------- 交互辅助

def post_pack(client, files):
    data = {k: (io.BytesIO(v), k + ".dat") for k, v in files.items()}
    return client.post("/pack", data=data, content_type="multipart/form-data")


def make_pack(client, files):
    """走完整制包流程，返回下载到的阅览包字节。"""
    resp = post_pack(client, files)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200, resp.status_code
    assert "下载阅览包" in html, html
    m = re.search(r"/download/([A-Za-z0-9_-]+)", html)
    assert m, "页面缺少下载链接"
    resp = client.get("/download/" + m.group(1))
    assert resp.status_code == 200, resp.status_code
    return resp.data


def upload_pack(client, pack_bytes):
    resp = client.post(
        "/viewer/upload",
        data={"pack": (io.BytesIO(pack_bytes), "pack.zip")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)
    m = re.search(r"pack=([A-Za-z0-9_-]+)", resp.headers["Location"])
    assert m, resp.headers["Location"]
    return m.group(1)


def query(client, token, word, start=None):
    qs = {"pack": token, "word": word}
    if start is not None:
        qs["start"] = start
    return client.get("/viewer/query", query_string=qs)


def result_rows(html):
    """从查询结果页提取 (页键, 计数) 行。"""
    return [(p, int(c)) for p, c in re.findall(
        r'<tr><td class="num">\d+</td><td>([^<]+)</td><td>(\d+)</td></tr>',
        html)]


def read_manifest(pack_bytes):
    with zipfile.ZipFile(io.BytesIO(pack_bytes)) as zf:
        return json.loads(zf.read("manifest.json"))


def read_block(pack_bytes, name):
    with zipfile.ZipFile(io.BytesIO(pack_bytes)) as zf:
        raw = zf.read(name)
    return [json.loads(l) for l in raw.decode("utf-8").splitlines() if l]


# ---------------------------------------------------------------- 包篡改辅助

def corrupt_member(pack_bytes, name):
    """翻转指定成员压缩数据中的若干字节（CRC 校验将失败）。"""
    with zipfile.ZipFile(io.BytesIO(pack_bytes)) as zf:
        info = zf.getinfo(name)
    off = info.header_offset
    assert pack_bytes[off:off + 4] == b"PK\x03\x04"
    fname_len, extra_len = struct.unpack_from("<HH", pack_bytes, off + 26)
    data_start = off + 30 + fname_len + extra_len
    data_end = data_start + info.compress_size
    data = bytearray(pack_bytes)
    mid = (data_start + data_end) // 2
    for i in range(mid, min(mid + 16, data_end)):
        data[i] ^= 0xFF
    return bytes(data)


def rewrite_pack(pack_bytes, drop=(), replace=None):
    """重打包：可删除成员（drop）或替换成员内容（replace，CRC 重新计算）。"""
    src = zipfile.ZipFile(io.BytesIO(pack_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            if name in drop:
                continue
            data = replace[name] if replace and name in replace else src.read(name)
            dst.writestr(name, data)
    return buf.getvalue()


PASSED = []


def check(name, fn):
    fn()
    PASSED.append(name)
    print(f"  ok - {name}")


# ---------------------------------------------------------------- 制包用例

def t_pack_small_structure():
    client = app.test_client()
    pack = make_pack(client, small_files())
    with zipfile.ZipFile(io.BytesIO(pack)) as zf:
        assert sorted(zf.namelist()) == ["blocks/000000.z", "manifest.json"], \
            zf.namelist()
    m = read_manifest(pack)
    assert m["format"] == "archive-wordfreq-pack" and m["version"] == 1
    assert m["block_capacity"] == 512
    assert m["pages"] == ["p1", "p2", "p3", "p4"]
    assert m["words"] == ["apple", "banana", "cherry"]  # 排序且去重
    assert m["nnz"] == 4
    assert len(m["blocks"]) == 1
    b = m["blocks"][0]
    assert b["id"] == 0 and b["member"] == "blocks/000000.z"
    assert b["records"] == 4 <= 512
    # 分段：apple 合计 3+2=5，banana 合计 1+5=6
    assert b["segments"] == [["apple", 2, 5, "p1", "p2"],
                             ["banana", 2, 6, "p1", "p3"]], b["segments"]
    # 块内容按（词, 页键）排序，与分段一一对应
    records = read_block(pack, "blocks/000000.z")
    assert records == [["p1", 3], ["p2", 2], ["p1", 1], ["p3", 5]], records
    # 清单中的 SHA-256 与块内容一致
    import hashlib
    with zipfile.ZipFile(io.BytesIO(pack)) as zf:
        raw = zf.read("blocks/000000.z")
    assert hashlib.sha256(raw).hexdigest() == b["sha256"]


def t_pack_format_errors():
    client = app.test_client()
    base = small_files()

    def expect(files, *fragments):
        resp = post_pack(client, files)
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200, resp.status_code
        assert "数据格式有误" in html, html
        for frag in fragments:
            assert frag in html, (frag, html)

    f = dict(base)
    f["matrix"] = b"%MatrixMarket matrix coordinate integer general\n4 4 0\n"
    expect(f, "首行应为")

    f = dict(base); f["matrix"] = mtx(5, 4, [])
    expect(f, "与 CSV 行列数不符", "5×4", "页数×词数 = 4×4")

    f = dict(base); f["matrix"] = mtx(4, 4, [(1, 1, -2)])
    expect(f, "为负")

    f = dict(base); f["matrix"] = mtx(4, 4, [(1, 9, 1)])
    expect(f, "超出范围 1..4")

    f = dict(base)
    f["matrix"] = b"%%MatrixMarket matrix coordinate integer general\n4 4 3\n1 1 1\n"
    expect(f, "声明 3 个非零项，实际只读到 1 个")

    f = dict(base)
    f["matrix"] = (b"%%MatrixMarket matrix coordinate integer general\n"
                   b"4 4 1\n1 1 1\n2 2 2\n")
    expect(f, "多余内容")

    f = dict(base); f["pages"] = b"p1\np2\np1\np4\n"
    expect(f, "重复", "页键必须唯一")

    f = dict(base); f["words"] = b"apple,x\nbanana,y\napple,z\ncherry,w\n"
    expect(f, "单列 CSV")

    f = dict(base)
    f["matrix"] = mtx(4, 4, [(1, 1, 1)]).replace(b"1 1 1", b"1 1 2.5")
    expect(f, "三个整数")

    f = dict(base)
    f["matrix"] = b"%%MatrixMarket matrix coordinate real general\n4 4 0\n"
    expect(f, "字段类型", "integer")

    f = dict(base); f["matrix"] = b"\xff\xfe%%MatrixMarket"
    expect(f, "UTF-8")


def t_pack_missing_field():
    client = app.test_client()
    files = small_files()
    del files["words"]
    resp = client.post(
        "/pack",
        data={k: (io.BytesIO(v), k) for k, v in files.items()},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "缺少文件" in resp.get_data(as_text=True)


def t_download_guards():
    client = app.test_client()
    assert client.get("/download/nonexistent-token").status_code == 404
    assert client.get("/download/..%2F..%2Fetc").status_code == 404


# ---------------------------------------------------------------- 查询用例

def t_query_small():
    client = app.test_client()
    token = upload_pack(client, make_pack(client, small_files()))
    # 阅览页显示包概要
    html = client.get("/viewer?pack=" + token).get_data(as_text=True)
    assert "已载入阅览包" in html and ">4</td>" in html and ">3</td>" in html

    resp = query(client, token, "apple")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert result_rows(html) == [("p1", 3), ("p2", 2)]
    assert "全卷共 <strong>2</strong> 页" in html
    assert "总词频 <strong>5</strong>" in html
    assert "没有更多结果" in html

    html = query(client, token, "banana").get_data(as_text=True)
    assert result_rows(html) == [("p1", 1), ("p3", 5)]


def t_query_vocab_distinction():
    """词表中没有的词与全卷计数为零的词分开提示。"""
    client = app.test_client()
    token = upload_pack(client, make_pack(client, small_files()))
    html = query(client, token, "durian").get_data(as_text=True)
    assert "词表中没有该词" in html and "全卷计数为零" not in html
    html = query(client, token, "cherry").get_data(as_text=True)
    assert "全卷计数为零" in html and "词表中没有该词" not in html


def t_paging():
    client = app.test_client()
    token = upload_pack(client, make_pack(client, big_files()))

    html = query(client, token, "hot").get_data(as_text=True)
    rows = result_rows(html)
    assert len(rows) == 100 and rows[0] == ("p0001", 2) and rows[-1][0] == "p0100"
    assert "还有后续结果" in html
    assert "全卷共 <strong>650</strong> 页" in html
    # 下一页链接以上一页末键为起始键
    assert "start=p0100" in html

    rows = result_rows(query(client, token, "hot", start="p0100")
                       .get_data(as_text=True))
    assert len(rows) == 100 and rows[0][0] == "p0101" and rows[-1][0] == "p0200"

    html = query(client, token, "hot", start="p0600").get_data(as_text=True)
    rows = result_rows(html)
    assert len(rows) == 50 and rows[0][0] == "p0601" and rows[-1][0] == "p0650"
    assert "没有更多结果" in html

    # mid：恰好 200 条，第二页 100 条后无后续
    html = query(client, token, "mid", start="p0100").get_data(as_text=True)
    rows = result_rows(html)
    assert len(rows) == 100 and rows[-1][0] == "p0200"
    assert "没有更多结果" in html

    # rare：3 条
    rows = result_rows(query(client, token, "rare").get_data(as_text=True))
    assert rows == [("p0001", 9), ("p0002", 9), ("p0003", 9)]


def t_start_key():
    client = app.test_client()
    token = upload_pack(client, make_pack(client, big_files()))
    # 起始键本身不计入（从起始键之后开始）
    rows = result_rows(query(client, token, "hot", start="p0001")
                       .get_data(as_text=True))
    assert rows[0][0] == "p0002"
    # 起始键不在页键表中
    html = query(client, token, "hot", start="nope").get_data(as_text=True)
    assert "起始页键" in html and "不在本卷页键表中" in html
    # 起始键为最后一页：无结果但页键合法
    html = query(client, token, "hot", start="p0650").get_data(as_text=True)
    assert result_rows(html) == []
    assert "没有更多结果" in html
    assert "不在本卷页键表中" not in html


def t_block_capacity_and_span():
    """每块最多 512 条；一词可跨块。"""
    client = app.test_client()
    pack = make_pack(client, big_files())
    m = read_manifest(pack)
    assert len(m["blocks"]) == 2
    b0, b1 = m["blocks"]
    assert b0["records"] == 512 and b1["records"] == 341
    assert all(b["records"] <= 512 for b in m["blocks"])
    # hot 跨两个块：块 0 含 p0001..p0512，块 1 含 p0513..p0650
    assert b0["segments"][0] == ["hot", 512, b0["segments"][0][2], "p0001", "p0512"]
    assert b1["segments"][0][0] == "hot" and b1["segments"][0][1] == 138
    assert b1["segments"][0][3] == "p0513" and b1["segments"][0][4] == "p0650"
    # mid、rare 都在块 1
    assert [s[0] for s in b1["segments"]] == ["hot", "mid", "rare"]
    # 跨块合计：hot 总词频 = sum(i%7+1, i=1..650)
    total = sum(i % 7 + 1 for i in range(1, 651))
    segsum = b0["segments"][0][2] + b1["segments"][0][2]
    assert segsum == total, (segsum, total)


def t_corrupt_irrelevant_block():
    """无关块损坏仍可查询；且只解压本页结果所需的块。"""
    client = app.test_client()
    pack = make_pack(client, big_files())
    bad = corrupt_member(pack, "blocks/000001.z")
    token = upload_pack(client, bad)
    # hot 第一页只需块 0：正常返回且标明有后续
    html = query(client, token, "hot").get_data(as_text=True)
    rows = result_rows(html)
    assert len(rows) == 100 and rows[0][0] == "p0001", rows[:3]
    assert "还有后续结果" in html


def t_corrupt_needed_block():
    """所需块损坏（CRC）时整页报错。"""
    client = app.test_client()
    pack = make_pack(client, big_files())
    bad = corrupt_member(pack, "blocks/000001.z")
    token = upload_pack(client, bad)
    # rare 的记录在块 1：整页报错
    html = query(client, token, "rare").get_data(as_text=True)
    assert "无法完成操作" in html and "数据块" in html
    assert "完整性校验" in html
    # hot 翻到需要块 1 的位置也报错
    html = query(client, token, "hot", start="p0512").get_data(as_text=True)
    assert "无法完成操作" in html and "blocks/000001.z" in html


def t_missing_needed_block():
    """所需块缺失时整页报错；不需要该块的词不受影响。"""
    client = app.test_client()
    pack = make_pack(client, big_files())
    broken = rewrite_pack(pack, drop=("blocks/000000.z",))
    token = upload_pack(client, broken)
    html = query(client, token, "hot").get_data(as_text=True)
    assert "无法完成操作" in html and "缺失" in html
    # rare 只需块 1：正常
    rows = result_rows(query(client, token, "rare").get_data(as_text=True))
    assert rows == [("p0001", 9), ("p0002", 9), ("p0003", 9)]


def t_tampered_block_content():
    """块内容被替换但 CRC 正常时，SHA-256 校验失败，整页报错。"""
    client = app.test_client()
    pack = make_pack(client, big_files())
    fake = b'["p9999", 1]\n' * 512  # 记录数凑够但内容不同
    broken = rewrite_pack(pack, replace={"blocks/000000.z": fake})
    token = upload_pack(client, broken)
    html = query(client, token, "hot").get_data(as_text=True)
    assert "无法完成操作" in html and "SHA-256" in html
    # 不需要块 0 的查询不受影响
    rows = result_rows(query(client, token, "rare").get_data(as_text=True))
    assert rows == [("p0001", 9), ("p0002", 9), ("p0003", 9)]


def t_bad_pack_upload():
    client = app.test_client()
    # 非 ZIP 文件
    resp = client.post(
        "/viewer/upload",
        data={"pack": (io.BytesIO(b"not a zip"), "pack.zip")},
        content_type="multipart/form-data",
    )
    assert "ZIP" in resp.get_data(as_text=True)
    # 缺 manifest 的 ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("blocks/000000.z", b"[]\n")
    resp = client.post(
        "/viewer/upload",
        data={"pack": (io.BytesIO(buf.getvalue()), "pack.zip")},
        content_type="multipart/form-data",
    )
    assert "manifest.json" in resp.get_data(as_text=True)
    # 缺文件字段
    resp = client.post("/viewer/upload", data={},
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def t_viewer_guards():
    client = app.test_client()
    assert client.get("/viewer/query", query_string={"pack": "x" * 8}).status_code == 400
    assert client.get("/viewer/query",
                      query_string={"pack": "x" * 8, "word": "w"}).status_code == 404
    assert client.get("/viewer?pack=" + "x" * 8).status_code == 404
    assert client.get("/viewer?pack=..%2Fetc").status_code == 404


# ---------------------------------------------------------------- 单元/资源用例

def t_spooled_stream():
    """werkzeug 的 SpooledTemporaryFile 也能正确包装读取。"""
    sp = tempfile.SpooledTemporaryFile(max_size=4)  # 强制落盘
    sp.write(b"%%MatrixMarket matrix coordinate integer general\n1 1 1\n1 1 5\n")
    sp.seek(0)
    work = tempfile.mkdtemp(prefix="pack-unit-")
    ds, errors = packio.ingest_dataset(
        sp, io.BytesIO(b"p1\n"), io.BytesIO(b"w1\n"), work
    )
    assert not errors, errors
    assert ds.nnz == 1 and ds.rows == 1 and ds.cols == 1


def t_memory_bounded():
    """非零项约 100 万时，制包的 Python 侧内存峰值保持有界。"""
    import gc
    import random
    import tracemalloc

    random.seed(7)
    P, W, N = 300, 200, 1_000_000
    pages = [f"page-{i:04d}" for i in range(P)]
    words = [f"w-{i:04d}" for i in range(W)]

    work = tempfile.mkdtemp(prefix="pack-big-")
    mtx_path = os.path.join(work, "big.mtx")
    with open(mtx_path, "w") as f:
        f.write("%%MatrixMarket matrix coordinate integer general\n")
        f.write(f"{P} {W} {N}\n")
        for _ in range(N):
            f.write(f"{random.randint(1, P)} {random.randint(1, W)} "
                    f"{random.randint(0, 5)}\n")

    # 预先算出期望计数（内存峰值统计开始前的准备工作）
    expected = {}
    with open(mtx_path) as f:
        next(f)
        next(f)
        for line in f:
            r, c, v = line.split()
            key = (pages[int(r) - 1], words[int(c) - 1])
            expected[key] = expected.get(key, 0) + int(v)
    expected = {k: v for k, v in expected.items() if v}
    probe_word = words[3]
    probe_expected = sorted((p, v) for (p, w), v in expected.items()
                            if w == probe_word)
    del expected
    gc.collect()

    pages_b = ("\n".join(pages) + "\n").encode()
    words_b = ("\n".join(words) + "\n").encode()

    tracemalloc.start()
    try:
        ds, errors = packio.ingest_dataset(
            open(mtx_path, "rb"), io.BytesIO(pages_b), io.BytesIO(words_b), work)
        assert not errors, errors
        pack_path = os.path.join(work, "out.zip")
        stats = packio.build_pack(pack_path, ds, work)
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    peak_mb = peak / 1e6
    print(f"    [nnz={N:,} 条输入，合计后 {stats['nnz']:,} 条记录、"
          f"{stats['blocks']} 块，Python 侧内存峰值 {peak_mb:.1f} MB]")
    assert peak_mb < 64, f"内存峰值 {peak_mb:.1f} MB 超出 64 MB 上限"
    assert stats["blocks"] == (stats["nnz"] + 511) // 512

    # 包内容可查询且与期望一致（第一页 100 条）
    result = packio.query_pack(pack_path, probe_word, None)
    assert result["status"] == "ok"
    assert result["results"] == probe_expected[:100]
    assert result["has_more"] == (len(probe_expected) > 100)
    assert result["total_pages"] == len(probe_expected)
    # 从起始键之后继续
    if len(probe_expected) > 100:
        result2 = packio.query_pack(pack_path, probe_word,
                                    probe_expected[99][0])
        assert result2["results"] == probe_expected[100:200]


def main():
    print("运行测试…")
    check("制包：小样例的包结构与合计口径", t_pack_small_structure)
    check("制包：各类格式错误提示", t_pack_format_errors)
    check("制包：缺少上传字段", t_pack_missing_field)
    check("下载链接防护", t_download_guards)
    check("查询：基本查询与包概要", t_query_small)
    check("查询：词表没有 vs 全卷零计数", t_query_vocab_distinction)
    check("查询：每页 100 条与翻页", t_paging)
    check("查询：起始页键（之后开始 / 不存在）", t_start_key)
    check("分块：512 上限与一词跨块", t_block_capacity_and_span)
    check("容错：无关块损坏仍可查询", t_corrupt_irrelevant_block)
    check("容错：所需块损坏（CRC）整页报错", t_corrupt_needed_block)
    check("容错：所需块缺失整页报错", t_missing_needed_block)
    check("容错：块内容被替换（SHA-256）整页报错", t_tampered_block_content)
    check("阅览页：坏包上传被拒", t_bad_pack_upload)
    check("阅览页：参数防护", t_viewer_guards)
    check("SpooledTemporaryFile 流包装", t_spooled_stream)
    check("内存不随非零项数增长", t_memory_bounded)
    print(f"\n全部通过：{len(PASSED)} 项")


if __name__ == "__main__":
    main()
