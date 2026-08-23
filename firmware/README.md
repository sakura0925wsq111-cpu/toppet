# TopPet ESP32-S3 固件

目标硬件：微雪 `ESP32-S3-Touch-AMOLED-2.16`，480×480 CO5300 AMOLED、
16MB Flash、8MB OPI PSRAM。
主板型号中的 `Touch` 仅表示硬件自带电容触摸；TopPet 产品不启用触摸交互，屏幕只负责显示，用户输入由实体按键完成。

当前固件会把 `characters/personal/expressions` 中的 20 个 GIF 表情全部转换为板载资源。
待机时保持当前桌宠表情，不再自动轮播；主机回复会驱动情绪表情，播放结束回到 `NEUTRAL`。
角色显示在屏幕下方，顶部显示带圆角和尾巴的白色中文对话气泡。
动画采用 RGB565 调色板/RLE 编码，存储在 LittleFS 分区中。

## 构建并烧录

开发板连接到 USB 后，在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File firmware\build_and_upload.ps1 -Port COM5
```

只编译、不烧录：

```powershell
powershell -ExecutionPolicy Bypass -File firmware\build_and_upload.ps1 -CompileOnly
```

## Wi-Fi 与主机地址

`net_config.h` 只作为首次启动默认值。设备会优先读取 NVS 中保存的网络配置；
如果没有配置或连接失败，会自动开启配网热点 `TopPet-Setup-xxxx`，密码为
`TopPetSetup`，手机连接后访问 `http://192.168.4.1/` 完成配置。

首次联调也可以编辑 `firmware/toppet_pet/net_config.h`：

```cpp
#define TOPPET_WIFI_SSID "你的 2.4GHz Wi-Fi"
#define TOPPET_WIFI_PASSWORD "Wi-Fi 密码"
#define TOPPET_HOST "运行主机服务的电脑局域网 IP"
#define TOPPET_HOST_PORT 8765
```

电脑端先启动：

```powershell
powershell -ExecutionPolicy Bypass -File host_service\start.ps1
```

当前固件会在松开 GPIO18 后通过 WebSocket `/ws` 上传 16kHz/16bit/单声道
PCM，并接收主机归一化后的 WAV，通过 ES8311/GPIO8 播放，GPIO46 控制功放。

固件使用以下关键配置：

- Arduino-ESP32 `3.3.10`
- GFX Library for Arduino `1.6.4`
- ESP32S3 Dev Module
- 16MB Flash
- 8MB OPI PSRAM
- 自定义分区：2MB 应用程序 + 约 14MB LittleFS 表情资源
- USB CDC On Boot

## 出厂固件恢复

官方恢复镜像保存在忽略提交的本地产物目录：

`artifacts/hardware-backups/ESP32-S3-Touch-AMOLED-2.16-official-factory-recovery-260318.bin`

刷写自定义固件会覆盖当前出厂应用，但不会损伤硬件。需要恢复时使用官方镜像在地址
`0x0` 重新烧录。
