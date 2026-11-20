"""档案馆检索索引核对：解析、比对、打包的核心逻辑。

内存占用不随矩阵非零项数增长：
- 矩阵条目逐行解析后批量写入磁盘上的 SQLite 数据库；
- 重复坐标 / 同词多行多列的合计与排序由 SQLite 在磁盘上完成
  （PRAGMA temp_store=FILE，排序溢出到临时文件而非内存）；
- 核对时对两条已排序的聚合流做归并比较，任意时刻只持有当前行；
- 打包时矩阵内容先写入磁盘临时文件，再流式并入 ZIP。

只有页键表、词表及其索引驻留内存（随页数/词数增长，与非零项数无关）。
"""

import csv
import io
import os
import re
import shutil
import sqlite3
import sys
import zipfile

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

_INT_RE = re.compile(r"[+-]?[0-9]+")
_BATCH_SIZE = 20000
_INT64_MAX = 2**63 - 1

# 归并/聚合用的 SQL：重复坐标与同词多行多列在此合计；和为 0 的坐标
# （只可能来自显式写出的 0）按“未写出即零”的约定剔除。
_AGGREGATE_SQL = (
    "SELECT page, word, SUM(v) AS s FROM entries "
    "GROUP BY page, word HAVING s <> 0 ORDER BY page, word"
)


class FormatError(Exception):
    """用户上传数据的格式错误，消息直接展示在页面上。"""


class Dataset:
    """一套已解析并通过格式校验的数据。"""

    def __init__(self, label, orientation, pages, words, rows, cols, nnz, db_path):
        self.label = label                # “旧导出” / “新导出”
        self.orientation = orientation    # 'old'（页×词） / 'new'（词×页）
        self.pages = pages                # 页键列表（唯一），顺序与矩阵页轴一致
        self.words = words                # 词列表（可重复），顺序与矩阵词轴一致
        self.rows = rows
        self.cols = cols
        self.nnz = nnz                    # 矩阵声明的非零项数
        self.db_path = db_path            # 磁盘上的 SQLite 库，含 entries 表


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

def read_list_csv(stream, label, kind, require_unique):
    """读取单列 CSV（每行一个值、无表头），返回字符串列表。

    kind 为“页键”或“词”。页键要求唯一；词允许重复（同词多行/列将合计）。
    """
    items = []
    text = _text_stream(stream)
    try:
        for lineno, row in enumerate(csv.reader(text), 1):
            if not row:  # 跳过完全空白的行
                continue
            if len(row) != 1:
                raise FormatError(
                    f"{label}{kind}表第 {lineno} 行有 {len(row)} 列；"
                    f"应为单列 CSV（每行一个{kind}，无表头）"
                )
            items.append(row[0])
    except UnicodeDecodeError:
        raise FormatError(f"{label}{kind}表不是有效的 UTF-8 文本") from None
    except csv.Error as exc:
        raise FormatError(f"{label}{kind}表 CSV 解析失败：{exc}") from None
    if require_unique:
        seen = set()
        for it in items:
            if it in seen:
                raise FormatError(f"{label}页键 “{it}” 重复出现；页键必须唯一")
            seen.add(it)
    return items


# ---------------------------------------------------------------- Matrix Market

def _parse_int(token):
    return int(token) if _INT_RE.fullmatch(token) else None


def read_mtx_header(text, label):
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
        raise FormatError(f"{label}矩阵文件为空")
    tokens = banner.split()
    if len(tokens) != 5 or tokens[0] != "%%MatrixMarket":
        raise FormatError(
            f"{label}矩阵首行应为 “%%MatrixMarket matrix coordinate integer general”，"
            f"实际为 “{_snip(banner)}”"
        )
    names = ["对象", "存储格式", "字段类型", "对称性"]
    expect = ["matrix", "coordinate", "integer", "general"]
    got = [t.lower() for t in tokens[1:]]
    for i in range(4):
        if got[i] != expect[i]:
            raise FormatError(
                f"{label}矩阵头{names[i]}应为 “{expect[i]}”，实际为 “{tokens[1 + i]}”；"
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
                f"{label}矩阵第 {line_no} 行应为维度行 “行数 列数 非零项数”（三个整数），"
                f"实际为 “{_snip(s)}”"
            )
        rows, cols, nnz = vals
        if rows < 0 or cols < 0 or nnz < 0:
            raise FormatError(f"{label}矩阵第 {line_no} 行维度不能为负")
        return rows, cols, nnz, line_no
    raise FormatError(f"{label}矩阵缺少维度行（行数 列数 非零项数）")


def _ingest_entries(text, label, rows, cols, nnz, line_no, mapper, db):
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
                f"{label}矩阵声明 {nnz} 个非零项，但第 {line_no} 行还有多余内容："
                f"“{_snip(s)}”"
            )
        parts = s.split()
        vals = [_parse_int(p) for p in parts]
        if len(parts) != 3 or any(v is None for v in vals):
            raise FormatError(
                f"{label}矩阵第 {line_no} 行应为 “行号 列号 计数”（三个整数），"
                f"实际为 “{_snip(s)}”"
            )
        r, c, v = vals
        if r < 1 or r > rows:
            raise FormatError(
                f"{label}矩阵第 {line_no} 行行号 {r} 超出范围 1..{rows}"
            )
        if c < 1 or c > cols:
            raise FormatError(
                f"{label}矩阵第 {line_no} 行列号 {c} 超出范围 1..{cols}"
            )
        if v < 0:
            raise FormatError(
                f"{label}矩阵第 {line_no} 行计数 {v} 为负；计数必须非负"
            )
        if v > _INT64_MAX:
            raise FormatError(f"{label}矩阵第 {line_no} 行计数超出 64 位整数范围")
        batch.append(mapper(r, c, v))
        count += 1
        if len(batch) >= _BATCH_SIZE:
            db.executemany(insert, batch)
            batch.clear()
    if batch:
        db.executemany(insert, batch)
    db.commit()
    if count < nnz:
        raise FormatError(
            f"{label}矩阵声明 {nnz} 个非零项，实际只读到 {count} 个"
        )


def ingest_dataset(label, orientation, mtx_stream, pages_stream, words_stream, workdir):
    """解析并校验一套数据。返回 (Dataset 或 None, 错误消息列表)。

    orientation: 'old' 表示矩阵为 页×词（行=页）；'new' 表示 词×页（行=词）。
    """
    errors = []
    try:
        pages = read_list_csv(pages_stream, label, "页键", require_unique=True)
    except FormatError as e:
        pages = None
        errors.append(str(e))
    try:
        words = read_list_csv(words_stream, label, "词", require_unique=False)
    except FormatError as e:
        words = None
        errors.append(str(e))
    if errors:
        return None, errors

    try:
        text = _text_stream(mtx_stream)
        rows, cols, nnz, line_no = read_mtx_header(text, label)
        if orientation == "old":
            expected = (len(pages), len(words))
            shape = f"页数×词数 = {len(pages)}×{len(words)}"
        else:
            expected = (len(words), len(pages))
            shape = f"词数×页数 = {len(words)}×{len(pages)}"
        if (rows, cols) != expected:
            raise FormatError(
                f"{label}矩阵维度为 {rows}×{cols}，与 CSV 行列数不符：应为 {shape}"
            )
        db_path = os.path.join(workdir, orientation + ".db")
        db = sqlite3.connect(db_path)
        try:
            db.execute("PRAGMA temp_store = FILE")
            db.execute("PRAGMA synchronous = OFF")
            db.execute(
                "CREATE TABLE entries (page TEXT NOT NULL, word TEXT NOT NULL, "
                "v INTEGER NOT NULL)"
            )
            if orientation == "old":

                def mapper(r, c, v, _p=pages, _w=words):
                    return (_p[r - 1], _w[c - 1], v)
            else:

                def mapper(r, c, v, _p=pages, _w=words):
                    return (_p[c - 1], _w[r - 1], v)

            _ingest_entries(text, label, rows, cols, nnz, line_no, mapper, db)
        finally:
            db.close()
        return Dataset(label, orientation, pages, words, rows, cols, nnz, db_path), []
    except UnicodeDecodeError:
        return None, [f"{label}矩阵不是有效的 UTF-8 文本"]
    except FormatError as e:
        return None, [str(e)]


# ---------------------------------------------------------------- 核对

def verify_datasets(old_ds, new_ds, workdir):
    """核对两套数据。返回 dict：

    一致：{'diff': None, 'pages': [...], 'words': [...], 'nnz': n,
           'entries_path': 重新编号后的条目临时文件}
    不一致：{'diff': {...}}，diff 指出一处差异。
    """
    old_pages, new_pages = set(old_ds.pages), set(new_ds.pages)
    if old_pages != new_pages:
        only_old = sorted(old_pages - new_pages)
        only_new = sorted(new_pages - old_pages)
        if only_old:
            return {"diff": {"kind": "page", "key": only_old[0],
                             "present_in": "旧导出", "missing_in": "新导出"}}
        return {"diff": {"kind": "page", "key": only_new[0],
                         "present_in": "新导出", "missing_in": "旧导出"}}

    old_words, new_words = set(old_ds.words), set(new_ds.words)
    if old_words != new_words:
        only_old = sorted(old_words - new_words)
        only_new = sorted(new_words - old_words)
        if only_old:
            return {"diff": {"kind": "word", "key": only_old[0],
                             "present_in": "旧导出", "missing_in": "新导出"}}
        return {"diff": {"kind": "word", "key": only_new[0],
                         "present_in": "新导出", "missing_in": "旧导出"}}

    # 页键、词按字符串（Unicode 码位）排序并重新编号；
    # 与 SQLite 的 BINARY 排序（UTF-8 字节序）一致，二者等价。
    pages = sorted(old_pages)
    words = sorted(old_words)
    page_idx = {p: i + 1 for i, p in enumerate(pages)}
    word_idx = {w: i + 1 for i, w in enumerate(words)}

    entries_path = os.path.join(workdir, "canonical_entries.txt")
    nnz = 0
    db_old = sqlite3.connect(old_ds.db_path)
    db_new = sqlite3.connect(new_ds.db_path)
    try:
        db_old.execute("PRAGMA temp_store = FILE")
        db_new.execute("PRAGMA temp_store = FILE")
        cur_old = iter(db_old.execute(_AGGREGATE_SQL))
        cur_new = iter(db_new.execute(_AGGREGATE_SQL))
        row_old = next(cur_old, None)
        row_new = next(cur_new, None)
        with open(entries_path, "w", encoding="utf-8", newline="\n") as out:
            while row_old is not None or row_new is not None:
                if row_new is None or (row_old is not None
                                       and row_old[:2] < row_new[:2]):
                    return {"diff": {"kind": "count", "page": row_old[0],
                                     "word": row_old[1],
                                     "old": row_old[2], "new": 0}}
                if row_old is None or row_new[:2] < row_old[:2]:
                    return {"diff": {"kind": "count", "page": row_new[0],
                                     "word": row_new[1],
                                     "old": 0, "new": row_new[2]}}
                if row_old[2] != row_new[2]:
                    return {"diff": {"kind": "count", "page": row_old[0],
                                     "word": row_old[1],
                                     "old": row_old[2], "new": row_new[2]}}
                out.write(
                    f"{page_idx[row_old[0]]} {word_idx[row_old[1]]} {row_old[2]}\n"
                )
                nnz += 1
                row_old = next(cur_old, None)
                row_new = next(cur_new, None)
    finally:
        db_old.close()
        db_new.close()
    return {"diff": None, "pages": pages, "words": words,
            "nnz": nnz, "entries_path": entries_path}


# ---------------------------------------------------------------- 打包

def _write_list_csv(zf, arcname, items):
    with zf.open(arcname, "w") as f:
        text = io.TextIOWrapper(f, encoding="utf-8", newline="")
        writer = csv.writer(text, lineterminator="\n")
        for it in items:
            writer.writerow([it])
        text.flush()
        text.detach()


def build_zip(zip_path, pages, words, nnz, entries_path):
    """生成旧格式 ZIP：matrix.mtx（页×词，按页键与词排序、重新编号）
    以及配套的 pages.csv / words.csv。全程流式写盘。"""
    tmp_path = zip_path + ".part"
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED,
                         allowZip64=True) as zf:
        with zf.open("matrix.mtx", "w") as f:
            header = (
                "%%MatrixMarket matrix coordinate integer general\n"
                "% generated by archive-checker: pages x words, sorted by "
                "page key and word, renumbered\n"
                f"{len(pages)} {len(words)} {nnz}\n"
            )
            f.write(header.encode("ascii"))
            with open(entries_path, "rb") as src:
                shutil.copyfileobj(src, f, 1 << 20)
        _write_list_csv(zf, "pages.csv", pages)
        _write_list_csv(zf, "words.csv", words)
    os.replace(tmp_path, zip_path)  # 原子发布，下载方只会看到完整文件
