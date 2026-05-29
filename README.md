# safe-edit skill

通用的安全文本编辑 Agent Skill。它用一个跨平台 Python 脚本检查和编辑已有文本文件，并尽量保留原文件的编码、BOM、行尾格式、普通权限和写入完整性。

适合 Agent 修改源代码、配置文件、中文项目文件、MSVC/Windows 项目文件，以及任何不希望被 `cat`、`sed`、`Set-Content` 或临时脚本弄乱编码和 Git diff 的场景。

## 特性

- 单文件 Python 标准库实现，Windows/Linux/macOS 通用。
- `inspect` 只检查不写入，可输出编码、BOM、行尾统计、文件大小、行数、NUL 字符和权限位。
- `stat` 简洁摘要，只显示编码、行尾、大小、行数，适合 AI Agent 快速查看。
- `convert` 显式转换编码、行尾、最终换行，或清理尾随空白；普通编辑默认仍保留原格式。
- 自动检测并保留 `utf-8`、`utf-8-bom`、`gbk`、UTF-16 BOM，以及清晰 NUL 模式下的无 BOM UTF-16。
- 支持手动指定 `shift-jis`、`big5`、`latin-1`、`utf-16-le`、`utf-16-be`。
- 自动检测并保留 CRLF、LF、CR 行尾风格。
- 支持字面量替换、显式正则替换、插入行、文件头追加、文件尾追加、删除行、替换行范围、删除行范围。
- 支持 `--old-file`、`--new-file`、`--text-file`、stdin 等方式传入大段/多行内容。
- 支持 `--dry-run --diff` 预览、`--expected-count` 防误匹配、`--backup` 备份、JSON batch 一次读写多步编辑。
- **结构化 JSON 错误输出**：`--json` 模式下错误也输出 JSON，包含错误类型分类、建议和附近内容片段。
- **匹配级别报告**：成功时在 operations 中报告 `matchStrategy`（exact、ignore-eol、ignore-indent、fuzzy 等）。
- **自动容错匹配**：`--auto-match` 精确匹配失败后自动尝试宽松匹配（ignore-eol → ignore-indent → normalize-whitespace），`--fuzzy` 启用模糊匹配。
- **上下文消歧**：`--context-before` / `--context-after` 辅助多匹配消歧。
- **SEARCH/REPLACE 输入格式**：`--diff-input` / `--diff-input-file` 支持 Agent 友好的 diff 格式输入。
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

## 基本用法

直接运行 Python 脚本即可。Windows 上如果 `python` 不在 `PATH`，可以用 `py -3` 替代。

```bash
python safe_edit.py inspect --file path/to/file --json
python safe_edit.py stat --file path/to/file
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

# 失败时输出错误类型、建议和附近内容
python safe_edit.py edit --file path/to/file --old "missing" --new "bar" --json
```

错误类型包括：`match_not_found`、`match_ambiguous`、`match_count_mismatch`、`encoding_error`、`file_error`、`lock_error`、`validation_error`、`format_error`、`unknown`。

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

## Stdin 和跨平台传参

不同 shell 对特殊字符的处理不同，safe-edit 提供多种传参方式避免转义问题。

### Stdin 方式

通过管道传入内容，避免 shell 解析特殊字符：

**PowerShell (Windows):**
```powershell
# 避免 % 符号问题
"foo bar" | py -3 safe_edit.py edit --file a.cpp --old-stdin --new "new text"

# 从文件读取
Get-Content old.txt | py -3 safe_edit.py edit --file a.cpp --old-stdin --new-file new.txt
```

**CMD (Windows):**
```cmd
type old.txt | py -3 safe_edit.py edit --file a.cpp --old-stdin --new-file new.txt
```

**Bash (Linux/macOS):**
```bash
cat old.txt | python3 safe_edit.py edit --file a.cpp --old-stdin --new-file new.txt
```

### 文件方式

多行或大段内容推荐用文件传参：

```bash
python safe_edit.py edit --file a.cpp --old-file old.txt --new-file new.txt
python safe_edit.py insert --file a.cpp --line 5 --text-file block.txt
```

### 特殊字符问题

| Shell | 问题字符 | 示例 | 解决方案 |
|-------|---------|------|---------|
| PowerShell | `` ` `` `$` `%` | `"foo %VAR%"` | 用 stdin 或文件 |
| CMD | `%` `^` | `%PATH%` | 用 stdin 或文件 |
| Bash | `$` `` ` `` `\` | `$HOME` | 用单引号或文件 |

### PowerShell 编码问题

如果中文乱码，设置输出编码：

```powershell
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Get-Content old.txt | py -3 safe_edit.py edit --file a.cpp --old-stdin --new-file new.txt
```
## 多行内容

命令行参数不适合塞大段代码。推荐用文件或 stdin：

```bash
python safe_edit.py edit --file a.cpp --old-file old.txt --new-file new.txt
python safe_edit.py insert --file a.cpp --line 5 --text-file block.txt
python safe_edit.py prepend --file a.cpp --text-file header.txt
python safe_edit.py append --file a.cpp --text-file footer.txt
python safe_edit.py regex --file a.cpp --pattern-file pattern.txt --replacement-file replacement.txt
```

参数文件默认按 UTF-8 读取；需要时使用 `--arg-encoding` 覆盖。

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
python safe_edit.py batch --file path/to/file --ops-file ops.json
```

`*_file` 相对路径会按 `ops.json` 所在目录解析。

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
- Windows 上无法像 Unix 一样 fsync 目录，因此断电级别保证受平台限制。

## 许可证

MIT
