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

当当前角色使用 VoxCPM2 时，主机服务会自动检查 `127.0.0.1:9890`，在需要时
启动并预热本地 VoxCPM2 服务，确认声音服务就绪后再接受硬件请求。主机退出时只会
关闭由自己启动的 VoxCPM2 进程；如果使用的是已经手动启动的服务，则不会关闭它。

如需调试或改用外部服务，可以关闭自动启动：

```powershell
$env:TOPPET_VOXCPM_AUTOSTART = "0"
powershell -ExecutionPolicy Bypass -File host_service\start.ps1
```

默认最多等待 180 秒，可以通过 `TOPPET_VOXCPM_STARTUP_TIMEOUT` 调整。

主机启动时会常驻并预热 CPU int8 的 faster-whisper base 模型；个人角色的 TTS 按角色配置优先调用 VoxCPM2，当前开发配置保留 EdgeTTS 兼容回退。

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

## WebSocket v2 流式协议

连接地址仍为 `ws://<主机 IP>:8765/ws`。设备启动后建立一次连接，在多轮会话间
保持连接；断线后每秒重试，并在成功后重新发送 `hello`。协商消息如下：

```json
{
  "type": "hello",
  "protocol": "toppet-host/v2",
  "device_id": "top-pet-01",
  "audio": {
    "format": "pcm_s16le",
    "sample_rate": 16000,
    "channels": 1,
    "bits_per_sample": 16,
    "frame_ms": 20
  }
}
```

主机返回 `hello_ack`，其中 `protocol` 必须为 `toppet-host/v2`。按下 GPIO18 后设备
立即发送：

```json
{"type":"listen_start","session_id":"设备生成的唯一 ID"}
```

每个麦克风帧由一条元数据事件和紧随其后的一个 WebSocket 二进制帧组成。二进制帧
本身恰好是 640 字节，即 320 个 little-endian int16 样本：

```json
{"type":"mic_frame","session_id":"同一 ID","sequence":0,"bytes":640}
```

WebSocket 保证元数据和二进制帧的顺序。主机检查 `session_id`、长度及递增
`sequence`；缺帧会发出 `PCM_SEQUENCE_MISMATCH` 并插入等长静音，重复/倒序帧会
丢弃。松开按键后，在最后一个二进制帧之后发送：

```json
{"type":"listen_stop","session_id":"同一 ID"}
```

主机不等待完整 LLM 回复或完整 TTS。典型下行顺序为：

```text
stt_final
emotion
tts_start
tts_sentence_start(sequence)
subtitle
tts_audio(sequence, bytes) → 紧随一个裸 PCM 二进制帧（可重复多次）
tts_sentence_end(start_sequence, end_sequence)
...后续句子...
reply
tts_stop
```

`tts_audio` 的 PCM 固定为 16kHz、16bit、单声道，没有 WAV 头。设备在收到首批 PCM
后预缓冲 120ms 即开始播放，不能等待整句。`tts_sentence_start.sequence` 是该句首个
音频块的全会话序号；设备用它在播放游标真正进入该句时切换字幕，避免提前显示未来
句子。`subtitle` 仍是主机生成的 480×140 一位蒙版，经 zero/literal RLE 和 Base64
压缩；角色区继续按 420×340 局部刷新。

所有会话事件都带 `session_id`。设备和主机均只接收当前会话的字幕、表情和音频，
因此断线残留或被打断会话的迟到帧不会进入新会话。打断消息为：

```json
{"type":"abort","session_id":"当前 ID"}
```

设备先本地清空播放环形缓冲并静音，再通知主机；主机协作取消 LLM/TTS 并返回
`cancelled`。`error` 包含 `code`、`message` 和对应 `session_id`。空 ASR 使用
`INVALID_AUDIO_OR_TRANSCRIPT`；服务内部错误使用 `HOST_PIPELINE_ERROR`。连接空闲时
设备可发送 `ping`，主机返回 `pong`。

## v1 兼容

未声明 `protocol: toppet-host/v2` 的连接继续走 `toppet-host/v1`：`audio_start` 后
上传一个或多个 PCM 二进制块，以 `audio_end` 结束；主机完成整轮处理后发送
`audio_start`、完整 WAV 二进制分片和 `audio_end`。v1 是诊断/回退路径，不具备首句
流式延迟优势。HTTP `POST /api/v1/voice` 和 `POST /api/v1/cancel/<session_id>` 同样
保留。

## 延迟日志与查询

主机所有阶段使用单调时钟记录 `session_id`、`device_id`、`duration_ms`、序号和缓冲
水位。查询原始记录及 P50/P95：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/v2/latency
```

设备通过串口输出同格式的 `[LATENCY]` 行，包括按键按下/松开、首/末麦克风帧、首个
TTS 帧、首个 I2S 写入、欠载和播放完成。两端均不使用墙上时钟计算单机阶段耗时。
