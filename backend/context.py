"""上下文管理：token 估算与预算截断（v0.6）。

设计口径（见 v0.6 设计草稿）：
- 只做估算与截断，不动存储层（storage.py 一行不改，库里 20 轮照存，
  截断只发生在组装上下文时）。
- 预算 CONTEXT_TOKEN_BUDGET 默认 8000 token，只算 history + memory
  两部分——SYSTEM_PROMPT 与本轮 user 消息必须在，不计入预算。
- 超预算时从最旧的 history 轮次开始整轮丢（user + assistant 一对
  一起丢），直到 fits 为止；system 和 memory 永不丢，最新一轮永不丢。
- 估算是字符换算启发式，不是精确分词：精确分词要接模型 tokenizer，
  没有标准库方案。英文约 4 字符/token，中文约 1.5–2 字符/token
  （这里取中间值 1.75）。

标准库，零新增依赖。
"""
import math
import os

CONTEXT_TOKEN_BUDGET = int(os.environ.get("CONTEXT_TOKEN_BUDGET", "8000"))

# 中文字符约 1.5–2 字符/token，取中间值；英文等约 4 字符/token。
_CJK_CHARS_PER_TOKEN = 1.75
_OTHER_CHARS_PER_TOKEN = 4.0


def _is_cjk(ch: str) -> bool:
    """按 1.5–2 字符/token 计价的字符：中日韩统一表意文字、假名、
    谚文、全角形式。英文、数字、标点、空格走 4 字符/token 那档。"""
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF  # CJK 统一表意文字
        or 0x3400 <= o <= 0x4DBF  # CJK 扩展 A
        or 0xF900 <= o <= 0xFAFF  # CJK 兼容表意文字
        or 0x3040 <= o <= 0x30FF  # 日文假名
        or 0xAC00 <= o <= 0xD7AF  # 谚文音节
        or 0xFF00 <= o <= 0xFFEF  # 全角形式（含全角标点）
    )


def estimate_tokens(text: str) -> int:
    """字符换算估算 token 数（启发式，非精确分词）。

    空串 0；非空至少 1。英文 "hello world"（11 字符）≈ 3 token，
    中文 "你好世界"（4 字符）≈ 3 token——中文单字比英文单词贵，
    这正是要单独计价的原因。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return max(1, math.ceil(cjk / _CJK_CHARS_PER_TOKEN
                            + other / _OTHER_CHARS_PER_TOKEN))


def message_tokens(msg: dict) -> int:
    """一条消息的估算 token 数。content 为空（比如只有 tool_calls 的
    assistant 消息）按 0 算；非字符串 content 先转字符串再估算。"""
    content = msg.get("content")
    if not content:
        return 0
    if isinstance(content, str):
        return estimate_tokens(content)
    return estimate_tokens(str(content))


def truncate_history(history: list[dict], budget: int,
                     memory_tokens: int = 0) -> tuple[list[dict], int]:
    """在 history 上做预算截断，返回 (保留下来的 history, 丢掉的轮数)。

    规则：
    - 预算 = history + memory，memory_tokens 由调用方传入（memory 永不丢，
      只参与计数）。
    - 从最旧的轮次开始整轮丢：history 正常是 user/assistant 交替、
      以 user 开头，一轮 = 一条 user + 紧跟的一条 assistant；不成对的
      开头（理论上不该出现）一次只丢一条，仍计一轮。
    - 最新一轮永不丢：history 只剩一轮（≤2 条）时停手，哪怕它自己就
      超预算——丢了最新一轮等于丢了用户刚说的话，没法聊。
    """
    kept = list(history)
    dropped = 0
    total = memory_tokens + sum(message_tokens(m) for m in kept)
    while total > budget and len(kept) > 2:
        if kept[0].get("role") == "user" and kept[1].get("role") == "assistant":
            n = 2
        else:
            n = 1
        total -= sum(message_tokens(m) for m in kept[:n])
        kept = kept[n:]
        dropped += 1
    return kept, dropped


def apply_budget(messages: list[dict], budget: int | None = None) -> tuple[list[dict], dict]:
    """对已组装好的 [system, memory?, *history, user] 做预算截断。

    返回 (截断后的 messages, {"context_tokens": int, "dropped_rounds": int})。
    context_tokens 是预算口径的估算值：截断后 history + memory 的 token
    数（SYSTEM_PROMPT 与本轮 user 不计入，它们必须在）。

    只动 history 部分：system 和 memory（第二条 system 消息）永不丢，
    本轮 user（最后一条）永不丢。history 为空或预算内时原样返回。
    budget 不传时读 CONTEXT_TOKEN_BUDGET（调用时读，单测可 monkeypatch）。
    """
    if budget is None:
        budget = CONTEXT_TOKEN_BUDGET
    head = messages[:1]  # SYSTEM_PROMPT
    rest = messages[1:]
    memory_msgs = []
    if rest and rest[0].get("role") == "system":
        memory_msgs = [rest[0]]  # 第二条 system 消息 = v0.4 长期记忆
        rest = rest[1:]
    history, user_msgs = rest[:-1], rest[-1:]
    memory_tokens = sum(message_tokens(m) for m in memory_msgs)
    kept, dropped = truncate_history(history, budget, memory_tokens)
    context_tokens = memory_tokens + sum(message_tokens(m) for m in kept)
    return (head + memory_msgs + kept + user_msgs,
            {"context_tokens": context_tokens, "dropped_rounds": dropped})
