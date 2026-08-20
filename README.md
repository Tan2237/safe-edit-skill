# safe-edit skill

通用的安全文本编辑 Agent Skill。它用一个跨平台 Python 脚本检查和编辑已有文本文件，并尽量保留原文件的编码、BOM、行尾格式、普通权限和写入完整性。

适合 Agent 修改源代码、配置文件、中文项目文件、MSVC/Windows 项目文件，以及任何不希望被 `cat`、`sed`、`Set-Content` 或临时脚本弄乱编码和 Git diff 的场景。

## 特性

- 单文件 Python 标准库实现，Windows/Linux/macOS 通用；native binding 按需解析并在进程内复用，handle、buffer 和错误状态仍按调用创建。
- `inspect` 只检查不写入，可输出编码、BOM、行尾统计、文件大小、行数、NUL 字符和权限位。
- `stat` 简洁摘要，包含编码、BOM、行尾统计、文件大小、行数，以及推荐的编辑策略（`editStrategy`）。
- `stat-many` 在一个进程内检查多个文件，并用随机临时名探测能力；同父目录结果短期合并，temp/lock 标志共用一次 probe；结果只是 best-effort 观测。
- `preflight` 在写入前报告 Python、stdin、Base64、临时目录、锁和目标目录能力。
- `transaction` 接收结构化多文件请求，先生成不可变 prepared plan，再按稳定顺序加锁并统一重验路径、身份与哈希后写入；失败时执行冲突安全的尽力回滚。
- 结构化事务先精确匹配，再仅对多行目标或上下文回退到 LF/CRLF/CR 兼容匹配；混合行尾文件也无需为纯 EOL 差异开启 `autoMatch`。
- MCP dry-run 默认返回有上限的精简 diff；只有预演结果成功进入有界缓存时才签发有效期最长 10 分钟的一次性 `transactionId`，确认时无需重传大 payload。
- 成功写入会返回新 `sha256`，可直接作为下一轮 `expectedSha256`；`old == new` 操作会明确跳过。
- Codex 插件及跨客户端安装器提供常驻 MCP 工具，直接接收结构化正文，避免 Base64、Windows argv 限制和重复启动 Python。
- `create` 受控新建任务成果文件：拒绝覆盖、要求父目录已存在，并强制显式选择编码和行尾。
- `convert` 显式转换编码、行尾、最终换行，或清理尾随空白；ASCII space/tab 优先走分阶段短运行快路，复杂或残留模式回退 regex；普通编辑仍保留原格式。
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
- 同目录临时文件写入、原子替换；create/直接单文件写入后以 1 MiB 分块严格比对全部字节和 EOF；诊断 SHA 仅在稳定性复验通过时返回，分块且最多读取 50 MiB；协作锁用于降低并发写入风险。
- v2 锁在整个编辑生命周期持有 kernel stripe；legacy marker 继续使用旧 namespace 以兼容 v1，但混跑时仍保留旧协议固有的窄竞态窗口。
- 如果变换后的字节和原文件完全一致，默认跳过写入，避免无意义的 mtime 和 Git 状态变化。
- `--explain-match-failure` 匹配失败时显示详细诊断，可视化空白差异。
- `--anchor-pattern` 锚点定位替换，配合 `--offset-start/end` 实现相对行号定位。
- `--interactive/-i` 交互确认模式，类似 `git add -p` 的 y/n/a/q/? 提示。
- `--ignore-indent`、`--ignore-eol`、`--normalize-whitespace` 可控空白匹配放宽。
- 附带 `unittest` 测试套件和 GitHub Actions，覆盖 Windows、Linux、macOS。

## 安装

### 仅安装 Skill / CLI 回退

现有安装方式保持兼容，适合尚不支持 MCP 的 Agent：

```bash
npx skills add Tan2237/safe-edit-skill
npx skills add Tan2237/safe-edit-skill -g
npx skills add Tan2237/safe-edit-skill -a opencode
npx skills add Tan2237/safe-edit-skill -a claude-code
```

这一路径安装 `skills/safe-edit/` 下的工作流和 `safe_edit.py`，
不会自动注册常驻 MCP 服务。

### 安装跨客户端 MCP 命令（推荐）

使用 `pipx` 或 `uv` 从 Git 仓库安装零运行时依赖的命令：

```bash
pipx install git+https://github.com/Tan2237/safe-edit-skill.git
# 或
uv tool install git+https://github.com/Tan2237/safe-edit-skill.git
```

安装后会得到两个稳定入口：

- `safe-edit`：原 CLI 回退。
- `safe-edit-mcp`：常驻 stdio MCP 服务及跨客户端安装器。

```bash
safe-edit-mcp --version
safe-edit-mcp install --client all --scope project --project-dir . --dry-run
```

## Codex 结构化工具（推荐）

仓库自带 Codex marketplace 和插件清单，可直接安装 skill 与常驻 MCP：

```bash
codex plugin marketplace add Tan2237/safe-edit-skill
codex plugin add safe-edit-skill@safe-edit
```

仓库根目录的 `.codex-plugin/plugin.json` 会加载 `skills/` 与
`.mcp.json`。插件启用后提供三个工具：

- `safe_edit_preflight`：检查 Python、临时目录、锁和目标目录能力。
- `safe_edit_stat`：一次检查一个或多个文件，返回 `editStrategy` 与 SHA-256。
- `safe_edit_transaction`：直接接收 `old`、`new`、`text` 和 `operations`，
  dry-run 先生成不可变 prepared plan；只有结果成功进入有界缓存时才返回一次性
  `transactionId`，有效期最长 10 分钟。

推荐一次批量 `stat`，随后一次批量 transaction。MCP 服务是常驻进程，
只导入一次编辑内核并只构建一次参数解析器；普通热路径不启动子进程（大型 fuzzy 查找除外），
不会把已结构化请求再次 JSON 解码，也不会产生 Base64 的约 33% 体积膨胀。
大型、相互独立的 exact batch 只有在能严格证明与有序逐操作语义等价时才会合并为
单次扫描；无法完成证明时会自动回退到常规逐操作路径。

结构化 transaction 示例：

```json
{
  "files": [
    {
      "file": "src/a.py",
      "action": "edit",
      "expectedSha256": "SHA256_FROM_SAFE_EDIT_STAT",
      "operations": [
        {"op": "edit", "old": "before", "new": "after", "expected_count": 1}
      ]
    }
  ],
  "dryRun": true
}
```

dry-run 会先生成不可变 prepared plan，并为每个文件返回精简 `diff` 与
`resultSha256`。只有计划成功进入有界缓存时，顶层才会返回一次性事务 ID；
缓存已满时会拒绝新的 dry-run admission，不会逐出尚未过期的已签发 token。
正式执行只需：

```json
{"transactionId": "tx_FROM_DRY_RUN"}
```

确认 ID 只可消费一次、有效期最长 10 分钟。确认时先取得协作锁，再统一重验
canonical path、parent/target identity 与原始哈希。缓存的是 prepared plan 时，
确认不会重跑解码、匹配或编辑计算；若计划超过 MCP prepared 保留阈值，则缓存
有界 JSON 请求，确认时会重新 prepare。也可以用 `dryRun: false` 直接提交完整请求。
成功结果中每个文件的 `sha256` 是落盘后的新 guard，后续迭代无需重新
`safe_edit_stat`。

结构化事务默认启用 `autoEolMatch`：先做精确匹配，仅在未找到时对多行
`old` 或上下文追加 EOL-only 回退，因此可处理 CRLF、LF、CR 与混合行尾，
同时避免把原本唯一的精确目标放宽成多匹配。计数不符时不会继续放宽；如需
严格 EOL 匹配可显式设为 `false`。

MCP 单次请求的 `maxBytes` 上限为 128 MiB；每次最多 128 个文件、每个文件
最多 256 个操作、合计最多 1,024 个操作，JSON 最大嵌套深度为 128。服务端
以 `2025-11-25` 为当前稳定协议版本，同时兼容 `2025-06-18` 和
`2024-11-05`；不宣称支持 `2025-03-26` 的 batch 过渡版本。

对于已经识别的工具，字段、类型、SHA-256、限额或执行状态错误会作为正常
`tools/call` 响应返回，并设置 `result.isError: true`。无效 JSON-RPC envelope、
无效 request id、未知工具名或非对象 `params`/`arguments` 属于协议层错误，
返回对应 JSON-RPC error；二者不应混为一类。

CLI 保留为未加载插件时的兼容回退。

## Claude Code、Cursor、OpenCode 与 VS Code

这些客户端都连接同一个 `safe-edit-mcp` stdio 进程；每个客户端或工作区
只启动一个常驻进程，工具调用不会重复启动 Python。统一安装器只合并
`safe-edit` 条目，不覆盖其它 MCP 配置；修改已有 JSON 前会创建
`.safe-edit.bak` 备份。

```bash
# 当前用户；VS Code 需要 code CLI
safe-edit-mcp install --client all --scope user

# 当前项目，适合提交团队配置
safe-edit-mcp install --client all --scope project --project-dir .

# 先查看将写入的路径和配置
safe-edit-mcp install --client cursor --scope user --dry-run --json
```

客户端配置位置：

| 客户端 | 用户级 | 项目级 |
|---|---|---|
| Claude Code | `~/.claude.json` | `.mcp.json` |
| Cursor | `~/.cursor/mcp.json` | `.cursor/mcp.json` |
| OpenCode | `~/.config/opencode/opencode.json` | `opencode.json` |
| VS Code | 通过 `code --add-mcp` | `.vscode/mcp.json` |

如果目标配置不是有效 JSON（例如含 JSONC 注释），安装器会拒绝重写并保持
原文件不变；此时使用 `--dry-run --json` 获取配置片段后手工合并。

## Recommended Workflow

插件工具可用时，先用 `safe_edit_preflight` 检查能力，再把所有相关文件
放进一次 `safe_edit_stat` 调用。CLI 回退流程如下：

```bash
python safe_edit.py preflight --file foo.cpp --json
python safe_edit.py stat --file foo.cpp --json
```

多个相关文件优先在一个进程内检查。请求可以是路径字符串或带编码选项的对象：

```json
{"files":["src/a.py",{"file":"src/b.py","encoding":"utf-8"}]}
```

```bash
python safe_edit.py stat-many --request-stdin --json
```

如果 `python` 和 `py -3` 都不可用，应优先使用宿主提供的 Python
运行时绝对路径；仍无法执行时，在修改任何文件前停止。

返回的 `editStrategy` 告诉你该用什么工具编辑这个文件：

| editStrategy | 含义 |
|---|---|
| `edit-tool` | 使用宿主提供的内置文件编辑工具即可（实际工具名称可能不同） |
| `safe-edit` | 必须用 safe-edit（文件有 BOM、非 UTF-8 编码、CRLF 行尾等） |

如果 `editStrategy` 是 `safe-edit`，后续对该文件的所有编辑都应使用 safe-edit。

## 基本用法

直接运行 Python 脚本即可。Windows 上如果 `python` 不在 `PATH`，可以用 `py -3` 替代。

```bash
python safe_edit.py preflight --json
python safe_edit.py stat --file path/to/file --json
python safe_edit.py stat-many --request-stdin --json
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

模糊比较会忽略每行开头和结尾的空白、CRLF/LF/CR 差异，以及文件末尾是否有换行；行内空白仍然有意义，不会被折叠。

`--fuzzy-workers auto`（默认）只为解码后占用约 8 MiB 以上且计算量足够的 fuzzy 查找启用低优先级多进程，最多使用 4 个进程并预留 2 个逻辑核；低基数重复内容和普通文件保持串行。使用 `--fuzzy-workers 1` 可强制串行，`2`–`8` 可显式设置进程上限。

**关键安全约束**：自动容错在输出中报告使用的匹配级别（`matchStrategy` 字段），不会静默降级。

## 结构化 JSON 输出

`--json` 模式下，成功和失败都输出结构化 JSON：

```bash
# 成功时包含 matchStrategy
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --auto-match --expected-count 1 --json

# 失败时输出错误类型、诊断和恢复建议
python safe_edit.py edit --file path/to/file --old "missing" --new "bar" --json
```

错误类型包括：`match_not_found`、`match_ambiguous`、`match_count_mismatch`、`hash_mismatch`、`encoding_error`、`file_error`、`lock_error`、`validation_error`、`format_error`、`unknown`。

### Agent Recovery Protocol

匹配失败时，JSON 错误输出包含完整的恢复协议字段，供 Agent 自动决策。以下以结构化事务 prepare 阶段的失败为例：

```json
{
  "ok": false,
  "error": {
    "type": "match_not_found",
    "message": "old text was not found"
  },
  "failedFile": {"index": 1, "file": "src/a.py"},
  "failedOperation": {
    "index": 7,
    "op": "edit",
    "targetFragment": "expected source fragment"
  },
  "operationIndex": 7,
  "failureReason": "indentation_difference",
  "failureClass": "RETRYABLE",
  "rootCause": "indentation_difference",
  "closestMatch": {"line": 42, "similarity": 0.91},
  "recommendedAction": {"type": "retry", "confidence": 0.9},
  "retryStrategy": {"flags": ["--ignore-indent"], "alternativeFlags": ["--auto-match"], "argumentsPatch": {"autoMatch": true}},
  "phase": "prepare",
  "failureStage": "target",
  "writeAttempted": false,
  "statRequired": false
}
```

| 字段 | 说明 |
|------|------|
| `failedFile` | 事务内失败文件的 1-based 序号和路径 |
| `failedOperation` | 失败操作的 1-based 序号、操作类型和有上限的目标片段 |
| `failureReason` | 面向调用方的直接失败原因；匹配错误时等同于 `rootCause` |
| `failureClass` | `RETRYABLE`（可自动重试）、`RE_READ_REQUIRED`（需重新读取文件）、`USER_INPUT`（需用户修正）、`FATAL`（不可恢复） |
| `rootCause` | 根因分类：`indentation_difference`、`line_ending_difference`、`whitespace_difference`、`context_mismatch`、`count_mismatch`、`content_not_found`、`multiple_matches`、`similar_content_exists` |
| `closestMatch` | 最接近匹配的位置、片段和相似度（0.0–1.0） |
| `recommendedAction` | 推荐的恢复动作（`retry`、`re_read_file`、`ask_user`、`stop`）及其置信度 |
| `retryStrategy` | 仅 `RETRYABLE` 时返回；`argumentsPatch` 是事务级参数，只能在所有 edit 均有 `expected_count` 时合并到一次 dry-run |
| `phase` / `failureStage` | 事务匹配失败时标识 prepare 阶段及 target/context_filter；后者再由 `contextField` 指出 before/after |
| `writeAttempted` | `false` 表示尚未进入写阶段 |
| `statRequired` | `false` 表示可复用原 `expectedSha256`，无需重新 stat |
| `contextField` / `contextFragment` | 上下文过滤失败时指出需要重新读取的上下文字段及有界片段 |
| `expectedCount` / `actualCount` | 计数不符时给出期望值与实际值；实际过多为 `multiple_matches`，实际不足为 `count_mismatch` |

`argumentsPatch` 可能放宽整个多文件事务。只有当每个 edit 都显式设置了
`expected_count` 时，才可复用原哈希把 patch 合并到 `dryRun: true` 请求；
检查预演后仅用返回的 `transactionId` 确认。缺少计数或预演不符合预期时应
重新读取，不能直接提交放宽后的请求。

### 过期哈希恢复

当 `stat`、`inspect`、`edit` 或 `transaction` 的 `expectedSha256` 与目标文件当前内容不一致时，错误类型为 `hash_mismatch`，并直接携带可用于重试的字段，无需重新运行 `stat`：

```json
{
  "ok": false,
  "error": {"type": "hash_mismatch", "message": "SHA-256 mismatch: ..."},
  "failureClass": "RETRYABLE",
  "rootCause": "stale_expected_sha256",
  "expectedSha256": "0000...",
  "actualSha256": "e49c...",
  "recommendedAction": {"type": "retry_with_actual_sha256", "confidence": 0.9},
  "retryStrategy": {"expectedSha256": "e49c..."}
}
```

`retryStrategy.expectedSha256` 就是文件当前的哈希，可直接作为下一次非删除请求的 `expectedSha256`。文件内容已经变化，重试前应重新核对编辑上下文。

有两个安全例外：

- `remove-file` 哈希失效时仍返回 `actualSha256`，但不会返回 `retryStrategy`；必须重新读取并确认变化后的文件，再决定是否删除。
- `create` 目标已存在时返回现有文件的 `actualSha256`（若能安全计算），但不会自动建议改成 `edit`；必须先检查既有文件，避免把“创建新文件”升级成“修改已有文件”。

此外，`stat`/`edit` 目标不存在时会返回 `recommendedAction: {"type": "create_file_if_intended"}`。

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

1. 插件工具可用时，直接调用 `safe_edit_stat` 和 `safe_edit_transaction`，传入原始结构化字符串。
2. CLI 回退且执行工具提供原生 stdin 时，使用 `--ops-stdin`、`--diff-input-stdin` 或 `--text-stdin`。
3. 载荷文件已经存在时，使用 `--ops-file`、`--old-file`、`--new-file` 或 `--text-file`。
4. 没有原生 stdin 或现有载荷文件时，使用 URL-safe UTF-8 Base64。
5. 仅对短且不含 shell 敏感字符的内容使用字面 `--old`、`--new`、`--text`。

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

多行、超过 100 个字符，或包含 `"`、`'`、`%`、`!`、反引号、反斜杠等字符时，优先直接通过 `safe_edit_transaction` 传递原始结构化正文。只有 CLI 回退才使用 `batch --ops-stdin`；无原生 stdin 时再使用 `batch --ops-base64`。完成后仍应重新读取，并按文件类型执行编译或测试。

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
（`--request-base64`）。已有文件必须提供本轮 `stat` 或 `stat-many` 返回的
`expectedSha256`；新文件仍拒绝覆盖并要求显式 `encoding` 和
`lineEnding`。

事务先生成不可变 prepared plan；提交阶段取得协作锁后统一重验 canonical path、
parent/target identity 和输入哈希，再直接写入计划字节，不重跑解码、匹配或编辑计算。
结构化 MCP 优先缓存该计划；超过 prepared 保留阈值时才缓存有界 JSON 请求并在
确认时重新 prepare。dry-run 未显式设置 `diff` 时返回最多 80 行、12,000 字符的
精简 diff；显式 `diff: true` 仍返回完整 diff。

每个输出先在已 pin 的父目录内写入随机隐藏 sibling stage，完成全部字节写入、文件
`fsync` 和内容复验后才进入发布步骤。编辑已有文件时，事务先用 no-replace 操作把
当前 basename claim 到随机 quarantine，复验 identity、SHA-256、mode、size 和
mtime，再用 no-replace 操作把完整 stage 安装为目标 basename；创建文件只会安装
已经完整复验的 stage。所有编辑的原始 quarantine 会保留到全部 planned mutation
完成 post-install 复验，且全部 pinned 父目录通过最终 validation sweep。两个
no-replace 步骤之间，目标 basename 可能短暂不存在。

rollback 同样先把事务输出 no-replace claim 到新的 quarantine，验证它仍是本事务
写入的 generation，才 no-replace 恢复原始 generation 或删除本事务创建的文件；
它不会覆盖已经出现的未知或外部目标。Linux 需要 `renameat2(RENAME_NOREPLACE)`，
macOS 需要 `renameatx_np(RENAME_EXCL)`，Windows 使用不允许覆盖的
`MoveFileExW`。平台或文件系统缺少所需 primitive 时会安全失败，而不会退回到
可能覆盖其它 generation 的实现。

对每个将要写入的文件，内存 journal entry（不是持久化 WAL）会在该文件首次 staging
syscall 前创建。它预分配随机 stage 和 rollback-quarantine basename；编辑操作还会
预分配 original-quarantine basename。每个可能改变文件或目录状态的 syscall 前，
journal 先进入对应的 `ATTEMPT_*` phase。调用结果不明、捕获到进程内 control-flow
exception 或后续复验失败时，恢复逻辑会重新探测相关端点，按 marker 盘点 stage、
target 和 quarantine，并按相反顺序回滚已确认写入。只有观测到的 generation 经
identity、SHA-256、mode、size 和 mtime 证明归属后，恢复逻辑才会尝试清理或恢复；
下述同权限竞态边界仍适用。

全部 planned mutation 完成 post-install 复验，且全部 pinned 父目录通过最终
validation sweep 后，原始 quarantine 才进入 `ATTEMPT_FINALIZE` 清理并同步父目录。
提交成功时的清理或目录同步问题会出现在 `cleanupWarnings`。如果 finalization 中断，
已删除、被替换或状态不明的原始 generation 不会被误报为已回滚；状态字段和
`rollbackErrors` 会按实际盘点结果报告 partial/uncertain outcome。

如果 publish、盘点、rollback 或 finalization 的结果无法证明，safe-edit 会优先保留
stage/quarantine，不会有意删除未经验证或 unknown 的对象。错误结果中凡涉及 recovery
artifact 的 `rollbackErrors` 条目都使用固定 label：`artifact basename=...; pinned
parent identity=(device=..., inode=..., file_type=...); best-effort path=...`。恢复以 pinned
parent 和 basename 为准；path 仅供定位，父目录被重新绑定后可能已过时。`written`、
`rolledBack`、`partialWrite` 和 `rollbackConflict` 用于判断真实结果。

每个预演文件返回计划值 `resultSha256`；每个成功落盘或无字节变化的文件返回
当前 `sha256`。字面编辑中 `old` 与 `new` 完全相同时，操作结果为
`skipped: true`、`reason: old_equals_new`。返回的 `atomicity` 为
`prevalidated-with-rollback`，`crashAtomic` 为 `false`；事务不保证进程崩溃、
断电、跨文件系统或跨文件的全局原子提交。

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

GitHub Actions 在 Windows、Linux、macOS 上分别运行 Python 3.9 与最新 Python，共 6 个组合；每个组合都会执行源码编译、完整测试、安装包、installed import、CLI preflight 和 stdio MCP initialize handshake，性能 smoke 仅在 3 个最新 Python 组合运行。

## 常用选项

| 选项 | 说明 |
| --- | --- |
| `safe_edit_preflight` | 常驻结构化工具：检查运行时、临时目录、锁和目标目录能力 |
| `safe_edit_stat` | 常驻结构化工具：批量检查文件并返回 SHA-256 |
| `safe_edit_transaction` | 常驻结构化工具：直接传递正文并执行受保护事务 |
| `preflight` | CLI 回退：检查运行时、载荷传输、临时目录、锁和目标目录能力 |
| `stat-many` | 在一个进程内检查多个文件并返回逐文件摘要和 SHA-256 |
| `transaction` | 结构化多文件预演、写入和失败回滚 |
| `create` | 受控创建不存在的任务成果文件；要求显式编码和行尾 |
| `remove-file` | 受控删除一个明确指定的普通文件；要求工作区根目录和 SHA-256 |
| `--workspace-root DIR` | `remove-file` 的强制路径边界 |
| `--expected-sha256 HASH` | 要求当前文件 SHA-256 与 `stat` 输出一致 |
| `--request-stdin` / `--request-file PATH` / `--request-base64 B64` | 读取 `stat-many` 或 transaction JSON |
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
| `--auto-eol-match` / `--no-auto-eol-match` | 开启或关闭基于目标文件行尾的多行兼容匹配；transaction 默认开启 |
| `--fuzzy` | 启用模糊匹配（需配合 `--auto-match`，相似度 ≥ 0.6；忽略逐行首尾空白、行尾格式和最终换行） |
| `--fuzzy-workers auto\|N` | fuzzy 进程上限；默认 `auto`，`1` 强制串行，`N` 为 2–8 |
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
- 事务的 no-replace publish 与 generation 复验会在普通并发写者修改目标 basename 时安全冲突，并保留无法证明归属的外部对象；协作锁只保证遵守同一锁协议的参与者互斥。
- POSIX 没有可移植的 unlink-by-inode CAS。随机内部 `.txn` 名称的清理不承诺抵御拥有相同目录写权限的 deliberate name hijack；不确定时选择保留 artifact 并报告恢复信息。
- 无法阻止通过已经打开的可写文件描述符修改 generation，也无法阻止经其它 hardlink 路径修改同一 inode；原子替换通常会生成新文件对象，不保证保留硬链接关系。
- 直接单文件 `edit`/`batch` 的 atomic replace 依赖协作锁，并不是严格的 basename CAS。需要最强并发检查与冲突安全回滚时应使用 `transaction` / `safe_edit_transaction`。
- 仅尽力保留普通权限位，不完整保留 ACL、扩展属性或创建时间。
- 多文件事务使用冲突安全的 best-effort rollback，并通过状态字段报告部分写入；`crashAtomic` 为 `false`，不保证进程崩溃、断电或跨文件系统时的全局原子性。
- v2 进程以全生命周期 kernel stripe 保证新版之间互斥；legacy marker 保留旧 namespace，但与 v1 混跑仍有旧协议无法消除的窄竞态窗口。
- Windows 锁依赖用户私有 TEMP 目录的 ACL；弱 ACL 或共享 TEMP 是已知安全边界。
- Windows 上无法像 Unix 一样 fsync 目录，因此断电级别保证受平台限制。

## 许可证

MIT
