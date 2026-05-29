# safe-edit skill

[![skills.sh](https://skills.sh/b/Tan2237/safe-edit-skill)](https://skills.sh/Tan2237/safe-edit-skill)

安全编辑文件，自动保留编码和行尾格式。跨平台 Python 单脚本实现。

## 功能

- 自动检测文件编码（UTF-8/GBK/UTF-16）
- 自动检测行尾格式（CRLF/LF/CR）
- 原子写入，权限保留
- 支持 `--dry-run` 预览
- 支持 `--backup` 备份
- 支持 `--expected-count` 验证匹配次数

## 安装

```bash
npx skills add Tan2237/safe-edit-skill
```

### 全局安装

```bash
npx skills add Tan2237/safe-edit-skill -g
```

### 指定 Agent

```bash
npx skills add Tan2237/safe-edit-skill -a opencode
npx skills add Tan2237/safe-edit-skill -a claude-code
```

## 使用

```bash
script="$HOME/.config/opencode/skills/safe-edit/safe_edit.py"

# 替换文本
python "$script" edit --file path/to/file --old "foo" --new "bar"

# 替换并验证匹配次数
python "$script" edit --file path/to/file --old "foo" --new "bar" --expected-count 1

# 插入行（在第 10 行之前）
python "$script" insert --file path/to/file --line 10 --text "new line"

# 删除行
python "$script" delete --file path/to/file --line 10

# 预览（不写入）
python "$script" edit --file path/to/file --old "foo" --new "bar" --dry-run

# 备份原文件
python "$script" edit --file path/to/file --old "foo" --new "bar" --backup
```

Windows 下也可使用 `py -3` 代替 `python`。

## 命令说明

| 命令 | 参数 | 说明 |
|------|------|------|
| `edit` | `--file --old --new` | 替换文本（字面匹配，非正则） |
| `insert` | `--file --line --text` | 在指定行号之前插入新行 |
| `delete` | `--file --line` | 删除指定行号 |

## 高级选项

| 选项 | 说明 |
|------|------|
| `--dry-run` | 预览模式，不写入文件 |
| `--backup` | 创建时间戳备份文件 |
| `--expected-count N` | 验证匹配次数，不匹配则失败 |
| `--first` | 只替换第一个匹配 |
| `--no-op-ok` | 允许未找到匹配 |
| `--encoding` | 强制指定编码 |
| `--json` | 输出 JSON 格式结果 |

## 支持的编码

| 编码 | BOM | 说明 |
|------|-----|------|
| utf-8 | 无 | 默认，无 BOM 的 UTF-8 |
| utf-8-bom | EF BB BF | 带 BOM 的 UTF-8，MSVC 推荐 |
| gbk | 无 | 简体中文 |
| utf-16-le | FF FE | 小端 UTF-16 |
| utf-16-be | FE FF | 大端 UTF-16 |

## 支持的行尾格式

| 格式 | 字节 | 常见系统 |
|------|------|----------|
| lf | 0A | Unix/Linux/macOS |
| crlf | 0D 0A | Windows |
| cr | 0D | 旧版 Mac |

## 适用场景

- 中文源代码文件（C++/C#/Java/Python）
- MSVC 项目的源文件
- 需要保留 BOM 的 UTF-8 文件
- Windows 和 Unix 混合开发环境

## 许可证

MIT
