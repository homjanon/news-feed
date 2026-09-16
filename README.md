# 老张新闻源（news-feed）

独立的每日新闻抓取服务：**谷歌新闻（Business·美国区）+ 联合早报（中港台即时）各取最新 10 条 = 20 条**，每天下午（15:20）、夜间（22:30）各抓一场，产出静态 JSON（供「老张工具箱」App 读取）。

> 与 `portfolio` 仓完全解耦：portfolio 的每日 06:30 日报不受本项目任何影响；本项目也**不依赖** portfolio。

## 为什么独立建仓

- **下午茶 / 夜豆浆双场次**：portfolio 的日报是"隔夜+今晨"语义，加"下午场"要动它的模式判定，风险大。独立抓取则一天两场天然成立。
- **App 数据源**：老张工具箱 App（一期）读这里的 JSON 渲染新闻流，阅读统一在 App 内（网页阅读页已于 2026-09-15 下线）。
- **可扩展**：后续新增 RSS 只改 `scripts/sources.json`，不改代码。

## 数据流

```
触发（Cloudflare Worker qdii-dispatch 心跳 → workflow_dispatch）
        ↓
scripts/fetch_news.py --edition afternoon|night
        ├─ 抓谷歌 Business 美国区（62 条 → 去重 → 按 pubDate 倒序 → 取 10；失败换英国区）
        ├─ 抓联合早报中港台即时（多实例兜底 → 去重 → 按 pubDate 倒序 → 取 10）
        ├─ LLM 译标题 + 摘要（按语言拆 2 批：英文组译、中文组原样；三级模型兜底）
        ├─ 按 block 分组、块内按发布时间降序（最新在前）
        └─ 写 docs/news/{日期}-{场次}.json + docs/latest.json（保留 7 天归档）
        ↓
docs/（数据存放）→ App 经 Cloudflare 代理读 raw（Pages 已下线）
```

## 关键设计（踩过的坑）

| 规则 | 原因 |
|---|---|
| **必须按 pubDate 倒序后再取前 N** | 谷歌 feed 顺序是"热度混合"不按时间（实测第1条 04:54、第2条 07:54），直接取前 10 会混入旧闻；早报源顺序同样不可依赖，故两个源都开了 `sort_desc` |
| **选条不用 LLM** | "最新 10 条"是硬规则要可复现；LLM 只做译标题+摘要 |
| **LLM 按语言拆批调用** | 英文组与中文组的 prompt 规则互斥（译 vs 原样），合并成一个 prompt 会让模型"整批统一处理"导致英文标题漏译 |
| **译文必须校验含中文** | 只判"字段非空"会把漏译当成功，静默输出英文且不降级 |
| **翻译失败降级英文** | 翻译链任一环节挂掉都不能让整场空窗 |
| **块内按时间降序** | 两个来源各自最新在前，便于按来源顺序阅读 |
| **场次由外部显式传入** | 脚本不做时间判定，`--edition` 是唯一依据；漏传 `inputs` 会静默落回 `afternoon` 覆盖当天下午茶 |
| **抓取失败不覆盖旧数据** | 本场全失败时脚本退出码 1 且不写文件，`latest.json` 保持上一场内容 |
| **两块独立不补位** | 谷歌挂了就是少一块，不用早报凑 20 条（与 portfolio Top20 口径一致） |

## 场次与触发

| 场次 | 北京时间 | edition | 内容 |
|---|---|---|---|
| 🍵 下午茶 | 15:20 | `afternoon` | 谷歌最新 10 + 早报最新 10 |
| 🥛 夜豆浆 | 22:30 | `night` | 同上（含上一场之后的增量，标「新」） |

**触发方式（单通道 · 2026-09-13 起）**：GitHub 侧已**移除 schedule**，只由 Cloudflare Worker `qdii-dispatch` 定时调用（心跳每 5 分钟），亦可在 Actions 页面手动 Run workflow：

```
POST https://api.github.com/repos/homjanon/news-feed/actions/workflows/fetch.yml/dispatches
Authorization: Bearer <GITHUB_TOKEN>      # 需 repo + workflow 权限
{"ref":"main","inputs":{"edition":"afternoon"}}   # 下午茶
{"ref":"main","inputs":{"edition":"night"}}       # 夜豆浆
```

### ⚠️ 场次判定：脚本不做时间判断，全靠外部传入

**`fetch_news.py` 内没有任何"几点钟就该是哪一场"的逻辑**，它拿到什么 `--edition` 就当场次是什么。完整链路：

```
Cloudflare Worker / Actions 手动 Run
   ↓  {"ref":"main","inputs":{"edition":"night"}}
workflow_dispatch.inputs.edition   ← 定义在 fetch.yml（type: choice, default: afternoon）
   ↓  echo "edition=${{ inputs.edition }}" >> "$GITHUB_OUTPUT"
fetch_news.py --edition <值>
```

**必须显式传 `inputs.edition`**——若触发时不传或传空，GitHub 会落回 workflow 里定义的 `default: afternoon`。后果是：**22:30 那一场也会被当成下午茶，写出 `{日期}-afternoon.json` 覆盖当天下午茶那份，夜豆浆永远不出现**，且不报错、不告警，只静默覆盖。排查时优先检查 Worker 的 payload 是否带 `inputs` 字段。

## LLM 译标题 + 摘要

### 调用方式：按语言拆 2 批

每场**调用 2 次**（不依赖源的划分，而是按语言划分）：

| 批次 | 内容 | prompt 要求 |
|---|---|---|
| 英文组 | 谷歌条目（纯英文） | **必须译成简洁中文**（专有名词用通用译名） |
| 中文组 | 早报条目（纯中文） | **一字不改原样返回标题**，只产出摘要 |

两组各自独立调用、独立降级，互不影响。

> **为什么要拆**：2026-09-15 曾把"每源一次"改为"全场一次"，同一 prompt 内同时出现【英文→译】与【中文→原样】两条**互斥规则**。单源调用时整批同语言、这两条等于废话；合并后模型须逐条判断语言，倾向"整批统一处理"，导致英文标题漏译。9/15 夜场即因此整场退回英文原文（`translator=none(原文)`）。
>
> 另：原回填逻辑只判"译文字段非空"就采纳，**漏译被当作成功**，既不降级也不重试。现已加内容校验：英文条目译文**必须含中文**，否则判该批判失败；整批无任何译文产出亦判失败。

### 模型链（三档，按序尝试）

| 位置 | name | model | Key | Free Tier |
|---|---|---|---|---|
| ① | `gemini-3-flash` | `gemini-3-flash-preview` | `GEMINI_API_KEY` | ✅ 免费（1,500 RPD） |
| ② | `agnes` | `agnes-2.0-flash` | `AGNES_API_KEY` | — |
| ③ | `gemini-3.1-flash-lite` | `gemini-3.1-flash-lite` | `GEMINI_API_KEY` | ✅ 免费（1,000 RPD） |

- Gemini 3 Flash 为主力（质量较高），agnes 为二级，Flash-Lite 兜底；任一组失败自动降级为 RSS 原文（不空窗）。
- Gemini 3 Flash 与 3.1 Flash-Lite 是**独立配额桶**，叠加日上限 2,500 次。配额按 **project** 计（非按 key）；每场仅 2 次调用，余量充足。

**⚠️ 后缀差异（2026-09-16 实测确认，勿随意改动）**：

| 模型 | 正确调用名 | 实测 |
|---|---|---|
| Gemini 3 Flash | **`gemini-3-flash-preview`**（**必须带后缀**） | 无后缀 `gemini-3-flash` → **404 Not Found** |
| Gemini 3.1 Flash-Lite | **`gemini-3.1-flash-lite`**（**不带后缀**） | 现网验证可用 |

Google 只对部分模型做了别名兼容，**两个模型的后缀规则不同，改名前务必先触发一次验证**。

## Secrets（用户自管，与 portfolio 同名）

| Secret | 用途 | 缺失时 |
|---|---|---|
| `GEMINI_API_KEY` | ①③层（gemini-3-flash / gemini-3.1-flash-lite 共用） | 降级下一档 |
| `AGNES_API_KEY` | ②层 | 降级下一档 |

> 三档全缺或全失败时降级为 RSS 原文标题（`translator=none(原文)`），绝不空窗。

## 输出格式

```json
{
  "edition": "afternoon",        // afternoon=下午茶 / night=夜豆浆
  "date": "2026-09-16",
  "fetched_at": "2026-09-16 10:38",
  "translator": "gemini-3-flash(20条)",   // 模型名(处理条数)；none(原文)=未配 Key 或翻译失败
  "counts": { "total": 20, "new": 8 },
  "items": [{
    "title": "标普500收涨，CPI强化加息预期",
    "summary": "美国8月CPI高于预期，交易员定价9月加息概率约90%",
    "source": "Reuters",
    "url": "https://news.google.com/...",
    "pubTime": "09:31",                        // 北京时间智能显示：今天=HH:MM / 昨天 / M月D日
    "pubTs": "2026-09-16T09:31:50+08:00",      // ISO 北京时间，供 App 自行格式化/二次排序
    "block": "谷歌精选",                        // 或 联合早报
    "isNew": true                              // 与上一场比对
  }]
}
```

**`items` 顺序**：按 `block` 分组（谷歌精选 → 联合早报），**块内按发布时间降序、最新在前**。

**`translator` 透明记账**：记录各模型实际处理的条数与失败组，便于判断 AI 是否真生效：

```
gemini-3-flash(20条)                                  # 全部成功
gemini-3-flash(10条) + gemini-3.1-flash-lite(10条)    # 英文组走 3-flash、中文组走 lite
gemini-3-flash(10条) ⚠️降级[summarize:10]             # 中文组失败降级
none(原文)                                            # 全失败 / 未配 Key
```

## 时间显示（北京时间）

`bj_pub()` 分级显示，贴合国内阅读习惯：

| 距今 | 显示 |
|---|---|
| 今天 | `09:31` |
| 昨天 | `昨天 23:22` |
| 今年内 | `9月15日 23:22` |
| 跨年 | `2025-09-15 23:22` |

- 两个源的 `pubDate` 均为 GMT，统一用 `astimezone(TZ_CN)` 转为北京时间。
- 另有 `pubTs` 字段（ISO 8601 带 `+08:00`），供 App 端自行格式化或二次排序。

## 如何新增 RSS 源

只改 `scripts/sources.json` 的 `sources` 数组，加一个对象：

```json
{
  "id": "my-new-source",
  "block": "显示名",
  "take": 10,
  "dedupe": true,
  "sort_desc": true,             // feed 顺序不可信就开（谷歌系必须开）
  "fresh_check": false,          // 拦旧缓存镜像（早报类建议开）
  "title_suffix_source": false,  // 「标题 - 来源」后缀拆媒体名（谷歌系才需要）
  "translate": false,            // 英文源才开
  "urls": ["https://…", "https://…备用实例"]
}
```

App 端按 `block` 自动配色。中文源（如联合早报）建议配 `default_source` 补来源媒体名。

## 数据读取（App 用）

GitHub Pages 不再发布，改为经 Cloudflare 代理读 raw：

```
https://proxy.hellohopo.dpdns.org/?url=<encodeURIComponent(
    https://raw.githubusercontent.com/homjanon/news-feed/main/docs/latest.json )>
```

单场文件：`.../main/docs/news/{日期}-{afternoon|night}.json`

## 本地调试

```bash
# 谷歌源本机被墙时走代理；未配置 LLM Key 会自动降级英文标题
HTTPS_PROXY=http://127.0.0.1:7890 python scripts/fetch_news.py --edition afternoon --outdir docs
```

## 文件结构

```
news-feed/
├── scripts/fetch_news.py    # 抓取 + 硬规则选条 + LLM 译摘要 + 写 JSON
│                            #   _sys_prompt(mode)    两套提示词（translate/summarize）
│                            #   llm_translate        按语言拆批，各组独立降级
│                            #   group_by_block       按 block 分组、块内时间降序
│                            #   bj_pub / bj_iso      北京时间智能显示 / ISO 输出
├── scripts/sources.json     # 源配置 + LLM 模型链（单一数据源）
├── .github/workflows/fetch.yml  # 只接受 workflow_dispatch，透传 inputs.edition
├── docs/latest.json         # 最新一场（App 读取）
└── docs/news/*.json         # 7 天归档
```

## 注意事项

- 本仓**只作为数据存放**，网页阅读页 `docs/index.html` 已于 2026-09-15 删除，阅读统一在「老张工具箱」App 内。
- 抓取失败（全部源挂掉）时脚本退出码 1 且**不写任何文件**，`latest.json` 保持上一场内容。
- 所有条目都有 `source` 字段；联合早报源在 `sources.json` 里配了 `default_source`。
- 时区：所有对外时间均为北京时间（UTC+8）。

免责声明：内容来自公开 RSS，仅供研究参考，不构成投资建议。
