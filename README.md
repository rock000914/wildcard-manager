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

## 主な改善点（v2）

### バグ修正
- コンテキストメニューの「削除」が入力フィールド自体を消すバグを修正
- 数値入力が不正な場合の `ValueError` クラッシュを防止
- API 接続テストでプロキシの HTML 応答を誤検出しないよう検証強化
- ファイル上書き時のデータロスト风险を排除（`unlink` → `move` 順序の修正）
- パストラバーサルによるライブラリ外への脱出を防止

### パフォーマンス
- サムネイルの存在確認を `exists()` 単発呼び出し → 事前ビルド `set` の O(1) ルックアップに変更
- `scan_library` を `rglob` + `sorted` → `os.walk` 単一パスに変更
- SQLite の `PRAGMA` を永続化（毎回発行しない）
- `dir_mtimes.json` のデッドコードを削除
- 不足サムネイルの一括生成を `QThread` バックグラウンド化（UI フリーズ解消）
- API 監視周期を 3秒 → 5秒に延長

### アーキテクチャ
- `main_window.py` からサムネイル関連を独立モジュールに分離
- ファイルマネージャー操作・右クリックメニューを `ui_utils.py` に共通化
- `rel_path` に UNIQUE 制約を導入し、重複行を自動解消

## 既知の制限

- GUI は PySide6 のまま。将来さらに重い一覧表示が必要なら `QAbstractTableModel + QTableView` への移行を検討
- テストは未実装（動作未確認の箇所あり）

## ライセンス

未定
