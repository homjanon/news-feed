# 老张新闻源（news-feed）

独立的每日新闻抓取服务：**按场次错开**——下午茶（15:20）= 谷歌精选 + 中国视野（联合早报-中港台）+ 财经深度（财联社）；夜豆浆（22:30）= 谷歌精选 + 国际视野（联合早报-国际）+ 财经深度（财联社）。每块取 7 条，产出静态 JSON（供「老张工具箱」App 读取）。

> 与 `portfolio` 仓完全解耦：portfolio 的每日日报不受本项目任何影响；本项目也**不依赖** portfolio。

## 为什么独立建仓

- **下午茶 / 夜豆浆双场次**：portfolio 的日报是"隔夜+今晨"语义，加"下午场"要动它的模式判定，风险大。独立抓取则一天两场天然成立。
- **App 数据源**：老张工具箱 App 读这里的 JSON 渲染新闻流，阅读统一在 App 内（网页阅读页已于 2026-09-15 下线）。
- **可扩展**：新增 RSS 只改 `scripts/sources.json`，不改代码。

## 数据流

```
触发（Cloudflare Worker qdii-dispatch 心跳 → workflow_dispatch）
        ↓
scripts/fetch_news.py --edition afternoon|night
        ├─ 抓谷歌 Business 美国区（去重 → 按 pubDate 倒序 → 取 7；失败换英国区）
        ├─ 抓联合早报（下午=中港台 / 夜间=国际；多实例兜底 → 去重 → 按 pubDate 倒序 → 取 7）
        ├─ 抓财联社 /cls/depth/1000（多实例兜底 → 去重 → 按 pubDate 倒序 → 取 7）
        ├─ LLM 译标题 + 摘要（按语言拆 2 批：英文组译、中文组原样；三级模型兜底）
        ├─ 按 block 分组、块内按发布时间降序（最新在前）
        └─ 写 docs/news/{日期}-{场次}.json + docs/latest.json（保留 7 天归档）
        ↓
docs/（数据存放）→ App 经 Cloudflare 代理读 raw（Pages 已下线）
```

## 源清单（当前）

| 块 | 来源 | 路由 | 场次 | 条数 |
|---|---|---|---|---|
| 谷歌精选 | Google News Business（美区，失败换英区） | `news.google.com/rss/topics/...` | 两场 | 7 |
| 中国视野 | 联合早报 | `zaobao/realtime/china` | 仅下午茶 | 7 |
| 国际视野 | 联合早报 | `zaobao/realtime/world` | 仅夜豆浆 | 7 |
| 财经深度 | 财联社 | `cls/depth/1000` | 两场 | 7 |

- 联合早报、财联社均为中文源 → 归入「中文组」，标题原样、只产摘要（无需翻译）。
- 谷歌为英文源 → 归入「英文组」，译标题 + 产摘要。

## 场次与触发

| 场次 | 北京时间 | edition |
|---|---|---|
| 🍵 下午茶 | 15:20 | `afternoon` |
| 🥛 夜豆浆 | 22:30 | `night` |

**触发方式（单通道）**：GitHub 侧无 schedule，只由 Cloudflare Worker `qdii-dispatch` 定时调用（心跳每 5 分钟），亦可在 Actions 页面手动 Run workflow：

```
POST https://api.github.com/repos/homjanon/news-feed/actions/workflows/fetch.yml/dispatches
Authorization: Bearer <GITHUB_TOKEN>      # 需 repo + workflow 权限
{"ref":"main","inputs":{"edition":"afternoon"}}   # 下午茶
{"ref":"main","inputs":{"edition":"night"}}       # 夜豆浆
```

## LLM 译标题 + 摘要

### 调用方式：按语言拆 2 批

每场**调用 2 次**（按语言划分，不依赖源的划分）：

| 批次 | 内容 | prompt 要求 |
|---|---|---|
| 英文组 | 谷歌条目（纯英文） | **必须译成简洁中文**（专有名词用通用译名） |
| 中文组 | 早报 / 财联社条目（纯中文） | **一字不改原样返回标题**，只产出摘要 |

两组各自独立调用、独立降级，互不影响。

### 模型链（三档，按序尝试）

| 位置 | name | model | Key | Free Tier |
|---|---|---|---|---|
| ① | `gemini-3-flash` | `gemini-3-flash-preview` | `GEMINI_API_KEY` | ✅ 免费 |
| ② | `agnes-3.0-flash` | `agnes-3.0-flash` | `AGNES_API_KEY` | ✅ 免费（输入/输出/缓存均 $0）|
| ③ | `gemini-3.5-flash-lite` | `gemini-3.5-flash-lite` | `GEMINI_API_KEY` | ✅ 免费（500 RPD）|

- Gemini 3 Flash 为主力（质量较高），agnes 为二级，Flash-Lite 兜底；任一组失败自动降级为 RSS 原文（不空窗）。
- 配额按 **project** 计（非按 key）；每场仅 2 次调用，余量充足。

**⚠️ 模型名后缀差异（实测确认，勿随意改动）**：

| 模型 | 正确调用名 |
|---|---|
| Gemini 3 Flash | **`gemini-3-flash-preview`**（**必须带 `-preview`**） |
| Gemini 3.5 Flash-Lite | **`gemini-3.5-flash-lite`**（不带后缀） |
| Agnes 3.0 Flash | **`agnes-3.0-flash`**（不带后缀） |

Google 只对部分模型做了别名兼容，**两个 Gemini 模型的后缀规则不同，改名前务必先跑 `probe-models` 验证**。

## Secrets（用户自管）

| Secret | 用途 | 缺失时 |
|---|---|---|
| `GEMINI_API_KEY` | ①③层共用 | 降级下一档 |
| `AGNES_API_KEY` | ②层 | 降级下一档 |

> 三档全缺或全失败时降级为 RSS 原文标题（`translator=none(原文)`），绝不空窗。

## 输出格式

```json
{
  "edition": "afternoon",        // afternoon=下午茶 / night=夜豆浆
  "editionName": "下午茶",
  "date": "2026-10-05",
  "fetched_at": "2026-10-05 15:20",
  "translator": "gemini-3-flash(14条)",   // 模型名(处理条数)；none(原文)=未配 Key 或翻译失败
  "counts": { "total": 21, "new": 8 },
  "items": [{
    "title": "港股收盘 | 三大指数集体收涨 算力硬件产业链领跑",
    "summary": "港股三大指数今日分化震荡，恒指涨0.28%报24040.34点……",
    "source": "财联社",
    "url": "https://www.cls.cn/detail/2497884",
    "pubTime": "16:37",                        // 北京时间智能显示：今天=HH:MM / 昨天 / M月D日
    "pubTs": "2026-10-05T16:37:11+08:00",      // ISO 北京时间，供 App 自行格式化/二次排序
    "block": "财经深度",                        // 谷歌精选 / 中国视野 / 国际视野 / 财经深度
    "isNew": true                              // 与上一场比对
  }]
}
```

**`items` 顺序**：按 `block` 分组（顺序同 `sources.json`），**块内按发布时间降序、最新在前**。

**`translator` 透明记账**：记录各模型实际处理的条数与失败组，便于判断 AI 是否真生效：

```
gemini-3-flash(14条)                                  # 全部成功
gemini-3-flash(7条) + gemini-3.5-flash-lite(7条)      # 英文组走 3-flash、中文组走 lite
gemini-3-flash(7条) ⚠️降级[summarize:7]               # 中文组失败降级
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

- 各源的 `pubDate` 均为 GMT，统一用 `astimezone(TZ_CN)` 转为北京时间。
- 另有 `pubTs` 字段（ISO 8601 带 `+08:00`），供 App 端自行格式化或二次排序。

## 如何新增 RSS 源

只改 `scripts/sources.json` 的 `sources` 数组，加一个对象：

```json
{
  "id": "my-new-source",
  "block": "显示名",
  "take": 7,
  "dedupe": true,
  "sort_desc": true,             // feed 顺序不可信就开（谷歌系必须开）
  "fresh_check": false,          // 拦旧缓存镜像（早报类建议开）
  "title_suffix_source": false,  // 「标题 - 来源」后缀拆媒体名（谷歌系才需要）
  "translate": true,             // 必填 true，否则连摘要都不会有
  "editions": ["afternoon"],     // 限定场次（不填=两场都跑）
  "urls": ["https://…", "https://…备用实例"],
  "default_source": "某某媒体"    // 无来源时补此固定值（中文源建议配）
}
```

App 端按 `block` 自动配色。

## 数据读取（App 用）

GitHub Pages 不再发布，改为经 Cloudflare 代理读 raw：

```
https://proxy.hellohopo.dpdns.org/?url=<encodeURIComponent(
    https://raw.githubusercontent.com/homjanon/news-feed/main/docs/latest.json )>
```

单场文件：`.../main/docs/news/{日期}-{afternoon|night}.json`

## 本地调试

```bash
# 谷歌源本机被墙时走代理；未配置 LLM Key 会自动降级为 RSS 原文
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
├── scripts/probe_models.py  # 模型连通性探测（换模型前先验：名字/参数/Key）
├── .github/workflows/fetch.yml  # 只接受 workflow_dispatch，透传 inputs.edition
├── docs/latest.json         # 最新一场（App 读取）
└── docs/news/*.json         # 7 天归档
```

## 关键设计（踩过的坑）

| 规则 | 原因 |
|---|---|
| **必须按 pubDate 倒序后再取前 N** | 谷歌 / 早报 feed 顺序都不可信（谷歌是"热度混合"，实测第1条比第2条还早），直接取前 N 会混入旧闻；故所有源都开 `sort_desc` |
| **选条不用 LLM** | "最新 N 条"是硬规则要可复现；LLM 只做译标题 + 摘要 |
| **LLM 按语言拆批调用** | 英文组与中文组的 prompt 规则互斥（译 vs 原样），合并成一个 prompt 会让模型"整批统一处理"导致英文标题漏译 |
| **译文必须校验含中文** | 只判"字段非空"会把漏译当成功，静默输出英文且不降级；须校验译文确含中文字符 |
| **翻译失败降级原文** | 翻译链任一环节挂掉都不能让整场空窗 |
| **场次由外部显式传入** | 脚本不做时间判定，`--edition` 是唯一依据；漏传 `inputs` 会静默落回 `afternoon` 覆盖当天下午茶（**22:30 那场会被写成下午茶，夜豆浆永不出现，且不报错**） |
| **抓取失败不覆盖旧数据** | 本场全失败时脚本退出码 1 且不写文件，`latest.json` 保持上一场内容 |
| **块独立不补位** | 某块挂了就是少一块，不用其他源凑数 |
| **中文源必须配 `default_source`** | 联合早报 / 财联社的 RSS 无 `<source>` 标签，不配会导致来源字段为空 |
| **`translate` 必须为 true** | `_llm = bool(src.get("translate"))`，设 false 的源**连中文摘要都不会有** |

## 注意事项

- 本仓**只作为数据存放**，网页阅读页 `docs/index.html` 已于 2026-09-15 删除，阅读统一在「老张工具箱」App 内。
- 抓取失败（全部源挂掉）时脚本退出码 1 且**不写任何文件**，`latest.json` 保持上一场内容。
- 所有条目都有 `source` 字段。
- 时区：所有对外时间均为北京时间（UTC+8）。
- ⚠️ **`/cls/depth/1000` 本质是「头条」流**：RSSHub 该路由返回的频道名是「财联社 - 头条」，前 7 条约 6/7 为纯财经，夹带少量非财经（文化/社会/时政）属路由固有限制，已确认接受。
- 财联社 **实例池单独排序**（其余源的实例池顺序为全项目统一）：`rsshub.isrss.com` / `rsshub.ktachibana.party` / `rsshub-balancer.virworks.moe` 实测可用排最前；`rss.injahow.cn` 实测 503，与 slarker / umzzz / rssforever 一同降为兜底。

免责声明：内容来自公开 RSS，仅供研究参考，不构成投资建议。
