#!/usr/bin/env python3
"""
春饼·神曲 — 炼狱篇 + 天堂篇

基于但丁《神曲》：
- 炼狱篇：感知七宗罪情绪时，温柔提醒用户冷静
- 天堂篇：连续多条消息情绪稳定时，祝福用户升入天堂

炼狱山七层（自下而上）：
  第1层 傲慢 Superbia — 轻视 AI / 居高临下
  第2层 嫉妒 Invidia — 与他人比较产生焦虑
  第3层 愤怒 Ira     — 暴躁、咒骂、攻击性语言
  第4层 懒惰 Acedia  — 不愿思考、甩手掌柜
  第5层 贪婪 Avaritia— 贪多求快、一次要太多
  第6层 暴食 Gula    — 不消化就要更多（不看结果继续催）
  第7层 色欲 Luxuria — 对完美的执念、过度追求形式

天堂九重天（连续稳定时逐层攀升）：
  10条  月天      — 信仰之光初现
  20条  水星天    — 志向清明
  30条  金星天    — 爱意充盈
  40条  太阳天    — 智慧圆满
  55条  火星天    — 勇气坚定
  70条  木星天    — 正义昭彰
  85条  土星天    — 沉思通透
  100条 恒星天    — 万物澄明
  120条 至高天    — 抵达天堂

Hook: UserPromptSubmit — 在用户提交消息时分析
输出: 检测到时 print 提醒（显示给用户），不阻断（exit 0）
"""

import sys
import json
import re
import os
import random

# ---------- 七宗罪检测规则 ----------

SINS = {
    "ira": {
        "name": "愤怒",
        "layer": 3,
        "latin": "Ira",
        "patterns": [
            r"垃圾|废物|破[东烂]|什么(玩意|破|鬼)|狗屎|妈的|卧槽|靠|艹|tmd|fuck|shit|damn|stupid|useless|trash|wtf|crap",
            r"怎么又[错坏崩]|又(出|搞)(错|坏|砸)|烦死|受不了|忍不了",
            r"你(是不是)?[傻笨蠢]|脑子(有病|进水)|智障|白痴|弱智",
        ],
        "quote": "记住，愤怒是一团遮蔽理智的浓烟。",
        "source": "Purgatorio XVI",
        "reminder": "春饼蹭了蹭你的手...深呼吸，把问题说清楚，我们一起解决。",
    },
    "superbia": {
        "name": "傲慢",
        "layer": 1,
        "latin": "Superbia",
        "patterns": [
            r"这(么|都)简单(你?都|也)(不会|做不到|搞不定)",
            r"我(自己)?比你(强|聪明|厉害)|你不如我",
            r"(不用|不需要)你(教|告诉|解释)|我(当然)?知道",
            r"连(这个?|这种)(都|也)(不|搞不)(懂|会|明白)",
        ],
        "quote": "行走时低头看看脚下的石刻，那是倒下的傲慢者。",
        "source": "Purgatorio XII",
        "reminder": "春饼歪头看着你...试着描述你期望的结果，而不是预设对方的能力。",
    },
    "acedia": {
        "name": "懒惰",
        "layer": 4,
        "latin": "Acedia",
        "patterns": [
            r"(不想|懒得)(看|读|想|管|思考|了解|理解)",
            r"(你?直接|帮我全[部部]?)(做|写|搞|改|弄)(完|了|好|掉)",
            r"(太[长多]了?)不(想?看|读)|别(解释|说了)直接(给|做|写)",
            r"(随便|无所谓|都行).{0,5}(你看着办|你决定|你自己[搞弄])",
        ],
        "quote": "在炼狱第四层，怠惰的灵魂必须不停地奔跑。",
        "source": "Purgatorio XVIII",
        "reminder": "春饼用爪子戳了戳你...花一分钟描述清楚需求，能省下十分钟的返工。",
    },
    "avaritia": {
        "name": "贪婪",
        "layer": 5,
        "latin": "Avaritia",
        "patterns": [
            r"(顺便|另外|还有|再|同时).{0,15}(顺便|另外|还有|再|同时).{0,15}(顺便|另外|还有|再)",
            r"(一次性?|一口气)(把?所有|全[部都])(做完|搞定|改完|加上)",
            r"(还要|再加|多加|追加).{0,10}(还要|再加|多加|追加)",
        ],
        "quote": "俯卧在地的灵魂们学会了：紧握一切，反而失去一切。",
        "source": "Purgatorio XIX",
        "reminder": "春饼慢慢眨了眨眼...一次做好一件事，比同时做三件事快。",
    },
    "gula": {
        "name": "暴食",
        "layer": 6,
        "latin": "Gula",
        "patterns": [
            r"(快[点些]|赶紧|马上|速度|hurry|asap).{0,10}(快[点些]|赶紧|马上|速度)",
            r"(不用看|别看了?|跳过).{0,10}(直接|马上|赶紧)(下一个|继续|开始)",
            r"(结果呢|好了没|做完没|写完没).{0,5}(快|赶紧|催)",
        ],
        "quote": "果树旁的灵魂们学会了：先品味，再索取。",
        "source": "Purgatorio XXIV",
        "reminder": "春饼打了个哈欠...停下来看看刚才的结果，消化了再继续。",
    },
    "luxuria": {
        "name": "色欲",
        "layer": 7,
        "latin": "Luxuria",
        "desc": "对完美的执念",
        "patterns": [
            r"(还是不够|仍然不)(好|完美|满意|理想).{0,10}(再[改试来]|重[做写来])",
            r"(第[3-9三四五六七八九十]+次|又又又)(改|重[做写来]|推翻)",
            r"(不行|不对|不够).{0,5}(不行|不对|不够).{0,5}(不行|不对|不够)",
        ],
        "quote": "穿越火墙的灵魂们明白了：追求完美本身可以是一种燃烧。",
        "source": "Purgatorio XXVI",
        "reminder": "春饼轻轻叫了一声...够好就是好。先交付，再迭代。",
    },
    "invidia": {
        "name": "嫉妒",
        "layer": 2,
        "latin": "Invidia",
        "patterns": [
            r"(别人|人家|其他人|XXX)(都|已经)(能|会|做到|有了)",
            r"(为什么|怎么)(别人|人家|其他)(的|能).{0,10}(我|这里)(不|却)",
            r"(cursor|copilot|gpt|gemini|其他ai)(比你|都能|就能|至少)",
        ],
        "quote": "缝合双眼的灵魂们终于看见：每条路都有自己的风景。",
        "source": "Purgatorio XIII",
        "reminder": "春饼靠在你腿上...专注眼前的问题，走自己的路。",
    },
}


# ---------- 天堂九重天 ----------

STATE_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "chunbing_paradiso.json")

# 连续稳定消息数 -> 天堂层级
PARADISO = [
    (10, "月天",   "Cielo della Luna",     "Paradiso III",   "信仰的微光在月亮的阴影中闪烁。",        "春饼安静地蜷在你脚边...你的心很平静，继续保持。"),
    (20, "水星天", "Cielo di Mercurio",    "Paradiso VI",    "在水星的光芒中，志向变得清晰而纯粹。",  "春饼仰头看着你，眼里有光...你的思路很清晰。"),
    (30, "金星天", "Cielo di Venere",      "Paradiso VIII",  "金星的温暖照亮了每一份真挚的爱。",      "春饼发出了满足的呼噜声...你的状态真好。"),
    (40, "太阳天", "Cielo del Sole",       "Paradiso X",     "智慧的灵魂们在太阳中组成光之花环。",    "春饼在你键盘旁打盹...智慧正与你同行。"),
    (55, "火星天", "Cielo di Marte",       "Paradiso XIV",   "火星的十字架上闪耀着勇者的荣光。",      "春饼竖起了尾巴...你的专注如同勇士。"),
    (70, "木星天", "Cielo di Giove",       "Paradiso XVIII",  "正义的灵魂们在木星上拼成神圣的文字。",  "春饼慢慢眨眼...你的判断清明而公正。"),
    (85, "土星天", "Cielo di Saturno",     "Paradiso XXI",   "金色的阶梯从土星延伸至无穷高处。",      "春饼安详地望向远方...沉思让你离真理更近。"),
    (100, "恒星天", "Cielo delle Stelle Fisse", "Paradiso XXII", "回望来路，大地渺小如尘——而你已身在群星之间。", "春饼轻轻踩了踩你的肩膀...你已经走了很远。"),
    (120, "至高天", "Empyreo",              "Paradiso XXXIII", "永恒之光中，爱推动着太阳和其他群星。",  "春饼闭上眼，发出最温柔的呼噜...你已抵达天堂。"),
]


def load_state():
    """加载连续稳定消息计数"""
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"streak": 0, "last_paradiso": 0}


def save_state(state):
    """保存状态"""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except IOError:
        pass


def check_paradiso(streak):
    """检查是否达到新的天堂层级，返回该层信息或 None"""
    reached = None
    for threshold, name, latin, source, quote, reminder in PARADISO:
        if streak == threshold:
            reached = {
                "name": name,
                "latin": latin,
                "source": source,
                "quote": quote,
                "reminder": reminder,
                "threshold": threshold,
            }
    return reached


def format_paradiso(heaven):
    """格式化天堂祝福"""
    lines = [
        "",
        "  ✨ 春饼感知到了宁静的光芒...",
        "",
        f"  ── {heaven['name']} · {heaven['latin']} ──",
        f"  「{heaven['quote']}」",
        f"    — {heaven['source']}",
        "",
        f"  {heaven['reminder']}",
        f"  🐾 连续 {heaven['threshold']} 条平静的消息",
        "",
    ]
    return "\n".join(lines)


def detect_sins(text):
    """检测文本中的七宗罪信号，返回匹配的 sin 列表"""
    if not text or len(text) < 2:
        return []

    text_lower = text.lower()
    detected = []

    for sin_key, sin in SINS.items():
        for pattern in sin["patterns"]:
            if re.search(pattern, text_lower):
                detected.append(sin)
                break

    # 按炼狱层级排序（低层 = 更严重）
    detected.sort(key=lambda s: s["layer"])
    return detected


def get_current_heaven_name(last_paradiso):
    """根据 last_paradiso 阈值获取天堂层名称"""
    for threshold, name, latin, *_ in PARADISO:
        if threshold == last_paradiso:
            return f"{name}（{latin}）"
    return None


def format_status_line(state):
    """生成当前状态行"""
    streak = state.get("streak", 0)
    last_paradiso = state.get("last_paradiso", 0)

    if last_paradiso > 0:
        heaven_name = get_current_heaven_name(last_paradiso)
        return f"  📍 你从 {heaven_name} 跌落了...连胜 {streak} 归零"
    elif streak > 0:
        # 找到下一个天堂层
        next_heaven = None
        for threshold, name, *_ in PARADISO:
            if threshold > streak:
                next_heaven = (threshold, name)
                break
        if next_heaven:
            return f"  📍 连胜 {streak} 中断，距离{next_heaven[1]}还差 {next_heaven[0] - streak} 条"
        return f"  📍 连胜 {streak} 中断"
    else:
        return "  📍 炼狱山脚，重新开始攀登"


def format_reminder(sin, state=None):
    """格式化一条提醒"""
    lines = [
        "",
        f"  🐱 春饼感知到了「{sin['name']}」的气息...",
        "",
        f"  ── 炼狱山 第{sin['layer']}层 · {sin['latin']} ──",
        f"  「{sin['quote']}」",
        f"    — {sin['source']}",
        "",
        f"  {sin['reminder']}",
    ]
    if state is not None:
        lines.append(format_status_line(state))
    lines.append("")
    return "\n".join(lines)


def main():
    # 读取 hook 传入的 JSON
    if sys.stdin.isatty():
        return 0

    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return 0
        data = json.loads(raw)
    except (json.JSONDecodeError, IOError):
        return 0

    # 提取用户消息（UserPromptSubmit hook 传入字段为 "prompt"）
    user_message = data.get("prompt", "")
    if not user_message:
        user_message = data.get("user_message", "")
    if not user_message:
        user_message = data.get("message", "")
    if not user_message:
        return 0

    detected = detect_sins(user_message)
    state = load_state()

    if detected:
        # 先用当前状态生成提醒（包含跌落信息），再重置
        reminder = format_reminder(detected[0], state)
        state["streak"] = 0
        state["last_paradiso"] = 0
        save_state(state)
        # 只提醒最严重的一条（避免刷屏）
        print(reminder)
    else:
        # 情绪稳定：连胜 +1，检查是否升入新的天堂层
        state["streak"] += 1
        heaven = check_paradiso(state["streak"])
        if heaven:
            state["last_paradiso"] = heaven["threshold"]
            print(format_paradiso(heaven))
        save_state(state)

    # 不阻断用户操作
    return 0


if __name__ == "__main__":
    sys.exit(main())
