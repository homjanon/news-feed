#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型连通性探测 —— 切模型前先验一遍，避免"改完才发现 404/400/401"。

背景：2026-09-16 曾因模型改名规则不同（Gemini 3 Flash 必须带 `-preview`、3.1 Flash-Lite 不带）
踩过坑；2026-09-17 又遇到"3.8 Flash 对 temperature 等采样参数的处理与旧版不同"的疑点。
本脚本把这些验证做成一次点击：Actions 页面手动 Run 即可，或本地 `python scripts/probe_models.py`。

设计原则：
  · 只发【一个最小请求】，不写任何数据文件、不影响生产流程（绝不覆盖 docs/）
  · 每个候选分别测「带 temperature」与「不带 temperature」，用于发现新版模型的参数兼容性差异
  · 输出结论表 + 可直接照做的建议行

用法：
  python scripts/probe_models.py                 # sources.json 当前链 + 内置候选
  python scripts/probe_models.py --only agnes    # 只测某家
  python scripts/probe_models.py --chain-only    # 只测 sources.json 当前链
仅用标准库（urllib / json），Actions 上零安装依赖。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

PROVIDERS = {
    'gemini': {'base': 'https://generativelanguage.googleapis.com/v1beta/openai',
               'key_env': 'GEMINI_API_KEY'},
    'agnes': {'base': 'https://apihub.agnes-ai.com/v1',
              'key_env': 'AGNES_API_KEY'},
}

# ── 想验证别的模型名，直接加在这里（不用改代码）──
CANDIDATES = [
    ('gemini', 'gemini-3.8-flash'),            # 2026-09-02 GA；多方称无 preview 后缀
    ('gemini', 'gemini-3.8-flash-preview'),    # 备用写法（以防官方实际要求后缀）
    ('gemini', 'gemini-3-flash-preview'),      # 当前主力
    ('gemini', 'gemini-3.5-flash-lite'),       # 拟用三级（500 RPD）
    ('gemini', 'gemini-3.1-flash-lite'),       # 现三级
    ('agnes', 'agnes-3.0-flash'),              # 2026-09-11 上线，免费、512K
    ('agnes', 'agnes-2.5-flash'),              # 现二级
]

SYS = '你是财经新闻编辑。只输出 JSON 数组本身，不要任何解释、不要 markdown 代码块。'
ITEMS = [{'i': 0, 'title': 'Fed cuts rates by 25 basis points',
          'desc': 'The Federal Reserve lowered its benchmark rate.'}]


def log(msg):
    print(msg, flush=True)


def call(base, key, model, with_temp, timeout=90):
    """发一个最小请求，返回 (状态码字符串, 耗时秒, 说明)。"""
    payload = {
        'model': model,
        'messages': [{'role': 'system', 'content': SYS},
                     {'role': 'user', 'content': json.dumps(ITEMS, ensure_ascii=False)}],
        'max_tokens': 600,
    }
    if with_temp:
        payload['temperature'] = 0.2          # 与生产脚本当前一致
    req = urllib.request.Request(
        base.rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        dt = time.time() - t0
        msg = d['choices'][0]['message']
        txt = (msg.get('content') or msg.get('reasoning_content') or '').strip()
        ok = False
        try:
            arr = json.loads(txt)
            ok = isinstance(arr, list) and len(arr) == len(ITEMS)
        except Exception:
            ok = False
        return '200', dt, ('JSON 校验=' + ('OK' if ok else '✗') + ' | 输出: ' + repr(txt[:70]))
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        try:
            body = e.read().decode('utf-8', 'replace')[:170]
        except Exception:
            body = ''
        return str(e.code), dt, body
    except Exception as e:
        return 'ERR', time.time() - t0, f'{type(e).__name__}: {str(e)[:90]}'


def verdict(code, note):
    """把结果翻译成一句人话。"""
    if code == '200':
        return '✅ 可用'
    if code == '404':
        return '❌ 模型名不存在 → 换名字（试 带/不带 -preview）'
    if code == '400':
        return '⚠️ 参数被拒 → 见注（可能是 temperature）' if 'temperature' in note.lower() else '⚠️ 请求参数不被接受'
    if code == '401' or code == '403':
        return '🔑 Key 无效/无权限 → 检查对应 Secret'
    if code == '429':
        return '⏳ 额度或频率超限 → 稍后重试 / 换下一层'
    return '⚠️ 未预期结果'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default=None, choices=['gemini', 'agnes'], help='只测某一家')
    ap.add_argument('--chain-only', action='store_true', help='只测 sources.json 当前链')
    a = ap.parse_args()

    cfg = json.load(open(os.path.join(HERE, 'sources.json'), encoding='utf-8'))
    tcfg = cfg.get('translate', {})

    targets = []   # (label, base, model, key_env)
    for i, m in enumerate(tcfg.get('models', [])):
        targets.append((f'当前链 第{i + 1}层 {m.get("name")}', m.get('base') or '', m.get('model'), m.get('key_env')))
    if not a.chain_only:
        for prov, model in CANDIDATES:
            p = PROVIDERS[prov]
            targets.append((f'候选 [{prov}]', p['base'], model, p['key_env']))

    if a.only:
        targets = [t for t in targets if ('[' + a.only + ']') in t[0] or a.only == 'gemini' and 'googleapis' in t[1] or a.only == 'agnes' and 'agnes' in t[1]]

    keys = {}
    for _, _, _, env in targets:
        if env and env not in keys:
            keys[env] = os.environ.get(env, '')
    log('== 模型探测 ==')
    for env, v in keys.items():
        log(f'  {env}: ' + ('已配置 ✓' if v else '未配置 ✗（该家全部跳过）'))
    log('')

    rows = []
    for label, base, model, env in targets:
        key = os.environ.get(env or '', '')
        if not base or not model:
            log(f'  ⏭️ 跳过 {label}（缺少 base/model）')
            continue
        if not key:
            log(f'  ⏭️ 跳过 {label} / {model}（{env} 未配置）')
            continue
        log(f'[{label}] {model}')
        for with_temp, tag in [(True, '带 temperature'), (False, '不带 temperature')]:
            code, dt, note = call(base, key, model, with_temp)
            log(f'   {tag:16s} → HTTP {code:4s} {dt:5.1f}s | {note}')
            rows.append((model, tag, code, note))
        log('')

    # ── 结论 ──
    log('== 结论 ==')
    seen = {}
    for model, tag, code, note in rows:
        if tag != '带 temperature':
            continue
        seen.setdefault(model, []).append(('带', code))
    for model, tag, code, note in rows:
        if tag == '不带 temperature':
            seen.setdefault(model, []).append(('不带', code))
    for model, res in seen.items():
        codes = dict(res)
        with_t = codes.get('带')
        no_t = codes.get('不带')
        line = f'  {model:32s} '
        if with_t == '200' and no_t == '200':
            line += '✅ 两种写法都可用' + ('' if with_t == '200' else verdict(with_t, ''))
        elif no_t == '200' and with_t != '200':
            line += '⚠️ 仅【不带 temperature】可用 → 该层需要省略 temperature 参数'
        elif with_t == '200' and no_t != '200':
            line += '⚠️ 仅【带 temperature】可用（少见）'
        else:
            line += verdict(with_t or 'ERR', '')
        log(line)
    log('')
    log('提示：探测不写任何数据文件。确认可用后再改 sources.json 的 model 值。')


if __name__ == '__main__':
    main()
