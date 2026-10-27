# Sticky-Notion

常に最前面に表示される小さなウィンドウから、Notion のデータベースへ新規ページを素早く作成する Windows 用アプリです。
思いついたタスクやメモを、作業を止めずにすぐ Notion に投げ込めます。

A tiny always-on-top Windows app for capturing quick notes and tasks into a Notion database.

## 主な機能

- **すぐ入力**: ホットキー（既定 `Ctrl+Alt+N`）で呼び出し → タイトルと本文を入力 → `Ctrl+Enter` で作成
- **最前面表示**（📌で切替）・タスクトレイ常駐・ウィンドウ位置／サイズ／透明度の記憶
- **投稿先 DB の選択**: 複数 DB を登録し、`Ctrl+1`〜`Ctrl+9` で切替
- **プロパティ入力**: ステータス・セレクト・マルチセレクト・日付（今日／明日ボタン）・チェックボックス
- **簡易 Markdown**: `#` 見出し、`-` 箇条書き、`1.` 番号付き、`[]` / `[x]` ToDo、`>` 引用
- **タイトル省略可**: タイトルが空なら本文 1 行目をタイトルにする
- **オフライン送信キュー**: 送信できなかった分は保存し、60 秒ごとに自動再送
- **複数の接続**: 複数のワークスペースを同時に登録し、DB を 1 つの一覧で切り替えられる

## 動作環境

- Windows 10 / 11
- Python 3.12 以上（exe 版を使う場合は不要）

## インストールと起動

```powershell
git clone <このリポジトリの URL>
cd <リポジトリ名>
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m notiontask
```

初回起動時に設定画面が開くので、接続を追加してください。

## 接続方式

接続ごとに 2 つの方式から選べます。

| 方式 | 準備 | 特徴 |
|------|------|------|
| MCP | 不要。⚙ →「MCP で追加」→ ブラウザで Notion にログインして許可 | インテグレーションの作成が不要。ログインは 30 日未使用か 180 日で失効 |
| トークン | Internal Integration を作成し、使う DB に接続する（下記） | 期限なし。共有した DB を自動で一覧表示 |

### トークン方式の準備

1. <https://www.notion.so/my-integrations> で Internal Integration を作成し、シークレット（`ntn_...`）をコピー
2. 使いたい DB を開き、右上「…」→「接続」から作成した Integration を追加
3. アプリの ⚙ →「トークンで追加」でシークレットを貼り付け

### MCP 方式の DB 登録

MCP 方式では DB の一覧を自動取得できないため、メイン画面の「＋」（DB 管理）から DB 名で検索するか、DB の URL を貼り付けて登録します。

> ワークスペースの管理者が外部連携や MCP の利用を制限している場合は、どちらの方式も使えないことがあります。

## キー操作

| キー | 動作 |
|------|------|
| `Ctrl+Alt+N`（変更可） | ウィンドウを呼び出す |
| `Ctrl+Enter` | ページを作成 |
| `Enter`（タイトル欄） | 本文へ移動 |
| `Ctrl+1`〜`Ctrl+9` | 投稿先 DB を切替 |
| `Esc` | トレイに隠す |

## exe 化

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.\build.bat
```

`dist\Sticky-Notion.exe` が生成されます。

## テスト

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

## 設定とデータの保存場所

- `%APPDATA%\Sticky-Notion\config.json`: 接続・DB 一覧・ウィンドウ状態など。トークン類は Windows DPAPI で暗号化して保存します（同じ Windows ユーザーでしか復号できません）
- `%APPDATA%\Sticky-Notion\queue.json`: 未送信のページ

（旧名 `NotionTask` 時代の `%APPDATA%\NotionTask` フォルダは、初回起動時に自動で引き継ぎます。）

トークン類が送信されるのは Notion（`api.notion.com` / `mcp.notion.com`）だけです。

## 補足

- 証明書の検証には Windows の証明書ストアを使っています（[truststore](https://github.com/sethmlarson/truststore)）。
- 本ソフトウェアは Notion Labs, Inc. とは関係のない非公式ツールです。

## ライセンス

[MIT](LICENSE)
