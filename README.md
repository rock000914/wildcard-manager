# Wildcard Manager

Stable Diffusion のワイルドカードを管理するデスクトップアプリです。wildcard の整理・検索・コピー・サムネ生成を One GUI で完結させます。

## できること

### 基本操作
- `txt` ワイルドカードの読み込み・編集・保存
- フォルダツリーによる絞り込み
- ファイル名・本文・カスタムタグ・LoRA 名での検索
- LoRA タグを含む / 外してのコピー切替
- `sd-dynamic-prompts` / `wildcard-gallery` からのライブラリへのコピー・移動
- カスタムタグによる分類（各 txt と同名の `.wcm.json` に保存）

### サムネイル
- ワイルドカード一覧にサムネイル表示
- StabilityMatrix / Forge 系 `sdapi` でサムネイル生成 → `.webp` 保存
- 不足サムネイルの一括生成（バックグラウンドスレッド対応）
- 生成時にランダム wildcard を組み合わせ可能

### 新規ワイルドカード作成
- キャラクタータブ + 動的カスタムタブ（「+」ボタンで追加）
- 衣装ごとのプロンプトをまとめて作成
- ダイアログ内でサムネイルプレビュー生成

### API 連携
- sd-webui API 接続テスト（JSON 応答検証付き）
- チェックポイント一覧取得
- 生成状態のリアルタイム監視（API 状態インジケーター表示）
- ADetailer 対応

## 保存構造

```
library/
├── wildcards/          # wildcard 本体（txt ファイル）
│   ├── Position/
│   │   ├── character1.txt
│   │   └── character1.txt.wcm.json  # カスタムタグ
│   └── ...
├── thumbnails/         # サムネイル（webp ファイル）
│   ├── Position/
│   │   └── character1.preview.webp
│   └── ...
wildcard_manager.db     # SQLite インデックス（高速化用）
config.json             # 設定
ui_state.json           # UI 状態（ソート・分割バー位置等）
```

## セットアップ

### 必要なもの
- Python 3.10+
- PySide6
- Pillow
- requests

### インストール

```powershell
pip install PySide6 Pillow requests
```

### 起動

```powershell
python app.py
```

## 初回設定

1. `設定` タブで以下を確認・入力:
   - **管理ルート**: wildcard の保存先（デフォルト: `library/wildcards`）
   - **サムネイルルート**: サムネイルの保存先（デフォルト: `library/thumbnails`）
   - **API ベース URL**: sd-webui の API（デフォルト: `http://127.0.0.1:7860`）
   - **移行元パス**: 既存の `sd-dynamic-prompts` や `wildcard-gallery` のパス
2. `再読込` でライブラリをスキャン

## ファイル構成

| ファイル | 役割 |
|---------|------|
| `app.py` | エントリーポイント |
| `wildcard_manager/main_window.py` | メインウィンドウ |
| `wildcard_manager/api.py` | sd-webui API クライアント |
| `wildcard_manager/repository.py` | SQLite + ファイルシステム操作 |
| `wildcard_manager/models.py` | データモデル（AppSettings, WildcardEntry） |
| `wildcard_manager/config.py` | 設定の読み書き |
| `wildcard_manager/thumbnail_model.py` | サムネイル一覧のモデルとデリゲート |
| `wildcard_manager/thumbnail_workers.py` | サムネイル読込ワーカー |
| `wildcard_manager/tag_editor.py` | タグエディットウィジェット |
| `wildcard_manager/new_wildcard_dialog.py` | 新規ワイルドカード作成ダイアログ |
| `wildcard_manager/single_thumbnail_worker.py` | 個別サムネイル生成ワーカー |
| `wildcard_manager/ui_utils.py` | ファイルマネージャー操作・コンテキストメニュー共通化 |

## 更新履歴

### v2.0 (2026-06-24) - 大規模改善リリース

外部レビューに基づくバグ修正・パフォーマンス改善・アーキテクチャ刷新。

#### バグ修正
- **コンテキストメニューの deleteLater バグ**: 右クリック→削除 で入力フィールド自体が消える重大バグを修正。`ui_utils.py` に委譲
- **数値パースのクラッシュ防止**: `int()` / `float()` の直接呼び出しを `_parse_int` / `_parse_float` ヘルパに置き換え。不正入力でも `ValueError` にならない
- **API 応答の誤検出防止**: プロキシや認証ページの HTML 応答（200 OK）を成功扱いしないよう、Content-Type が JSON かを追加検証
- **ファイル上書き時のデータロスト防止**: `unlink` → `move` の順序を廃止。`shutil.move` / `copy2` が直接上書きするように変更
- **パストラバーサル防止**: `move_entry` / `delete_folder` で `resolve()` 後に `library_root` 配下か検証。`../../etc` でライブラリ外へ脱出する問題を解消
- **LoRA タグ除去の正規化**: `.replace("  ", " ")` → `re.sub(r"\s+", " ", ...)` に変更。3連続スペースを正しく1つに潰す
- **LoRA 順序の保持**: `set` + `sorted` → `dict.fromkeys` で実出現順を保持
- **API 監視の未知 state 対応**: `_LAMP_COLORS.get(state, ...)` でフォールバック。未知の状態でも `KeyError` にならない
- **PathLabel の初期化保証**: 防衛的 `getattr` を削除。`_build_ui` で確実に初期化されるため直接アクセスに変更
- **dead code 削除**: `_pending_preview_request` / `_pending_content_abs_path` は書き込まれるだけで読み出されない変数だったため削除
- **Painter save/restore の整合**: `restore()` 呼び出し前に `save()` を追加。Qt の警告を防止
- **サムネイル反映の即時化**: 新規作成ダイアログで生成したサムネイルを `refresh_entry` で即座に DB に反映
- **サムネイルの重複生成防止**: 事前生成済みサムネイルをコピーする前に `dest.exists()` チェックを追加

#### パフォーマンス改善
- **サムネイル存在確認の O(1) 化**: 1ファイルずつの `exists()` → 事前ビルドした `set` でルックアップ。5,000 ファイルのライブラリで劇的改善
- **scan_library の単一パス化**: `rglob` + `sorted` → `os.walk` で1回の走査に。メモリ使用量も削減
- **SQLite PRAGMA の永続化**: `journal_mode=WAL` を毎回発行していたのを初回のみに。接続ごとのオーバーヘッド削減
- **PRAGMA 追加**: `synchronous=NORMAL` / `temp_store=MEMORY` / `cache_size=-2000` で書き込み速度とキャッシュ効率改善
- **dir_mtimes.json のデッドコード削除**: スキップ判定が `pass` だったため実質無効。毎回の二重ディレクトリ走査が不要に
- **不足サムネイル一括生成の QThread 化**: `processEvents()` ループを廃止。UI フリーズが解消
- **API 監視周期の延長**: 3秒 → 5秒。アイドル時の通信負荷軽減
- **コンテンツキャッシュの LRU 化**: `dict` → `OrderedDict`（上限 500 エントリ）。大規模ライブラリでのメモリ膨張を抑制
- **load_entries の最適化**: `SELECT *` → 必要カラムのみ。Python 側ソート → SQL で `GROUP BY` + `ORDER BY`
- **scan_profile.log のサイズ制限**: 1 MiB 超過時に古いエントリを切り詰め。ログの無限膨張を防止

#### アーキテクチャ改善
- **モジュール分離**: `main_window.py`（2,469行）から以下を独立モジュールに分離
  - `thumbnail_model.py`（422行）: サムネイル一覧のモデルとデリゲート
  - `thumbnail_workers.py`（151行）: サムネイル読込ワーカー
  - `tag_editor.py`（405行）: タグエディットウィジェット
  - `new_wildcard_dialog.py`（1,828行）: 新規ワイルドカード作成ダイアログ
  - `ui_utils.py`（116行）: ファイルマネージャー操作・コンテキストメニュー共通化
- **rel_path の UNIQUE 制約**: 重複行の自動解消。移行時に古い行を削除してから再作成
- **DB エントリ数の確認メソッド追加**: `db_entry_count()` / `count_txt_files_on_disk()` で整合性チェックが可能に

#### 機能追加
- **ランダム wildcard 組み合わせ**: サムネイル生成時に指定フォルダからランダムに wildcard を1つ選んでプロンプトに追加
- **ADetailer 対応**: `generation_adetailer_enabled` が True のとき `alwayson_scripts` に ADetailer を追加
- **API チェックポイント一覧取得**: `list_checkpoints()` で sd-webui のモデル一覧を取得
- **API 生成状態監視**: `check_progress()` / `is_generating()` でリアルタイムに進捗を確認
- **不足サムネイルのバッチ生成**: `MissingThumbnailBatchWorker` でバックグラウンド逐次生成。進捗表示・キャンセル対応
- **新規ワイルドカードダイアログの動的タブ**: 「+」ボタンでカスタムタブを追加可能。ダブルクリックでリネーム、右クリックで削除
- **設定の永続化拡張**: `sort_key` / `sort_order` を `ui_state.json` に保存
- **ハードコードパスの除去**: 開発者個人の `H:\StabilityMatrix\...` をデフォルトから削除。初回起動時にユーザーが設定

#### セキュリティ
- **パス検証の強化**: `move_entry` / `delete_folder` で `resolve()` 後に親ディレクトリ参照を検証
- **ファイル上書きの安全化**: `unlink` を廃止し、`shutil.move` / `copy2` の直接上書きに変更。失敗時は例外が上がり、source は失われない

---

### v1.x - 初期リリース

#### 機能
- wildcard の読み込み・編集・保存
- フォルダツリーでの絞り込み
- 検索（ファイル名・本文・カスタムタグ・LoRA 名）
- サムネイル表示・生成
- `sd-dynamic-prompts` / `wildcard-gallery` からのコピー・ムーブ
- カスタムタグ（`.wcm.json`）

#### 既知の問題（v2 で修正済み）
- コンテキストメニューの「削除」が入力フィールド自体を消す
- 不正な数値入力で `ValueError` クラッシュ
- API 接続テストでプロキシの HTML を成功扱い
- ファイル上書き時に `unlink` → `move` の順序でデータロスト风险
- パストラバーサルでライブラリ外へ脱出可能
- サムネイルの存在確認が遅い（大規模ライブラリ）
- 不足サムネイルの一括生成で UI フリーズ

---

## 今後の予定

- [ ] テストの実装
- [ ] `QAbstractTableModel + QTableView` への移行（大規模一覧のさらなる高速化）
- [ ] ドラッグ&ドロップ対応
- [ ] wildcard のバッチリネーム
- [ ] 複数 API エンドポイントの同時監視
- [ ] サムネイルの自動生成ルール（条件付き）
- [ ] ダーク/ライトテーマ切り替え
- [ ] エクスポート機能（CSV / JSON）

## 既知の制限

- GUI は PySide6 のまま。将来さらに重い一覧表示が必要なら `QAbstractTableModel + QTableView` への移行を検討
- テストは未実装（動作未確認の箇所あり）

## ライセンス

未定
