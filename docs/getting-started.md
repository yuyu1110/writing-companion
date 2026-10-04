# 安装与使用

## 安装 Skill

将本仓库 `skills/writing-companion` 整个目录复制到以下位置之一：

- 个人使用：`~/.agents/skills/writing-companion`。
- 只在一个项目中使用：该项目的 `.agents/skills/writing-companion`。

如果已有同名 Skill，先比较内容并备份本机 `local-workspace.json`，不要直接覆盖。Codex 支持这些本地发现路径和符号链接；其他宿主的路径与脚本能力需要另外核对。见 [官方安装与发现说明](https://learn.chatgpt.com/docs/build-skills)。

也可请支持 Skill 安装的客户端从本 GitHub 仓库安装 `skills/writing-companion` 子目录。打开新对话后选择“写作伴侣”；CLI 或 IDE 可用 `$writing-companion` 明确调用。看不到时重新启动客户端再检查。

仅使用对话规则无需 Python。需要本地全文检索时，准备 Python 3.9+，且其 SQLite 支持 FTS5。脚本仅依赖 Python 标准库。

## 创建自己的工作区

将 Skill 中的 `assets/workspace-template` 复制到独立目录，例如自己的 `my-writing-workspace`。不要把私人资料直接写回模板。模板内作者档案、会话进度和目录均为空。

优先在项目指令或对话中指定该工作区。也可以将 `local-workspace.example.json` 复制成 Skill 内的 `local-workspace.json`，将 `default_workspace` 改为工作区绝对路径。Windows JSON 路径推荐用正斜杠。这个绑定文件不能提交到仓库。

`workspace.json` 的相对路径均以工作区本身为基准。对话中的工作区选择由 Skill 处理；运行脚本时推荐显式传入 `--workspace`，避免读到其他资料。

## 最小检索流程

以下命令从安装后的 Skill 目录执行，将 `WORKSPACE` 替换为自己的工作区路径：

```sh
python scripts/library_reader.py --workspace "WORKSPACE" build
python scripts/library_reader.py --workspace "WORKSPACE" search "回想" --kind literature
python scripts/library_reader.py --workspace "WORKSPACE" search --concept family --concept concealment
python scripts/library_reader.py --workspace "WORKSPACE" read --source SOURCE_ID --start 1 --end 8
python scripts/library_reader.py --workspace "WORKSPACE" notes "节奏"
```

空白模板可成功建空索引，但搜索不会凭空得到书籍。先按 [书库格式](library-format.md) 加入 Markdown、来源记录和目录条目，再运行 `build`。搜索结果带完整 `passage` 标识时，可用 `read --passage "完整标识"` 查看证据。

`search` 返回的是候选，不是可以直接引用的证据。`read` 校验文件 SHA-256，并返回原文位置。`output_truncated=true` 时需继续读取或增加 `--budget`，不能据半段话下结论。新资料或正文变化后再次 `build`。

繁简转换默认不启用。若持有可用的单字映射字典，可在工作区 `techniques/retrieval-config.json` 中设置 `normalization_dictionary`，指向工作区内 UTF-8 字典文件。每行格式为“繁体单字、Tab、简体候选”，多个候选以空格分开，采用首个候选。改变映射需重建派生索引，详见 [书库格式](library-format.md)。

## 日常调用示例

- “读这篇散文，先告诉我你理解的核心感受，再质疑最影响它的一处写法。”
- “从我的整个适用书库找几处相似的人物告别场景，核对语境后比较。”
- “这篇小说停在这里了，先核对已确认设定，和我一起决定下一场戏。”
- “围绕这次发现的问题带我练一次；先让我试，再解释。”
- “把这篇作品归档，保留原稿，并将这个改写标成尚未采纳的候选。”

普通咨询不会自动把稿件存为长期作品。保存、归档和版本采纳依据作者的实际请求执行。
