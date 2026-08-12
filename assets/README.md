# 静态与本地资源

- `characters/`：按角色 ID 分组的 PNG/GIF 表情资源。
- `audio/`：本地语音服务使用的参考音频。
- `whisper_models/`：Faster Whisper 模型缓存。

运行时通过 `common/paths.py` 定位这些目录，不依赖启动时的工作目录。
