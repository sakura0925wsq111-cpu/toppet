# 老勺子 VoxCPM2 音色

老勺子的 `personal` 角色使用本机 VoxCPM2 零样本克隆。桌宠启动时会立即使用
EdgeTTS 云希音色，随后在后台加载并预热 VoxCPM2；服务就绪后自动切换。单次本地
推理失败或超时会回退到 EdgeTTS。`shinchan` 角色继续使用原有 EdgeTTS。

## 启动

正常使用只需运行：

```powershell
Set-Location D:\toppet
& D:\toppet\.venv\Scripts\python.exe D:\toppet\laoshaozi.py
```

桌宠会自动管理本地服务，无须先手动启动。如果需要单独调试服务：

```powershell
& D:\VoxCPM2\.venv\Scripts\python.exe `
  D:\VoxCPM2\service\voxcpm_server.py `
  --model-path D:\VoxCPM2\models\models\OpenBMB--VoxCPM2\snapshots\master `
  --reference-path D:\toppet\assets\audio\ref_normal.wav `
  --prompt-text "今天真的有点忙，我本来以为很快就能弄完。" `
  --host 127.0.0.1 --port 9890 --steps 4
```

健康检查地址是 `http://127.0.0.1:9890/health`。服务只允许绑定本机地址。

## 验证

```powershell
Set-Location D:\toppet
& D:\toppet\.venv\Scripts\python.exe -m pytest -q

$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
& D:\toppet\.venv\Scripts\python.exe `
  D:\toppet\tests\smoke_voxcpm_e2e.py `
  --output D:\VoxCPM2\outputs\laoshaozi_final\laoshaozi_toppet_e2e.wav
```

训练数据、模型、基准音频和 checkpoint 必须留在 `D:\VoxCPM2`，不要放进 toppet
仓库。
