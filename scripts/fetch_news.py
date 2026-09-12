#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""老张自建新闻源：RSS 抓取 → 硬规则选条 → 可选 LLM 中文化 → 静态 JSON。

设计原则（2026-09-12 与用户确认的方案）：
  1. 选条是硬规则：标题去重 + 按 pubDate 倒序取前 N 条，LLM 不参与选条；
  2. LLM 只做「译标题 + 一句话摘要」，且失败时降级保留原文标题，绝不因翻译失败空窗；
  3. 源全部配置化（scripts/sources.json），后续新增 RSS 只改配置不改代码；
  4. 抓取失败保留上一场数据（本脚本只写当天文件，不删旧文件，latest.json 由成功场次覆盖）。

用法：
  python scripts/fetch_news.py --edition morning  --outdir docs
  python scripts/fetch_news.py --edition afternoon --outdir docs

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
EDITION_NAME = {"morning": "早咖啡", "afternoon": "下午茶"}


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
            items = items[: int(cfg.get("take", 10))]
            log(f"  ✅ {host} → 采纳 {len(items)} 条")
            return items, host
        except Exception as e:
            last_err = e
            log(f"  ❌ {host} → {type(e).__name__}: {str(e)[:90]}")
            time.sleep(1)
    raise RuntimeError(f"所有实例失败，最后一个错误：{last_err}")


def llm_translate(items, tcfg):
    """对 block 含需翻译条目做「译标题+摘要」。任一模型成功即返回；全失败保留原文。"""
    todo = [i for i in items if i.get("_need_translate")]
    if not todo:
        return "none"
    smax = int(tcfg.get("summary_max", 60))
    sys_prompt = (
        "你是财经新闻编辑。输入是 JSON 数组 [{\"i\":序号,\"title\":英文标题,\"desc\":英文摘要}]。"
        f"输出同样是 JSON 数组 [{{\"i\":序号,\"title\":中文标题,\"summary\":中文摘要}}]，"
        f"title 翻译成简洁中文（专有名词保留通用译名），summary 不超过 {smax} 字、概括要点。"
        "只输出 JSON 数组本身，不要任何解释、不要 markdown 代码块。")
    for m in tcfg.get("models", []):
        key = os.environ.get(m.get("key_env", ""))
        if not key:
            log(f"  ⏭️ 跳过 {m['name']}：环境变量 {m.get('key_env')} 未设置（Secret 未配置时属预期）")
            continue
        payload = {
            "model": m["model"],
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(
                    [{"i": n, "title": i["title"], "desc": i["desc"][:400]}
                     for n, i in enumerate(todo)], ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "max_tokens": 4000,
        }
        try:
            req = urllib.request.Request(
                m["base"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
            arr = json.loads(text)
            if not isinstance(arr, list) or len(arr) != len(todo):
                raise RuntimeError(f"返回条数不符（期望 {len(todo)}，得 {len(arr) if isinstance(arr, list) else '非数组'}）")
            for row in arr:
                i = int(row.get("i", -1))
                if 0 <= i < len(todo):
                    t = str(row.get("title", "")).strip()
                    s = str(row.get("summary", "")).strip()
                    if t:
                        todo[i]["title"] = t
                    if s:
                        todo[i]["summary"] = s[: smax + 10]
            log(f"  🌐 中文化完成（{m['name']} / {m['model']}，{len(todo)} 条）")
            return m["name"]
        except Exception as e:
            log(f"  ⚠️ {m['name']} 失败：{type(e).__name__}: {str(e)[:120]}")
    log("  ⬇️ 翻译全部失败，降级保留英文原标题（不空窗）")
    return "none(原文)"


def mark_new(items, outdir, date, edition):
    """与「同日期另一场」比对打 isNew；没有则与最近一份历史归档比对。"""
    prev_path = None
    other = "afternoon" if edition == "morning" else "morning"
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
    ap.add_argument("--edition", required=True, choices=["morning", "afternoon"])
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
            i["_need_translate"] = bool(src.get("translate"))
        if src.get("translate"):
            translator = llm_translate(items, tcfg)
        all_items.extend(items)

    if not all_items:
        log("⛔ 全部源失败：不写任何文件（保留上一场数据，latest.json 不被覆盖）")
        sys.exit(1)

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
            "summary": i.get("summary", i["desc"][:80]),
            "source": i["source"],
            "url": i["url"],
            "pubTime": bj_pub(i["pubDate"]),
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
    d = pub_dt(s)
    return d.astimezone(TZ_CN).strftime("%m-%d %H:%M") if d else ""


if __name__ == "__main__":
    main()
