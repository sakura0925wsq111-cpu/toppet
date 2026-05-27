import pathlib

fp = pathlib.Path("D:/toppet/_gen_tts.py")
body = fp.read_text(encoding="utf-8-sig")
# Fix: replace the outer triple-double-quotes with triple-single-quotes
body = body.replace('BODY = """', "BODY = '''")
# Find and fix the closing triple quotes - the last one before TARGET.write_text
body = body.replace("\n"""\n\nTARGET.write_text", "\n'''\n\nTARGET.write_text")
fp.write_text(body, encoding="utf-8")
print("Fixed")