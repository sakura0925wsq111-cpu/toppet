# TopPet 主机服务

主机服务把电脑端的 Faster Whisper、`PetBrain`、长期记忆和角色 TTS 暴露给 ESP32。服务不启动桌面窗口，也不在设备端保存 API Key、聊天记录或永久录音。

## 启动

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File host_service\start.ps1
```

启动后先检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8765/api/v1/info
```

首次调用 ASR 时会加载本地 faster-whisper 模型；个人角色的 TTS 按角色配置优先调用 VoxCPM2，当前开发配置保留 EdgeTTS 兼容回退。

## HTTP 单次接口

`POST /api/v1/voice`

请求体支持两种形式：

- `audio/wav`：WAV 必须是 16kHz、16bit、单声道。
- `application/octet-stream`：裸 little-endian PCM，默认同样是 16kHz、16bit、单声道。

可选请求头：

```text
X-Top-Pet-Device-Id: toppet-evT-01
X-Top-Pet-Session-Id: <唯一会话 ID>
X-Top-Pet-Sample-Rate: 16000
X-Top-Pet-Channels: 1
X-Top-Pet-Bits: 16
```

返回 JSON 包含 `transcript`、`emotion`、`reply`、`audio_format` 和 `audio_base64`。单次 HTTP 适合电脑端诊断和联调；实体端优先使用 WebSocket。

## WebSocket 协议

连接：`ws://<主机 IP>:8765/ws`

客户端先发送：

```json
{"type":"hello","device_id":"top-pet-01"}
```

开始一轮录音：

```json
{"type":"audio_start","session_id":"abc123","sample_rate":16000,"channels":1,"bits_per_sample":16,"content_type":"application/octet-stream"}
```

随后发送一个或多个二进制 PCM 分片，最后发送：

```json
{"type":"audio_end"}
```

主机依次发送 JSON 状态事件：

```text
THINKING → transcript → emotion/reply → audio_ready → TALKING
```

收到 `audio_start` 事件后，主机以不超过 2048 字节的二进制分片发送 WAV，再发送 `audio_end` 并标记 `IDLE`。主机已把 PCM WAV 归一化为 16kHz、16bit、单声道，便于 ES8311 固定时钟播放；编码格式（例如 MP3）不会被设备端当前播放器处理。

取消当前会话：

```json
{"type":"cancel"}
```

HTTP 客户端也可以调用 `POST /api/v1/cancel/<session_id>`。共享角色记忆当前按单飞行队列串行处理，避免多个设备同时写入同一会话造成串话。

## 当前边界

服务协议和当前 ESP32 固件已经具备 Wi-Fi、PCM 上传、主机处理和 WAV 播放所需的联调入口。完整产品化仍需继续验证扬声器声学效果、异常断线恢复和多轮稳定性。
