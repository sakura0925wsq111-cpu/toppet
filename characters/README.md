# 角色包

这里的每个子目录都是一个可以直接加载的角色包。目录名通常与
`character.json` 中的 `id` 相同。

```text
characters/<id>/
├── character.json
├── starter_memory.json     # 可选
├── voice_reference.wav     # 可选
└── expressions/
```

复制 `personal` 目录即可制作一个新角色。修改 `character.json` 中的
`display_name`、`persona`、`user_address`、`ui`、`tts` 和
`expression_mapping`，再替换 `expressions/` 中的 PNG/GIF 文件。

`starter_memory.json` 只会在第一次运行时复制到运行时记忆文件。运行中的
长期记忆保存在项目的 `logs/` 目录，不会写回角色包。
