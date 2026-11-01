# 共同来源与维护

公开仓库以 `skills/writing-companion` 为规则来源。`references/` 保存共享行为，`assets/gpt/` 保存对话环境适配和说明，`assets/record-templates/` 保存按需记录表单；正文和来源指纹属于使用者的私人工作区，不放进模板。

从仓库根目录运行：

```sh
python maintenance/sync_writing_companion.py --self-test
python maintenance/sync_writing_companion.py --write
python maintenance/sync_writing_companion.py --check
```

导出器以显式清单生成 `gpt/写作伴侣-GPT对话完整版.md` 和忽略提交的 `dist/skills/writing-companion`。默认只检查；`--write` 才写入，并回读核对。README、设计文档和变更记录由维护者更新，不会被导出器覆盖。

共享规则修改后直接生成 GPT 正文。环境专属的本地工具段使用命名导出边界，并在 GPT 适配文件中保留对应能力与限制；不要手工修改生成的 GPT 作为另一份规则源。新增资源先加入 `maintenance/export-manifest.json`；私有路径不因被列入清单就获准导出。

普通写作只加载入口和当前任务需要的参考，模板按需使用；维护检查及模型试运行不是每轮辅导流程，也不会自动建立课程、计分或作者偏好。

“深问构思”借鉴 [Matt Pocock 的 grill-me](https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md) 及其 [grilling 核心](https://github.com/mattpocock/skills/blob/main/skills/productivity/grilling/SKILL.md)（2026-11-02 实际读取）。按写作需求重述为先厘清依赖、逐题检验与适时收束，保留一次一问；没有打包或安装上游代码。
