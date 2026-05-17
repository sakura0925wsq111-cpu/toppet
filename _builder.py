# -*- coding: utf-8 -*-
"""\u751f\u6210\u6240\u6709\u65b0\u6a21\u5757\u6587\u4ef6"""
import pathlib

BASE = pathlib.Path(r"D:\toppet")

def w(rel_path, content):
    p = BASE / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    print(f"  OK: {rel_path}")

print("Creating utils layer...")
