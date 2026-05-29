# safe-edit skill

通用的安全文本编辑 Agent Skill。它用一个跨平台 Python 脚本检查和编辑已有文本文件，并尽量保留原文件的编码、BOM、行尾格式、普通权限和写入完整性。

适合 Agent 修改源代码、配置文件、中文项目文件、MSVC/Windows 项目文件，以及任何不希望被 `cat`、`sed`、`Set-Content` 或临时脚本弄乱编码和 Git diff 的场景。

## 特性

- 单文件 Python 标准库实现，Windows/Linux/macOS 通用。
- `inspect` 只检查不写入，可输出编码、BOM、行尾统计、文件大小、行数、NUL 字符和权限位。
- 自动检测并保留 `utf-8`、`utf-8-bom`、`gbk`、UTF-16 BOM，以及清晰 NUL 模式下的无 BOM UTF-16。
- 支持手动指定 `shift-jis`、`big5`、`latin-1`、`utf-16-le`、`utf-16-be`。
- 自动检测并保留 CRLF、LF、CR 行尾风格。
- 支持字面量替换、显式正则替换、插入行、文件头追加、文件尾追加、删除行、替换行范围、删除行范围。
- 支持 `--old-file`、`--new-file`、`--text-file`、stdin 等方式传入大段/多行内容。
- 支持 `--dry-run --diff` 预览、`--expected-count` 防误匹配、`--backup` 备份、JSON batch 一次读写多步编辑。
- 同目录临时文件写入、原子替换、写后字节校验，并带有协作锁以降低并发写入风险。
- 如果变换后的字节和原文件完全一致，默认跳过写入，避免无意义的 mtime 和 Git 状态变化。
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
python safe_edit.py edit --file path/to/file --old "foo" --new "bar" --expected-count 1
python safe_edit.py regex --file path/to/file --pattern "foo\\d+" --replacement "bar" --expected-count 1
python safe_edit.py insert --file path/to/file --line 10 --text "new line"
python safe_edit.py prepend --file path/to/file --text-file header.txt
python safe_edit.py append --file path/to/file --text-file footer.txt
python safe_edit.py delete --file path/to/file --line 10
python safe_edit.py replace-lines --file path/to/file --start 10 --end 20 --text-file block.txt
python safe_edit.py delete-lines --file path/to/file --start 10 --end 20
python safe_edit.py batch --file path/to/file --ops-file ops.json
```

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
| `--expected-count N` | 要求匹配次数恰好为 `N` |
| `--first` | 只替换第一个匹配 |
| `--count N` | 正则替换数量限制，`0` 表示全部 |
| `--no-op-ok` | 允许没有匹配 |
| `--dry-run` | 只验证和预览，不写入 |
| `--force-write` | 即使输出字节相同也强制写入 |
| `--diff --context N` | 输出 unified diff |
| `--backup` | 写入前创建时间戳备份 |
| `--json` | 输出机器可读状态 |
| `--follow-symlink` | 编辑符号链接目标 |
| `--max-bytes N` | 覆盖默认 50 MiB 文件大小限制 |
| `--lock-timeout N` | 等待 safe-edit 协作锁，默认 10 秒 |
| `--no-lock` | 跳过协作锁 |

## 边界

- 不适合二进制文件和复杂结构化文件格式。
- 原子替换通常会生成新的文件对象；不保证保留硬链接关系。
- 仅尽力保留普通权限位，不完整保留 ACL、扩展属性或创建时间。
- Windows 上无法像 Unix 一样 fsync 目录，因此断电级别保证受平台限制。

## 许可证

MIT
