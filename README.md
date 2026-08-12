# toppet

一个基于 PyQt6 的桌面 AI 宠物。项目将对话、角色表情、语音识别与语音合成拆成独立层，入口保持为 `laoshaozi.py`。

## 快速开始

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python laoshaozi.py
```

聊天 API Key 优先从环境变量 `DEEPSEEK_API_KEY` 读取，也可以放在项目根目录的 `api_key.txt` 中；该文件不会被 Git 跟踪。

运行测试：

```powershell
python -m pytest
```

## 目录结构

```text
toppet/
├── laoshaozi.py          # 应用入口
├── core/                 # 对话、AI 客户端、记忆和应用编排
├── systems/              # 情绪、状态、角色映射和本地服务生命周期
├── audio/                # ASR、TTS、录音和播放
├── ui/                   # PyQt6 界面与后台工作线程
├── common/               # 无框架依赖的共享基础设施
├── config/               # 角色档案和表情映射数据
├── assets/               # 角色、参考音频和本地模型资源
├── tests/                # 自动化测试与显式运行的 smoke 脚本
├── tools/                # 可重复使用的开发工具
├── docs/                 # 架构、语音和硬件文档
├── artifacts/            # 生成帧、调试图和旧补丁（不进 Git）
└── logs/                 # 运行日志、PID 和本地记忆（不进 Git）
```

详细的模块边界和新增功能放置规则见 [docs/architecture.md](docs/architecture.md)。
