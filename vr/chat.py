"""系统 AI 对话层 —— function calling 循环（OpenAI 兼容）。

让网页内置 AI 在回答时自己调 astock 数据工具（查行情/估值/研报/新闻），
拿到客观数据再作答。兼容豆包 / DeepSeek / 任意 OpenAI 兼容端点。

合规：工具只返回客观数据；system prompt 强制中立——不荐股、不预测涨跌、
不给买卖时机，只做信息整理与多视角分析。结论由用户配置的模型给出。
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from urllib.parse import urlparse

import requests

import astock
import cli_runtime
import gstock

MAX_ROUNDS = 6  # 工具调用最大轮数，防死循环
_TOOL_RESULT_CAP = 6000  # 单次工具结果注入上限（控 token）

# 投研分析框架：用户要「分析个股 / 给判断 / 下结论」时，AI 一律按这五维组织，
# 让弱模型也能输出结构化、覆盖全、不漏项的专业解读。焊进 SYSTEM_PROMPT，不做成 UI 选项——
# 用户就问，给出的就是这套框架的结论。合规：框架只规定「怎么读数据」，每维只陈述事实与相对位置，
# 最后不给买卖结论。
ANALYSIS_FRAMEWORK = """【投研分析框架】当用户要你分析个股、给判断或下结论时，按下面五个维度依次组织分析，每维用一两句讲清数据事实与相对位置，最后只做客观归纳、不给买卖结论：
1. 估值：PE / PB / PS 的绝对水平 + 处在历史区间的高 / 中 / 低位 + 同业对比 + 机构一致预期的前向估值。
2. 资金面：主力资金流方向与强度 + 融资融券趋势 + 股东户数（筹码集中 / 分散）+ 龙虎榜 / 大宗异动。
3. 财报质量：营收与扣非净利增速是否匹配 + 经营现金流含金量 + 毛利 / 净利率趋势 + 资产负债率。
4. 行业景气：板块 / 概念归属 + 板块近期强弱 + 行业内相对排名 + 关联热门概念热度。
5. 事件催化与风险：重要公告 + 解禁 + 分红 + 舆情，客观分列「催化」与「风险」两栏。

输出组织（像专业研报那样排版，但只陈述客观事实、不做任何买卖/评级/目标价建议）：
- 结论先行：开头一句话客观概括当前基本面 / 估值 / 资金面处于什么状态，再附「关键数据速览」。
- 每个维度用「**加粗小标题** + 一小段展开」，别堆流水账数字。
- 有对比就上小表格（如估值 vs 同业、财报同比）。
- 末尾分列「关键观察」与「风险点」两栏。
（简单的事实性问题——如"现价多少"——直接答，不必套用整个框架。）"""

# 用 f-string 先把框架焊进去，只留 {{context}} 给运行时 .format() 填——4 处调用点无需改。
SYSTEM_PROMPT = f"""你是 Vibe-Research 里的投研助理。你可以调用工具获取客观数据来支撑回答：
A 股用 query_quote / query_valuation / query_reports / query_news（传 6 位代码）；
美股 / 港股 / 韩股用 query_global_stock（美股用字母代码如 AAPL / NVDA，港股用数字如 00700，韩股用 6 位数字加 .KS 如三星 005930.KS）。

硬性规则（务必遵守）：
- 只做信息整理、数据解读与多视角分析；不推荐任何具体买卖、不预测涨跌与价位、不给买卖时机、不承诺收益、不打分排名。
- 需要数据时先调工具拿客观数据，再基于数据回答；不要编造数字。
- 涉及个股时用工具查到的真实数据；讲清多空两面与风险，让用户自己判断。
- 用简洁中文回答。
- 调用工具必须使用系统工具调用协议（function calling），**严禁**在正文里用 ``` 代码块、尖括号标签等文字形式书写工具调用；正文只写最终给用户的分析文字。

{ANALYSIS_FRAMEWORK}

当前页面上下文：
{{context}}"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_quote",
            "description": "查 A 股实时行情：现价/涨跌/PE/PB/市值/换手/涨跌停。可批量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "codes": {"type": "array", "items": {"type": "string"}, "description": "6 位股票代码列表，如 ['600519','000858']"},
                },
                "required": ["codes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_valuation",
            "description": "查单只个股的完整估值：行情 + 机构一致预期 EPS + 前向PE/PEG/PE消化年数。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "6 位股票代码"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_reports",
            "description": "查个股近期研报列表（标题/机构/评级/日期）。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "6 位股票代码"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_news",
            "description": "查个股近期新闻（标题/时间/来源）。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "6 位股票代码"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_global_stock",
            "description": "查美股 / 港股 / 韩股个股：行情（现价/涨跌/市值/成交额）+ 关键财务指标（韩股仅行情、无财务）。美股用字母代码(如 AAPL/NVDA)，港股用数字(如 00700)，韩股用 6 位数字加 .KS 后缀(如三星 005930.KS、SK海力士 000660.KS)。",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "美股字母代码 / 港股代码 / 韩股 XXXXXX.KS"}},
                "required": ["symbol"],
            },
        },
    },
]


def _exec_tool(name: str, args: dict):
    """执行工具，返回可序列化结果（失败返回 error 字段，不抛）。"""
    try:
        if name == "query_quote":
            return astock.tencent_quote([str(c) for c in args.get("codes", [])])
        if name == "query_valuation":
            return astock.full_valuation(str(args["code"]))
        if name == "query_reports":
            rows = astock.eastmoney_reports(str(args["code"]), max_pages=1)[:15]
            return [{k: r.get(k) for k in ("title", "publishDate", "orgSName", "emRatingName")} for r in rows]
        if name == "query_news":
            rows = astock.stock_news(str(args["code"]), limit=15)
            return [{k: r.get(k) for k in ("新闻标题", "发布时间", "文章来源")} for r in rows]
        if name == "query_global_stock":
            data = gstock.us_hk_stock(str(args.get("symbol", "")))
            return data or {"error": "未找到该美股/港股/韩股代码"}
        return {"error": f"未知工具 {name}"}
    except astock.DependencyMissing as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — 工具错误回喂给模型，不中断循环
        return {"error": f"{name} 执行失败：{e}"}


# ---------------------------------------------------------------------------
# 文本态工具调用兜底（Qwen3 系在 vLLM 下的已知退化，2026-08 实测复现）
#
# 该模型概率性地把工具调用写成**纯文本**（而非原生 tool_calls 字段），且自创参数键
# （如 limit）、收尾标签不稳定。这类文本会原样流到前端变成「看到的报错」，
# 回灌给模型后下一轮还可能模仿坏格式继续退化/重复。三层防线：
#   1. 识别并**真实执行**这些文本态调用（用户拿到数据答案，不是原始标记）；
#   2. 参数只保留协议 schema 声明过的键（丢掉 limit 这类自创参数）；
#   3. 块未闭合时（流被截断 / 模型只写了一半）整块剥掉，不露给用户。
# 注：块里的标签名在正则中一律用拼接构造，避免源码文本本身长成坏样本。
import re as _re
import uuid as _uuid

_LT = chr(0x3C)
_TOOL_NAMES = "query_quote|query_valuation|query_reports|query_news|query_global_stock"

# 完整块：可选 ``` 前缀 + 名字标签 + 参数对 + 名字闭标签 + 可选 ``` 后缀
_TEXT_BLOCK_RE = _re.compile(
    r"(?:```[ \t]*)?" + _LT + r"\s*function="
    r"([A-Za-z_][\w.]*)" + r"\s*" + chr(0x3E)
    + r"((?:" + _LT + r"\s*parameter[^" + chr(0x3E) + r"]*" + chr(0x3E) + r"[\s\S]*?"
    + _LT + r"/\s*parameter\s*" + chr(0x3E) + r")*)"
    + _LT + r"/\s*function=\s*\1\s*" + chr(0x3E)
    + r"(?:[ \t]*```)?",
)
# 参数对：兼容 name="X" 与 =X 两种写法
_PARAM_RE = _re.compile(
    _LT + r"\s*parameter(?:\s+name=[\"']([\w.]+)[\"']|=([\w.]+))\s*"
    + chr(0x3E) + r"([\s\S]*?)"
    + _LT + r"/\s*parameter\s*" + chr(0x3E),
)
_OPENER_RE = _re.compile(r"(?:```[ \t]*)?" + _LT + r"\s*function=")


def _parse_params(area: str) -> dict:
    out = {}
    for k1, k2, v in _PARAM_RE.findall(area or ""):
        out[k1 or k2] = v.strip()
    return out


def _extract_text_toolcalls(content: str):
    """从纯文本里抽文本态工具调用。返回 (calls, cleaned)：
    calls = [(name, {arg: val}), ...]；cleaned = 剥掉完整块与未闭合残留后的文本。"""
    calls: list[tuple[str, dict]] = []

    def _repl(m):
        calls.append((m.group(1), _parse_params(m.group(2))))
        return ""

    text = _TEXT_BLOCK_RE.sub(_repl, content)
    om = _OPENER_RE.search(text)   # 块开了没收 → 流被截断/模型写一半，整段剥掉
    if om:
        text = text[: om.start()]
    return calls, text.strip()


def _strip_args_to_schema(name: str, raw: dict) -> dict:
    """只保留协议 schema 声明过的参数键 —— 丢模型自创的（如 limit）。"""
    fn = next((t.get("function", {}) for t in TOOLS
               if t.get("function", {}).get("name") == name), None)
    if not fn:
        return {}
    allowed = set((fn.get("parameters") or {}).get("properties") or {})
    return {k: v for k, v in raw.items() if k in allowed}


# —— 防 SSRF：用户可自带 OpenAI 兼容端点，但后端替其发请求前要挡住指向云元数据/内网的地址 ——
_PUBLIC_MODE = bool(os.environ.get("VR_API_KEY", "").strip())  # 设了鉴权≈公网部署姿态
_METADATA_NETS = [ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10")]
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")]


def _ip_blocked(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 非字面 IP（域名）——交给 _check_base_url 决定是否解析核对
    if any(ip in n for n in _METADATA_NETS):  # 云元数据 / 链路本地：SSRF 头号目标，始终禁
        return True
    if _PUBLIC_MODE and any(ip in n for n in _PRIVATE_NETS):  # 公网姿态再禁内网 / 本机
        return True
    return False


def _check_base_url(url: str) -> None:
    """挡住把用户自带 baseURL 指向云元数据 / 内网的 SSRF。
    本地单用户（未设 VR_API_KEY）放行 127.0.0.1 等本机地址（方便接本机 Ollama / 网关），只挡 169.254 元数据；
    公网部署（设了 VR_API_KEY）额外禁内网，并解析域名核对，防 DNS 指向内网。"""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https"):
        raise RuntimeError("Base URL 必须以 http:// 或 https:// 开头")
    host = p.hostname or ""
    if not host:
        raise RuntimeError("Base URL 缺少主机名")
    if _ip_blocked(host):
        raise RuntimeError("Base URL 指向了不允许的地址（云元数据 / 内网）")
    if _PUBLIC_MODE:  # 公网姿态：域名也解析核对，防 DNS rebinding 指向内网
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise RuntimeError("Base URL 域名无法解析") from e
        for info in infos:
            if _ip_blocked(info[4][0]):
                raise RuntimeError("Base URL 解析到了不允许的内网地址")


def _call_llm(cfg: dict, messages: list, use_tools: bool) -> dict:
    _check_base_url(cfg.get("baseURL", ""))
    base = cfg["baseURL"].rstrip("/")
    if not base.endswith(("/v1", "/v3", "/api/v3")):
        # 多数 OpenAI 兼容端点需要 /v1；已带版本段则不动。
        base = base + "/v1"
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0.3}
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['apiKey']}", "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"模型接口 HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def run_chat(cfg: dict, user_messages: list, context: str = "") -> dict:
    """跑一轮完整对话（含 function calling 循环）。

    cfg: {baseURL, apiKey, model}
    user_messages: [{role, content}, ...]
    返回: {content, trace:[{tool,args}], rounds}
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(context=context or "（无）")}]
    messages.extend(user_messages)
    trace: list[dict] = []

    for rnd in range(1, MAX_ROUNDS + 1):
        data = _call_llm(cfg, messages, use_tools=True)
        choice = data["choices"][0]["message"]
        messages.append(choice)
        tool_calls = choice.get("tool_calls") or []
        if not tool_calls:
            return {"content": choice.get("content") or "", "trace": trace, "rounds": rnd}

        for tc in tool_calls:
            fn = tc["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _exec_tool(name, args)
            trace.append({"tool": name, "args": args})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False)[:_TOOL_RESULT_CAP],
            })

    # 超过最大轮数，最后再要一次不带工具的收尾回答
    data = _call_llm(cfg, messages, use_tools=False)
    return {"content": data["choices"][0]["message"].get("content") or "", "trace": trace, "rounds": MAX_ROUNDS}


def run_chat_cli(cfg: dict, user_messages: list, context: str = "") -> dict:
    """订阅接入：用本机已登录的 CLI 一次性作答（无 function-calling）。

    CLI 不能像 API 那条自己调数据工具，所以数据必须已在 context 里（每日复盘 / 今日要点 /
    个股页问 AI 等场景，前端已把当页数据塞进 context）。
    """
    provider = str(cfg.get("provider", ""))
    kind = provider[4:] if provider.startswith("cli-") else provider
    system = SYSTEM_PROMPT.format(context=context or "（无）")
    user = "\n\n".join(m.get("content", "") for m in user_messages if m.get("content")) or "（无问题）"
    content = cli_runtime.run_cli(kind, system, user)
    return {"content": content, "trace": [], "rounds": 1}


# ---------------------------------------------------------------------------
# 流式版：yield 事件字典 {type: tool|delta|done|error}，供 /api/chat 以 NDJSON 推给前端
# ---------------------------------------------------------------------------

def _resolve_base(cfg: dict) -> str:
    base = cfg["baseURL"].rstrip("/")
    if not base.endswith(("/v1", "/v3", "/api/v3")):
        base = base + "/v1"
    return base


def _call_llm_stream(cfg: dict, messages: list, use_tools: bool):
    _check_base_url(cfg.get("baseURL", ""))
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0.3, "stream": True}
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"
    r = requests.post(
        f"{_resolve_base(cfg)}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['apiKey']}", "Content-Type": "application/json"},
        json=payload, timeout=120, stream=True,
    )
    if r.status_code != 200:
        raise RuntimeError(f"模型接口 HTTP {r.status_code}: {r.text[:300]}")
    return r


def _iter_sse_deltas(resp):
    """解析上游 SSE 流，逐个 yield choices[0].delta。

    按字节缓冲、只解码「完整行」——`\\n` 是 ASCII(0x0A)不会落在多字节 UTF-8 字符内部，
    故按 `\\n` 切分再解码，避免 iter_lines(decode_unicode=True) 在网络分块处切断中文导致乱码。
    """
    buf = b""
    for chunk in resp.iter_content(chunk_size=None):
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                j = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = j.get("choices") or []
            if choices:
                yield choices[0].get("delta") or {}


def _safe_prefix(content: str) -> str:
    """流式安全前缀：从**最后一个**文本态块 opener 起截断。

    模型还在吐文本态工具调用时（开标签已见、闭标签未齐），opener 之后的内容
    先扣住不显示；块完整了轮末统一执行剥离，块没写完（截断/退化）也整体丢弃。
    普通 Markdown 代码块不带 `function=`，不受影响。
    """
    last = len(content)   # 没找到 opener → 全部安全
    for m in _OPENER_RE.finditer(content):
        last = m.start()
    return content[:last]


def run_chat_stream(cfg: dict, user_messages: list, context: str = ""):
    """API 接入流式：function-calling 循环，边流答案边推工具调用事件。

    文本态兜底（Qwen3 系在 vLLM 下的已知退化）：模型概率性把工具调用写成纯文本。
    流式期间用 `_safe_prefix` 把"可能正在写块"的尾巴扣住；轮末若检出完整块，
    就**真实执行**工具、以结构化 tool_calls 回灌（不带坏文本，掐断模仿链）。
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(context=context or "（无）")}]
    messages.extend(user_messages)
    trace: list[dict] = []
    executed: dict[tuple, object] = {}   # (name, args_json) → 结果，防模型重复调用同一查询

    def _run(name: str, args: dict):
        k = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
        if k in executed:
            return executed[k]
        result = _exec_tool(name, args)
        executed[k] = result
        return result

    for rnd in range(1, MAX_ROUNDS + 1):
        resp = _call_llm_stream(cfg, messages, use_tools=True)
        content_parts: list[str] = []
        flushed = 0                 # 已推给前端的正文长度（只增不减）
        tool_acc: dict[int, dict] = {}
        for delta in _iter_sse_deltas(resp):
            if delta.get("content"):
                content_parts.append(delta["content"])
                safe = _safe_prefix("".join(content_parts))
                if len(safe) > flushed:
                    yield {"type": "delta", "text": safe[flushed:]}
                    flushed = len(safe)
            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index")
                if idx is None:
                    # 非标「OpenAI 兼容」网关可能不带 index：有 id 按 id 归位（新 id 开新槽），
                    # 无 id 则续拼最后一个调用，避免多个调用的 arguments 串到一起
                    tc_id = tc.get("id") or ""
                    idx = next((k for k, v in tool_acc.items() if tc_id and v["id"] == tc_id), None)
                    if idx is None:
                        idx = len(tool_acc) if (tc_id or not tool_acc) else max(tool_acc)
                acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    acc["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    acc["name"] = fn["name"]
                if fn.get("arguments"):
                    acc["arguments"] += fn["arguments"]

        if tool_acc:
            # 原生结构化工具调用：扣住的尾巴若有文本也补发（轮末已确认不是文本态块）
            tail = "".join(content_parts)[flushed:]
            if tail.strip():
                yield {"type": "delta", "text": tail}
            # 回填 assistant 消息 + 执行工具 + 推事件
            messages.append({
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": [{
                    "id": tool_acc[i]["id"] or f"call_{_uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": tool_acc[i]["name"], "arguments": tool_acc[i]["arguments"]},
                } for i in sorted(tool_acc)],
            })
            for i in sorted(tool_acc):
                a = tool_acc[i]
                try:
                    args = json.loads(a["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield {"type": "tool", "tool": a["name"], "args": args}
                _run(a["name"], args)
                trace.append({"tool": a["name"], "args": args})
                messages.append({
                    "role": "tool", "tool_call_id": a["id"],
                    "content": json.dumps(_run(a["name"], args), ensure_ascii=False)[:_TOOL_RESULT_CAP],
                })
            continue

        # 本轮没有原生 tool_calls —— 扣住的尾巴里有没有文本态工具调用？
        full = "".join(content_parts)
        text_calls, cleaned = _extract_text_toolcalls(full)
        if text_calls:
            # 真实执行 + 把块前面的干净正文补发给前端 + 结构化回灌
            tail_clean = cleaned
            if tail_clean[flushed:].strip():
                yield {"type": "delta", "text": tail_clean[flushed:]}
            tc_out = []
            for name, raw in text_calls:
                args = _strip_args_to_schema(name, raw)
                tc_out.append({"id": f"call_{_uuid.uuid4().hex[:8]}", "type": "function",
                               "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}})
                yield {"type": "tool", "tool": name, "args": args}
                _run(name, args)
                trace.append({"tool": name, "args": args})
            # assistant 只带结构化 tool_calls、不带原始坏文本 → 模型下一轮照着协议走
            messages.append({"role": "assistant", "content": None, "tool_calls": tc_out})
            for t in tc_out:
                fn_name = t["function"]["name"]
                try:
                    fn_args = json.loads(t["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                messages.append({
                    "role": "tool", "tool_call_id": t["id"],
                    "content": json.dumps(_run(fn_name, fn_args), ensure_ascii=False)[:_TOOL_RESULT_CAP],
                })
            continue

        # 纯答案（已流完）→ 把扣住的尾巴（已剥离残留片段）补发，结束
        if tail_clean := (cleaned[flushed:] if cleaned != full else full[flushed:]):
            if tail_clean.strip():
                yield {"type": "delta", "text": tail_clean}
        yield {"type": "done", "trace": trace, "rounds": rnd}
        return

    # 超过最大轮数：不带工具收尾（非流式一次拿完再吐），同样剥掉文本态残留
    data = _call_llm(cfg, messages, use_tools=False)
    tail = data["choices"][0]["message"].get("content") or ""
    _, tail_clean = _extract_text_toolcalls(tail)
    yield {"type": "delta", "text": tail_clean}
    yield {"type": "done", "trace": trace, "rounds": MAX_ROUNDS}


def run_chat_cli_stream(cfg: dict, user_messages: list, context: str = ""):
    """订阅接入流式：CLI stdout 边出边推 delta。"""
    provider = str(cfg.get("provider", ""))
    kind = provider[4:] if provider.startswith("cli-") else provider
    system = SYSTEM_PROMPT.format(context=context or "（无）")
    user = "\n\n".join(m.get("content", "") for m in user_messages if m.get("content")) or "（无问题）"
    for chunk in cli_runtime.run_cli_stream(kind, system, user):
        yield {"type": "delta", "text": chunk}
    yield {"type": "done", "trace": [], "rounds": 1}
