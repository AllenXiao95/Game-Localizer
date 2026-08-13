# 首次安装与第一次运行

**中文** | [English](en/getting-started.md)

## 前置条件

- Python 3.10 或更高版本
- Git
- 一个兼容 OpenAI Chat Completions 的翻译接口；只做配置校验和资源扫描时可以暂不准备 API 密钥

文档命令默认从仓库根目录执行。Windows 示例使用 PowerShell；macOS/Linux 只需要替换虚拟环境的激活命令。

## 创建隔离环境

不要直接把依赖安装到系统 Python。venv 和 Conda 二选一即可。

### 使用 venv

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

macOS/Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

退出环境使用 `deactivate`。

### 使用 Conda

仓库自带 `environment.yml`：

```powershell
conda env create -f environment.yml
conda activate game_localizer
```

`environment.yml` 只负责创建 Python 环境；仍需在下一步安装当前项目，这样本地源码改动才能立即生效。

## 选择安装方式

第一次使用推荐完整安装：

```powershell
python -m pip install -e ".[all]"
```

`-e` 表示 editable install，即命令行入口指向当前源码目录。`all` 包含：

| 能力 | 依赖组 | 何时需要 |
| --- | --- | --- |
| Hugging Face 本地 tokenizer | `tokenizer-huggingface` | 配置了 `provider.tokenizer` 时 |
| AES-256 加密压缩包 | `artifact-aes` | `build.encryption: aes256` 时 |
| Cloudflare R2 与阿里云 OSS | `publish-all` | 使用对应远端发布目标时 |

只使用本地构建、并且不配置本地 tokenizer 时，可以最小安装：

```powershell
python -m pip install -e .
```

也可以单独补装某项能力，例如：

```powershell
python -m pip install -e ".[tokenizer-huggingface]"
```

### tokenizer 到底是不是必选项

它不是框架核心的无条件依赖，但它与配置是强一致的：

- `provider.tokenizer` 存在：必须安装 `transformers`，程序会在分析/翻译开始前加载指定 tokenizer；加载失败会立即停止，避免使用错误的 token 预算。
- `provider.tokenizer` 不存在：无需安装 `transformers`，程序使用内置的保守估算器。

因此下面的报错不是缺少基础依赖，而是“配置启用了可选能力，但对应依赖未安装”：

```text
provider.tokenizer is configured but transformers is not installed
```

解决方法二选一：安装 `.[tokenizer-huggingface]`，或删除 `project.yaml` 中整个 `provider.tokenizer` 块。不要只删除其中的 `model` 字段。

如果使用 Hugging Face tokenizer，还要注意：

- `model` 是 tokenizer 仓库或本地快照标识，不会从翻译 API 的 `provider.model` 自动推断。
- `revision` 建议固定到确定版本，以便不同运行得到一致的 token 切分。
- `local_files_only: true` 要求缓存中已经存在完整快照；首次联网下载时应为 `false`。
- 缓存位置由 `cache.root` 管理，默认位于其 `tokenizers` 子目录。

## 验证安装

```powershell
localizer --help
localizer validate-config projects/example/project.yaml
localizer scan projects/example/project.yaml
```

`validate-config` 只检查配置结构和关联文件；`scan` 会列出匹配的资源。二者都不会调用翻译 API。

示例配置中的 API 地址、模型名和 tokenizer 标识是占位值。真正执行分析或构建前，必须把它们替换为所用服务的真实配置；如果不需要本地 tokenizer，就删除该配置块。

## 创建自己的项目

复制整个示例目录，而不是只复制 YAML：

```powershell
Copy-Item -Recurse projects\example projects\my-project
```

至少修改以下内容：

1. `project.id`、`project.name`、`project.game_version`。
2. `paths.source`，指向待翻译资源目录。
3. `languages.source` 与 `languages.target`。
4. `resources.adapters`，选择与资源格式匹配的适配器。
5. `provider.base_url`、`provider.model` 和 `provider.api_key_env`。
6. `tm.database`、`paths.workspace`、`paths.output`，避免多个项目互相覆盖。

所有相对路径都以当前 `project.yaml` 所在目录为基准，而不是以终端当前目录为基准。

## 设置 API 密钥

YAML 中只写环境变量名：

```yaml
provider:
  api_key_env: LOCALIZER_API_KEY
```

PowerShell 临时设置：

```powershell
$env:LOCALIZER_API_KEY = "your-api-key"
```

也可以在 Git 忽略的 `.env` 中写：

```dotenv
LOCALIZER_API_KEY=your-api-key
```

并启用：

```yaml
environment:
  auto_discover: true
  override_existing: false
```

`override_existing: false` 表示系统环境变量优先于 `.env`，适合本机和 CI 共用同一份项目配置。

## 第一次运行

先校验和扫描：

```powershell
localizer validate-config projects/my-project/project.yaml
localizer scan projects/my-project/project.yaml
```

再执行预览：

```powershell
localizer build projects/my-project/project.yaml --mode preview --run-id first-preview
```

运行 `build` 时会自动创建 `tm.database` 指定的 SQLite 文件、父目录和表结构，不需要手工运行 SQL。预览结果用于检查，不会被伪装成可发布正式制品。

也可以使用 Dashboard：

```powershell
localizer dashboard projects/my-project/project.yaml --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>。Dashboard 只绑定回环地址时才开放写操作。

## 常见首次启动问题

### PowerShell 禁止激活脚本

可以只对当前进程放开：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

也可以不激活，直接使用 `.\.venv\Scripts\python.exe -m pip ...`。

### 找不到 `localizer`

确认已经激活正确环境，并重新执行：

```powershell
python -m pip install -e ".[all]"
python -m pip show game-localizer
```

### tokenizer 下载失败

先确认 `provider.tokenizer.model` 和 `revision` 确实存在。如果环境不能联网，应提前把快照放入 `cache.root/tokenizers`，然后使用 `local_files_only: true`；如果不需要精确 token 计数，则删除 tokenizer 配置块。

### 扫描结果为空

依次检查 `paths.source`、适配器的 `include`/`exclude` glob，以及资源格式是否与 `type` 匹配。用 `localizer scan` 比直接运行构建更容易定位这类问题。
