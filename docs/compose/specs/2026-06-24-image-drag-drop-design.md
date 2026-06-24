# [S1] Problem

画像ファイルをドラッグ&ドロップしたとき、その画像からプロンプト情報を抽出し、新規ワイルドカード作成ダイアログに渡す機能がない。

現状のドラッグ&ドロップは `.cm-info.json` ファイルのみ対応。

## [S2] 対応フォーマットと抽出方法

| フォーマット | 抽出方法 | メタデータの場所 |
|-------------|---------|----------------|
| PNG | メタデータからプロンプト抽出 | tEXtチャンク（"parameters" キー） |
| WebP | メタデータからプロンプト抽出 | EXIF / XMP（sd-webui 出力次第） |
| JPG/JPEG | wd14 tagger で自動タグ付け | メタデータなし（プロンプト非対応） |

### PNG プロンプト抽出の詳細
- sd-webui が生成する PNG は `parameters` キーにプロンプトが格納されている
- 形式: `"positive_prompt\nNegative prompt: negative_prompt\nSteps: N, Sampler: X, CFG: Y, Size: WxH"`
- Pillow の `PngImagePlugin.PngInfo` で読み取り可能

### wd14 tagger の呼び出し
- スタンドアロン tagger サーバー（別途起動）を API 経由で呼び出し
- 画像を base64 エンコードして POST
- 戻り値: タグのリスト（信頼度付き）

## [S3] 動作フロー

```
1. ユーザーが画像ファイルをメインウィンドウにドロップ
2. ファイル形式を判定
3. PNG/WebP → メタデータからプロンプト抽出
   JPG → wd14 tagger API でタグ抽出
4. 新規ワイルドカード作成ダイアログを開く
   - プロンプト欄に抽出結果をプリフィル
   - ドロップした画像をサムネイルプレビューとして表示
5. ユーザーが内容を編集
6. 「作成」ボタン押下 → ワイルドカード保存 + サムネイル設定
```

## [S4] 対象ファイル

| ファイル | 変更内容 |
|---------|---------|
| `wildcard_manager/main_window.py` | `dragEnterEvent` / `dropEvent` の拡張。画像ファイル受信時の処理追加 |
| `wildcard_manager/new_wildcard_dialog.py` | プロンプトプリフィル機能、サムネイルプレビュー表示機能の追加 |
| `wildcard_manager/api.py` | wd14 tagger API クライアントの追加（スタンドアロンサーバー用） |
| `wildcard_manager/models.py` | `AppSettings` に tagger サーバー URL を追加（任意） |

## [S5] 設計詳細

### dragEnterEvent の拡張
```python
def dragEnterEvent(self, event) -> None:
    if event.mimeData().hasUrls():
        for url in event.mimeData().urls():
            filepath = url.toLocalFile()
            if filepath.lower().endswith(('.png', '.webp', '.jpg', '.jpeg')):
                event.acceptProposedAction()
                event.accept()
                return
            if filepath.endswith(".cm-info.json"):
                event.acceptProposedAction()
                event.accept()
                return
    event.ignore()
```

### dropEvent の拡張
```python
def dropEvent(self, event) -> None:
    if event.mimeData().hasUrls():
        for url in event.mimeData().urls():
            filepath = url.toLocalFile()
            if filepath.lower().endswith(('.png', '.webp', '.jpg', '.jpeg')):
                self._handle_image_drop(filepath)
            elif filepath.endswith(".cm-info.json"):
                self._import_cm_info_json(filepath)
        event.accept()
    else:
        event.ignore()
```

### 画像ドロップ処理
```python
def _handle_image_drop(self, filepath: str) -> None:
    """画像ファイルをドロップしたときの処理"""
    ext = Path(filepath).suffix.lower()
    
    if ext in ('.png', '.webp'):
        prompt_data = self._extract_prompt_from_metadata(filepath)
    elif ext in ('.jpg', '.jpeg'):
        prompt_data = self._extract_tags_from_tagger(filepath)
    else:
        return
    
    # 新規作成ダイアログを開く
    dialog = NewWildcardDialog(self.settings, self.repo, self.api, self.folder_children_map)
    dialog.set_dropped_image(filepath, prompt_data)
    dialog.exec()
```

### プロンプト抽出（PNG/WebP）
```python
def _extract_prompt_from_metadata(self, filepath: str) -> dict:
    """PNG/WebP のメタデータからプロンプトを抽出"""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    
    img = Image.open(filepath)
    info = img.info or {}
    
    # PNG の場合
    if hasattr(img, 'text'):
        params = img.text.get('parameters', '')
    else:
        params = info.get('parameters', '')
    
    if params:
        return self._parse_sdwebui_parameters(params)
    return {"prompt": "", "negative_prompt": "", "settings": {}}
```

### wd14 tagger 呼び出し
```python
def _extract_tags_from_tagger(self, filepath: str) -> dict:
    """wd14 tagger で画像からタグを抽出"""
    import base64
    
    with open(filepath, 'rb') as f:
        image_data = base64.b64encode(f.read()).decode()
    
    response = requests.post(
        f"{self.settings.tagger_url}/tag",
        json={"image": image_data},
        timeout=30
    )
    response.raise_for_status()
    tags = response.json().get("tags", [])
    
    # タグをプロンプト形式に変換
    prompt = ", ".join(tag["name"] for tag in tags if tag["confidence"] > 0.5)
    return {"prompt": prompt, "negative_prompt": "", "settings": {}}
```

### NewWildcardDialog への画像渡し
```python
# new_wildcard_dialog.py に追加
def set_dropped_image(self, filepath: str, prompt_data: dict) -> None:
    """ドロップされた画像とプロンプトデータを設定"""
    self._dropped_image_path = filepath
    self._prompt_data = prompt_data
    
    # キャラクタータブのプロンプト欄にプリフィル
    if prompt_data.get("prompt"):
        self._costume_prompt_edit.setPlainText(prompt_data["prompt"])
    
    # サムネイルプレビューを表示
    self._preview_thumbnail(filepath)
```

## [S6] wd14 tagger サーバー

スタンドアロン tagger は別途用意が必要。

### 最小構成
- Python + transformers + Pillow
- FastAPI / Flask で API エンドポイントを公開
- `POST /tag` で画像を受け取り、タグリストを返す

### API スペック（想定）
```
POST /tag
Request:  { "image": "<base64>", "threshold": 0.5 }
Response: { "tags": [{"name": "1girl", "confidence": 0.95}, ...] }
```

## [S7] 既知の制限

- PNG のメタデータ形式は sd-webui の出力に依存。他のツールが出力した PNG は対応外の可能性
- WebP のメタデータ対応は sd-webui の設定次第
- wd14 tagger は別途サーバー起動が必要
- JPG はプロンプトを持たないため、タガー必須

## [S8] テスト方針

- PNG メタデータ抽出のユニットテスト（sd-webui 出力の PNG テストデータ作成）
- wd14 tagger API のモックテスト
- ドロップ→ダイアログ表示の統合テスト
