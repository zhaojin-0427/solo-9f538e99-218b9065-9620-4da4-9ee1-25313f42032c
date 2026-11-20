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
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
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

def canonicalize_dataset(ds, workdir):
    """把一套 页×词 数据整理为规范条目：重复坐标、同词多列合计，
    按页键与词（字符串）排序并重新编号，剔除合计为 0 的坐标。

    返回 {'pages': [...], 'words': [...], 'nnz': n, 'entries_path': ...}；
    聚合与排序都在磁盘 SQLite 中完成，内存不随非零项数增长。
    """
    pages = sorted(set(ds.pages))
    words = sorted(set(ds.words))
    page_idx = {p: i + 1 for i, p in enumerate(pages)}
    word_idx = {w: i + 1 for i, w in enumerate(words)}

    entries_path = os.path.join(workdir, "canonical_entries.txt")
    nnz = 0
    db = sqlite3.connect(ds.db_path)
    try:
        db.execute("PRAGMA temp_store = FILE")
        with open(entries_path, "w", encoding="utf-8", newline="\n") as out:
            for page, word, value in db.execute(_AGGREGATE_SQL):
                out.write(f"{page_idx[page]} {word_idx[word]} {value}\n")
                nnz += 1
    finally:
        db.close()
    return {"pages": pages, "words": words, "nnz": nnz,
            "entries_path": entries_path}


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


# ---------------------------------------------------------------- 单文件阅览包
#
# 包结构（ZIP，单文件）：
#   manifest.json      包标识、块数、页/词表摘要，以及每块的摘要、行数与词号范围
#   pages.csv          页键表：第 n 行（从 1 起）= 页编号 n
#   words.csv          词表：第 m 行（从 1 起）= 词编号 m
#   blocks/<k>.csv     第 k 块，每行 “词编号,页编号,计数”
#
# 词按词编号连续切块，每块最多 BLOCK_MAX_ROWS 条非零记录，因此一个词可以
# 跨块；一个词在同一块中至多对应一个页一次。查询某词时只需解压覆盖该词的
# 块（外加体量很小的 manifest 与页/词索引表），按页编号归并即可。

BROWSE_FORMAT = "archive-browse-package"
BROWSE_VERSION = 1
BLOCK_MAX_ROWS = 512

_META_MAX_BYTES = 64 * 1024 * 1024    # manifest / 页词索引表解压后的上限
_BLOCK_MAX_BYTES = 16 * 1024 * 1024   # 单块解压后的上限（512 行，远小于此）
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BLOCK_NAME_RE = re.compile(r"blocks/[1-9][0-9]*\.csv")


class PackageError(Exception):
    """阅览包缺失、损坏或查询参数有误；消息直接展示在页面上。"""


def _write_csv_member(zf, arcname, items):
    """把单列值流式写为 ZIP 成员，同时返回 (sha256, 行数)。"""
    digest = hashlib.sha256()
    rows = 0
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    def flush(f):
        data = buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate(0)
        if data:
            f.write(data)
            digest.update(data)

    with zf.open(arcname, "w") as f:
        for item in items:
            writer.writerow([item])
            rows += 1
            if buf.tell() > (1 << 16):
                flush(f)
        flush(f)
    return digest.hexdigest(), rows


def _flush_block(zf, frag, block_no, buf, first_word, last_word):
    """把一块（至多 BLOCK_MAX_ROWS 行）写入 ZIP，并把摘要追加到 manifest 片段。"""
    data = buf.getvalue().encode("utf-8")
    name = f"blocks/{block_no}.csv"
    with zf.open(name, "w") as f:
        f.write(data)
    meta = {
        "name": name,
        "rows": data.count(b"\n"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "first_word": first_word,
        "last_word": last_word,
    }
    frag.write(json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n")


def _write_browse_zip(tmp_path, frag_path, pages, words, nnz, db):
    """把阅览包各成员流式写入 tmp_path（先写临时名，后由调用方原子改名）。

    返回块数。块索引逐块写入 frag_path，最后拼进 manifest；全程不持有整份条目。
    """
    block_count = 0
    total = 0
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED,
                         allowZip64=True) as zf:
        pages_sha, pages_rows = _write_csv_member(zf, "pages.csv", pages)
        words_sha, words_rows = _write_csv_member(zf, "words.csv", words)

        with open(frag_path, "w", encoding="utf-8", newline="\n") as frag:
            buf = io.StringIO()
            rows_in_block = 0
            first_word = last_word = None
            for w, p, v in db.execute("SELECT w, p, v FROM e ORDER BY w, p"):
                if rows_in_block == BLOCK_MAX_ROWS:
                    _flush_block(zf, frag, block_count + 1, buf,
                                 first_word, last_word)
                    block_count += 1
                    buf = io.StringIO()
                    rows_in_block = 0
                    first_word = last_word = None
                if first_word is None:
                    first_word = w
                last_word = w
                buf.write(f"{w},{p},{v}\n")
                rows_in_block += 1
                total += 1
            if rows_in_block:
                _flush_block(zf, frag, block_count + 1, buf,
                             first_word, last_word)
                block_count += 1

        if total != nnz:
            raise ValueError(
                f"规范条目数 {total} 与声明的非零项数 {nnz} 不一致")

        head = json.dumps({
            "format": BROWSE_FORMAT,
            "version": BROWSE_VERSION,
            "block_max_rows": BLOCK_MAX_ROWS,
            "pages_count": pages_rows,
            "pages_sha256": pages_sha,
            "words_count": words_rows,
            "words_sha256": words_sha,
            "nnz": nnz,
        }, ensure_ascii=False, sort_keys=True)
        with zf.open("manifest.json", "w") as f:
            f.write(head[:-1].encode("utf-8"))
            f.write(b', "blocks": [')
            first = True
            with open(frag_path, "r", encoding="utf-8") as frag:
                for line in frag:
                    if not first:
                        f.write(b",")
                    f.write(line.strip().encode("utf-8"))
                    first = False
            f.write(b"]}")
    return block_count


def build_browse_package(zip_path, pages, words, nnz, entries_path, workdir):
    """生成单文件阅览包。返回 {'blocks': 块数, 'nnz': 非零项数}。

    词坐标先经磁盘上的 SQLite 重排为 词、页 顺序（内存不随非零项数增长）；
    块缓冲至多 BLOCK_MAX_ROWS 行；manifest 中的块索引也逐块流式写出。
    包先写入 .part，成功关闭后才原子改名，下载方只会看到完整的包。
    """
    tmp_path = zip_path + ".part"
    frag_path = tmp_path + ".blocks.jsonl"
    db_path = os.path.join(workdir, "browse-order.db")
    for p in (tmp_path, frag_path, db_path):
        try:
            os.remove(p)
        except OSError:
            pass

    db = sqlite3.connect(db_path)
    try:
        db.execute("PRAGMA temp_store = FILE")
        db.execute("PRAGMA synchronous = OFF")
        db.execute("CREATE TABLE e (w INTEGER NOT NULL, p INTEGER NOT NULL, "
                   "v INTEGER NOT NULL)")
        batch = []
        insert = "INSERT INTO e (w, p, v) VALUES (?,?,?)"
        try:
            with open(entries_path, "r", encoding="utf-8") as src:
                for lineno, line in enumerate(src, 1):
                    parts = line.split()
                    if len(parts) != 3:
                        raise ValueError(f"规范条目第 {lineno} 行格式有误")
                    batch.append((int(parts[1]), int(parts[0]), int(parts[2])))
                    if len(batch) >= _BATCH_SIZE:
                        db.executemany(insert, batch)
                        batch.clear()
        finally:
            if batch:
                db.executemany(insert, batch)
        db.commit()

        try:
            block_count = _write_browse_zip(
                tmp_path, frag_path, pages, words, nnz, db)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        os.replace(tmp_path, zip_path)
        return {"blocks": block_count, "nnz": nnz}
    finally:
        db.close()
        for p in (db_path, db_path + "-journal", db_path + "-wal", frag_path):
            try:
                os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------- 阅览查询

def _read_member(zf, name, cap, label):
    """有上限地解压一个成员；成员缺失、超限或解压出错都视为包不完整/损坏。"""
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise PackageError(f"包不完整：缺少 {label}") from None
    if info.file_size > cap:
        raise PackageError(f"完整性校验失败：{label} 大小异常")
    try:
        with zf.open(info) as f:
            data = f.read(cap + 1)
            extra = f.read(1)
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, ValueError) as exc:
        raise PackageError(f"包已损坏：无法解压 {label}（{exc}）") from None
    if extra or len(data) > cap:
        raise PackageError(f"完整性校验失败：{label} 大小异常")
    return data


def _check_digest(data, expected, label):
    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected) \
            or hashlib.sha256(data).hexdigest() != expected:
        raise PackageError(f"完整性校验失败：{label} 与 manifest 记录不符")


def _load_list_member(zf, name, expected_rows, expected_sha, label):
    """读取并校验页/词索引表，返回逐行字符串列表。"""
    raw = _read_member(zf, name, _META_MAX_BYTES, name)
    _check_digest(raw, expected_sha, name)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError(f"包已损坏：{name} 不是 UTF-8 文本") from None
    items = []
    try:
        for row in csv.reader(io.StringIO(text)):
            if not row:
                continue
            if len(row) != 1:
                raise PackageError(f"包已损坏：{label}表不是单列 CSV")
            items.append(row[0])
    except csv.Error as exc:
        raise PackageError(f"包已损坏：{label}表解析失败（{exc}）") from None
    if len(items) != expected_rows:
        raise PackageError(
            f"完整性校验失败：{label}表行数与 manifest 记录不符")
    return items


def _manifest_int(manifest, key, minimum):
    value = manifest.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise PackageError(f"包已损坏：manifest.json 的 {key} 无效")
    return value


def _seekable_copy(stream):
    """zipfile 需要可定位的流；上传流不可定位时先落到临时文件。"""
    if getattr(stream, "seekable", lambda: False)():
        return stream, None
    tmp = tempfile.TemporaryFile()
    shutil.copyfileobj(stream, tmp, 1 << 20)
    tmp.seek(0)
    return tmp, tmp


def query_browse_package(stream, word, start_page_key=None, limit=100):
    """在阅览包中查询一个词。

    只解压覆盖该词的块；其余块即使损坏也不影响本次查询。
    返回 {'status': 'ok'|'not_in_vocabulary'|'zero_total', ...}；
    所需块缺失或校验失败时抛出 PackageError（整页报错）。
    """
    stream, close_me = _seekable_copy(stream)
    try:
        return _query_package(stream, word, start_page_key, limit)
    finally:
        if close_me is not None:
            close_me.close()


def _query_package(stream, word, start_page_key=None, limit=100):
    try:
        zf = zipfile.ZipFile(stream)
    except (zipfile.BadZipFile, OSError):
        raise PackageError("无法读取：上传的文件不是有效的 ZIP 包") from None
    with zf:
        raw = _read_member(zf, "manifest.json", _META_MAX_BYTES, "manifest.json")
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PackageError("包已损坏：manifest.json 不是有效的 JSON") from None
        if not isinstance(manifest, dict):
            raise PackageError("包已损坏：manifest.json 结构无效")
        if manifest.get("format") != BROWSE_FORMAT:
            raise PackageError("不是本工具的阅览包（manifest 标识不符）")
        if manifest.get("version") != BROWSE_VERSION:
            raise PackageError(
                f"不支持的阅览包版本：{manifest.get('version')!r}")
        page_count = _manifest_int(manifest, "pages_count", 0)
        words_count = _manifest_int(manifest, "words_count", 0)
        nnz = _manifest_int(manifest, "nnz", 0)
        block_max = _manifest_int(manifest, "block_max_rows", 1)
        raw_blocks = manifest.get("blocks")
        if not isinstance(raw_blocks, list):
            raise PackageError("包已损坏：manifest.json 的 blocks 无效")

        blocks = []
        prev_first = 0
        rows_total = 0
        for b in raw_blocks:
            if not isinstance(b, dict):
                raise PackageError("包已损坏：manifest.json 的块记录无效")
            name = b.get("name")
            rows = b.get("rows")
            first_w = b.get("first_word")
            last_w = b.get("last_word")
            if not isinstance(name, str) or not _BLOCK_NAME_RE.fullmatch(name):
                raise PackageError("包已损坏：manifest.json 的块名无效")
            for value, key in ((rows, "rows"), (first_w, "first_word"),
                               (last_w, "last_word")):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise PackageError(f"包已损坏：块 {name} 的 {key} 无效")
            if rows < 1 or rows > block_max:
                raise PackageError(f"包已损坏：块 {name} 的行数超出约定")
            if first_w < 1 or last_w < first_w or first_w < prev_first:
                raise PackageError(f"包已损坏：块 {name} 的词号范围无效")
            if not isinstance(b.get("sha256"), str) \
                    or not _SHA256_RE.fullmatch(b["sha256"]):
                raise PackageError(f"包已损坏：块 {name} 的摘要无效")
            prev_first = first_w
            rows_total += rows
            blocks.append(b)
        if rows_total != nnz:
            raise PackageError("包不完整：各块行数合计与 manifest 记录不符")

        pages = _load_list_member(
            zf, "pages.csv", page_count, manifest.get("pages_sha256"), "页键")
        words = _load_list_member(
            zf, "words.csv", words_count, manifest.get("words_sha256"), "词")

        start_num = None
        if start_page_key is not None and start_page_key != "":
            try:
                start_num = pages.index(start_page_key) + 1
            except ValueError:
                raise PackageError(
                    f"起始页键 “{start_page_key}” 不在本包的页键表中") from None

        word_num = None
        for i, w in enumerate(words, 1):
            if w == word:
                word_num = i
                break

        base = {
            "word": word,
            "start_page_key": start_page_key or "",
            "vocabulary_size": len(words),
            "pages_count": len(pages),
            "nnz": nnz,
            "block_count": len(blocks),
            "limit": limit,
        }
        if word_num is None:
            base["status"] = "not_in_vocabulary"
            return base

        needed = [b for b in blocks
                  if b["first_word"] <= word_num <= b["last_word"]]

        counts = {}
        for b in needed:
            name = b["name"]
            label = f"查询所需的分块 {name}"
            data = _read_member(zf, name, _BLOCK_MAX_BYTES, label)
            _check_digest(data, b["sha256"], name)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise PackageError(f"完整性校验失败：{name} 内容无效") from None
            parsed_rows = 0
            for lineno, line in enumerate(text.splitlines(), 1):
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) != 3:
                    raise PackageError(
                        f"完整性校验失败：{name} 第 {lineno} 行内容无效")
                try:
                    w, p, v = (int(x) for x in parts)
                except ValueError:
                    raise PackageError(
                        f"完整性校验失败：{name} 第 {lineno} 行内容无效") from None
                if w < 1 or p < 1 or p > len(pages) or v < 1:
                    raise PackageError(
                        f"完整性校验失败：{name} 第 {lineno} 行内容无效")
                parsed_rows += 1
                if w == word_num:
                    counts[p] = counts.get(p, 0) + v
            if parsed_rows != b["rows"]:
                raise PackageError(
                    f"完整性校验失败：{name} 行数与 manifest 记录不符")

        base["block_count_used"] = len(needed)
        base["word_number"] = word_num
        if not counts:
            base["status"] = "zero_total"
            return base

        all_matched = sorted(counts.items())
        base["total_nonzero"] = len(all_matched)
        if start_num is not None:
            remaining = [(p, c) for p, c in all_matched if p > start_num]
        else:
            remaining = all_matched
        base["matched_total"] = len(remaining)
        if not remaining:
            # 词本身有非零页，但都在起始页键之前（或就是该页）。
            base["status"] = "none_after_start"
            return base
        shown = remaining[:limit]
        base["rows"] = [
            {"index": p, "page_key": pages[p - 1], "count": c}
            for p, c in shown
        ]
        base["has_more"] = len(remaining) > limit
        base["status"] = "ok"
        return base
