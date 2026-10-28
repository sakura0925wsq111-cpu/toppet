"""Small deterministic routing policy, not a replacement chatbot."""
from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DialogueScene:
    name: str
    guidance: str
    max_tokens: int = 180


def scene_for(text: str) -> DialogueScene:
    if re.fullmatch(r"(?:你)?(?:先|请|现在)?(?:安静(?:一会儿?|一下)?|别说话|不要说话|别打扰我|不要打扰我)[吧了。！!，,\s]*", text.strip()):
        return DialogueScene("quiet", "确认一次后保持安静；不要追问。", 40)
    if re.search(r"难过|委屈|想哭|伤心|没(?:有)?通过|失败|不开心|焦虑|崩溃", text):
        return DialogueScene("distress", "先接住这件具体的难受。先不讲大道理，不笑、不庆祝；只在合适时轻问一句。", 200)
    if re.search(r"生气|讨厌|烦死|不想理|不喜欢你", text):
        return DialogueScene("anger", "不反呛、不卖萌化解，先弄清对方在气什么，不擅自认定自己是过错方。", 160)
    if re.search(r"你记得|还记得|我们.*(?:第一次|那次|一起)|上次我们", text):
        return DialogueScene("shared_recall", "只引用有来源的共同经历；信息不足就坦白说没有可靠记录，请对方给提示。", 180)
    if re.search(r"成功|通过了|好消息|太开心|终于.*(?:完成|搞定)", text):
        return DialogueScene("joy", "跟上她的开心，回应具体好消息；别把庆祝写成客服夸奖。", 140)
    if re.search(r"吐槽|烦|加班|抱怨|破事|累", text):
        return DialogueScene("complaint", "先站在对方这边接住吐槽。没被问建议时，不急着安排解决步骤。", 180)
    if re.search(r"笨蛋|人机|吹牛|菜鸡|调侃|逗你", text):
        return DialogueScene("teasing", "轻轻接梗，只用资料允许的玩笑方式；不贬低用户、不突然升级亲密程度。", 120)
    if re.fullmatch(r"你好|嗨|哈喽|在吗|早(?:上好)?|晚好", text.strip("。！？! ?")):
        return DialogueScene("greeting", "一句简短回应就够，不介绍功能，不连问三个问题。", 90)
    return DialogueScene("conversation", "根据上下文回应；允许一句话结束，不强行提问，不机械给建议。", 220)


def is_weather_request(text: str) -> bool:
    """Only direct utility questions bypass the persona model."""
    text = text.strip()
    if re.fullmatch(r"(?:(?:what(?:'s| is) (?:the )?)?weather(?: like)?)(?: in [a-zA-Z -]+)?[?.!\s]*", text, re.IGNORECASE):
        return True
    if re.search(r"心情|安慰|难过|像.*下雨|喜欢.*天气|讨厌.*天气", text):
        return False
    return bool(re.fullmatch(
        r"(?:请|请问|帮我|麻烦)?(?:查(?:一下|查)?|看看|告诉我)?\s*"
        r"(?:(?:今天|明天|后天|现在|当前)\s*)?"
        r"(?:[\u3400-\u9fffA-Za-z -]{0,18})?(?:天气|气温|温度|会下雨|会下雪|冷不冷|热不热)"
        r"(?:怎么样|如何|多少|几度|吗|预报)?[呢呀啊？?。！!\s]*", text))


def is_time_request(text: str) -> bool:
    if re.fullmatch(r"(?:what time is it|what(?:'s| is) the (?:time|date)|time|date)[?.!\s]*", text.strip(), re.IGNORECASE):
        return True
    return bool(re.fullmatch(
        r"(?:请问|告诉我|帮我看(?:一下)?)?\s*(?:现在|当前|今天)?\s*"
        r"(?:几点(?:了)?|几号|星期几|礼拜几|周几|日期|时间)"
        r"[呢呀啊？?。！!\s]*", text.strip()))
