<p align="center">
  <img src="assets/game-localizer-logo.png" alt="Game Localizer" width="720">
</p>

# Game Localizer

[简体中文](README.md) | [English](README.en.md) | **日本語**

Game Localizer は、ゲーム内テキスト向けのローカライズパイプラインです。リソースのスキャン、翻訳メモリ、機械翻訳、品質チェック、人手による修正、成果物のビルド、公開を、追跡可能な一つのワークフローに統合します。

本プロジェクトは宣言的な設定で動作し、単一ゲーム、複数のリソースディレクトリ、複数バージョンの継続的な更新に対応します。デフォルトでは、SQLite を翻訳メモリ（TM）の信頼できるデータソースとして使用します。

## 主な機能

- Gettext PO/MO、ParaTranz JSON、Paradox YAML リソースに対応します。
- OpenAI 互換 API を介して翻訳モデルに接続し、並行処理、レート制限、ローカル tokenizer をサポートします。
- 安定した座標と原文フィンガープリントで SQLite TM を管理し、原文の変更後に古い訳文が誤って再利用されることを防ぎます。
- 機械翻訳、人手で確定した翻訳、過去の移行記録を区別し、レビュー済みコンテンツを保護します。
- プレースホルダー、原言語の残存、用語、フィルタリング、正規化のチェックを提供します。
- `preview` と `release` の 2 つのビルドモードに対応し、正式な成果物には QualityGate の通過を必須とします。
- 実行状態、チェックポイント、レポート、マニフェストを保存し、失敗した実行の再開や、親実行からの差分再ビルドを可能にします。
- 実行状況の確認、QA 問題の特定、監査可能な人手修正の送信に使用できるローカル監視ダッシュボードを提供します。
- ローカルディレクトリ、GitHub Release、Cloudflare R2、Alibaba Cloud OSS への公開に対応します。

## 動作要件

- Python 3.10 以降
- Git（バージョン管理用。パイプラインの実行自体はリモートリポジトリを必要としません）

開発版をインストールします。

```powershell
python -m pip install -e .
```

必要に応じてオプション機能をインストールします。

```powershell
# AES-256 で暗号化された成果物
python -m pip install -e ".[artifact-aes]"

# Hugging Face tokenizer
python -m pip install -e ".[tokenizer-huggingface]"

# すべてのリモート公開アダプター
python -m pip install -e ".[publish-all]"
```

## クイックスタート

### 1. Dashboard を起動する（推奨）

Dashboard は日常作業の推奨エントリーポイントです。ローカルタスクの開始、実行状況の確認、QA 問題の特定、人手修正の送信、差分再ビルドの開始を行えます。インストール後、同梱サンプルですぐに起動できます。

```powershell
localizer dashboard projects/example/project.yaml --host 127.0.0.1 --port 8765
```

起動後、<http://127.0.0.1:8765> を開いてください。API キーがなくても Dashboard は開けますが、機械翻訳タスクを実行する前に、以下のプロジェクト設定と認証情報が必要です。書き込み操作はループバックアドレスでのみ有効です。

### 2. プロジェクトディレクトリを準備する

ゲームごとの設定とルールを個別のディレクトリに配置することを推奨します。

```text
projects/my-game/
├── project.yaml
├── prompt.md
├── background.md
├── glossary.yaml
└── rules.yaml
```

最小構成の `project.yaml` の例です。

```yaml
schema_version: 1

project:
  id: my-game
  name: My Game
  game_version: 1.0.0

paths:
  source: ../../data/my-game/source
  workspace: ../../var/my-game/workspace
  output: ../../var/my-game/output

languages:
  source: en-US
  target: zh-Hans

resources:
  adapters:
    - type: gettext
      include:
        - "**/*.po"
        - "**/*.mo"
      options:
        layout: standard
        empty_source: skip
        source_filter: all

prompt:
  template: ./prompt.md
  background: ./background.md

glossary:
  file: ./glossary.yaml
  auto_discovery: candidate_only

rules:
  file: ./rules.yaml

provider:
  type: openai_compatible
  base_url: https://api.example.com/v1
  api_key_env: LOCALIZER_API_KEY
  model: provider-model-name
  concurrency: 4
  timeout_seconds: 120
  context_window: 32768
  max_output_tokens: 4096

tm:
  database: ../../var/my-game/localizer.sqlite
  global_exact_match: reviewed_only
  commit_policy: quality_gate

workflow:
  mode: local

build:
  format: zip
  release_channel: stable
  artifact_prefix: localization
  compression: deflate
  encryption: none

publish:
  targets:
    - type: local
      destination: ../../releases/my-game
      versioned_prefix: true
```

設定内の相対パスは、`project.yaml` が存在するディレクトリを基準に解決されます。認証情報のフィールドには環境変数名のみを指定し、秘密情報を YAML に書き込まないでください。

補助ファイルを準備します。

```yaml
# glossary.yaml
schema_version: 1
terms: []
```

```yaml
# rules.yaml
schema_version: 1
```

`prompt.md` には対象言語、文体、書式上の制約を記述し、入力内のプレースホルダーを保持するようモデルに指示します。任意の `background.md` でゲーム世界や UI のコンテキストを補足できます。モデル応答の具体的なバッチ形式はパイプラインが組み立てます。

設定、用語集、ルール、リソースのコメント付き完全版は [`projects/example`](projects/example/project.yaml) を参照してください。

### 3. 認証情報を設定する

PowerShell：

```powershell
$env:LOCALIZER_API_KEY = "your-api-key"
```

`.env` ファイルを使用し、設定の `environment` セクションで自動検出を有効にすることもできます。`.env` は必ずバージョン管理の対象外にしてください。

### 4. 検証してスキャンする

```powershell
localizer validate-config projects/my-game/project.yaml
localizer scan projects/my-game/project.yaml
```

### 5. CLI でプレビューをビルドする（任意）

```powershell
localizer build projects/my-game/project.yaml --mode preview --run-id preview-001
```

プレビュー実行では、出力、QA レポート、実行記録が生成されますが、公開可能な正式成果物としては扱われません。

Dashboard からプレビュータスクを直接開始できます。次の CLI コマンドは、スクリプト、CI、無人実行向けです。

### 6. CLI で正式成果物をビルドして公開する

```powershell
localizer build projects/my-game/project.yaml --mode release --run-id release-001
localizer publish projects/my-game/project.yaml path/to/artifact-manifest.json
```

`release` は完全な品質ゲートを実行します。ゲートを通過したマニフェストだけが公開ワークフローに進めます。

## よく使うワークフロー

### Dashboard を使った日常作業

```powershell
localizer dashboard projects/my-game/project.yaml --host 127.0.0.1 --port 8765
```

Dashboard で行った人手修正には、操作者と append-only の判断ログが記録され、正式な TM に同期されます。設定ファイルは引き続きバージョン管理で管理し、Dashboard は実行、監視、制御された修正を担当します。

### 親実行から差分再ビルドする

正式チェックの失敗後に問題を手動で修正済みで、すべての機械翻訳を再度リクエストしたくない場合に適しています。

```powershell
localizer rebuild-from-run projects/my-game/project.yaml `
  --parent-run-id release-001 `
  --run-id release-002 `
  --mode release
```

このコマンドは原文フィンガープリントを検証し、親実行に含まれる有効な結果を再利用して、未解決のエントリだけを再処理します。

### 複数のリソースバリアント

プロジェクトでは `paths.sources` に複数のリソースディレクトリを宣言し、`paths.default_variant` または CLI の `--variant` で選択できます。各バリアントは独立した実行ディレクトリと出力ディレクトリを持ちますが、TM、用語集、ルールセットは共有します。

```powershell
localizer build projects/my-game/project.yaml --variant beta --mode preview --run-id beta-001
```

### 既存の正式成果物だけをコピーする

```powershell
localizer publish-local path/to/artifact-manifest.json path/to/destination
```

## 翻訳メモリ（TM）

SQLite は実行時の翻訳と人手修正における信頼できるソースです。正式な書き込みは次の原則に従います。

- 座標は、プロジェクト、アダプター、相対パス、論理キーの組み合わせで決まります。
- 検索時には原文フィンガープリントも検証されます。原文が変わると、古い訳文は直接一致として返されません。
- 人手でレビューされたレコードは機械翻訳の結果より優先され、一括操作で暗黙に上書きすることはできません。
- 機械翻訳は品質ゲートを通過した後にのみ正式レコードとしてコミットされます。
- 各変更には、出所、状態、実行識別子、監査情報が保持されます。

旧システムから移行する場合は、先に同期と検証を行い、その後で信頼できるソースを切り替えます。

```powershell
localizer tm-sync-legacy projects/my-game/project.yaml path/to/legacy-tm.json

# デフォルトでは差分レポートのみを生成
localizer tm-adopt-artifact projects/my-game/project.yaml path/to/artifact-manifest.json

# 人手で確認した後に SQLite へ書き込む
localizer tm-adopt-artifact projects/my-game/project.yaml path/to/artifact-manifest.json `
  --apply `
  --accepted-by project-owner

localizer tm-verify-artifact projects/my-game/project.yaml path/to/artifact-manifest.json `
  --run-id verify-001
```

`tm-switch-authority` はガバナンス操作であり、通常のビルド手順ではありません。実行前に、動作ベースライン、データベースライン、旧 TM を保存し、ロールバックおよび監査資料が揃っていることを確認してください。

## ディレクトリと成果物

通常の実行データは、設定で指定した `workspace` と `output` に保存されます。

```text
var/my-game/
├── localizer.sqlite
├── workspace/
│   └── <run-id>/
│       ├── checkpoints/
│       ├── reports/
│       └── run-state.json
└── output/
    ├── preview/
    └── release/
```

実行ディレクトリには再生成可能なデータが含まれます。正式成果物とそのマニフェストが、公開および読み戻し検証の起点です。アーカイブのファイル名だけでバージョン、モード、品質状態を判断しないでください。

## 公開とセキュリティ

- ローカル公開には認証情報ガバナンスの宣言は不要です。
- リモート公開はデフォルトで無効です。認証情報をローテーションし、監査記録を設定した後にのみ有効になります。
- 設定ファイルでは環境変数名のみを参照できます。トークン、パスワード、アクセスキーを保存してはいけません。
- 各公開先は独立して実行されます。一つの公開先で失敗してもローカル成果物は破損せず、再試行可能な結果が返されます。
- 正式リリースではバージョンを固定し、マニフェストを保持して、アップロード結果の読み戻し検証を実施してください。

## テスト

完全なテストスイートを実行します。

```powershell
python -X utf8 -m unittest discover -s tests
```

コミット前には、次のコマンドの実行も推奨します。

```powershell
python -m pre_commit run --all-files
```

## サンプルプロジェクト

- [最小の実行可能な設定](projects/example/project.yaml)
- [ゲーム背景の例](projects/example/background.md)
- [翻訳プロンプトの例](projects/example/prompt.md)
- [用語集の例](projects/example/glossary.yaml)
- [ルールの例](projects/example/rules.yaml)
- [Gettext リソースの例](projects/example/source/messages.po)

## 現在の制限

- コミュニティプラットフォーム向けワークフローの設定モデルとオフライン同期コンポーネントは存在しますが、オンライン API クライアントはまだ実装されていません。
- Web ダッシュボードはローカル監視と単一ユーザーによる個別修正を目的としており、認証、タスク割り当て、複数ユーザー承認を備えた共同作業プラットフォームではありません。
- リモート公開には、対応するオプション依存関係、環境認証情報、明示的なガバナンス設定が必要です。

## ライセンス

本プロジェクトは、[PolyForm Noncommercial License 1.0.0](LICENSE) に基づくソースアベイラブル方式で配布されています。

- ライセンスで定義された非商用の範囲で、使用、研究、変更、配布できます。
- 商用利用は公開ライセンスの対象外であり、事前にライセンサーから個別の許諾を得る必要があります。
- 商用利用が制限されているため、本プロジェクトは OSI の定義におけるオープンソースソフトウェアではありません。
- このライセンスは当該 `LICENSE` とともに配布されるバージョンに適用されます。別のライセンスで取得済みの過去バージョンに対する既存の許諾が、遡って取り消されることはありません。
- サードパーティ依存関係と外部リソースには、それぞれのライセンスが引き続き適用されます。

完全かつ法的拘束力のある条項については、[LICENSE](LICENSE) を参照してください。
