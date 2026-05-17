# -*- coding: utf-8 -*-
"""
老勺子 (Laoshaozi) —— 桌面 AI 宠物 入口文件

启动方式：
    python laoshaozi.py

架构概览：
    laoshaozi.py          ← 入口（本文件，~30行）
    core/                  ← 核心逻辑（与UI无关）
        config.py          配置常量、API Key
        conversation.py    对话记忆
        client.py          AI API / 天气 / 时间
        pet_brain.py       交互中枢
    ui/                    ← PyQt6 桌面界面
        desktop_pet.py     宠物窗口、图片渲染、鼠标交互
        ai_worker.py       后台线程包装
    audio/                 ← 语音扩展（预留，未实现）
    systems/               ← 状态机、空闲调度、表情管理
    emotion_scheduler.py   ← 情绪调度器（兼容封装）

依赖：
    pip install -r requirements.txt
"""

from __future__ import annotations

import os
import sys

# =====================================================================
# 🚨 终极防崩溃补丁
# 必须在所有 PyQt6 导入语句之前设置
# 强制使用纯软件渲染，解决显卡驱动冲突导致的闪退问题
# =====================================================================
os.environ["QT_QUICK_BACKEND"] = "software"
os.environ["QT_OPENGL"] = "software"
os.environ["QSG_RHI_BACKEND"] = "software"
os.environ["QT_DEVICE_PIXEL_RATIO"] = "0"
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

from PyQt6.QtWidgets import QApplication

from core.config import PetConfig
from core.pet_brain import PetBrain
from ui.desktop_pet import DesktopPet


def main() -> None:
    """启动老勺子桌面宠物。"""
    app = QApplication(sys.argv)

    # ── 创建配置 ──
    config = PetConfig()

    # ── 创建大脑（纯逻辑层，与 UI 无关）──
    brain = PetBrain(config)

    # ── 创建桌面宠物（UI 层，负责显示和交互）──
    pet = DesktopPet(config, brain)
    pet.show()

    # ── 运行事件循环 ──
    try:
        sys.exit(app.exec())
    except Exception as e:
        print(f"程序崩溃了，错误是: {e}")
        input("按回车键关闭...")


if __name__ == "__main__":
    main()
