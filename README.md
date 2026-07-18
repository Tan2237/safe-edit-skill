# safe-edit skill

通用的安全文本编辑 Agent Skill。它用一个跨平台 Python 脚本检查和编辑已有文本文件，并尽量保留原文件的编码、BOM、行尾格式、普通权限和写入完整性。

适合 Agent 修改源代码、配置文件、中文项目文件、MSVC/Windows 项目文件，以及任何不希望被 `cat`、`sed`、`Set-Content` 或临时脚本弄乱编码和 Git diff 的场景。

## 特性

- 单文件 Python 标准库实现，Windows/Linux/macOS 通用。
- `inspect` 只检查不写入，可输出编码、BOM、行尾统计、文件大小、行数、NUL 字符和权限位。
- `stat` 简洁摘要，包含编码、BOM、行尾统计、文件大小、行数，以及推荐的编辑策略（`editStrategy`）。
- `preflight` 在写入前报告 Python、stdin、Base64、临时目录、锁和目标目录能力。
- `transaction` 接收结构化多文件请求，先全量预演，再按稳定顺序加锁写入；失败时回滚已完成的写入。
- `create` 受控新建任务成果文件：拒绝覆盖、要求父目录已存在，并强制显式选择编码和行尾。
- `convert` 显式转换编码、行尾、最终换行，或清理尾随空白；普通编辑默认仍保留原格式。
- 自动检测并保留 `utf-8`、`utf-8-bom`、`gbk`、UTF-16 BOM，以及清晰 NUL 模式下的无 BOM UTF-16。
- 支持手动指定 `shift-jis`、`big5`、`latin-1`、`utf-16-le`、`utf-16-be`。
- 自动检测并保留 CRLF、LF、CR 行尾风格。
- 支持字面量替换、显式正则替换、插入行、文件头追加、文件尾追加、删除行、替换行范围、删除行范围。
- 支持 stdin、URL-safe/标准 Base64 和文件三类载荷通道，避免 Windows shell 改写多行代码与特殊字符。
- 支持 `--dry-run --diff` 预览、`--expected-count` 防误匹配、`--backup` 备份、JSON batch 一次读写多步编辑。
- **结构化 JSON 错误输出**：`--json` 模式下错误也输出 JSON，包含错误类型分类、根因分析、最近匹配、Agent 恢复协议（`failureClass`、`recommendedAction`、`retryStrategy`）。
- **匹配级别报告**：成功时在 operations 中报告 `matchStrategy`（exact、ignore-eol、ignore-indent、fuzzy 等）。
- **自动容错匹配**：`--auto-match` 精确匹配失败后自动尝试宽松匹配（ignore-eol → ignore-indent → normalize-whitespace），`--fuzzy` 启用模糊匹配。
- **上下文消歧**：`--context-before` / `--context-after` 辅助多匹配消歧。
- **SEARCH/REPLACE 输入格式**：`--diff-input`、`--diff-input-file`、`--diff-input-stdin`、`--diff-input-base64` 支持 Agent 友好的 diff 格式输入。
- 支持备份目录/后缀自定义，以及 stale lock 自动清理。
- 同目录临时文件写入、原子替换、写后字节校验，并带有协作锁以降低并发写入风险。
- 如果变换后的字节和原文件完全一致，默认跳过写入，避免无意义的 mtime 和 Git 状态变化。
- `--explain-match-failure` 匹配失败时显示详细诊断，可视化空白差异。
- `--anchor-pattern` 锚点定位替换，配合 `--offset-start/end` 实现相对行号定位。
- `--interactive/-i` 交互确认模式，类似 `git add -p` 的 y/n/a/q/? 提示。
- `--ignore-indent`、`--ignore-eol`、`--normalize-whitespace` 可控空白匹配放宽。
- 附带 `unittest` 测试套件和 GitHub Actions，覆盖 Windows、Linux、macOS。

## 安装

使用支持 `skills` 生态的安装器：

```bash
npx skills add Tan2237/safe-edit-skill
```

全局安装：

```bash
npx skills add Tan2237/safe-edit-skill -g
```

指定 Agent：

```bash
npx skills add Tan2237/safe-edit-skill -a opencode
npx skills add Tan2237/safe-edit-skill -a claude-code
```

仓库中的 skill 位于：

```text
skills/safe-edit/
  SKILL.md
  safe_edit.py
```

## Recommended Workflow

先确认运行时和传输能力，再检查文件属性：

```bash
python safe_edit.py preflight --file foo.cpp --json
python safe_edit.py stat --file foo.cpp --json
```

如果 `python` 和 `py -3` 都不可用，应优先使用宿主提供的 Python
运行时绝对路径；仍无法执行时，在修改任何文件前停止。

返回的 `editStrategy` 告诉你该用什么工具编辑这个文件：

| editStrategy | 含义 |
|---|---|
| `edit-tool` | 用内置 Edit 工具即可 |
| `safe-edit` | 必须用 safe-edit（文件有 BOM、非 UTF-8 编码、CRLF 行尾等） |

如果 `editStrategy` 是 `safe-edit`，后续对该文件的所有编辑都应使用 safe-edit。

## 基本用法

直接运行 Python 脚本即可。Windows 上如果 `python` 不在 `PATH`，可以用 `py -3` 替代。

```bash
python safe_edit.py preflight --json
python safe_edit.py stat --file path/to/file --json
python safe_edit.py transaction --request-stdin --json
python safe_edit.py create --file path/to/new.txt --to-encoding utf-8 --to-line-ending lf --text-base64 B64
python safe_edit.py remove-file --file path/to/obsolete.txt --workspace-root path/to/workspace --expected-sha256 SHA256
python safe_edit.py convert --file path/to/file --to-encoding utf-8-bom --to-line-ending crlf --final-newline ensure
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --expected-count 1
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --auto-match --expected-count 1
python safe_edit.py edit --file path/to/file --diff-input "------- SEARCH
foo
=======
bar
+++++++ REPLACE"
python safe_edit.py regex --file path/to/file --pattern "foo\\d+" --replacement "bar" --expected-count 1
python safe_edit.py insert --file path/to/file --line 10 --text "new line"
python safe_edit.py prepend --file path/to/file --text-file header.txt
python safe_edit.py append --file path/to/file --text-file footer.txt
python safe_edit.py delete --file path/to/file --line 10
python safe_edit.py replace-lines --file path/to/file --start 10 --end 20 --text-file block.txt
python safe_edit.py replace-lines --file path/to/file --anchor-pattern "keyword" --offset-start +2 --offset-end +4 --text "new line"
python safe_edit.py delete-lines --file path/to/file --start 10 --end 20
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" -i
python safe_edit.py batch --file path/to/file --ops-file ops.json
```

## 受控新建文件

新文件不能先执行 `stat`。当任务确实需要新增源码、测试、配置或文档时，使用 `create`：

```bash
python safe_edit.py create \
  --file path/to/new-file.txt \
  --to-encoding utf-8 \
  --to-line-ending lf \
  --text-base64 B64
```

`create` 的约束：

- 目标必须不存在，绝不隐式覆盖。
- 父目录必须已经存在，不会递归创建目录。
- 必须显式指定 `--to-encoding` 和 `--to-line-ending`。
- 支持 `--text`、`--text-file`、`--text-stdin`、`--text-base64`。
- 支持 `--dry-run --diff`，预览时不会落盘。
- 创建完成后，如需继续修改该文件，先执行一次 `stat` 并遵循返回的 `editStrategy`。

`create` 只用于任务成果文件，不用于临时脚本、补丁工具或为了绕过载荷传输规则而生成的中转文件。

## 受控删除文件

仅当任务明确要求删除某个文件时使用 `remove-file`。先运行 `stat --json`
取得当前内容的 SHA-256，再把工作区根目录和哈希一并传入：

```bash
python safe_edit.py stat --file path/to/obsolete.txt --json
python safe_edit.py remove-file \
  --file path/to/obsolete.txt \
  --workspace-root path/to/workspace \
  --expected-sha256 SHA256 \
  --dry-run \
  --json
python safe_edit.py remove-file \
  --file path/to/obsolete.txt \
  --workspace-root path/to/workspace \
  --expected-sha256 SHA256
```

安全约束：

- 一次只能删除一个工作区根目录内的普通文件。
- 拒绝目录、符号链接、通配符和递归删除。
- 文件内容或身份在检查后发生变化时拒绝删除。
- `--expected-sha256` 必须与当前文件内容一致。
- `--dry-run` 只报告 `wouldRemove`，不会删除文件。
- 不支持 `--follow-symlink`、`--backup`、`--diff` 或 `--interactive`。
- 删除仍受 `--max-bytes` 限制；大文件需要显式提高限制。

## 自动容错匹配

精确匹配失败时，使用 `--auto-match` 自动尝试宽松匹配策略：

```bash
# 自动尝试: exact → ignore-eol → ignore-indent → normalize-whitespace
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --auto-match --expected-count 1
```

启用模糊匹配作为最后手段（相似度 ≥ 0.6）：

```bash
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --auto-match --fuzzy --expected-count 1
```

**关键安全约束**：自动容错在输出中报告使用的匹配级别（`matchStrategy` 字段），不会静默降级。

## 结构化 JSON 输出

`--json` 模式下，成功和失败都输出结构化 JSON：

```bash
# 成功时包含 matchStrategy
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --auto-match --expected-count 1 --json

# 失败时输出错误类型、诊断和恢复建议
python safe_edit.py edit --file path/to/file --old "missing" --new "bar" --json
```

错误类型包括：`match_not_found`、`match_ambiguous`、`match_count_mismatch`、`encoding_error`、`file_error`、`lock_error`、`validation_error`、`format_error`、`unknown`。

### Agent Recovery Protocol

匹配失败时，JSON 错误输出包含完整的恢复协议字段，供 Agent 自动决策：

```json
{
  "ok": false,
  "error": {
    "type": "match_not_found",
    "message": "old text was not found"
  },
  "failureClass": "RETRYABLE",
  "rootCause": "indentation_difference",
  "closestMatch": {"line": 42, "similarity": 0.91},
  "recommendedAction": {"type": "retry", "confidence": 0.9},
  "retryStrategy": {"flags": ["--ignore-indent"], "alternativeFlags": ["--auto-match"]}
}
```

| 字段 | 说明 |
|------|------|
| `failureClass` | `RETRYABLE`（可自动重试）、`RE_READ_REQUIRED`（需重新读取文件）、`USER_INPUT`（需用户修正）、`FATAL`（不可恢复） |
| `rootCause` | 根因分类：`indentation_difference`、`line_ending_difference`、`whitespace_difference`、`content_not_found`、`multiple_matches`、`similar_content_exists` |
| `closestMatch` | 最接近匹配的位置、片段和相似度（0.0–1.0） |
| `recommendedAction` | 推荐的恢复动作（`retry`、`re_read_file`、`ask_user`、`stop`）及其置信度 |
| `retryStrategy` | 仅 `RETRYABLE` 时返回，包含推荐的重试参数 |

## 上下文消歧

当 `--old` 出现多次时，用上下文文本过滤：

```bash
# 只替换 "target" 前面有 "middle" 的那次
python safe_edit.py edit --file path/to/file --old "target" --new "replaced" \
  --context-before "middle" --expected-count 1

# 只替换 "target" 后面有 "suffix" 的那次
python safe_edit.py edit --file path/to/file --old "target" --new "replaced" \
  --context-after "suffix" --expected-count 1
```

上下文匹配在匹配位置附近的窗口内搜索（不搜索整个文件），使用子串包含。

## SEARCH/REPLACE 输入格式

Agent 友好的 diff 格式，一个字符串包含所有编辑信息：

```bash
python safe_edit.py edit --file path/to/file --diff-input "------- SEARCH
old text
=======
new text
+++++++ REPLACE"
```

支持多块编辑：

```bash
python safe_edit.py edit --file path/to/file --diff-input "------- SEARCH
first_old
=======
first_new
+++++++ REPLACE
------- SEARCH
second_old
=======
second_new
+++++++ REPLACE"
```

也支持从文件读取：`--diff-input-file diff.txt`

标记格式灵活（3个以上 `-`/`<`/`=`/`+`/`>` 均可，不区分大小写）：
- 搜索开始：`------- SEARCH` / `<<< SEARCH`
- 分隔符：`=======` / `===`
- 替换结束：`+++++++ REPLACE` / `>>> REPLACE`

## 预览和防误操作

建议在风险较高的修改前使用：

```bash
python safe_edit.py inspect --file src/main.cpp --json

python safe_edit.py edit \
  --file src/main.cpp \
  --old "oldName" \
  --new "newName" \
  --expected-count 1 \
  --dry-run \
  --diff
```

`--expected-count` 不匹配时会失败并保持文件不变。默认情况下，没有找到匹配也会失败，避免静默 no-op。

## 显式转换和规范化

普通编辑默认保留原编码和行尾。只有明确需要规范化时才使用 `convert` 或后处理选项：

```bash
python safe_edit.py convert --file a.cpp --to-encoding utf-8-bom
python safe_edit.py convert --file a.cpp --to-line-ending crlf
python safe_edit.py convert --file a.cpp --final-newline ensure
python safe_edit.py convert --file a.cpp --trim-trailing-whitespace
```

这些后处理选项也可以和 `edit`、`regex`、`batch` 等写入命令组合，做到一次读写内完成内容修改和格式规范化。

## 锚点定位替换

使用 `--anchor-pattern` 替代绝对行号，防止文件变动后改错位置：

```bash
python safe_edit.py replace-lines \
  --file a.cpp \
  --anchor-pattern "AcGePoint3d ptCenter" \
  --offset-start +2 \
  --offset-end +4 \
  --text "new line"
```

可选参数：
- `--anchor-occurrence N` - 当锚点匹配多处时指定第 N 个
- `--offset-start +N/-N` - 相对锚点的起始偏移
- `--offset-end +N/-N` - 相对锚点的结束偏移

## 可控空白匹配

放宽匹配条件，但保持行为可控：

```bash
python safe_edit.py edit --file a.cpp --old "    foo" --new "bar" --ignore-indent
python safe_edit.py edit --file a.cpp --old "foo\r\nbar" --new "baz" --ignore-eol
python safe_edit.py edit --file a.cpp --old "foo   bar" --new "baz" --normalize-whitespace
```

**原则**：只影响匹配，不影响替换内容。

## 交互确认

类似 `git add -p` 的交互模式：

```bash
python safe_edit.py edit --file a.cpp --old "foo" --new "bar" -i
```

提示选项：
- `y` - 应用此修改
- `n` - 跳过此修改
- `a` - 应用全部剩余修改
- `q` - 退出
- `?` - 显示帮助

## 匹配失败诊断

```bash
python safe_edit.py edit --file a.cpp --old "    foo" --new "bar" --explain-match-failure
```

输出：
```
Closest match found at line 284:
EXPECTED:
[SP][SP][SP][SP]foo
ACTUAL:
[TAB]foo
Differences:
- indentation uses tabs instead of spaces
```

## 安全载荷传输与跨平台传参

PowerShell、Windows 原生命令行和 MSYS2 都可能在 Python 收到参数前改写引号、反斜杠或路径形态。此时即使 safe-edit 报告成功，也只能证明“实际收到的载荷”被安全写入，不能证明它仍与调用者的原始文本一致。

推荐顺序：

1. 调用工具提供原生 stdin 时，使用 `--ops-stdin`、`--diff-input-stdin` 或 `--text-stdin`。
2. 没有原生 stdin 时，使用 URL-safe UTF-8 Base64：`--ops-base64`、`--diff-input-base64` 或字段级 `--*-base64`。
3. 载荷文件已经存在时，使用 `--ops-file`、`--old-file`、`--new-file` 或 `--text-file`。
4. 仅对短且不含 shell 敏感字符的内容使用字面 `--old`、`--new`、`--text`。

### Stdin 方式

一个 stdin 流只能供一个 `--*-stdin` 参数读取。只有一个文本字段时可以直接使用：

```bash
python safe_edit.py replace-lines --file a.cpp --start 10 --end 20 --text-stdin
python safe_edit.py edit --file a.cpp --diff-input-stdin
```

精确替换同时需要 `old` 和 `new`，应把整个操作封装为 JSON，交给：

```bash
python safe_edit.py batch --file a.cpp --ops-stdin
```

JSON 示例：

```json
[
  {
    "op": "edit",
    "old": "frozenset({\"old\", \"%\"})",
    "new": "frozenset({\"new\", \"!\"})",
    "expected_count": 1
  }
]
```

应优先通过执行工具的 stdin 字段传入 JSON，而不是把源码先放进 PowerShell here-string。若载荷已经存在于文件中，也可通过管道读取；这不用于绕过载荷文件本身的创建规则：

```powershell
Get-Content -Raw ops.json | py -3 safe_edit.py batch --file a.cpp --ops-stdin
```

```bash
cat ops.json | python3 safe_edit.py batch --file a.cpp --ops-stdin
```

### Base64 方式

所有 Base64 参数都将解码为严格 UTF-8。支持标准 Base64 和 URL-safe Base64，也支持省略末尾 `=`；Windows/MSYS2 下推荐无填充的 URL-safe 形式。

```bash
python safe_edit.py batch --file a.cpp --ops-base64 B64
python safe_edit.py edit --file a.cpp --diff-input-base64 B64
python safe_edit.py replace-lines --file a.cpp --start 10 --end 20 --text-base64 B64
python safe_edit.py edit --file a.cpp --old-base64 OLD_B64 --new-base64 NEW_B64 --expected-count 1
```

字段级入口包括：

- `--old-base64` / `--new-base64`
- `--pattern-base64` / `--replacement-base64`
- `--text-base64`
- `--diff-input-base64`
- `--ops-base64`

### 文件方式

`--*-file` 适合已经存在的 UTF-8 载荷文件：

```bash
python safe_edit.py edit --file a.cpp --old-file old.txt --new-file new.txt
python safe_edit.py insert --file a.cpp --line 5 --text-file block.txt
```

不要为了使用文件参数而通过 shell 重定向或临时脚本创建载荷文件。参数文件默认按 UTF-8 读取；需要时使用 `--arg-encoding` 覆盖。

## 多行内容

多行、超过 100 个字符，或包含 `"`、`'`、`%`、`!`、反引号、反斜杠等字符时，不要使用字面 argv。优先使用完整的 `batch --ops-stdin` JSON；无原生 stdin 时使用 `batch --ops-base64`。完成后仍应重新读取，并按文件类型执行编译或测试。

## 正则替换

`edit` 永远是字面量替换；只有 `regex` 命令会启用正则。

```bash
python safe_edit.py regex \
  --file package.toml \
  --pattern 'version = "[^"]+"' \
  --replacement 'version = "1.2.3"' \
  --expected-count 1
```

支持的 flag：`i`、`m`、`s`、`x`、`a`。如果 replacement 中的 `\1` 等内容不应被当作反向引用解释，使用 `--literal-replacement`。

## Batch JSON

多步编辑可以一次读、一次变换、一次原子写：

```json
[
  {"op": "edit", "old": "foo", "new": "bar", "expected_count": 1},
  {"op": "regex", "pattern": "version = \"[^\"]+\"", "replacement": "version = \"1.2.3\"", "expected_count": 1},
  {"op": "prepend", "text_file": "header.txt"},
  {"op": "replace-lines", "start": 10, "end": 12, "text_file": "block.txt"},
  {"op": "append", "text": "done"},
  {"op": "delete-lines", "start": 30, "end": 35}
]
```

执行：

```bash
# 原生 stdin
python safe_edit.py batch --file path/to/file --ops-stdin

# URL-safe UTF-8 Base64
python safe_edit.py batch --file path/to/file --ops-base64 B64

# 已存在的 JSON 文件
python safe_edit.py batch --file path/to/file --ops-file ops.json
```

`*_file` 相对路径会按 `ops.json` 所在目录解析。

## 结构化多文件事务

`transaction` 用一个 JSON 对象承载文件路径、正文、编码、行尾和编辑操作，
避免每个文本字段分别做 shell 转义或 Base64。单个新文件可直接传入：

```json
{"file": "src/new.py", "text": "print('ok')\n", "encoding": "utf-8", "lineEnding": "lf"}
```

多文件请求使用 `files` 清单：

```json
{
  "files": [
    {
      "file": "src/index.py",
      "action": "edit",
      "expectedSha256": "stat 返回的 SHA-256",
      "operations": [
        {"op": "edit", "old": "from .old import x", "new": "from .new import x", "expected_count": 1}
      ]
    },
    {
      "file": "src/new.py",
      "action": "create",
      "text": "def x():\n    return 1\n",
      "encoding": "utf-8",
      "lineEnding": "lf"
    }
  ]
}
```

优先通过宿主的结构化工具参数或原生 stdin 传入：

```bash
python safe_edit.py transaction --request-stdin --dry-run --json
python safe_edit.py transaction --request-stdin --json
```

没有原生 stdin 时，官方 fallback 顺序为：已存在的 JSON 文件
（`--request-file`），然后 URL-safe UTF-8 Base64
（`--request-base64`）。已有文件必须提供本轮 `stat` 返回的
`expectedSha256`；新文件仍拒绝覆盖并要求显式 `encoding` 和
`lineEnding`。

事务会在持锁后预演全部文件，并复用预演生成的最终字节作为提交计划；提交前仅重新
读取目标以拦截忽略协作锁的并发写入，不会重复解码、匹配和计算编辑。进程内写入失败
时会恢复原字节、删除本事务已创建的文件。返回的 `atomicity` 为
`prevalidated-with-rollback`；
这不是跨文件系统或断电级原子提交。

## 编码注意事项

自动检测优先级大致是 BOM、UTF-16 NUL 模式、UTF-8、GBK。纯 ASCII 文件会被视为 UTF-8；如果它属于 GBK、Shift-JIS 或 Big5 项目，并且本次要插入非 ASCII 字符，请显式指定：

```bash
python safe_edit.py edit --encoding gbk --file a.cpp --old "name" --new "中文"
python safe_edit.py edit --encoding shift-jis --file a.cpp --old "name" --new "日本語"
python safe_edit.py edit --encoding big5 --file a.cpp --old "name" --new "繁體"
```

## 测试

```bash
python -m py_compile skills/safe-edit/safe_edit.py
python -m unittest discover -s tests -v
```

GitHub Actions 会在 Windows、Linux、macOS 上运行同一套测试。

## 常用选项

| 选项 | 说明 |
| --- | --- |
| `preflight` | 检查运行时、载荷传输、临时目录、锁和目标目录能力 |
| `transaction` | 结构化多文件预演、写入和失败回滚 |
| `create` | 受控创建不存在的任务成果文件；要求显式编码和行尾 |
| `remove-file` | 受控删除一个明确指定的普通文件；要求工作区根目录和 SHA-256 |
| `--workspace-root DIR` | `remove-file` 的强制路径边界 |
| `--expected-sha256 HASH` | 要求当前文件 SHA-256 与 `stat` 输出一致 |
| `--request-stdin` / `--request-file PATH` / `--request-base64 B64` | 读取 transaction JSON |
| `--encoding` | 指定目标文件编码，默认 `auto` |
| `--to-encoding` | 指定输出编码，默认 `preserve` |
| `--to-line-ending` | 指定输出行尾，支持 `preserve`、`lf`、`crlf`、`cr` |
| `--final-newline` | 控制最终换行，支持 `preserve`、`ensure`、`strip` |
| `--trim-trailing-whitespace` | 清理每行末尾的空格和 tab |
| `--expected-count N` | 要求匹配次数恰好为 `N` |
| `--first` | 只替换第一个匹配 |
| `--count N` | 正则替换数量限制，`0` 表示全部 |
| `--no-op-ok` | 允许没有匹配 |
| `-i, --interactive` | 交互确认，y/n/a/q/? |
| `--explain-match-failure` | 匹配失败时显示诊断 |
| `--auto-match` | 自动容错匹配（exact → ignore-eol → ignore-indent → normalize-whitespace） |
| `--fuzzy` | 启用模糊匹配（需配合 `--auto-match`，相似度 ≥ 0.6） |
| `--context-before T` | 匹配位置前面必须包含的文本 |
| `--context-after T` | 匹配位置后面必须包含的文本 |
| `--diff-input TEXT` | SEARCH/REPLACE 格式输入 |
| `--diff-input-file PATH` | 从文件读取 SEARCH/REPLACE 格式 |
| `--diff-input-stdin` | 从 stdin 读取 SEARCH/REPLACE 格式 |
| `--diff-input-base64 B64` | 从 Base64 UTF-8 读取 SEARCH/REPLACE 格式 |
| `--old-base64 B64` / `--new-base64 B64` | Base64 UTF-8 字面替换载荷 |
| `--pattern-base64 B64` / `--replacement-base64 B64` | Base64 UTF-8 正则载荷 |
| `--text-base64 B64` | Base64 UTF-8 插入或行替换载荷 |
| `--ops-stdin` / `--ops-base64 B64` | 从 stdin 或 Base64 UTF-8 读取 batch JSON |
| `--anchor-pattern` | 锚点定位模式 |
| `--offset-start` | 起始偏移（如 +2、-1） |
| `--offset-end` | 结束偏移 |
| `--anchor-occurrence` | 消除锚点歧义 |
| `--ignore-indent` | 匹配时忽略缩进 |
| `--ignore-eol` | 匹配时忽略行尾 |
| `--normalize-whitespace` | 连续空白视为相同 |
| `--param-encoding` | `--arg-encoding` 别名 |
| `--input-encoding` | `--arg-encoding` 别名 |
| `--dry-run` | 只验证和预览，不写入 |
| `--force-write` | 即使输出字节相同也强制写入 |
| `--diff --context N` | 输出 unified diff |
| `--backup` | 写入前创建时间戳备份 |
| `--backup-dir DIR` | 把备份放到指定目录 |
| `--backup-suffix SUFFIX` | 自定义备份后缀，支持 `{timestamp}` |
| `--json` | 输出机器可读状态（成功和失败均输出 JSON） |
| `--follow-symlink` | 编辑符号链接目标 |
| `--max-bytes N` | 覆盖默认 50 MiB 文件大小限制 |
| `--lock-timeout N` | 等待 safe-edit 协作锁，默认 10 秒 |
| `--lock-stale-seconds N` | 删除超过 `N` 秒的 stale safe-edit 锁 |
| `--no-lock` | 跳过协作锁 |

## 边界

- 不适合二进制文件和复杂结构化文件格式。
- 原子替换通常会生成新的文件对象；不保证保留硬链接关系。
- 仅尽力保留普通权限位，不完整保留 ACL、扩展属性或创建时间。
- 多文件事务提供全量预演和进程内失败回滚，但不保证进程崩溃、断电或跨文件系统时的全局原子性。
- Windows 上无法像 Unix 一样 fsync 目录，因此断电级别保证受平台限制。

## 许可证

MIT
