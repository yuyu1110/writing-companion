# 自建书库的最小格式

所有路径相对于包含 `workspace.json` 的工作区。正式资料放在自己的工作区，仓库中的模板保持空白。书籍正文统一为 UTF-8 Markdown，原文件可一并保留在私人目录。

## 一个来源

```text
my-writing-workspace/
  workspace.json
  catalog.json
  library/example/
    text.md
    record.md
  techniques/
    retrieval-config.json
    reading-cards.json
```

在 `catalog.json` 的 `sources` 数组中加入如下条目；保留其他三组数组：

```json
{
  "id": "example",
  "title": "示例材料",
  "authors": ["实际作者或未知"],
  "record_path": "library/example/record.md",
  "text_path": "library/example/text.md",
  "coverage": "实际收录范围，例如第一章节选",
  "edition_notes": "实际版本、译者与已知差异"
}
```

`record.md` 记录 ID、题名、作者、版本/译者、来源、原文件位置、转换方式、覆盖范围、已核验范围和缺陷。没有纸本页码不补造；引用使用章节和实际 Markdown 行号。

同一作品的不同译本或不同正文使用不同来源 ID；书目需求与实际可用资料分开。一个 EPUB 合集也不自动等于多本独立来源。转换后的章节顺序、段落和图中文字需要抽查；本发布包没有通用 EPUB/PDF 转换器，可由宿主工具完成后登记。

## 类型与概念词

`techniques/retrieval-config.json` 中的 `guide_source_ids` 放写作指导来源的 ID。其余来源在脚本中归入 `literature`，该名称包含诗歌、散文等，不能视为已经核验的小说分类。

`concepts` 是人工词语线索。`--concept` 读取相应 `terms`；多个概念之间使用 AND，一个概念内使用 OR。这些词仅用于发现候选，不代表自动识别文学技法。

可选 `normalization_dictionary` 必须指向工作区内的 UTF-8 单字映射文件。映射在首次构建时写入索引；若增删或更改字典，先备份或移走 `library/retrieval.sqlite3`，再 `build`，保留原文与目录不动。`--force` 只重建正文片段，不更新已经保存的映射。无字典时繁简分开匹配，可手动把两种写法放入 `--group`。

## 阅读卡

`techniques/reading-cards.json` 以 `cards` 数组导航独立 Markdown 卡片：

```json
{
  "id": "sample-card",
  "title": "某个具体问题的阅读卡",
  "kind": "literature",
  "tags": ["节奏"],
  "summary": "基于已经核验的局部材料的分析",
  "path": "techniques/sample-card.md",
  "evidence": [{
    "source": "example",
    "sha256": "这里填写 read 返回的完整 source_sha256",
    "lines": [1, 8]
  }]
}
```

这是字段示意，不可直接当成有效证据。必须先读取真实原文，再填写真实哈希和行段。`kind` 可为 `guide`、`fiction` 或 `literature`。卡片正文注明书名、版本、读到的范围、必要短引、方法解释、适用条件与局限；不要替未读作品生成整书总结。

## 构建与核验

运行 `build` 将已登记正文写入可重建的索引。`search` 通过文件大小和修改时间排除常见的过期候选；`read` 和阅读卡核验还比较原文件 SHA-256。结果中若有 `stale_sources` 或 `unindexed_count`，按实际情况更新索引或补齐来源后再引用。

索引与记录只反映明确完成的步骤，不能证明全书已读。数据可以留在本机，但 AI 客户端实际读取的片段会进入相应对话上下文。分享资料和上传仓库前需自行确定内容与版本的使用范围。
