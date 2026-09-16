# 老张新闻源（news-feed）

独立的每日新闻抓取服务：**谷歌新闻（Business·美国区）+ 联合早报（中港台即时）各取最新 10 条 = 20 条**，每天下午（15:20）、夜间（22:30）各抓一场，产出静态 JSON（供「老张工具箱」App 读取）。

> 与 `portfolio` 仓完全解耦：portfolio 的每日 06:30 日报不受本项目任何影响；本项目也**不依赖** portfolio。

## 为什么独立建仓

- **下午茶 / 夜豆浆双场次**：portfolio 的日报是"隔夜+今晨"语义，加"下午场"要动它的模式判定，风险大。独立抓取则一天两场天然成立。
- **App 数据源**：老张工具箱 App（一期）读这里的 JSON 渲染新闻流，阅读统一在「老张工具箱」App 内（网页阅读页已于 2026-09-15 下线）。
- **可扩展**：后续新增 RSS 只改 `scripts/sources.json`，不改代码。

## 数据流

```
触发（Cloudflare Worker qdii-dispatch 心跳 → workflow_dispatch）
        ↓
scripts/fetch_news.py --edition afternoon|night
        ├─ 抓谷歌 Business 美国区（62 条 → 去重 → 按 pubDate 倒序 → 取 10；失败换英国区）
        ├─ 抓联合早报中港台即时（三实例兜底 → 取 10）
        ├─ LLM 中文化谷歌词（Agnes → Gemini 链；失败降级英文原标题，绝不空窗）
        └─ 写 docs/news/{日期}-{场次}.json + docs/latest.json（保留 7 天归档）
        ↓
docs/（数据存放）→ App 经 Cloudflare 代理读 raw（Pages 已下线）
```

## 关键设计（踩过的坑）

| 规则 | 原因 |
|---|---|
| **必须按 pubDate 倒序后再取前 N** | 谷歌 feed 顺序是"热度混合"不按时间（实测第1条 04:54、第2条 07:54），直接取前 10 会混入旧闻 |
| **选条不用 LLM** | "最新 10 条"是硬规则要可复现；LLM 只做译标题+摘要 |
| **翻译失败降级英文** | 翻译链（Agnes/Gemini）任一环节挂掉都不能让整场空窗 |
| **LLM 按语言拆批**（2026-09-16） | 英文组与中文组的 prompt 规则互斥（译 vs 原样），合并调用会让模型"整批统一处理"导致漏译 |
| **译文必须校验含中文**（2026-09-16） | 只判"字段非空"会把漏译当成功，静默输出英文且不降级 |
| **块内按时间降序**（2026-09-16） | 两个来源各自最新在前；早报源顺序不可依赖，需显式 `sort_desc` |
| **抓取失败不覆盖旧数据** | 本场全失败时脚本退出码 1 且不写文件，`latest.json` 保持上一场内容 |
| **两块独立不补位** | 谷歌挂了就是少一块，不用早报凑 20 条（与 portfolio Top20 口径一致） |

## 场次与触发

| 场次 | 北京时间 | edition | 内容 |
|---|---|---|---|
| 🍵 下午茶 | 15:20 | `afternoon` | 谷歌最新 10 + 早报最新 10 |
| 🥛 夜豆浆 | 22:30 | `night` | 同上（含上一场之后的增量，标「新」） |

**触发方式（单通道 · 2026-09-13 起）**：GitHub 侧已**移除 schedule**，只由 Cloudflare Worker `qdii-dispatch` 定时调用（心跳每 5 分钟）：

端点与参数：
   ```
   POST https://api.github.com/repos/homjanon/news-feed/actions/workflows/fetch.yml/dispatches
   Authorization: Bearer <GITHUB_TOKEN>      # 需 repo + workflow 权限
   {"ref":"main","inputs":{"edition":"afternoon"}}   # 下午茶
   {"ref":"main","inputs":{"edition":"night"}}       # 夜豆浆
   ```

## Secrets（用户自管，与 portfolio 同名）

| Secret | 用途 | 缺失时 |
|---|---|---|
| `AGNES_API_KEY` | 谷歌词中文化主模型 | 降级英文标题 |
| `GEMINI_API_KEY` | 第②③层（gemini-3-flash / gemini-3.1-flash-lite 共用） | 同上 |

## 输出格式

```json
{
  "edition": "afternoon",        // afternoon=下午茶 / night=夜豆浆
  "date": "2026-09-12",
  "fetched_at": "2026-09-12 07:04",
  "translator": "gemini-3-flash(20条)",   // 模型名(处理条数)；none(原文)=未配 Key 或翻译失败
  "counts": { "total": 20, "new": 8 },
  "items": [{
    "title": "标普500收涨，CPI强化加息预期",
    "summary": "美国8月CPI高于预期，交易员定价9月加息概率约90%",
    "source": "Reuters",
    "url": "https://news.google.com/...",
    "pubTime": "09:31",                        // 北京时间智能显示：今天=HH:MM / 昨天 / M月D日
    "pubTs": "2026-09-16T09:31:50+08:00",      // ISO 北京时间，供 App 自行格式化
    "block": "谷歌精选",                        // 或 联合早报
    "isNew": true                              // 与上一场比对
  }]
}
```

> **items 顺序**：按 `block` 分组（谷歌精选 → 联合早报），**块内按发布时间降序、最新在前**。

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

App 端按 `block` 自动配色（网页阅读页已下线）。

## 本地调试

```bash
# 谷歌源本机被墙时走代理；未配置 LLM Key 会自动降级英文标题
HTTPS_PROXY=http://127.0.0.1:7890 python scripts/fetch_news.py --edition afternoon --outdir docs
```

## 页面

- 阅读：**已迁至「老张工具箱」App**（网页阅读页 `docs/index.html` 于 2026-09-15 下线）
- 数据：`docs/latest.json`（最新一场）、`docs/news/{日期}-{场次}.json`（7 天归档）

免责声明：内容来自公开 RSS，仅供研究参考，不构成投资建议。

---

## ⚠️ 2026-09-15 变更（请以此为准）

1. **触发方式**：已**取消 GitHub Actions 定时**，只由 **Cloudflare Worker 定时调用**
   （保留手动 Run workflow）。两个场次与参数：
   | 时间（北京） | 场次 | inputs |
   |---|---|---|
   | 15:20 | 下午茶 | `{"ref":"main","inputs":{"edition":"afternoon"}}` |
   | 22:30 | 夜豆浆 | `{"ref":"main","inputs":{"edition":"night"}}` |
   端点：`POST /repos/homjanon/news-feed/actions/workflows/fetch.yml/dispatches`

2. **网页阅读页已下线**：`docs/index.html` 已删除，本仓只作为**数据存放**（`docs/latest.json`、`docs/news/*.json`）。
   阅读统一在「老张工具箱」App 内进行。

3. **数据读取地址（App 用）**：GitHub Pages 不再发布，改为经 Cloudflare 代理读 raw：
   ```
   https://proxy.hellohopo.dpdns.org/?url=<encodeURIComponent(
       https://raw.githubusercontent.com/homjanon/news-feed/main/docs/latest.json )>
   ```
   单场文件：`.../main/docs/news/{日期}-{afternoon|night}.json`

4. **来源字段**：所有条目都有 `source`；联合早报源在 `sources.json` 里配了 `default_source: 联合早报`。

5. **LLM 调用**：全场**只调一次**（合并两个源），整批失败会自动拆 2 批重试，仍失败才降级为 RSS 原文。

---

## ⚠️ 2026-09-16 变更（请以此为准，覆盖上文第 5 条）

### 1. LLM 改为「按语言拆批」调用（每场 2 次）

**事故背景**：9/15 把"每源一次"改成"全场一次"后，当晚夜豆浆整场退回英文原文（`translator=none(原文)`）。

**根因**：合并后同一个 prompt 内同时出现**互斥指令**——谷歌条目（英文）要求"译成中文"，早报条目（中文）要求"一字不改原样返回"。单源调用时整批同语言、这两条规则等于废话；合并后模型必须**逐条判断语言**，倾向于"整批统一处理"，导致英文标题漏译。而原回填代码只判"译文字段非空"就采纳，**漏译被当作成功**，既不降级也不重试 → 静默输出英文。

> 旁证：9/15 下午茶（未报错）里，同为英文的条目 `[1]` 未译、`[2]` 已译——**同一批内行为不一致**，说明退化早已发生。

**修复**：
- `llm_translate` 按语言分组：**英文组**（译标题+摘要）、**中文组**（标题原样+摘要），各自独立调用、独立降级；
- `_sys_prompt(smin, smax, mode)` 拆成两套提示词，**各自指令单一、无条件分支**；
- `_call_llm_batch` 新增**内容校验**：英文条目的译文必须含中文（否则判失败）；整批无任何译文产出也判失败。**彻底杜绝"漏译当成功"**。

**调用次数**：每场 **2 次**（英文组 1 次 + 中文组 1 次）。Gemini 免费层 1,500 RPD，一天 2 场共 4 次，余量充足。

### 2. LLM 模型链（三档）

| 位置 | name | model | Key | Free Tier |
|---|---|---|---|---|
| ① | `agnes` | `agnes-2.0-flash` | `AGNES_API_KEY` | — |
| ② | `gemini-3-flash` | `gemini-3-flash-preview` | `GEMINI_API_KEY` | ✅ 免费（1,500 RPD） |
| ③ | `gemini-3.1-flash-lite` | `gemini-3.1-flash-lite` | `GEMINI_API_KEY` | ✅ 免费（1,000 RPD） |

> Gemini 3 Flash 与 3.1 Flash-Lite 是**独立配额桶**，叠加日上限 2,500 次。配额按 **project** 计（非按 key）。
> `gemini-3.1-flash-lite` 沿用无 `-preview` 后缀的调用名（现网已验证可用，**勿改**）；新增的 3 Flash 用官方 `-preview` id。

### 3. 时间显示：统一北京时间 + 智能相对格式

`bj_pub()` 改为分级显示（贴合国内阅读习惯）：

| 距今 | 显示 |
|---|---|
| 今天 | `09:31` |
| 昨天 | `昨天 23:22` |
| 今年内 | `9月15日 23:22` |
| 跨年 | `2025-09-15 23:22` |

- **时区本身无误**：两个源的 `pubDate` 均为 GMT，`astimezone(TZ_CN)` 早已正确转为北京时间，此次仅优化**展示粒度**。
- **新增字段 `pubTs`**：ISO 8601 北京时间（如 `2026-09-16T09:31:50+08:00`），供 App 端自行格式化或二次排序。

### 4. 排序：两个来源**各自按时间降序**（最新在前）

- `main()` 新增 `group_by_block()`：按 `block` 分组，**块内按 `pubDate` 降序**；块顺序 = `sources.json` 声明顺序（谷歌精选 → 联合早报）；
- 早报源 `sort_desc` 由 `false` 改为 **`true`**——原先恰好有序是巧合，现显式排序不再依赖源站顺序；
- **不做全局混排**（保持"两块独立不补位"的设计）。

### 5. `translator` 字段改为透明记账

不再只写模型名，而是记录**各模型实际处理的条数与失败组**，例如：

```
gemini-3-flash(20条)                      # 全部成功
gemini-3-flash(10条) + gemini-3.1-flash-lite(10条)   # 英文组走 3-flash、中文组走 lite
gemini-3-flash(10条) ⚠️降级[summarize:10]            # 中文组失败降级
none(原文)                                 # 全失败 / 未配 Key
```

便于一眼判断"AI 到底有没有真生效"。

