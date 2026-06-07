# Wildcard Manager

PySide6 で作った、Stable Diffusion に最適な wildcard 管理ツールです。wildcard の整理、検索、コピー、サムネ生成までひとつにまとめて、相性のいい運用を快適にします。

## できること

- `txt` wildcard の読み込み、編集、保存
- フォルダツリーでの絞り込み
- 検索
  - ファイル名
  - 本文
  - カスタムタグ
  - LoRA 名
- 右クリックや LoRA 一覧から、同じ LoRA を使う wildcard を絞り込み
- コピー時に LoRA タグを含める / 外すを切り替え
- 既存の `sd-dynamic-prompts` / `wildcard-gallery` から独立ライブラリへコピーまたは移動
- サムネイル表示
- サムネイルがない wildcard に対して StabilityMatrix / Forge 系 `sdapi` で生成して `.webp` 保存
- wildcard 本文とは別に、検索用のカスタムタグを保存

## 保存の考え方

- wildcard 本体は `library/wildcards`
- thumbnail は `library/thumbnails`
- カスタムタグは各 `txt` の横に `*.txt.wcm.json`
- 高速化用インデックスは `wildcard_manager.db`

`*.txt.wcm.json` を使っているので、別 PC へ持っていっても検索タグを一緒に移しやすい構成です。

## 起動

```powershell
python app.py
```

## 最初にやること

1. `設定` で管理ルート、移行元ルート、API URL を確認
2. `移行コピー` または `移行ムーブ`
3. `再読込`

## メモ

- GUI は PySide6 のままで問題ありません。今回の用途だと、重要なのは GUI ライブラリ変更より `SQLite インデックス + 遅延読込` です。
- もし将来的にさらに重い一覧表示へ寄せるなら、次の改善候補は `QTableWidget` から `QAbstractTableModel + QTableView` への移行です。
