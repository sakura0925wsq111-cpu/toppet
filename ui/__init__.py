# -*- coding: utf-8 -*-
"""
UI 模块 - PyQt6 桌面界面层

本模块包含所有与桌面显示相关的代码。
如果未来移植到其他平台（Web、嵌入式屏幕），只需替换本层即可，
core 层无需修改。

子模块说明：
    ai_worker.py   - AIWorker(QThread)，后台线程包装 core.AIClient
    desktop_pet.py - DesktopPet(QWidget)，主窗口、图片渲染、鼠标交互
"""
