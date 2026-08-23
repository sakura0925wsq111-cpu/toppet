# 配置数据

## 角色包（推荐）

每个角色可以完整放在 `characters/<角色 ID>/` 中：

```text
characters/<角色 ID>/
├── character.json          # 人格、称呼、UI、TTS、表情映射
├── starter_memory.json     # 可选：首次运行时复制的初始记忆
├── voice_reference.wav     # 可选：VoxCPM2 参考音频
└── expressions/            # PNG/GIF 表情资源
```

新增或定制角色时，复制一个角色目录并修改 `character.json` 即可。程序启动时
会优先读取角色包；下面两份文件只作为旧项目和缺失角色包时的兼容回退。

- `character_profiles.json`：角色名称、人格、UI 文案、TTS 和记忆配置。
- `expression_mapping.json`：统一情绪到各角色表情资源的映射。

角色包中的 `expression_mapping` 会覆盖旧的全局映射。角色包中的
`starter_memory.json` 只在对应的运行时记忆文件不存在时使用，运行后产生的记忆
仍然写入 `logs/`，不会修改角色包。
