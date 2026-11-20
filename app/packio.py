"""档案词频阅览包：制包与查询的核心逻辑。

制包（build_pack）内存占用不随矩阵非零项数增长：
- 矩阵条目逐行解析后批量写入磁盘上的 SQLite；
- 重复坐标 / 同词多列的合计与排序由 SQLite 在磁盘完成
  （PRAGMA temp_store=FILE，排序溢出到临时文件而非内存）；
- 聚合结果按（词, 页键）流式读出，每攒满 512 条非零记录就压缩成
  一个可独立解压的数据块写入 ZIP；块清单先落临时文件，最后流式
  并入 manifest.json；
- 包先写入 .part 临时文件，完成后原子改名，下载方只会看到完整包。

只有页键表、词表驻留内存（随页数/词数增长，与非零项数无关）。

阅览包（单文件 ZIP）结构：
- manifest.json：格式与版本、排序后的页键表与词表、非零记录总数，
  以及每个数据块的成员名、记录数、SHA-256 和块内分段
  （词、记录数、词频合计、首末页键）；
- blocks/NNNNNN.z：数据块，每块最多 512 条非零记录，JSON 行格式
  （每行 ["页键", 计数]），按（词, 页键）排序，一词可跨块；
  块内记录与清单中的分段一一对应。

查询（query_pack）只解压本页结果所需的数据块：按分段的首末页键跳过
不需要的块，攒够 100 条结果（外加 1 条用于判断有无后续）即停。所需块
缺失或未通过 CRC / SHA-256 完整性校验时抛 PackError（页面整体报错）；
无关的块损坏不影响查询。
"""

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import zipfile
import zlib

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

_INT_RE = re.compile(r"[+-]?[0-9]+")
_BATCH_SIZE = 20000
_INT64_MAX = 2**63 - 1

BLOCK_CAPACITY = 512            # 每块最多 512 条非零记录
PAGE_SIZE = 100                 # 查询每页列出的非零页数
_PACK_FORMAT = "archive-wordfreq-pack"
_PACK_VERSION = 1
_MANIFEST_MEMBER = "manifest.json"
_BLOCK_MEMBER_FMT = "blocks/{:06d}.z"

# 重复坐标与同词多列在此合计；和为 0 的坐标（只可能来自显式写出的 0）
# 按“未写出即零”的约定剔除。结果按（词, 页键）排序，供分块流式写出。
_AGGREGATE_SQL = (
    "SELECT page, word, SUM(v) AS s FROM entries "
    "GROUP BY page, word HAVING s <> 0 ORDER BY word, page"
)


class FormatError(Exception):
    """用户上传数据的格式错误，消息直接展示在制包结果页上。"""


class PackError(Exception):
    """阅览包缺失、损坏或完整性校验失败，查询页整体报错。"""


class Dataset:
    """一套已解析并通过格式校验的 页×词 数据。"""

    def __init__(self, pages, words, rows, cols, nnz, db_path):
        self.pages = pages      # 页键列表（唯一），顺序与矩阵行一致
        self.words = words      # 词列表（可重复），顺序与矩阵列一致
        self.rows = rows
        self.cols = cols
        self.nnz = nnz          # 矩阵声明的非零项数
        self.db_path = db_path  # 磁盘上的 SQLite 库，含 entries 表


# ---------------------------------------------------------------- 输入流

class _RawAdapter(io.RawIOBase):
    """把只保证 read() 的二进制流（如 werkzeug 的 SpooledTemporaryFile）
    适配成 RawIOBase，以便套上 BufferedReader/TextIOWrapper。"""

    def __init__(self, raw):
        self._raw = raw

    def readable(self):
        return True

    def readinto(self, b):
        data = self._raw.read(len(b))
        if not data:
            return 0
        b[: len(data)] = data
        return len(data)


def _text_stream(stream):
    """把上传的二进制流包装成 UTF-8（容忍 BOM）文本流，逐行读取。"""
    if not isinstance(stream, io.BufferedIOBase):
        stream = io.BufferedReader(_RawAdapter(stream), buffer_size=1 << 18)
    return io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")


def _snip(s, limit=60):
    s = s if len(s) <= limit else s[:limit] + "…"
    return s.replace("\r", " ").replace("\n", " ")


# ---------------------------------------------------------------- CSV

def read_list_csv(stream, kind, require_unique):
    """读取单列 CSV（每行一个值、无表头），返回字符串列表。

    kind 为“页键”或“词”。页键要求唯一；词允许重复（同词多列将合计）。
    """
    items = []
    text = _text_stream(stream)
    try:
        for lineno, row in enumerate(csv.reader(text), 1):
            if not row:  # 跳过完全空白的行
                continue
            if len(row) != 1:
                raise FormatError(
                    f"{kind}表第 {lineno} 行有 {len(row)} 列；"
                    f"应为单列 CSV（每行一个{kind}，无表头）"
                )
            items.append(row[0])
    except UnicodeDecodeError:
        raise FormatError(f"{kind}表不是有效的 UTF-8 文本") from None
    except csv.Error as exc:
        raise FormatError(f"{kind}表 CSV 解析失败：{exc}") from None
    if require_unique:
        seen = set()
        for it in items:
            if it in seen:
                raise FormatError(f"页键 “{it}” 重复出现；页键必须唯一")
            seen.add(it)
    return items


# ---------------------------------------------------------------- Matrix Market

def _parse_int(token):
    return int(token) if _INT_RE.fullmatch(token) else None


def read_mtx_header(text):
    """解析 Matrix Market 头与维度行，返回 (rows, cols, nnz, 已读行数)。"""
    banner = None
    line_no = 0
    for raw in text:
        line_no += 1
        s = raw.strip()
        if not s:
            continue
        banner = s
        break
    if banner is None:
        raise FormatError("矩阵文件为空")
    tokens = banner.split()
    if len(tokens) != 5 or tokens[0] != "%%MatrixMarket":
        raise FormatError(
            "矩阵首行应为 “%%MatrixMarket matrix coordinate integer general”，"
            f"实际为 “{_snip(banner)}”"
        )
    names = ["对象", "存储格式", "字段类型", "对称性"]
    expect = ["matrix", "coordinate", "integer", "general"]
    got = [t.lower() for t in tokens[1:]]
    for i in range(4):
        if got[i] != expect[i]:
            raise FormatError(
                f"矩阵头{names[i]}应为 “{expect[i]}”，实际为 “{tokens[1 + i]}”；"
                "本工具只接受通用整数坐标格式（matrix coordinate integer general）"
            )
    for raw in text:
        line_no += 1
        s = raw.strip()
        if not s or s.startswith("%"):
            continue  # 维度行之前允许注释与空行
        parts = s.split()
        vals = [_parse_int(p) for p in parts]
        if len(parts) != 3 or any(v is None for v in vals):
            raise FormatError(
                f"矩阵第 {line_no} 行应为维度行 “行数 列数 非零项数”（三个整数），"
                f"实际为 “{_snip(s)}”"
            )
        rows, cols, nnz = vals
        if rows < 0 or cols < 0 or nnz < 0:
            raise FormatError(f"矩阵第 {line_no} 行维度不能为负")
        return rows, cols, nnz, line_no
    raise FormatError("矩阵缺少维度行（行数 列数 非零项数）")


def _ingest_entries(text, rows, cols, nnz, line_no, mapper, db):
    """逐行读取矩阵条目，校验后映射为 (页键, 词, 计数) 批量写入 SQLite。"""
    count = 0
    batch = []
    insert = "INSERT INTO entries (page, word, v) VALUES (?,?,?)"
    for raw in text:
        line_no += 1
        s = raw.strip()
        if not s:
            continue
        if count >= nnz:
            raise FormatError(
                f"矩阵声明 {nnz} 个非零项，但第 {line_no} 行还有多余内容："
                f"“{_snip(s)}”"
            )
        parts = s.split()
        vals = [_parse_int(p) for p in parts]
        if len(parts) != 3 or any(v is None for v in vals):
            raise FormatError(
                f"矩阵第 {line_no} 行应为 “行号 列号 计数”（三个整数），"
                f"实际为 “{_snip(s)}”"
            )
        r, c, v = vals
        if r < 1 or r > rows:
            raise FormatError(f"矩阵第 {line_no} 行行号 {r} 超出范围 1..{rows}")
        if c < 1 or c > cols:
            raise FormatError(f"矩阵第 {line_no} 行列号 {c} 超出范围 1..{cols}")
        if v < 0:
            raise FormatError(f"矩阵第 {line_no} 行计数 {v} 为负；计数必须非负")
        if v > _INT64_MAX:
            raise FormatError(f"矩阵第 {line_no} 行计数超出 64 位整数范围")
        batch.append(mapper(r, c, v))
        count += 1
        if len(batch) >= _BATCH_SIZE:
            db.executemany(insert, batch)
            batch.clear()
    if batch:
        db.executemany(insert, batch)
    db.commit()
    if count < nnz:
        raise FormatError(f"矩阵声明 {nnz} 个非零项，实际只读到 {count} 个")


def ingest_dataset(mtx_stream, pages_stream, words_stream, workdir):
    """解析并校验一套 页×词 数据。返回 (Dataset 或 None, 错误消息列表)。"""
    errors = []
    try:
        pages = read_list_csv(pages_stream, "页键", require_unique=True)
    except FormatError as e:
        pages = None
        errors.append(str(e))
    try:
        words = read_list_csv(words_stream, "词", require_unique=False)
    except FormatError as e:
        words = None
        errors.append(str(e))
    if errors:
        return None, errors

    try:
        text = _text_stream(mtx_stream)
        rows, cols, nnz, line_no = read_mtx_header(text)
        if (rows, cols) != (len(pages), len(words)):
            raise FormatError(
                f"矩阵维度为 {rows}×{cols}，与 CSV 行列数不符："
                f"应为 页数×词数 = {len(pages)}×{len(words)}"
            )
        db_path = os.path.join(workdir, "entries.db")
        db = sqlite3.connect(db_path)
        try:
            db.execute("PRAGMA temp_store = FILE")
            db.execute("PRAGMA synchronous = OFF")
            db.execute(
                "CREATE TABLE entries (page TEXT NOT NULL, word TEXT NOT NULL, "
                "v INTEGER NOT NULL)"
            )

            def mapper(r, c, v, _p=pages, _w=words):
                return (_p[r - 1], _w[c - 1], v)

            _ingest_entries(text, rows, cols, nnz, line_no, mapper, db)
        finally:
            db.close()
        return Dataset(pages, words, rows, cols, nnz, db_path), []
    except UnicodeDecodeError:
        return None, ["矩阵不是有效的 UTF-8 文本"]
    except FormatError as e:
        return None, [str(e)]


# ---------------------------------------------------------------- 制包

def _write_manifest(f, pages, words, nnz, meta_path):
    """把清单流式写入 ZIP 成员；块元数据从临时文件逐行并入。"""
    def w(s):
        f.write(s.encode("utf-8"))

    w('{"format":')
    w(json.dumps(_PACK_FORMAT))
    w(',"version":%d,"block_capacity":%d,"nnz":%d'
      % (_PACK_VERSION, BLOCK_CAPACITY, nnz))
    w(',"pages":')
    w(json.dumps(pages, ensure_ascii=False, separators=(",", ":")))
    w(',"words":')
    w(json.dumps(words, ensure_ascii=False, separators=(",", ":")))
    w(',"blocks":[')
    first = True
    with open(meta_path, encoding="utf-8") as meta_in:
        for line in meta_in:
            line = line.strip()
            if not line:
                continue
            if not first:
                w(",")
            w(line)
            first = False
    w("]}")


def build_pack(zip_path, ds, workdir):
    """把一套数据制成单文件阅览包（ZIP）。返回统计信息 dict。

    全程流式：合计与排序由磁盘上的 SQLite 完成，数据块逐个压缩写入，
    块清单先落临时文件再流式并入 manifest.json；内存不随非零项数增长。
    """
    pages_sorted = sorted(ds.pages)
    words_sorted = sorted(set(ds.words))
    tmp_path = zip_path + ".part"
    meta_path = os.path.join(workdir, "blocks.jsonl")
    nnz = 0       # 合计后的非零记录数
    nblocks = 0
    records = []   # 当前块缓冲：(page, count)，最多 BLOCK_CAPACITY 条
    segments = []  # 当前块分段：[word, count, sum_v, first_page, last_page]
    seg_word = None
    seg_count = seg_sum = 0
    seg_first = seg_last = None

    db = sqlite3.connect(ds.db_path)
    try:
        db.execute("PRAGMA temp_store = FILE")
        cursor = db.execute(_AGGREGATE_SQL)
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED,
                             allowZip64=True) as zf:
            with open(meta_path, "w", encoding="utf-8", newline="\n") as meta_out:

                def close_segment():
                    nonlocal seg_word
                    if seg_word is not None:
                        segments.append([seg_word, seg_count, seg_sum,
                                         seg_first, seg_last])
                        seg_word = None

                def flush_block():
                    nonlocal nblocks
                    close_segment()
                    if not records:
                        return
                    member = _BLOCK_MEMBER_FMT.format(nblocks)
                    payload = "".join(
                        json.dumps([p, v], ensure_ascii=False,
                                   separators=(",", ":")) + "\n"
                        for p, v in records
                    ).encode("utf-8")
                    digest = hashlib.sha256(payload).hexdigest()
                    with zf.open(member, "w") as f:
                        f.write(payload)
                    meta_out.write(json.dumps({
                        "id": nblocks, "member": member,
                        "records": len(records), "sha256": digest,
                        "segments": segments,
                    }, ensure_ascii=False, separators=(",", ":")) + "\n")
                    nblocks += 1
                    records.clear()
                    segments.clear()

                for page, word, v in cursor:
                    if word != seg_word:
                        close_segment()
                        seg_word, seg_count, seg_sum, seg_first = word, 0, 0, page
                    seg_count += 1
                    seg_sum += v
                    seg_last = page
                    records.append((page, v))
                    nnz += 1
                    if len(records) >= BLOCK_CAPACITY:
                        flush_block()
                flush_block()
            # 数据块写完后再写清单（块元数据从临时文件流式并入）
            with zf.open(_MANIFEST_MEMBER, "w") as f:
                _write_manifest(f, pages_sorted, words_sorted, nnz, meta_path)
    finally:
        db.close()
    os.replace(tmp_path, zip_path)  # 原子发布，下载方只会看到完整包
    return {"pages": len(pages_sorted), "words": len(words_sorted),
            "nnz": nnz, "blocks": nblocks}


# ---------------------------------------------------------------- 读包

def _validate_manifest(m):
    if not isinstance(m, dict) or m.get("format") != _PACK_FORMAT:
        raise PackError("清单格式不符：不是本系统生成的阅览包")
    if m.get("version") != _PACK_VERSION:
        raise PackError(f"阅览包版本不受支持：{m.get('version')!r}")
    for key in ("pages", "words", "blocks"):
        if not isinstance(m.get(key), list):
            raise PackError(f"清单缺少 {key}：包已损坏")
    if not isinstance(m.get("nnz"), int):
        raise PackError("清单缺少 nnz：包已损坏")
    for i, b in enumerate(m["blocks"]):
        if not isinstance(b, dict) or b.get("id") != i:
            raise PackError("清单中的块编号不连续：包已损坏")
        if not isinstance(b.get("member"), str) \
                or not isinstance(b.get("sha256"), str):
            raise PackError("清单中的块信息不完整：包已损坏")
        if not isinstance(b.get("records"), int) \
                or not isinstance(b.get("segments"), list):
            raise PackError("清单中的块信息不完整：包已损坏")
        for seg in b["segments"]:
            if not (isinstance(seg, list) and len(seg) == 5
                    and isinstance(seg[0], str) and isinstance(seg[1], int)
                    and isinstance(seg[2], int) and isinstance(seg[3], str)
                    and isinstance(seg[4], str)):
                raise PackError("清单中的分段信息不完整：包已损坏")


def load_pack(pack_path):
    """打开阅览包并读取、校验清单。返回 (ZipFile, manifest)。

    清单本身由 ZIP 的 CRC 保护；损坏或格式不符时抛 PackError。
    """
    try:
        zf = zipfile.ZipFile(pack_path)
    except zipfile.BadZipFile:
        raise PackError("文件不是有效的 ZIP：不是本系统生成的阅览包") from None
    try:
        try:
            with zf.open(_MANIFEST_MEMBER) as f:
                raw = f.read()
        except KeyError:
            raise PackError("包内缺少 manifest.json：不是本系统生成的阅览包") from None
        except (zipfile.BadZipFile, zlib.error, EOFError):
            raise PackError("manifest.json 未通过完整性校验（CRC）") from None
        try:
            manifest = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise PackError("manifest.json 无法解析：包已损坏") from None
        _validate_manifest(manifest)
        return zf, manifest
    except Exception:
        zf.close()
        raise


def pack_summary(pack_path):
    """读取包的概要信息（页数、词数、非零记录数、块数）。"""
    zf, manifest = load_pack(pack_path)
    try:
        return {"pages": len(manifest["pages"]),
                "words": len(manifest["words"]),
                "nnz": manifest["nnz"],
                "blocks": len(manifest["blocks"])}
    finally:
        zf.close()


def _read_block(zf, bmeta):
    """读取并校验一个数据块，返回 [(page, count), ...] 记录列表。"""
    member = bmeta["member"]
    try:
        with zf.open(member) as f:
            raw = f.read()
    except KeyError:
        raise PackError(f"查询所需的数据块 {member} 缺失") from None
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise PackError(f"数据块 {member} 未通过完整性校验（CRC）") from None
    if hashlib.sha256(raw).hexdigest() != bmeta["sha256"]:
        raise PackError(f"数据块 {member} 未通过完整性校验（SHA-256）")
    try:
        text = raw.decode("utf-8")
        records = [json.loads(line) for line in text.splitlines() if line]
    except (UnicodeDecodeError, ValueError):
        raise PackError(f"数据块 {member} 内容无法解析：包已损坏") from None
    if len(records) != bmeta["records"]:
        raise PackError(f"数据块 {member} 的记录数与清单不符：包已损坏")
    for rec in records:
        if not (isinstance(rec, list) and len(rec) == 2
                and isinstance(rec[0], str) and isinstance(rec[1], int)):
            raise PackError(f"数据块 {member} 的记录格式不符：包已损坏")
    return records


def _segment_slice(bmeta, records, word):
    """按清单中的分段信息，从块记录中切出该词对应的一段。"""
    off = 0
    for seg in bmeta["segments"]:
        if seg[0] == word:
            return records[off:off + seg[1]]
        off += seg[1]
    raise PackError(f"数据块 {bmeta['member']} 中找不到该词的分段：包已损坏")


def _word_segments(manifest, word):
    """该词在全部块中的分段，按块序（即页键序）排列。"""
    found = []
    for b in manifest["blocks"]:
        for seg in b["segments"]:
            if seg[0] == word:
                found.append((b, seg))
    return found


def query_pack(pack_path, word, start):
    """查询词的非零页。返回 dict：

    status == "ok"：results 为最多 PAGE_SIZE 条 (页键, 计数)，has_more
                    标明有无后续，total_pages / total_freq 为全卷合计；
    status == "not_in_vocab"：词表中没有该词；
    status == "zero"：词在词表中，但全卷计数为零；
    status == "bad_start"：起始页键不在本卷页键表中。

    只解压本页结果所需的数据块；所需块缺失或未通过完整性校验时
    抛 PackError（页面整体报错），无关的块损坏不影响查询。
    """
    zf, manifest = load_pack(pack_path)
    try:
        if word not in set(manifest["words"]):
            return {"status": "not_in_vocab"}
        segs = _word_segments(manifest, word)
        total_pages = sum(seg[1] for _, seg in segs)
        total_freq = sum(seg[2] for _, seg in segs)
        if total_pages == 0:
            return {"status": "zero"}
        if start is not None and start not in set(manifest["pages"]):
            return {"status": "bad_start"}

        results = []
        has_more = False
        for bmeta, seg in segs:
            if start is not None and seg[4] <= start:
                continue  # 该段末页键不超过起始键，整块跳过，无需解压
            if len(results) >= PAGE_SIZE:
                # 后续段里仍有非零记录，无需解压即可断定有后续
                has_more = True
                break
            records = _read_block(zf, bmeta)
            for page, v in _segment_slice(bmeta, records, word):
                if start is not None and page <= start:
                    continue
                results.append((page, v))
                if len(results) > PAGE_SIZE:
                    has_more = True
                    break
            if has_more:
                break
        return {"status": "ok",
                "results": results[:PAGE_SIZE],
                "has_more": has_more,
                "total_pages": total_pages,
                "total_freq": total_freq}
    finally:
        zf.close()
