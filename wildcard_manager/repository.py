from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

from .models import AppSettings, WildcardEntry

LORA_PATTERN = re.compile(r"<lora:([^:>]+)(?::[^>]+)?>", re.IGNORECASE)
THUMBNAIL_EXTENSIONS = (".webp", ".png", ".jpg", ".jpeg")
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp932")
_NATURAL_SPLIT = re.compile(r"(\d+)")


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
    return re.sub(r"\s*<lora:[^>]+>\s*", " ", text).strip().replace("  ", " ")


def extract_lora_names(text: str) -> list[str]:
    names = {match.group(1).strip() for match in LORA_PATTERN.finditer(text)}
    return sorted(name for name in names if name)


def ensure_thumbnail_destination(thumbnail_root: Path, rel_path: str) -> Path:
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
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as con:
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

    def scan_library(self, settings: AppSettings, progress=None) -> dict[str, int]:
        library_root = Path(settings.library_root)
        thumbnail_root = Path(settings.thumbnail_root)
        library_root.mkdir(parents=True, exist_ok=True)
        thumbnail_root.mkdir(parents=True, exist_ok=True)

        txt_files = sorted(library_root.rglob("*.txt"))
        known_paths: set[str] = set()
        scanned = 0
        updated = 0

        with self._connect() as con:
            current_rows = {
                row["abs_path"]: row
                for row in con.execute("SELECT * FROM entries")
            }
            for index, txt_path in enumerate(txt_files, start=1):
                abs_path = str(txt_path.resolve())
                known_paths.add(abs_path)
                row = current_rows.get(abs_path)
                sidecar_path = sidecar_metadata_path(txt_path)
                thumb_path = self.resolve_thumbnail_path(settings, txt_path)
                file_mtime = txt_path.stat().st_mtime
                sidecar_mtime = sidecar_path.stat().st_mtime if sidecar_path.exists() else 0.0
                thumb_mtime = thumb_path.stat().st_mtime if thumb_path else 0.0

                unchanged = (
                    row
                    and row["file_mtime"] == file_mtime
                    and row["sidecar_mtime"] == sidecar_mtime
                    and row["thumb_mtime"] == thumb_mtime
                    and bool(row["search_text"])
                )
                if unchanged:
                    scanned += 1
                    if progress:
                        if progress(index, len(txt_files), txt_path.name) is False:
                            raise OperationCancelledError()
                    continue

                entry = self._build_entry(settings, txt_path)
                self._upsert(con, entry, file_mtime, sidecar_mtime, thumb_mtime)
                scanned += 1
                updated += 1
                if progress:
                    if progress(index, len(txt_files), txt_path.name) is False:
                        raise OperationCancelledError()

            stale_paths = set(current_rows) - known_paths
            if stale_paths:
                con.executemany("DELETE FROM entries WHERE abs_path = ?", [(path,) for path in stale_paths])

        return {"scanned": scanned, "updated": updated, "deleted": len(set(current_rows) - known_paths)}

    def load_entries(self, settings: AppSettings | None = None) -> list[WildcardEntry]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM entries ORDER BY rel_path COLLATE NOCASE").fetchall()
        entries = [self._row_to_entry(row, settings) for row in rows]
        entries.sort(key=lambda entry: natural_sort_key(entry.rel_path))
        return entries

    def load_entries_summary(self, settings: AppSettings | None = None) -> list[WildcardEntry]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT abs_path, rel_path, folder, name, stem, first_line, line_count,
                       custom_tags_json, lora_names_json, thumbnail_path, has_thumbnail, search_text
                FROM entries ORDER BY rel_path COLLATE NOCASE
                """
            ).fetchall()
        entries = [self._row_to_entry_summary(row, settings) for row in rows]
        entries.sort(key=lambda entry: natural_sort_key(entry.rel_path))
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
        entry = self._build_entry(settings, txt_path)
        with self._connect() as con:
            self._upsert(con, entry, file_mtime, sidecar_mtime, thumb_mtime)
        return entry

    def delete_folder(self, settings: AppSettings, folder_rel_path: str) -> dict[str, int]:
        folder_rel_path = folder_rel_path.strip().replace("\\", "/").strip("/")
        if not folder_rel_path:
            raise ValueError("ルートフォルダ全体は削除できません。")

        library_root = Path(settings.library_root).resolve()
        thumbnail_root = Path(settings.thumbnail_root).resolve()
        target_folder = (library_root / Path(folder_rel_path)).resolve()
        target_thumbnails = (thumbnail_root / Path(folder_rel_path)).resolve()

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
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == destination.resolve():
            return False
        if destination.exists() and not self._should_replace(source, destination, conflict_policy):
            return False
        if destination.exists():
            destination.unlink()
        if mode == "move":
            shutil.move(str(source), str(destination))
        else:
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
            return source.stat().st_size >= destination.stat().st_size
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

    def _build_entry(self, settings: AppSettings, txt_path: Path) -> WildcardEntry:
        content = self._read_text_file(txt_path)
        rel_path = txt_path.relative_to(Path(settings.library_root)).as_posix()
        folder = Path(rel_path).parent.as_posix()
        folder = "" if folder == "." else folder
        custom_tags = self._load_custom_tags(sidecar_metadata_path(txt_path))
        lora_names = extract_lora_names(content)
        thumb_path = self.resolve_thumbnail_path(settings, txt_path)
        lines = [line for line in content.splitlines() if line.strip()]
        return WildcardEntry(
            rel_path=rel_path,
            abs_path=str(txt_path.resolve()),
            folder=folder,
            name=txt_path.name,
            stem=txt_path.stem,
            content=content,
            first_line=lines[0] if lines else "",
            line_count=len(lines),
            custom_tags=custom_tags,
            lora_names=lora_names,
            thumbnail_path=str(thumb_path.resolve()) if thumb_path else None,
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

    def _portable_abs_path(self, settings: AppSettings, rel_path: str, fallback: str) -> str:
        candidate = Path(settings.library_root) / Path(rel_path)
        return str(candidate)

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
            return []
