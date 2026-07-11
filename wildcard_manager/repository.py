from __future__ import annotations

import json
import logging
import re
import shutil
import os
import sqlite3
from pathlib import Path
import time
from datetime import datetime

log = logging.getLogger(__name__)

from .models import AppSettings, WildcardEntry

LORA_PATTERN = re.compile(r"<lora:([^:>]+)(?::[^>]+)?>", re.IGNORECASE)
THUMBNAIL_EXTENSIONS = (".webp", ".png", ".jpg", ".jpeg")
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp932")
_NATURAL_SPLIT = re.compile(r"(\d+)")


def _safe_resolve(path: Path) -> str:
    """Windows 環境で Path.resolve() が OSError [WinError 6] を投げる問題へ対処する。

    strict=False で呼んでも Windows ではレースコンディションやハンドル問題で
    例外が上がるケースがある。その場合は absolute() にフォールバックし、
    それでも失敗する場合は str(path) を返す。

    これにより scan_library / move_entry / delete_folder / _transfer_file /
    _build_entry / _portable_abs_path など resolve() を呼ぶ全箇所で安全に
    絶対パス文字列を取得できる。

    【方針A】パス正規化を一元化する目的で用意した共通関数。
    """
    try:
        return str(path.resolve(strict=False))
    except (OSError, ValueError):
        try:
            return str(path.absolute())
        except Exception:
            log.debug("Failed to resolve path: %s", path, exc_info=True)
            return str(path)


def _safe_resolve_path(path: Path) -> Path:
    """Path 型で欲しい場合の _safe_resolve 版。

    move_entry / delete_folder では resolve() 済みの Path が必要なため用意した。
    """
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError):
        try:
            return path.absolute()
        except Exception:
            log.debug("Failed to resolve path: %s", path, exc_info=True)
            return path


def natural_sort_key(text: str) -> tuple[object, ...]:
    parts = _NATURAL_SPLIT.split(text.lower())
    key: list[object] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def sidecar_metadata_path(txt_path: Path) -> Path:
    return txt_path.with_suffix(txt_path.suffix + ".wcm.json")


def strip_lora_tags(text: str) -> str:
    """LoRA タグを除去し、空白の正規化を行う。

    以前の ``.replace("  ", " ")`` は3連続スペースを1回で潰せない問題があった
    ため ``re.sub(r"\\s+", " ", ...)`` に切り替えた（M13 修正）。
    """
    cleaned = re.sub(r"\s*<lora:[^>]+>\s*", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def parse_prompt_tags(text: str) -> list[str]:
    """プロンプト文字列をタグ列へ分割する。LoRAタグは先頭に配置する。

    以前は ``reversed`` して ``insert(0, ...)`` する二重逆転ロジックだったが、
    素直にリスト結合に書き直した（M4 修正）。結果は同一。

    M15 修正: 以前は extract_lora_names でLoRA名だけ取り出し f"<lora:{name}:1>" と
    強度を :1 に決め打ちしていたため、:0.6 など任意の強度で保存しても
    読み込み時に常に :1 に戻るバグがあった。
    LORA_PATTERN の group(0) でタグ全体をそのまま保持するよう修正。
    """
    seen: set[str] = set()
    lora_formatted: list[str] = []
    for match in LORA_PATTERN.finditer(text):
        tag = match.group(0)  # <lora:name:weight> をそのまま使う
        if tag not in seen:
            seen.add(tag)
            lora_formatted.append(tag)
    body_tags: list[str] = []
    clean_text = strip_lora_tags(text)
    for chunk in clean_text.replace("\n", ",").split(","):
        clean = chunk.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        body_tags.append(clean)
    return lora_formatted + body_tags


def extract_lora_names(text: str) -> list[str]:
    """テキスト中の LoRA 名を実出現順かつ重複なしで抽出する。

    以前は ``set`` + ``sorted`` で元の出現順序を失っていた。プロンプト内の
    LoRA 順序は意味を持つ場合があるため ``dict.fromkeys`` で順序を保存する
    （M12 修正）。
    """
    names = list(dict.fromkeys(
        match.group(1).strip() for match in LORA_PATTERN.finditer(text)
    ))
    return [name for name in names if name]


def ensure_thumbnail_destination(thumbnail_root: Path, rel_path: str) -> Path:
    """サムネイル生成時の保存先パスを返す。

    常に ``{stem}.preview.webp`` に書き込む。``_thumbnail_candidates`` も
    ``.preview.webp`` を最初に候補へ追加するため、生成済みファイルが
    最優先で表示される（M14 検証結果：優先度は実は整合していた）。
    """
    rel = Path(rel_path)
    return thumbnail_root / rel.parent / f"{rel.stem}.preview.webp"


CONFLICT_POLICIES = {
    "overwrite": "常に上書き",
    "keep_existing": "既存を残す",
    "keep_newer": "新しい日付を残す",
    "keep_larger": "大きい方を残す",
}


class OperationCancelledError(Exception):
    pass


class WildcardRepository:
    # scan_profile.log のサイズ上限（バイト）。超過時は先頭から切り詰める（L6 修正）。
    PROFILE_LOG_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # journal_mode=WAL は DB ファイル自体に永続化されるため、一度設定すれば
        # 次回以降は再設定不要。初期化時に 1 回だけ実行する（H2 修正）。
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """DB 接続を開く。

        以前は毎回 ``PRAGMA journal_mode=WAL`` 等を発行していたが、
        journal_mode は永続設定なので初回のみで済む。その他の PRAGMA は
        接続スコープなので毎回設定する（H2 修正）。
        """
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA cache_size = -2000")
        except Exception:
            log.debug("Failed to set PRAGMA settings", exc_info=True)
        return connection

    def _append_profile(self, message: str) -> None:
        try:
            log_path = self.db_path.parent / "scan_profile.log"
            # ログが膨張しすぎないよう、上限超過時は古いエントリを切り詰める。
            try:
                if log_path.exists() and log_path.stat().st_size > self.PROFILE_LOG_MAX_BYTES:
                    keep = log_path.read_text(encoding="utf-8", errors="replace")
                    keep = keep[-self.PROFILE_LOG_MAX_BYTES // 2:]
                    log_path.write_text(keep, encoding="utf-8")
            except Exception:
                log.debug("Failed to truncate profile log", exc_info=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} {message}\n")
        except Exception:
            log.debug("Failed to write profile log", exc_info=True)

    def _init_db(self) -> None:
        with self._connect() as con:
            # journal_mode=WAL はDBファイル自体に永続化される。
            # 初回起動時のみ発行すればよい（H2 修正）。
            try:
                con.execute("PRAGMA journal_mode=WAL")
            except Exception:
                log.debug("Failed to set WAL journal_mode", exc_info=True)
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    abs_path TEXT PRIMARY KEY,
                    rel_path TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    name TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    content TEXT NOT NULL,
                    search_text TEXT NOT NULL DEFAULT '',
                    first_line TEXT NOT NULL,
                    line_count INTEGER NOT NULL,
                    custom_tags_json TEXT NOT NULL,
                    lora_names_json TEXT NOT NULL,
                    thumbnail_path TEXT,
                    has_thumbnail INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    sidecar_mtime REAL NOT NULL,
                    thumb_mtime REAL NOT NULL
                )
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(entries)")}
            if "search_text" not in columns:
                con.execute("ALTER TABLE entries ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
            con.execute("CREATE INDEX IF NOT EXISTS idx_entries_rel_path ON entries(rel_path)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_entries_folder ON entries(folder)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_entries_name ON entries(name)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_entries_search_text ON entries(search_text)")
            # rel_path の一意制約を導入（H3 修正）。
            # 旧DBには重複行が存在し得るため、移行時に新しい UNIQUE INDEX 作成に失敗する
            # 可能性がある。その場合は重複行を削除してから再作成する。
            try:
                con.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_entries_rel_path ON entries(rel_path)"
                )
            except sqlite3.IntegrityError:
                # 重複行を残している古い rel_path のうち、rowid が大きい（新しい）方を残す
                con.execute(
                    """
                    DELETE FROM entries WHERE rowid NOT IN (
                        SELECT MAX(rowid) FROM entries GROUP BY rel_path
                    )
                    """
                )
                con.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_entries_rel_path ON entries(rel_path)"
                )

    def _build_thumbnail_index(self, thumbnail_root: Path) -> set[str]:
        """サムネイルフォルダ内の全ファイルパスを一度だけ列挙してセットにする。
        これにより1ファイルごとの exists() 呼び出しを廃止できる。"""
        index: set[str] = set()
        if not thumbnail_root.exists():
            return index
        try:
            for dirpath, _dirs, fnames in os.walk(thumbnail_root):
                for fname in fnames:
                    index.add(os.path.normcase(os.path.join(dirpath, fname)))
        except Exception:
            log.debug("Failed to walk thumbnail directory", exc_info=True)
        return index

    def resolve_thumbnail_path_fast(self, thumbnail_root: Path, rel_path: Path, thumb_index: set[str]) -> Path | None:
        """thumb_index（事前構築済みセット）を使って存在確認をO(1)で行う。"""
        for candidate in self._thumbnail_candidates(thumbnail_root, rel_path):
            if os.path.normcase(str(candidate)) in thumb_index:
                return candidate
        return None

    def scan_library(self, settings: AppSettings, progress=None, *, max_files_per_dir: int | None = None, skip_dirs: list[str] | None = None) -> dict[str, int]:
        library_root = Path(settings.library_root)
        thumbnail_root = Path(settings.thumbnail_root)
        library_root.mkdir(parents=True, exist_ok=True)
        thumbnail_root.mkdir(parents=True, exist_ok=True)
        # Use os.walk to avoid building large file lists in memory and to
        # process files in a single pass. Pass total=0 to progress for
        # indeterminate progress to avoid a double-pass for counting files.
        profile_start = time.perf_counter()
        self._append_profile("scan_library: start")

        known_paths: set[str] = set()
        scanned = 0
        updated = 0
        index = 0

        thumb_index_start = time.perf_counter()
        thumb_index = self._build_thumbnail_index(thumbnail_root)
        self._append_profile(f"scan_library: thumb_index built count={len(thumb_index)} elapsed={time.perf_counter()-thumb_index_start:.3f}s")

        with self._connect() as con:
            current_rows = {
                row["abs_path"]: row
                for row in con.execute(
                    "SELECT abs_path, file_mtime, sidecar_mtime, thumb_mtime FROM entries"
                )
            }
            # dir_mtimes.json の読み書きは実質デッドコードだった（スキップ判定が
            # 実装されておらず pass だった）。M10 修正で完全に削除し、毎回の
            # os.walk 二回走査も不要にした。

            # Organize DB rows by directory for quick known-paths population
            current_rows_by_dir: dict[str, list[str]] = {}
            for abs_p in current_rows.keys():
                # 【WinError 6 対策】_safe_resolve でラップ
                parent = _safe_resolve(Path(abs_p).parent)
                current_rows_by_dir.setdefault(parent, []).append(abs_p)

            walk_start = time.perf_counter()
            for root, dirs, files in os.walk(library_root):
                # 【WinError 6 対策】_safe_resolve でラップ
                root_res = _safe_resolve(Path(root))
                # M11 修正: 読みにくい for/else + デッドコード (root = root) を整理。
                if skip_dirs:
                    skip_resolved: list[str] = []
                    for s in skip_dirs:
                        # 【WinError 6 対策】_safe_resolve でラップ
                        skip_resolved.append(_safe_resolve(Path(s)))
                    if any(root_res.startswith(s_res) for s_res in skip_resolved):
                        self._append_profile(f"scan_library: skipping_dir_by_list={root_res}")
                        dirs[:] = []
                        continue
                # Optionally skip directories that contain too many files
                if max_files_per_dir is not None:
                    txt_count = sum(1 for f in files if f.lower().endswith('.txt'))
                    if txt_count > max_files_per_dir:
                        self._append_profile(f"scan_library: skipping_dir_by_size={root_res} files={txt_count}")
                        dirs[:] = []
                        known_for_dir = current_rows_by_dir.get(root_res)
                        if known_for_dir:
                            for p in known_for_dir:
                                known_paths.add(p)
                        continue
                for fname in files:
                    if not fname.lower().endswith(".txt"):
                        continue
                    index += 1
                    txt_path = Path(root) / fname
                    # 【WinError 6 対策】_safe_resolve でラップ
                    abs_path = _safe_resolve(txt_path)
                    known_paths.add(abs_path)
                    row = current_rows.get(abs_path)
                    sidecar_path = sidecar_metadata_path(txt_path)
                    rel_path_for_thumb = txt_path.relative_to(library_root)
                    thumb_path = self.resolve_thumbnail_path_fast(thumbnail_root, rel_path_for_thumb, thumb_index)
                    try:
                        file_mtime = txt_path.stat().st_mtime
                    except Exception:
                        log.debug("Failed to stat txt file: %s", txt_path, exc_info=True)
                        file_mtime = 0.0
                    try:
                        sidecar_mtime = sidecar_path.stat().st_mtime if sidecar_path.exists() else 0.0
                    except Exception:
                        log.debug("Failed to stat sidecar: %s", sidecar_path, exc_info=True)
                        sidecar_mtime = 0.0
                    try:
                        thumb_mtime = thumb_path.stat().st_mtime if thumb_path else 0.0
                    except Exception:
                        log.debug("Failed to stat thumbnail: %s", thumb_path, exc_info=True)
                        thumb_mtime = 0.0

                    unchanged = (
                        row
                        and row["file_mtime"] == file_mtime
                        and row["sidecar_mtime"] == sidecar_mtime
                        and row["thumb_mtime"] == thumb_mtime
                    )
                    if unchanged:
                        scanned += 1
                        if progress:
                            if progress(index, 0, txt_path.name) is False:
                                raise OperationCancelledError()
                        continue

                    file_start = time.perf_counter()
                    entry = self._build_entry(settings, txt_path, thumb_path)
                    self._upsert(con, entry, file_mtime, sidecar_mtime, thumb_mtime)
                    file_elapsed = time.perf_counter() - file_start
                    scanned += 1
                    updated += 1
                    if file_elapsed > 0.05:
                        self._append_profile(f"slow_file: {txt_path} build_upsert_time={file_elapsed:.3f}s")
                    if progress:
                        if progress(index, 0, txt_path.name) is False:
                            raise OperationCancelledError()

            walk_elapsed = time.perf_counter() - walk_start
            self._append_profile(f"scan_library: walk_elapsed={walk_elapsed:.3f}s files_indexed={index}")

            stale_paths = set(current_rows) - known_paths
            if stale_paths:
                con.executemany("DELETE FROM entries WHERE abs_path = ?", [(path,) for path in stale_paths])

        total_elapsed = time.perf_counter() - profile_start
        self._append_profile(f"scan_library: finished scanned={scanned} updated={updated} deleted={len(set(current_rows) - known_paths)} total_elapsed={total_elapsed:.3f}s")
        return {"scanned": scanned, "updated": updated, "deleted": len(set(current_rows) - known_paths)}

    def load_entries(self, settings: AppSettings | None = None) -> list[WildcardEntry]:
        start = time.perf_counter()
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM entries WHERE rowid IN (SELECT MAX(rowid) FROM entries GROUP BY rel_path) ORDER BY rel_path COLLATE NOCASE"
            ).fetchall()
        entries = [self._row_to_entry(row, settings) for row in rows]
        elapsed = time.perf_counter() - start
        try:
            self._append_profile(f"load_entries: count={len(entries)} elapsed={elapsed:.3f}s")
        except Exception:
            log.debug("Failed to append profile", exc_info=True)
        return entries

    def load_entries_summary(self, settings: AppSettings | None = None) -> list[WildcardEntry]:
        start = time.perf_counter()
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT abs_path, rel_path, folder, name, stem, first_line, line_count,
                       custom_tags_json, lora_names_json, thumbnail_path, has_thumbnail, search_text
                FROM entries
                WHERE rowid IN (
                    SELECT MAX(rowid) FROM entries GROUP BY rel_path
                )
                ORDER BY rel_path COLLATE NOCASE
                """
            ).fetchall()
        entries = [self._row_to_entry_summary(row, settings) for row in rows]
        elapsed = time.perf_counter() - start
        try:
            self._append_profile(f"load_entries_summary: count={len(entries)} elapsed={elapsed:.3f}s")
        except Exception:
            log.debug("Failed to append profile", exc_info=True)
        return entries

    def load_entry_content(self, path: Path) -> str:
        return self._read_text_file(path)

    def save_entry(self, settings: AppSettings, entry: WildcardEntry, content: str, custom_tags: list[str]) -> WildcardEntry:
        txt_path = Path(entry.abs_path)
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(content, encoding="utf-8")

        sidecar_path = sidecar_metadata_path(txt_path)
        sidecar_data = {"custom_tags": sorted({tag.strip() for tag in custom_tags if tag.strip()})}
        sidecar_path.write_text(json.dumps(sidecar_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.refresh_entry(settings, txt_path)

    def refresh_entry(self, settings: AppSettings, txt_path: Path) -> WildcardEntry:
        file_mtime = txt_path.stat().st_mtime
        sidecar_path = sidecar_metadata_path(txt_path)
        sidecar_mtime = sidecar_path.stat().st_mtime if sidecar_path.exists() else 0.0
        thumb_path = self.resolve_thumbnail_path(settings, txt_path)
        thumb_mtime = thumb_path.stat().st_mtime if thumb_path else 0.0
        entry = self._build_entry(settings, txt_path, thumb_path)
        with self._connect() as con:
            self._upsert(con, entry, file_mtime, sidecar_mtime, thumb_mtime)
        return entry

    def delete_entry(self, abs_path: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM entries WHERE abs_path = ?", (abs_path,))

    def move_entry(self, settings: AppSettings, entry: WildcardEntry, dest_folder: str) -> WildcardEntry:
        """エントリを別フォルダへ移動する。

        H6 修正: 以前は dest_folder が ``../../etc`` のような場合にライブラリ
        ルート外へ脱出できてしまった。delete_folder と同様に resolve() した
        上で library_root 配下か検証する。
        """
        # dest_folder は POSIX 区切り（"/"）で渡る想定。OS 非依存に合成する。
        # 【WinError 6 対策】_safe_resolve_path でラップ
        library_root = _safe_resolve_path(Path(settings.library_root))
        thumbnail_root = _safe_resolve_path(Path(settings.thumbnail_root))

        # ライブラリ外への脱出を防ぐため、dest_folder を POSIX 区切りで分割して
        # Path(*parts) で合成する。Path(dest_folder) だと Windows でバックスラッシュ
        # を含む場合に親ディレクトリ参照 ".." が素通ししてしまう。
        clean_folder = dest_folder.strip().replace("\\", "/").strip("/")
        if clean_folder:
            parts = [p for p in clean_folder.split("/") if p not in ("", ".")]
            dest_dir = _safe_resolve_path(library_root / Path(*parts)) if parts else library_root
        else:
            dest_dir = library_root

        if dest_dir != library_root and library_root not in dest_dir.parents:
            raise ValueError("移動先がライブラリ外です。")

        src_txt = Path(entry.abs_path)
        src_sidecar = sidecar_metadata_path(src_txt)
        src_thumb = Path(entry.thumbnail_path) if entry.thumbnail_path else None

        dest_txt = dest_dir / src_txt.name
        dest_sidecar = sidecar_metadata_path(dest_txt)

        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_txt), str(dest_txt))

        if src_sidecar.exists():
            shutil.move(str(src_sidecar), str(dest_sidecar))

        if src_thumb and Path(src_thumb).exists():
            # サムネ側も同じくライブラリ（サムネイルルート）配下に制限する
            if clean_folder:
                thumb_dest_dir = _safe_resolve_path(thumbnail_root / Path(*parts))
            else:
                thumb_dest_dir = thumbnail_root
            if thumb_dest_dir != thumbnail_root and thumbnail_root not in thumb_dest_dir.parents:
                raise ValueError("サムネイル移動先がルート外です。")
            thumb_dest_dir.mkdir(parents=True, exist_ok=True)
            thumb_dest = thumb_dest_dir / Path(src_thumb).name
            shutil.move(str(src_thumb), str(thumb_dest))

        self.delete_entry(entry.abs_path)
        return self.refresh_entry(settings, dest_txt)

    def delete_folder(self, settings: AppSettings, folder_rel_path: str) -> dict[str, int]:
        folder_rel_path = folder_rel_path.strip().replace("\\", "/").strip("/")
        if not folder_rel_path:
            raise ValueError("ルートフォルダ全体は削除できません。")

        # 【WinError 6 対策】_safe_resolve_path でラップ
        library_root = _safe_resolve_path(Path(settings.library_root))
        thumbnail_root = _safe_resolve_path(Path(settings.thumbnail_root))
        target_folder = _safe_resolve_path(library_root / Path(folder_rel_path))
        target_thumbnails = _safe_resolve_path(thumbnail_root / Path(folder_rel_path))

        if library_root not in target_folder.parents:
            raise ValueError("削除対象がライブラリ外です。")
        if not target_folder.exists():
            raise FileNotFoundError(f"フォルダが見つかりません: {target_folder}")

        wildcard_count = sum(1 for _ in target_folder.rglob("*.txt"))
        thumbnail_count = sum(1 for _ in target_thumbnails.rglob("*")) if target_thumbnails.exists() else 0

        shutil.rmtree(target_folder)
        if target_thumbnails.exists():
            shutil.rmtree(target_thumbnails)

        return {"wildcards": wildcard_count, "thumbnails": thumbnail_count}

    def import_from_sources(self, settings: AppSettings, mode: str = "copy", conflict_policy: str = "overwrite", progress=None) -> dict[str, int]:
        source_root = Path(settings.source_wildcard_root)
        library_root = Path(settings.library_root)
        thumbnail_root = Path(settings.thumbnail_root)
        library_root.mkdir(parents=True, exist_ok=True)
        thumbnail_root.mkdir(parents=True, exist_ok=True)

        if not source_root.exists():
            raise FileNotFoundError(f"source wildcard root が見つかりません: {source_root}")

        txt_files = sorted(source_root.rglob("*.txt"))
        imported = 0
        thumbs = 0
        skipped = 0
        inspection = self.inspect_source_thumbnail_roots(settings)
        thumbnail_roots = inspection["existing_roots"]
        for index, txt_path in enumerate(txt_files, start=1):
            rel_path = txt_path.relative_to(source_root)
            item_stats = self.import_single_from_sources(
                settings,
                rel_path,
                mode=mode,
                conflict_policy=conflict_policy,
                thumbnail_roots=thumbnail_roots,
            )
            imported += item_stats["imported"]
            thumbs += item_stats["thumbnails"]
            skipped += item_stats["skipped"]

            if progress:
                if progress(index, len(txt_files), rel_path.as_posix()) is False:
                    raise OperationCancelledError()

        return {
            "imported": imported,
            "thumbnails": thumbs,
            "skipped": skipped,
            "thumbnail_source_roots": len(thumbnail_roots),
            "thumbnail_source_display": " / ".join(str(path) for path in thumbnail_roots[:3]),
        }

    def import_single_from_sources(
        self,
        settings: AppSettings,
        rel_path: Path,
        mode: str = "copy",
        conflict_policy: str = "overwrite",
        thumbnail_roots: list[Path] | None = None,
    ) -> dict[str, int]:
        source_root = Path(settings.source_wildcard_root)
        library_root = Path(settings.library_root)
        thumbnail_root = Path(settings.thumbnail_root)
        txt_path = source_root / rel_path
        if not txt_path.exists():
            raise FileNotFoundError(f"元 wildcard が見つかりません: {txt_path}")

        if thumbnail_roots is None:
            thumbnail_roots = self.inspect_source_thumbnail_roots(settings)["existing_roots"]

        destination = library_root / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        imported = 1 if self._transfer_file(txt_path, destination, mode, conflict_policy) else 0
        skipped = 0 if imported else 1
        thumbs = 0

        thumb_src = self.resolve_thumbnail_from_roots(thumbnail_roots, rel_path)
        if thumb_src:
            thumb_dst = ensure_thumbnail_destination(thumbnail_root, rel_path.as_posix())
            thumb_dst.parent.mkdir(parents=True, exist_ok=True)
            thumbs = 1 if self._transfer_file(thumb_src, thumb_dst, mode, conflict_policy) else 0

        return {"imported": imported, "thumbnails": thumbs, "skipped": skipped}

    def _transfer_file(self, source: Path, destination: Path, mode: str, conflict_policy: str = "overwrite") -> bool:
        """ファイルを copy/move する。

        H7 修正: 以前は ``unlink`` → ``shutil.move/copy2`` の順で、
        move/copy が失敗するとデータロストになっていた。
        Python 3.8+ の ``shutil.move(src, dst)`` は dst が存在していても
        上書き可能なので unlink を省略し、copy2 も同様に直接上書きする。
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 【WinError 6 対策】_safe_resolve でラップ
        if _safe_resolve(source) == _safe_resolve(destination):
            return False
        if destination.exists() and not self._should_replace(source, destination, conflict_policy):
            return False
        # destination.exists() の場合は shutil 側で上書きされる。
        # 失敗時は例外が上がり、source は失われない。
        if mode == "move":
            shutil.move(str(source), str(destination))
        else:
            # copy2 は dst が既存でも上書きする
            shutil.copy2(str(source), str(destination))
        return True

    def _should_replace(self, source: Path, destination: Path, conflict_policy: str) -> bool:
        if conflict_policy == "overwrite":
            return True
        if conflict_policy == "keep_existing":
            return False
        if conflict_policy == "keep_newer":
            return source.stat().st_mtime >= destination.stat().st_mtime
        if conflict_policy == "keep_larger":
            # M5 修正: 同サイズのときは既存を残す（>= から > へ変更）。
            # 同サイズでの置換は mtime 更新→意図しない再スキャンを誘発するため。
            return source.stat().st_size > destination.stat().st_size
        return True

    def resolve_thumbnail_path(self, settings: AppSettings, txt_path: Path) -> Path | None:
        library_root = Path(settings.library_root)
        rel_path = txt_path.relative_to(library_root)
        thumb_root = Path(settings.thumbnail_root)
        for candidate in self._thumbnail_candidates(thumb_root, rel_path):
            if candidate.exists():
                return candidate
        return None

    def resolve_thumbnail_from_roots(self, thumbnail_roots: list[Path], rel_path: Path) -> Path | None:
        for thumbnail_root in thumbnail_roots:
            for candidate in self._thumbnail_candidates(thumbnail_root, rel_path):
                if candidate.exists():
                    return candidate
        return None

    def _source_thumbnail_roots(self, settings: AppSettings) -> list[Path]:
        raw_root = Path(settings.source_thumbnail_root)
        source_root = Path(settings.source_wildcard_root)
        candidates: list[Path] = [raw_root]

        raw_text = str(raw_root)
        if "extensions\\extensions\\" in raw_text:
            candidates.append(Path(raw_text.replace("extensions\\extensions\\", "extensions\\")))

        if source_root.parts:
            try:
                candidates.append(source_root.parents[1] / "wildcard-gallery" / "cards")
            except IndexError:
                pass
            try:
                candidates.append(source_root.parents[1] / "extensions" / "wildcard-gallery" / "cards")
            except IndexError:
                pass

        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    def inspect_source_thumbnail_roots(self, settings: AppSettings) -> dict[str, object]:
        candidates = self._source_thumbnail_roots(settings)
        existing = [path for path in candidates if path.exists()]
        return {
            "configured_root": Path(settings.source_thumbnail_root),
            "candidate_roots": candidates,
            "existing_roots": existing,
            "configured_exists": Path(settings.source_thumbnail_root).exists(),
        }

    def _thumbnail_candidates(self, thumbnail_root: Path, rel_path: Path) -> list[Path]:
        candidates = []
        for extension in THUMBNAIL_EXTENSIONS:
            candidates.append(thumbnail_root / rel_path.parent / f"{rel_path.stem}.preview{extension}")
            candidates.append(thumbnail_root / rel_path.parent / f"{rel_path.stem}{extension}")
        return candidates

    def _build_entry(self, settings: AppSettings, txt_path: Path, resolved_thumb: Path | None = None) -> WildcardEntry:
        content = self._read_text_file(txt_path)
        rel_path = txt_path.relative_to(Path(settings.library_root)).as_posix()
        folder = Path(rel_path).parent.as_posix()
        folder = "" if folder == "." else folder
        custom_tags = self._load_custom_tags(sidecar_metadata_path(txt_path))
        lora_names = extract_lora_names(content)
        thumb_path = resolved_thumb if resolved_thumb is not None else self.resolve_thumbnail_path(settings, txt_path)
        lines = [line for line in content.splitlines() if line.strip()]
        # 【方針A・WinError 6 対策】モジュールレベルの _safe_resolve を使用。
        # _build_entry と _portable_abs_path で同じ正規化ロジックを使うことで
        # abs_path の表記ゆれを防ぐ。ただし方針B（rel_path をキーに使用）により、
        # 仮に表記ゆれが残ってもキャッシュミスは起きない。
        return WildcardEntry(
            rel_path=rel_path,
            abs_path=_safe_resolve(txt_path),
            folder=folder,
            name=txt_path.name,
            stem=txt_path.stem,
            content=content,
            first_line=lines[0] if lines else "",
            line_count=len(lines),
            custom_tags=custom_tags,
            lora_names=lora_names,
            thumbnail_path=_safe_resolve(thumb_path) if thumb_path else None,
            has_thumbnail=bool(thumb_path),
            search_text=" ".join(
                [
                    rel_path.lower(),
                    content.lower(),
                    " ".join(custom_tags).lower(),
                    " ".join(lora_names).lower(),
                ]
            ),
        )

    def _upsert(self, con: sqlite3.Connection, entry: WildcardEntry, file_mtime: float, sidecar_mtime: float, thumb_mtime: float) -> None:
        """エントリを upsert する。

        H3 修正: ``rel_path`` に UNIQUE 制約を導入したため、両軸の conflict
        を扱う必要がある。``abs_path`` で conflict した場合は通常更新。
        ``rel_path`` で conflict した場合は（移動元の古い abs_path 行が残っている
        状況）、古い行を削除してから新しく挿入し直す。
        """
        try:
            con.execute(
                """
                INSERT INTO entries (
                    abs_path, rel_path, folder, name, stem, content, search_text, first_line, line_count,
                    custom_tags_json, lora_names_json, thumbnail_path, has_thumbnail,
                    file_mtime, sidecar_mtime, thumb_mtime
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(abs_path) DO UPDATE SET
                    rel_path=excluded.rel_path,
                    folder=excluded.folder,
                    name=excluded.name,
                    stem=excluded.stem,
                    content=excluded.content,
                    search_text=excluded.search_text,
                    first_line=excluded.first_line,
                    line_count=excluded.line_count,
                    custom_tags_json=excluded.custom_tags_json,
                    lora_names_json=excluded.lora_names_json,
                    thumbnail_path=excluded.thumbnail_path,
                    has_thumbnail=excluded.has_thumbnail,
                    file_mtime=excluded.file_mtime,
                    sidecar_mtime=excluded.sidecar_mtime,
                    thumb_mtime=excluded.thumb_mtime
                """,
                (
                    entry.abs_path,
                    entry.rel_path,
                    entry.folder,
                    entry.name,
                    entry.stem,
                    entry.content,
                    entry.search_text,
                    entry.first_line,
                    entry.line_count,
                    json.dumps(entry.custom_tags, ensure_ascii=False),
                    json.dumps(entry.lora_names, ensure_ascii=False),
                    entry.thumbnail_path,
                    1 if entry.has_thumbnail else 0,
                    file_mtime,
                    sidecar_mtime,
                    thumb_mtime,
                ),
            )
        except sqlite3.IntegrityError:
            # rel_path 側で conflict = 別 abs_path の古い行が残っている。
            # これを削除してから再挿入する。
            con.execute("DELETE FROM entries WHERE rel_path = ?", (entry.rel_path,))
            con.execute(
                """
                INSERT INTO entries (
                    abs_path, rel_path, folder, name, stem, content, search_text, first_line, line_count,
                    custom_tags_json, lora_names_json, thumbnail_path, has_thumbnail,
                    file_mtime, sidecar_mtime, thumb_mtime
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.abs_path,
                    entry.rel_path,
                    entry.folder,
                    entry.name,
                    entry.stem,
                    entry.content,
                    entry.search_text,
                    entry.first_line,
                    entry.line_count,
                    json.dumps(entry.custom_tags, ensure_ascii=False),
                    json.dumps(entry.lora_names, ensure_ascii=False),
                    entry.thumbnail_path,
                    1 if entry.has_thumbnail else 0,
                    file_mtime,
                    sidecar_mtime,
                    thumb_mtime,
                ),
            )

    def _portable_abs_path(self, settings: AppSettings, rel_path: str, fallback: str) -> str:
        candidate = Path(settings.library_root) / Path(rel_path)
        # 【方針A・WinError 6 対策】モジュールレベルの _safe_resolve を使用して
        # _build_entry と同じ正規化ロジックで abs_path を生成する。
        # これにより両者の表記ゆれを解消。仮にゆれが残っても方針B（rel_path を
        # キーに使用）によりキャッシュミスは起きない。
        return _safe_resolve(candidate)

    def _portable_thumbnail_path(self, settings: AppSettings, rel_path: str, stored_path: str | None) -> str | None:
        if not stored_path:
            return None
        rel = Path(rel_path)
        return str(Path(settings.thumbnail_root) / rel.parent / Path(stored_path).name)

    def _row_to_entry(self, row: sqlite3.Row, settings: AppSettings | None = None) -> WildcardEntry:
        abs_path = row["abs_path"]
        thumbnail_path = row["thumbnail_path"]
        if settings is not None:
            abs_path = self._portable_abs_path(settings, row["rel_path"], abs_path)
            thumbnail_path = self._portable_thumbnail_path(settings, row["rel_path"], thumbnail_path)
        return WildcardEntry(
            rel_path=row["rel_path"],
            abs_path=abs_path,
            folder=row["folder"],
            name=row["name"],
            stem=row["stem"],
            content=row["content"],
            first_line=row["first_line"],
            line_count=row["line_count"],
            custom_tags=json.loads(row["custom_tags_json"] or "[]"),
            lora_names=json.loads(row["lora_names_json"] or "[]"),
            thumbnail_path=thumbnail_path,
            has_thumbnail=bool(thumbnail_path),
            search_text=row["search_text"] or "",
        )

    def _row_to_entry_summary(self, row: sqlite3.Row, settings: AppSettings | None = None) -> WildcardEntry:
        abs_path = row["abs_path"]
        thumbnail_path = row["thumbnail_path"]
        if settings is not None:
            abs_path = self._portable_abs_path(settings, row["rel_path"], abs_path)
            thumbnail_path = self._portable_thumbnail_path(settings, row["rel_path"], thumbnail_path)
        return WildcardEntry(
            rel_path=row["rel_path"],
            abs_path=abs_path,
            folder=row["folder"],
            name=row["name"],
            stem=row["stem"],
            first_line=row["first_line"],
            line_count=row["line_count"],
            custom_tags=json.loads(row["custom_tags_json"] or "[]"),
            lora_names=json.loads(row["lora_names_json"] or "[]"),
            thumbnail_path=thumbnail_path,
            has_thumbnail=bool(thumbnail_path),
            search_text=row["search_text"] or "",
        )

    def _read_text_file(self, path: Path) -> str:
        for encoding in TEXT_ENCODINGS:
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        return path.read_text(encoding="utf-8", errors="replace")

    def _load_custom_tags(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tags = data.get("custom_tags", [])
            return sorted({str(tag).strip() for tag in tags if str(tag).strip()})
        except Exception:
            log.debug("Failed to load custom tags from %s", path, exc_info=True)
            return []

    def db_entry_count(self) -> int:
        with self._connect() as con:
            return con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def count_txt_files_on_disk(self, settings: AppSettings) -> int:
        library_root = Path(settings.library_root)
        if not library_root.exists():
            return 0
        return sum(1 for _, _, files in os.walk(library_root)
                   for f in files if f.lower().endswith(".txt"))
