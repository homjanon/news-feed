#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""老张自建新闻源：RSS 抓取 → 硬规则选条 → 可选 LLM 中文化 → 静态 JSON。

设计原则（2026-09-12 与用户确认的方案）：
  1. 选条是硬规则：标题去重 + 按 pubDate 倒序取前 N 条，LLM 不参与选条；
  2. LLM 只做「译标题 + 一句话摘要」；全场只调一次，整批失败自动拆 2 批兜底，仍失败降级保留原文标题，绝不空窗；
  3. 源全部配置化（scripts/sources.json），后续新增 RSS 只改配置不改代码；
  4. 抓取失败保留上一场数据（本脚本只写当天文件，不删旧文件，latest.json 由成功场次覆盖）。

用法：
  python scripts/fetch_news.py --edition afternoon --outdir docs   # 下午茶 15:20
  python scripts/fetch_news.py --edition night     --outdir docs   # 夜豆浆 22:30

仅用标准库（urllib / xml.etree），Actions 上零安装依赖。
本机调试谷歌源被墙时：HTTPS_PROXY=http://127.0.0.1:7890 python scripts/fetch_news.py ...
"""
import argparse
import datetime
import email.utils as eu
import glob
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

TZ_CN = datetime.timezone(datetime.timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))

# 谷歌链接是 news.google.com 重定向链，正文里展示来源媒体名即可
EDITION_NAME = {"afternoon": "下午茶", "night": "夜豆浆"}


def log(msg):
    print(msg, flush=True)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-feed)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return r.read()


def parse_rss(content):
    """通用 RSS 解析：title/desc/url/pubDate/source。"""
    root = ET.fromstring(content)
    items = []
    for it in root.findall(".//item"):
        def clean(s):
            s = re.sub(r"<[^>]+>", " ", s or "")
            return re.sub(r"\s+", " ", html.unescape(s)).strip()
        src_el = it.find("source")
        items.append({
            "title": clean(it.findtext("title")),
            "desc": clean(it.findtext("description"))[:300],
            "url": (it.findtext("link") or "").strip(),
            "pubDate": (it.findtext("pubDate") or "").strip(),
            "source": clean(src_el.text) if src_el is not None and src_el.text else "",
        })
    return items


def norm_title(t):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t or "").lower()


def pub_dt(s):
    try:
        d = eu.parsedate_to_datetime(s or "")
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d
    except Exception:
        return None


def fetch_source(cfg):
    """抓一个源：按 urls 顺序兜底，第一个有效即用；返回 (items, 采用的host)。"""
    last_err = None
    for url in cfg["urls"]:
        host = re.sub(r"^https?://", "", url).split("/")[0]
        try:
            items = parse_rss(http_get(url))
            if not items:
                raise RuntimeError("200 但无 <item>（疑似 HTML 错误页）")
            if cfg.get("fresh_check"):
                today = datetime.datetime.now(TZ_CN).date()
                yest = today - datetime.timedelta(days=1)
                fresh = any(
                    (d := pub_dt(i["pubDate"])) and d.astimezone(TZ_CN).date() in (today, yest)
                    for i in items)
                if not fresh:
                    raise RuntimeError("无昨天/今天内容（疑似旧缓存/镜像）")
            if cfg.get("dedupe", True):
                seen, dedup = set(), []
                for i in items:
                    k = norm_title(i["title"])
                    if k and k not in seen:
                        seen.add(k)
                        dedup.append(i)
                items = dedup
            # 关键硬规则：谷歌 feed 顺序是"热度混合"，不按时间排（2026-09-12 实测
            # 第1条 04:54、第2条 07:54），必须显式倒序后取前 N，不能直接取前 N。
            if cfg.get("sort_desc"):
                items.sort(key=lambda i: pub_dt(i["pubDate"])
                           or datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc),
                           reverse=True)
            if cfg.get("title_suffix_source"):
                for i in items:
                    if not i["source"]:
                        m = re.match(r"^(.*?)\s+-\s+([^-]{2,40})$", i["title"])
                        if m:
                            i["title"], i["source"] = m.group(1).strip(), m.group(2).strip()
            # 该源若在配置里指定了 default_source，则给仍没有来源的条目补上
            # （联合早报的 RSS 没有 <source> 标签，靠这条保证每条都有出处）
            if cfg.get("default_source"):
                for i in items:
                    if not i["source"]:
                        i["source"] = cfg["default_source"]
            items = items[: int(cfg.get("take", 10))]
            log(f"  ✅ {host} → 采纳 {len(items)} 条")
            return items, host
        except Exception as e:
            last_err = e
            log(f"  ❌ {host} → {type(e).__name__}: {str(e)[:90]}")
            time.sleep(1)
    raise RuntimeError(f"所有实例失败，最后一个错误：{last_err}")


def _sys_prompt(smin, smax, mode="translate"):
    """两套独立提示词：translate=英文组（译标题+摘要）/ summarize=中文组（仅摘要、标题原样）。

    2026-09-16 改造：原先单一提示词含"英文就译、中文就原样"的条件规则。单源调用时
    整批同语言、等于废话；合并为一次调用后同一个 prompt 里同时存在【译】与【不译】
    两种互斥指令，模型倾向于"整批统一处理"，导致英文标题漏译（9/15 夜场整场退回
    原文）。现按语言拆批、每批指令单一，从根上消除歧义。
    """
    base = (
        f"② summary：{smin}~{smax} 字左右的中文摘要，把正文开头的核心事实整理成完整通顺的一段话；"
        "要点较多时可适当超出字数——信息完整比控制字数更重要，不逐字照抄、绝不以半句话截断结尾。"
        "只输出 JSON 数组本身，不要任何解释、不要 markdown 代码块。")
    if mode == "translate":
        return (
            "你是财经新闻编辑。输入是 JSON 数组 [{\"i\":序号,\"title\":英文标题,\"desc\":正文开头}]。"
            "**本批全部条目均为英文。**"
            "输出 JSON 数组 [{\"i\":序号,\"title\":中文标题,\"summary\":中文摘要}]，规则："
            "① title：**必须译成简洁中文**（专有名词用通用译名），不得原样保留英文、不得留英文残句；"
            + base)
    return (
        "你是财经新闻编辑。输入是 JSON 数组 [{\"i\":序号,\"title\":中文标题,\"desc\":正文开头}]。"
        "**本批全部条目均为中文。**"
        "输出 JSON 数组 [{\"i\":序号,\"title\":标题,\"summary\":中文摘要}]，规则："
        "① title：**必须一字不改原样返回输入标题**，不要改写、不要润色、不要增删字词；"
        + base)


def _call_llm_batch(batch, tcfg, mode="translate"):
    """对「一批」条目依次尝试各模型：成功则回填并返回 (模型名, True, 尝试数)；全失败 (None, False, 尝试数)。

    mode：translate=英文组（需校验译文确为中文）/ summarize=中文组（标题原样，无需译）。
    """
    smin = int(tcfg.get("summary_min", 40))
    smax = int(tcfg.get("summary_max", 60))
    sys_prompt = _sys_prompt(smin, smax, mode)
    tried = 0          # 真正发起过请求的模型数（用于区分"没配 Key"与"调用失败"）
    for m in tcfg.get("models", []):
        key = os.environ.get(m.get("key_env", ""))
        if not key:
            log(f"  ⏭️ 跳过 {m['name']}：环境变量 {m.get('key_env')} 未设置（Secret 未配置时属预期）")
            continue
        tried += 1
        payload = {
            "model": m["model"],
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(
                    [{"i": n, "title": i["title"], "desc": i["desc"][:400]}
                     for n, i in enumerate(batch)], ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "max_tokens": 8000,
        }
        try:
            req = urllib.request.Request(
                m["base"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode())
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
            arr = json.loads(text)
            if not isinstance(arr, list) or len(arr) != len(batch):
                raise RuntimeError(f"返回条数不符（期望 {len(batch)}，得 {len(arr) if isinstance(arr, list) else '非数组'}）")
            changed_title = 0
            for row in arr:
                i = int(row.get("i", -1))
                if not (0 <= i < len(batch)):
                    continue
                it = batch[i]
                ti = str(row.get("title", "")).strip()
                s = str(row.get("summary", "")).strip()
                # 中文标题一律保留原样（防 LLM 擅自改写事实性标题）；仅英文标题采纳译文
                if it["_translate_title"] and ti:
                    # ★ 2026-09-16：译文必须含中文，否则判失败。原先只判"ti 非空"就采纳，
                    #   模型若把英文标题原样回吐，会被当作成功 → 静默漏译、不降级、不重试。
                    if not re.search(r"[\u4e00-\u9fff]", ti):
                        raise RuntimeError(
                            f"第 {i} 条英文标题未译成中文（疑似原样返回）：{ti[:60]}")
                    it["title"] = ti
                    changed_title += 1
                if s:
                    it["summary"] = s   # 原样保留，不截断（信息完整优先）
            # ★ 2026-09-16：translate 模式下整批必须有译文产出，否则整批判失败触发降级
            if mode == "translate" and changed_title == 0:
                raise RuntimeError(f"整批 {len(batch)} 条无任何标题被翻译，判定失败")
            log(f"  🌐 AI 摘要完成（{m['name']} / {m['model']}，{len(batch)}条，译标题 {changed_title} 条）")
            return m["name"], True, tried
        except Exception as e:
            log(f"  ⚠️ {m['name']} 失败：{type(e).__name__}: {str(e)[:120]}")
    return None, False, tried


def llm_translate(items, tcfg):
    """按语言拆批调用：英文组译标题+摘要，中文组只做摘要（标题原样）。

    2026-09-16 改造（根因修复）：9/15 改为"全场一次调用"后，同一 prompt 内同时存在
    【英文→译】与【中文→原样】两条互斥规则，模型倾向整批统一处理，导致英文标题漏译；
    且原回填逻辑只判"译文字段非空"，漏译被当作成功 → 静默输出英文原文（9/15 夜场
    整场 translator=none(原文)，下游只看到英文）。

    现按语言分组、每组内部指令单一，彻底消除歧义（回到"每源一次调用"之所以稳定的
    本质原因，但不依赖源的划分，而是按语言划分，更稳）。

    每组失败后该组自动降级为 RSS 原文；两组互不影响，绝不空窗。
    """
    todo = [i for i in items if i.get("_llm")]
    if not todo:
        return "none"
    en = [i for i in todo if i.get("_translate_title")]       # 英文：译标题 + 摘要
    cn = [i for i in todo if not i.get("_translate_title")]   # 中文：标题原样 + 摘要

    done = {}          # 模型名 -> 该模型成功处理的条数
    failed = []        # [(mode, 条数)]
    for grp, mode in ((en, "translate"), (cn, "summarize")):
        if not grp:
            continue
        name, ok, tried = _call_llm_batch(grp, tcfg, mode=mode)
        if ok:
            done[name] = done.get(name, 0) + len(grp)
        else:
            if tried == 0:
                log(f"  ⏭️ {mode} 组（{len(grp)}条）无可用模型：Key 均未配置")
            else:
                log(f"  ⬇️ {mode} 组（{len(grp)}条）全部失败，降级为 RSS 原文")
            failed.append(f"{mode}:{len(grp)}")

    if not done:
        return "none(原文)"
    # 透明记账：写明哪个模型处理了多少条，失败组一并标出（便于看板判断 AI 是否真生效）
    parts = [f"{n}({c}条)" for n, c in done.items()]
    out = " + ".join(parts)
    if failed:
        out += f" ⚠️降级[{','.join(failed)}]"
    return out


def mark_new(items, outdir, date, edition):
    """与「同日期另一场」比对打 isNew；没有则与最近一份历史归档比对。"""
    prev_path = None
    other = "night" if edition == "afternoon" else "afternoon"
    cand = os.path.join(outdir, "news", f"{date}-{other}.json")
    if os.path.exists(cand):
        prev_path = cand
    else:
        files = sorted(glob.glob(os.path.join(outdir, "news", "*.json")), reverse=True)
        files = [f for f in files if os.path.basename(f) != f"{date}-{edition}.json"]
        prev_path = files[0] if files else None
    prev_titles = set()
    if prev_path:
        try:
            with open(prev_path, encoding="utf-8") as f:
                prev_titles = {norm_title(i.get("title", "")) for i in json.load(f).get("items", [])}
        except Exception as e:
            log(f"  ⚠️ 读上一场失败（{os.path.basename(prev_path)}）：{e}")
    for i in items:
        i["isNew"] = bool(prev_titles) and norm_title(i["title"]) not in prev_titles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", required=True, choices=["afternoon", "night"])
    ap.add_argument("--outdir", default="docs")
    ap.add_argument("--config", default=os.path.join(HERE, "sources.json"))
    ap.add_argument("--keep-days", type=int, default=7, help="归档保留天数")
    a = ap.parse_args()

    cfg = json.load(open(a.config, encoding="utf-8"))
    tcfg = cfg.get("translate", {})
    now = datetime.datetime.now(TZ_CN)
    date = now.strftime("%Y-%m-%d")
    log(f"== 老张新闻源 · {EDITION_NAME[a.edition]} · {date} {now.strftime('%H:%M')} ==")

    all_items = []
    translator = "none"
    for src in cfg.get("sources", []):
        log(f"[{src['id']}] {src['block']}")
        try:
            items, host = fetch_source(src)
        except RuntimeError as e:
            log(f"  ⛔ 该源整体失败，本场少一块（不补位）：{e}")
            continue
        for i in items:
            i["block"] = src["block"]
            i["_llm"] = bool(src.get("translate"))
            # 标题含 CJK（联合早报）→ 保留原标题只做摘要；纯英文（谷歌）→ 标题也译
            i["_translate_title"] = not re.search(r"[\u4e00-\u9fff]", i["title"])
        all_items.extend(items)

    # 2026-09-15：所有源抓完后【一次性】送 LLM（原来是在源循环里每源一次）
    if any(i.get("_llm") for i in all_items):
        translator = llm_translate(all_items, tcfg)

    if not all_items:
        log("⛔ 全部源失败：不写任何文件（保留上一场数据，latest.json 不被覆盖）")
        sys.exit(1)

    # 2026-09-16：按 block 分组、块内按发布时间降序（最新在前）；块顺序同 sources.json
    block_order = [s["block"] for s in cfg.get("sources", [])]
    all_items = group_by_block(all_items, block_order)

    mark_new(all_items, a.outdir, date, a.edition)

    doc = {
        "version": 1,
        "edition": a.edition,
        "editionName": EDITION_NAME[a.edition],
        "date": date,
        "fetched_at": now.strftime("%Y-%m-%d %H:%M"),
        "translator": translator,
        "counts": {
            "total": len(all_items),
            "new": sum(1 for i in all_items if i["isNew"]),
        },
        "items": [{
            "title": i["title"],
            "summary": i.get("summary") or i["desc"],   # AI 失败时降级用 RSS 原文开头，不截断
            "source": i["source"],
            "url": i["url"],
            "pubTime": bj_pub(i["pubDate"]),   # 北京时间智能显示（今天=HH:MM / 昨天 / M月D日）
            "pubTs": bj_iso(i["pubDate"]),     # ISO 北京时间（带 +08:00），供 App 自行格式化
            "block": i["block"],
            "isNew": i["isNew"],
        } for i in all_items],
    }

    news_dir = os.path.join(a.outdir, "news")
    os.makedirs(news_dir, exist_ok=True)
    out = os.path.join(news_dir, f"{date}-{a.edition}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.outdir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    log(f"✅ 写出 {out}（{len(all_items)} 条，新增 {doc['counts']['new']} 条）+ latest.json")

    # 归档清理：超过保留天数的 json 删除（Actions 里随本次提交一并进 git）
    removed = 0
    cutoff = time.time() - a.keep_days * 86400
    for f in glob.glob(os.path.join(news_dir, "*.json")):
        if os.path.getmtime(f) < cutoff:
            os.remove(f)
            removed += 1
    if removed:
        log(f"🧹 清理 {removed} 个超过 {a.keep_days} 天的归档")


def bj_pub(s):
    """北京时间智能相对显示（2026-09-16 起，贴合国内阅读习惯）。

      今天    → 12:34
      昨天    → 昨天 23:22
      7 天内  → 9月15日 23:22
      今年内  → 9月15日 23:22
      跨年    → 2025-09-15 23:22

    注意：不用 %#m/%#d 去前导零（Windows 专有，Linux/Actions 会报错），
    改为手工拼 f"{月}月{日}日"，跨平台安全。
    """
    d = pub_dt(s)
    if not d:
        return ""
    t = d.astimezone(TZ_CN)
    now = datetime.datetime.now(TZ_CN)
    today = now.date()
    if t.date() == today:
        return t.strftime("%H:%M")
    if t.date() == today - datetime.timedelta(days=1):
        return t.strftime("昨天 %H:%M")
    if t.year == now.year:
        return f"{t.month}月{t.day}日 {t.strftime('%H:%M')}"
    return t.strftime("%Y-%m-%d %H:%M")


def bj_iso(s):
    """ISO 8601 北京时间（带 +08:00 时区），供 App 端自行格式化/二次排序。"""
    d = pub_dt(s)
    return d.astimezone(TZ_CN).isoformat() if d else ""


def sort_desc_by_time(items):
    """按 pubDate 降序（最新在前）；无时间的排到最后。"""
    _EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return sorted(items, key=lambda x: pub_dt(x["pubDate"]) or _EPOCH, reverse=True)


def group_by_block(items, block_order):
    """按 block 分组，块内按时间降序；块顺序按 sources.json 的声明顺序。

    2026-09-16 用户要求：两个来源【各自】按时间排序、最新在前（不做全局混排）。
    """
    grouped = {}
    for i in items:
        grouped.setdefault(i["block"], []).append(i)
    ordered = []
    seen = set()
    for blk in block_order:                 # 先按配置顺序输出已知块
        if blk in grouped:
            ordered.extend(sort_desc_by_time(grouped[blk]))
            seen.add(blk)
    for blk, arr in grouped.items():        # 兜底：配置外的块追加在后
        if blk not in seen:
            ordered.extend(sort_desc_by_time(arr))
    return ordered


if __name__ == "__main__":
    main()
