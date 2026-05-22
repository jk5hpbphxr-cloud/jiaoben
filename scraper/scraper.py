# -*- coding: utf-8 -*-
"""
scraper.py  v2.15
=================
百家号选题素材抓取器（P0 bug 修复 + 错误日志版 + 网易移动端入口 + 头条正文 Playwright + 古代硬配额 + GLM 选题分析）

v2.15 改动（基于 v2.14）：
1. ★ --analyze 新增：TOP 50 排好序后调 GLM-4.7-Flash 做精筛 + B5 主体类型判断
2. analyzer.analyze_top_n() 把 glm_analysis 字段写回每条 item
3. 触发逻辑（B 方案）：--analyze 单独可用；有 body_texts 用 body_texts，没有则降级用 card_excerpt
4. 不污染主流程：默认 --analyze 关闭（铁律 1+2）

v2.14 改动（基于 v2.13）：
1. ★ 古代硬配额：TOP 50 中古代题材 ≤ 7 条（默认开启，--no-ancient-quota 可关）
2. ★ --ancient-quota N 可临时覆盖配额值（如 8、10）
3. 配套 topic_config.py：ANCIENT 权重 0.6 → 0.3
4. 实测证据：v2.13 下 TOP 50 古代 28 条远超期望，单改权重无效（候选不足），故引入硬配额

v2.13 改动（基于 v2.12）：
1. ★ 头条正文改用 Playwright 抓（NewsCrawler 反爬抓不动）
2. 搜狐+163 继续走 NewsCrawler HTTP，互不影响
3. 复用 PROFILE_DIR + ANTI_DETECT_JS，无需新策略

v2.12 改动（基于 v2.11）：
1. ★ Bug1 修复：linkPattern 加 news/ 匹配移动端 URL
2. 修复 card 容器爬太高导致跟贴数虚高（signalText 500字回退）
3. --m163 改为默认开启（--no-m163 可关闭）
4. normalize_article_url 加 m.163.com + news/ 支持
5. ★ 新增 --fetch-content：调 NewsCrawler API 拿正文+图片

v2.11 改动（基于 v2.10，补诊断 + 修截屏 bug）：
1. Bug1 诊断
   - v2.9 实测发现：m.163.com/ 根路径返回的是【门户索引页】（要闻/推荐/原创/...）
     不是带【首页/国内/国际/历史】tab 的现代版
   - 用户通过手机 Safari 长按链接 + PC 浏览器多次验证，找到真正的深链：
     https://m.163.com/touch/news/sub/history  ←  直接落在"历史"tab，0 点击
2. 新策略：移动端不再 goto m.163.com 后 click-tab，直接 goto 历史频道深链
   - 删除 _scrape_163_mobile_tab 函数（点 tab 策略）—— 不再需要
   - 复用 _scrape_163_mobile_direct，URL 列表默认就是 [NETEASE_MOBILE_HISTORY]
   - --m163-url 后门保留：未来 URL 改了你可以临时覆盖
3. 保留 v2.9 全部其他改动：iOS 17 UA、截屏调试、跟贴正则

平台：
    今日头条（toutiao） → 历史频道
    搜狐历史（sohu）
    网易历史（163）→ v2.10 直链 m.163.com/touch/news/sub/history

使用：
    python scraper.py                    # 抓全部三个平台（默认行为不变，163 走老桌面入口）
    python scraper.py --m163             # ★ 163 走移动端历史深链（推荐）
    python scraper.py --m163 --m163-url https://m.163.com/other-url
                                         # 后门：覆盖默认 URL（未来网易改链时用）
    python scraper.py --only 163 --m163  # 只测试 163
    python scraper.py --only toutiao     # 只抓头条
    python scraper.py --skip toutiao     # 跳过头条
    python scraper.py --headless         # 无头模式（头条不建议）
    python scraper.py --min-pop 30       # 全局热度阈值
    python scraper.py --min-pop-toutiao 30 --min-pop-sohu 3000 --min-pop-163 30
    python scraper.py --max-items 100    # 覆盖每平台最大抓取条数
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Playwright
try:
    from playwright.async_api import async_playwright
except ImportError:
    print("[错误] 缺少依赖：py -m pip install playwright && py -m playwright install chromium")
    sys.exit(1)

# 日志基础设施（铁律 4）
import logging

logger = logging.getLogger("scraper")

try:
    from logger_utils import (
        setup_logger, log_scrape_start, log_scrape_done, log_scrape_fail,
        log_heat_item, log_heat_summary,
        log_dedup_url, log_dedup_published, log_dedup_crossplat,
        log_score_item, log_exception,
    )
except ImportError:
    setup_logger = None
    print("[警告] 找不到 logger_utils.py，日志仅输出到控制台（不写文件）")
    def _noop(*a, **kw): pass
    log_scrape_start = log_scrape_done = log_scrape_fail = _noop
    log_heat_item = log_heat_summary = _noop
    log_dedup_url = log_dedup_published = log_dedup_crossplat = _noop
    log_score_item = log_exception = _noop

# 本地模块
try:
    from topic_config import score_title
except ImportError:
    print("[警告] 找不到 topic_config.py，评分功能将禁用")
    score_title = None

try:
    from my_published import PublishedIndex
except ImportError:
    print("[警告] 找不到 my_published.py，已发去重将禁用")
    PublishedIndex = None

try:
    from dedup_utils import cross_platform_dedup
except ImportError:
    print("[警告] 找不到 dedup_utils.py，跨平台去重将禁用")
    cross_platform_dedup = None

# ============================================================
# 配置
# ============================================================
BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
PROFILE_DIR = BASE_DIR / "browser_profile"
MOBILE_PROFILE_DIR = BASE_DIR / "browser_profile_mobile"  # ★ v2.7: 移动端独立 profile
OUTPUT_DIR.mkdir(exist_ok=True)
PROFILE_DIR.mkdir(exist_ok=True)
# MOBILE_PROFILE_DIR 按需创建（启用 --m163 时才建），不强制

# ★ v2.10: 网易移动端历史频道直链
# v2.7-v2.9 走的弯路：试过 m.163.com/news/history/（404）和 m.163.com/（门户索引页）
# 用户手机验证（图 11，12）后确认真深链是 /touch/news/sub/history
# 这个 URL 打开即在"历史"tab，无需点击，直接渲染古代历史卡片 + X跟贴热度
NETEASE_MOBILE_HISTORY = "https://m.163.com/touch/news/sub/history"

# 保留旧常量名作软兼容（虽然不再使用，避免外部脚本如果引用了它会炸）
NETEASE_MOBILE_HOME = "https://m.163.com/"

# ★ v2.9: 真实 iOS 17 Safari UA
# v2.8 实测 Playwright 默认 iPhone 13 UA（Version/26.0）被 163 识别为非真实浏览器，
# 返回了简化老版本页面（要闻/推荐/原创平铺）而不是用户手机 Edge 看到的现代版本
# （首页/国内/国际/历史 tab）。换成真实 iOS 17 UA 试试能否拿到正常版本。
MOBILE_UA_IOS17 = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1"
)

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

ANTI_DETECT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
window.chrome = { runtime: {} };
"""


def _delay():
    return random.uniform(2.0, 4.0)


def _parse_num(text: str) -> int:
    """把 '1.2万'/'3千'/'500'/'1.5w' 转成 int。失败返回 0。"""
    if not text:
        return 0
    text = str(text).strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万千wWk]?)", text)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except Exception:
        return 0
    unit = m.group(2).lower()
    if unit in ("万", "w"):
        return int(n * 10000)
    if unit in ("千", "k"):
        return int(n * 1000)
    return int(n)


# ============================================================
# Bug2 修复：URL 归一化（切掉搜狐 spm/scm 等 query 参数）
# ============================================================
def normalize_article_url(url: str, source: str = None) -> str:
    """
    把列表页抓到的 URL 归一化成"等价类代表"，用于去重。
    搜狐 a/数字_数字、头条 article/数字、网易 article/XXX.html 各有专用正则，
    其他 URL 通用切掉 query string。归一化是幂等的。
    """
    if not url:
        return url
    if source is None:
        if "sohu.com" in url:
            source = "sohu"
        elif "163.com" in url:
            source = "163"
        elif "toutiao.com" in url:
            source = "toutiao"
    if source == "sohu":
        m = re.match(r"(https?://(?:www\.)?sohu\.com/a/\d+_\d+)", url)
        if m:
            return m.group(1)
    if source == "toutiao":
        m = re.match(r"(https?://(?:www\.)?toutiao\.com/(?:article|group|w|s|i\d|a\d)/\d+)", url)
        if m:
            return m.group(1)
    if source == "163":
        m = re.match(r"(https?://(?:(?:www|m)\.)?163\.com/(?:dy/|news/)?article/[A-Z0-9]+\.html)", url)
        if m:
            return m.group(1)
    return url.split("?", 1)[0]


# ============================================================
# 通用卡片抓取 JS（v2.5 核心新增）
# ============================================================
# 这段 JS 在浏览器里执行：
# 1) 找出所有匹配 link_pattern 的 a 标签
# 2) 沿 parentElement 往上找最近的"卡片"容器（高度 > 60 且文本 > 标题长度）
# 3) 在卡片文本里正则提取所有数字+单位组合
# 4) 返回 {title, url, popularity_raw} 数组
_CARDS_JS = r"""
(linkPatternStr) => {
    const linkPattern = new RegExp(linkPatternStr, 'i');
    const out = [];
    const seen = new Set();
    const anchors = document.querySelectorAll('a');

    // 在卡片文本里抓数字的正则（中文优先）
    // 匹配 "120 评论"、"1.2万阅读"、"500赞"、"3千跟帖"、"100w 阅读"
    // 单位词放最前可读性更好，但 JS 里我们循环匹配所有出现的位置
    // ★ v2.7: 加入"跟贴"（网易手机版用词，PC 端是"跟帖"）
    const reSignal = /(\d+(?:\.\d+)?)\s*([万千wWk]?)\s*(评论|阅读|阅|赞|跟帖|跟贴|播放|喜欢|点赞|浏览|查看|参与)/g;

    for (const a of anchors) {
        const href = a.href || '';
        if (!linkPattern.test(href)) continue;

        // Bug3 修复：优先从子标题元素取文本，避免吞入摘要/作者/阅读量
        const titleEl = a.querySelector('h2,h3,h4,strong,b,.title');
        let title;
        if (titleEl) {
            title = (titleEl.innerText || titleEl.textContent || '').trim();
        } else {
            title = ((a.innerText || a.textContent || '').split('\n')[0] || '').trim();
        }
        if (title.length < 8 || title.length > 200) continue;
        // 通用噪音过滤（菜单/按钮）
        if (/^(下载|登录|关注|发布作品|消息|频道列表|个人中心|添加到桌面|侵权举报|跟帖评论|更多|首页|返回顶部)/.test(title)) continue;
        if (seen.has(href)) continue;
        seen.add(href);

        // 往上找卡片容器（最多 6 层）
        let card = a;
        for (let i = 0; i < 6; i++) {
            if (!card.parentElement) break;
            const p = card.parentElement;
            if (p.tagName === 'BODY' || p.tagName === 'HTML') break;
            card = p;
            // 卡片高度足够 + 文本明显比标题多 → 认为是合理的卡片
            if (card.offsetHeight >= 60 && (card.innerText || '').length > title.length + 5) break;
        }
        const cardText = (card.innerText || '').trim();

        // v2.12: 防止 card 爬太高把整页跟贴数吃进来
        const signalText = cardText.length > 500 ? (a.innerText || '').trim() : cardText;

        // 收集所有热度信号
        const signals = [];
        let m;
        // reSignal 是 /g 全局匹配，需要 reset
        reSignal.lastIndex = 0;
        while ((m = reSignal.exec(signalText)) !== null) {
            signals.push({
                num: m[1],
                unit: m[2] || '',
                kind: m[3],
                raw: m[0]
            });
        }

        out.push({
            title: title,
            url: href,
            signals: signals,
            // 卡片文本截断 200 字（调试用）
            card_excerpt: cardText.slice(0, 200)
        });
    }
    return out;
}
"""


def _pick_popularity(signals: list[dict]) -> tuple[int, str]:
    """
    从信号列表里挑最能代表热度的那个。
    优先级：阅读 > 浏览/查看 > 跟帖 > 评论 > 播放 > 点赞/赞 > 喜欢/参与
    返回 (int 数值, 原始文本)
    """
    if not signals:
        return 0, ""

    priority = {
        "阅读": 10, "阅": 10, "浏览": 9, "查看": 9,
        "跟帖": 8, "跟贴": 8,  # ★ v2.7: 跟贴 = 跟帖（网易手机/PC 用词差异）
        "评论": 7, "播放": 6,
        "点赞": 5, "赞": 5, "喜欢": 4, "参与": 4,
    }

    best_score = -1
    best_num = 0
    best_raw = ""
    for s in signals:
        kind = s.get("kind", "")
        num_text = f"{s.get('num','')}{s.get('unit','')}"
        n = _parse_num(num_text)
        score = priority.get(kind, 1)
        # 阅读类必须 > 0，其他至少有数
        if n <= 0:
            continue
        if score > best_score or (score == best_score and n > best_num):
            best_score = score
            best_num = n
            best_raw = s.get("raw", "")
    return best_num, best_raw


# ============================================================
# 公共：从 page 里抽卡片信号 + 计算 popularity
# ============================================================
async def _harvest_cards(page, link_pattern: str, source: str, max_items: int) -> list[dict]:
    """三平台共用：执行 _CARDS_JS、解析 signals、URL 归一化去重、生成 item 列表"""
    items: list[dict] = []
    try:
        raw = await page.evaluate(_CARDS_JS, link_pattern)
    except Exception as e:
        logger.error(f"[{source}] evaluate 失败: {e}")
        return items

    seen_urls = set()  # Bug2: 同平台内 URL 归一化去重
    n_dup_url = 0
    for it in raw:
        if len(items) >= max_items:
            break

        raw_url = it["url"]
        norm_url = normalize_article_url(raw_url, source)

        # Bug2: URL 归一化去重 - spm 不同但实际同文章
        if norm_url in seen_urls:
            log_dedup_url(logger, source, raw_url, norm_url)
            n_dup_url += 1
            continue
        seen_urls.add(norm_url)

        signals = it.get("signals", []) or []
        pop, pop_raw = _pick_popularity(signals)
        card_excerpt = it.get("card_excerpt", "")

        # 铁律 4: 每条的热度提取过程落日志（card_excerpt 必须记录）
        log_heat_item(logger, source, it["title"], card_excerpt, signals, pop)

        items.append({
            "title": it["title"],
            "url": norm_url,           # ★ Bug2: 归一化后的干净 URL
            "popularity": pop,         # ★ 统一热度（int）
            "popularity_raw": pop_raw, # 原始文本，如 "1.2万阅读"
            "signals": signals,        # 全部命中的信号（调试/校验用）
            "card_excerpt": card_excerpt,  # ★ Bug1 排查：卡片原文前 200 字
            "views": pop,              # 兼容旧字段
            "source": source,
        })

    if n_dup_url > 0:
        logger.info(f"[去重-URL] {source} URL 归一化去重: 去掉 {n_dup_url} 条 spm 重复")

    return items


# ============================================================
# 头条（v2.5 用通用卡片采集 + 保留 v2.4 滚动/登录逻辑）
# ============================================================
async def scrape_toutiao(page, max_items: int = 80) -> list[dict]:
    t0 = time.time()
    log_scrape_start(logger, "toutiao", "https://www.toutiao.com/?channel=history")
    logger.info("[头条] 开始抓取...")

    try:
        await page.goto(
            "https://www.toutiao.com/?channel=history&source=channel",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)
    except Exception as e:
        logger.warning(f"[头条] goto 异常: {e}")

    logger.info(f"[头条] 当前 URL: {page.url}")

    # 登录检查
    has_login = False
    try:
        has_login = (
            await page.locator("text=扫码登录").count() > 0
            or await page.locator(".qrcode, .login-qrcode, .qrlogin-img").count() > 0
        )
    except Exception:
        pass

    if has_login:
        logger.warning("[头条] 检测到登录要求，请在浏览器窗口完成登录/扫码")
        print("\n  [!] 头条要求登录。请在浏览器窗口完成登录/扫码")
        print("      登录完成后回到 cmd，按 [回车] 继续抓取")
        try:
            input()
        except EOFError:
            pass
        try:
            await page.goto(
                "https://www.toutiao.com/?channel=history&source=channel",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"[头条] 登录后重新 goto 失败: {e}")

    # 兜底：URL 没切到 history
    if "channel=history" not in page.url:
        logger.info("[头条] URL 未带 history 参数，尝试点击「历史」标签")
        try:
            await page.locator("text=历史").first.click(timeout=5000)
            await asyncio.sleep(2.5)
        except Exception as e:
            logger.warning(f"[头条] 点击「历史」失败: {e}")

    await asyncio.sleep(2)
    try:
        first_count = await page.evaluate("() => document.querySelectorAll('a').length")
        logger.info(f"[头条] 首屏 a 标签数: {first_count}")
    except Exception:
        pass

    # 固定滚 8 次（v2.4 验证过的稳定逻辑）
    logger.info("[头条] 滚动加载（共 8 次）...")
    for i in range(8):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2.5)
            try:
                cnt = await page.evaluate("() => document.querySelectorAll('a').length")
                logger.info(f"      第 {i+1}/8 次，a 标签 {cnt} 个")
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"      第 {i+1}/8 次滚动失败: {e}")
            try:
                await page.goto(
                    "https://www.toutiao.com/?channel=history&source=channel",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
            except Exception:
                break

    # 用通用卡片提取（带热度信号）
    logger.info("[头条] 提取候选文章 + 热度信号...")
    pattern = r"toutiao\.com/(article|group|w|s|i\d|a\d)"
    items = await _harvest_cards(page, pattern, "toutiao", max_items)

    n_with_pop = sum(1 for x in items if x["popularity"] > 0)
    log_scrape_done(logger, "toutiao", len(items), n_with_pop, time.time() - t0)
    return items


# ============================================================
# 搜狐历史（v2.5 改用通用卡片采集）
# ============================================================
async def scrape_sohu(page, max_items: int = 60) -> list[dict]:
    t0 = time.time()
    log_scrape_start(logger, "sohu", "https://history.sohu.com/")
    logger.info("[搜狐] 开始抓取历史频道...")
    try:
        await page.goto("https://history.sohu.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(_delay())
    except Exception as e:
        log_scrape_fail(logger, "sohu", str(e), url="https://history.sohu.com/")
        logger.warning(f"[搜狐] goto 失败: {e}")
        return []

    for _ in range(5):
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight*2)")
        except Exception:
            break
        await asyncio.sleep(random.uniform(1, 2))

    pattern = r"sohu\.com/a/"
    items = await _harvest_cards(page, pattern, "sohu", max_items)

    n_with_pop = sum(1 for x in items if x["popularity"] > 0)
    log_scrape_done(logger, "sohu", len(items), n_with_pop, time.time() - t0)
    return items


# ============================================================
# 网易历史（v2.5 改用通用卡片采集）
# ============================================================
async def scrape_163(
    page,
    max_items: int = 60,
    mobile: bool = False,
    mobile_urls: list[str] | None = None,
) -> list[dict]:
    """
    抓网易历史频道。

    v2.8 变更：
        mobile=False（默认）  → 走老的 PC 入口 https://www.163.com/history/
                              （现状：渲染主站门户，0 条历史内容，保留兼容用）
        mobile=True           → 主策略：goto m.163.com → 点"历史" tab → 采
        mobile_urls           → 后门：如果传了 URL 列表（--m163-url），跳过 click-tab，
                              直接 goto 那个 URL 后采（未来 163 开新直链时备用）
    """
    t0 = time.time()

    if not mobile:
        # 老路径完全不动（铁律 1：默认行为不变）
        log_scrape_start(logger, "163", "https://www.163.com/history/")
        logger.info("[网易] 开始抓取历史频道...（PC 入口）")
        try:
            await page.goto("https://www.163.com/history/",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(_delay())
        except Exception as e:
            log_scrape_fail(logger, "163", str(e),
                            url="https://www.163.com/history/")
            logger.warning(f"[网易] goto 失败: {e}")
            return []

        for _ in range(5):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight*2)")
            except Exception:
                break
            await asyncio.sleep(random.uniform(1, 2))

        pattern = r"163\.com/(?:dy/|news/)?article/"
        items = await _harvest_cards(page, pattern, "163", max_items)

        n_with_pop = sum(1 for x in items if x["popularity"] > 0)
        log_scrape_done(logger, "163", len(items), n_with_pop, time.time() - t0)
        return items

    # ============= mobile=True 路径（v2.10 改为直链历史频道，不再 click-tab）=============
    # 默认 URL: https://m.163.com/touch/news/sub/history（直接落在历史 tab）
    # --m163-url 传值时覆盖
    urls = mobile_urls if mobile_urls else [NETEASE_MOBILE_HISTORY]
    if mobile_urls:
        logger.info(f"[网易-mobile] --m163-url 模式: {mobile_urls}")
    else:
        logger.info(f"[网易-mobile] 默认走历史频道直链: {NETEASE_MOBILE_HISTORY}")
    return await _scrape_163_mobile_direct(page, urls, max_items, t0)



async def _scrape_163_mobile_direct(
    page, urls: list[str], max_items: int, t0: float
) -> list[dict]:
    """
    主策略（v2.10 起为默认）：直接 goto 历史频道深链 → 滚动 → 采。

    v2.11 改动：
        1. 加 page.screenshot()（v2.10 删 _scrape_163_mobile_tab 时漏掉了，是 bug）
        2. 加载完成后 log title + actual_url
        3. wait_until 从 domcontentloaded 改成 networkidle（让 JS 渲染完）
        4. 抓 0 条时做诊断 dump：href 前缀分布 + body 文本采样 + 疑似标题候选
    """
    pattern = r"163\.com/(?:dy/|news/)?article/"
    items: list[dict] = []
    for idx, url in enumerate(urls, 1):
        log_scrape_start(logger, "163", url)
        logger.info(f"[网易-mobile] 直链 {idx}/{len(urls)}: {url}")
        try:
            # v2.11: networkidle 让 JS 异步加载完成
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(_delay())
        except Exception as e:
            log_scrape_fail(logger, "163", str(e), url=url)
            continue

        # v2.11: 截屏（v2.10 漏掉的功能补回来）
        try:
            ts_short = datetime.now().strftime("%Y-%m-%d-%H%M")
            shot_path = OUTPUT_DIR / f"m163_history_{ts_short}.png"
            await page.screenshot(path=str(shot_path), full_page=False)
            logger.info(f"[网易-mobile] 截屏 → {shot_path.name}")
        except Exception as e:
            logger.warning(f"[网易-mobile] 截屏失败（不影响主流程）: {e}")

        # v2.11: 记 title + actual_url
        try:
            page_title = await page.title()
            page_real_url = page.url
            logger.info(
                f"[网易-mobile] 加载完成: title={page_title!r} "
                f"url={page_real_url!r}"
            )
        except Exception:
            pass

        try:
            final_url = page.url
            if final_url != url:
                logger.info(f"[网易-mobile] URL 跳转: {url} -> {final_url}")
        except Exception:
            pass

        for _ in range(10):  # v2.11: 8 → 10 多滚两次提供 lazy-load 余地
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight*1.5)")
            except Exception:
                break
            await asyncio.sleep(random.uniform(1.0, 1.8))

        candidate = await _harvest_cards(page, pattern, "163", max_items)
        n_with_pop = sum(1 for x in candidate if x["popularity"] > 0)
        logger.info(
            f"[网易-mobile] 直链 {url} 抓到 {len(candidate)} 条, "
            f"{n_with_pop} 带热度"
        )

        # ★ v2.11: 抓 0 条时自动 dump 诊断信息
        if len(candidate) == 0:
            try:
                diag = await page.evaluate(r"""
                    () => {
                        // 1) 所有 <a> 标签 href 前缀分布 TOP 20
                        const hrefCount = {};
                        document.querySelectorAll('a').forEach(a => {
                            const h = a.href || '';
                            if (!h || h.startsWith('javascript:') ||
                                h.startsWith('#') || h === window.location.href) return;
                            // 取前 60 字符为前缀分组
                            const prefix = h.length > 60 ? h.slice(0, 60) + '...' : h;
                            hrefCount[prefix] = (hrefCount[prefix] || 0) + 1;
                        });
                        const top_hrefs = Object.entries(hrefCount)
                            .sort((a, b) => b[1] - a[1]).slice(0, 20);

                        // 2) body 可见文本前 400 字符
                        const bodyText = (document.body.innerText || '').slice(0, 400);

                        // 3) 疑似标题候选：text 长度 10-60 字的 <a> 元素
                        const titles = [];
                        document.querySelectorAll('a').forEach(a => {
                            const t = (a.innerText || '').trim();
                            if (t.length >= 10 && t.length <= 60) {
                                titles.push({
                                    text: t.slice(0, 50),
                                    href: (a.href || '').slice(0, 80),
                                });
                            }
                        });

                        // 4) 也找下 div/li 类的潜在卡片（万一不是 <a> 而是 onclick）
                        const onclickEls = [];
                        document.querySelectorAll('[onclick], [data-url], [data-href]').forEach(el => {
                            const t = (el.innerText || '').trim().slice(0, 50);
                            const link = el.getAttribute('data-url') ||
                                         el.getAttribute('data-href') ||
                                         (el.getAttribute('onclick') || '').slice(0, 80);
                            if (t.length >= 10) onclickEls.push({text: t, link});
                        });

                        return {
                            top_hrefs,
                            body_sample: bodyText,
                            title_candidates: titles.slice(0, 15),
                            onclick_count: onclickEls.length,
                            onclick_sample: onclickEls.slice(0, 10),
                        };
                    }
                """)
                logger.warning("[网易-mobile] 抓 0 条，启动诊断 dump:")
                logger.info(f"  body 文本采样（前 400 字）: {diag.get('body_sample', '')!r}")
                logger.info(f"  全页 <a> href 前缀分布 TOP 20（数量 前缀）:")
                for prefix, count in diag.get('top_hrefs', []):
                    logger.info(f"    [{count:>3}] {prefix}")
                logger.info(f"  疑似标题候选（text 长度 10-60 的 <a>）TOP 15:")
                for c in diag.get('title_candidates', []):
                    logger.info(
                        f"    text={c.get('text', '')!r} "
                        f"href={c.get('href', '')!r}"
                    )
                logger.info(
                    f"  带 onclick/data-url/data-href 元素数: "
                    f"{diag.get('onclick_count', 0)}"
                )
                if diag.get('onclick_sample'):
                    logger.info(f"  onclick 类元素采样 TOP 10:")
                    for c in diag.get('onclick_sample', []):
                        logger.info(
                            f"    text={c.get('text', '')!r} "
                            f"link={c.get('link', '')!r}"
                        )
            except Exception as e:
                logger.warning(f"[网易-mobile] 诊断 dump 失败: {e}")

        if len(candidate) >= 5:
            items = candidate
            break
        elif len(candidate) > len(items):
            items = candidate

    n_with_pop = sum(1 for x in items if x["popularity"] > 0)
    log_scrape_done(logger, "163", len(items), n_with_pop, time.time() - t0)
    return items


# ============================================================
# 输出 .txt 选题清单（v2.5 新增）
# ============================================================
def write_topics_txt(fresh_items: list[dict], out_path: Path, top_n: int = 50):
    """
    生成可直接复制粘贴的选题清单文本。
    格式：
        === 待写选题 TOP N（时间戳）===

        1. 【头条 | 评120】[标题]
           https://...
        ...
    """
    lines = []
    lines.append(f"=== 待写选题 TOP {top_n}（{datetime.now():%Y-%m-%d %H:%M}）===\n")
    lines.append("已剔除：已发文章 / 噪音词\n")
    lines.append("=" * 60 + "\n\n")

    src_label = {"toutiao": "头条", "sohu": "搜狐", "163": "网易"}

    for i, it in enumerate(fresh_items[:top_n], 1):
        src = src_label.get(it.get("source", "?"), it.get("source", "?"))
        pop_raw = it.get("popularity_raw") or ""
        pop = it.get("popularity", 0)
        # 热度展示：优先 raw（带"评论""阅读"字样），其次 int
        if pop_raw:
            pop_show = pop_raw
        elif pop > 0:
            pop_show = f"{pop}"
        else:
            pop_show = "无"
        score = it.get("scores", {}).get("final_score", 0)
        title = it.get("title", "")
        url = it.get("url", "")
        lines.append(f"{i:2d}. 【{src} | 热度:{pop_show} | 分:{score:.1f}】{title}\n")
        lines.append(f"    {url}\n\n")

    out_path.write_text("".join(lines), encoding="utf-8")


# ============================================================
# ★ v2.12: NewsCrawler 正文抓取（--fetch-content 开启）
# ============================================================
def _to_newscrawler_url(url: str, source: str):
    """
    把抓取到的 URL 转成 NewsCrawler 能识别的格式。
    返回 None 表示该平台暂不支持，跳过。

    163 移动端：m.163.com/news/article/ID.html → www.163.com/dy/article/ID.html
    toutiao：当前 NewsCrawler 不识别 /article/ 格式，暂时跳过
    其他平台：原样返回
    """
    if source == "163":
        import re as _re
        m = _re.search(r"/([A-Z0-9]{16,})\.html", url)
        if m:
            article_id = m.group(1)
            return f"https://www.163.com/dy/article/{article_id}.html"
        return url
    if source == "toutiao":
        # NewsCrawler 不识别 toutiao.com/article/ 格式（400 Bad Request）
        # 暂时跳过，等确认正确 URL 格式后补充
        return None
    return url


def fetch_content_for_items(
    items: list,
    top_n: int = 50,
    api_base: str = "http://localhost:8000",
    logger=None,
) -> list:
    """
    对 TOP N 条目调 NewsCrawler /api/extract，把正文和图片合并回 item。
    成功：item["body_texts"] = [...], item["body_images"] = [...], item["body_fetched"] = True
    跳过：item["body_fetched"] = None（平台暂不支持，不计入失败）
    失败：item["body_fetched"] = False, item["body_error"] = "..."
    """
    import urllib.request, urllib.error, json as _json, time as _time

    targets = items[:top_n]
    total = len(targets)
    n_skip = 0
    for i, item in enumerate(targets, 1):
        source = item.get("source", "")
        raw_url = item.get("url", "")
        api_url = _to_newscrawler_url(raw_url, source)
        title_short = item.get("title", "")[:20]

        if api_url is None:
            item["body_fetched"] = None  # 跳过，不算失败
            n_skip += 1
            if logger:
                logger.info(f"[正文] {i}/{total} 跳过(平台暂不支持) {title_short!r}")
            continue

        payload = _json.dumps({"url": api_url, "output_format": "json"}).encode("utf-8")
        req = urllib.request.Request(
            f"{api_base}/api/extract",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "success" and data.get("data"):
                d = data["data"]
                item["body_texts"] = d.get("texts", [])
                item["body_images"] = d.get("images", [])
                item["body_fetched"] = True
                n_text = len(item["body_texts"])
                n_img = len(item["body_images"])
                if logger:
                    logger.info(f"[正文] {i}/{total} OK {n_text}段/{n_img}图 {title_short!r}")
            else:
                err = str(data.get("detail", data))[:80]
                item["body_fetched"] = False
                item["body_error"] = err
                if logger:
                    logger.warning(f"[正文] {i}/{total} 失败({err}) {title_short!r}")
        except Exception as exc:
            item["body_fetched"] = False
            item["body_error"] = str(exc)[:80]
            if logger:
                logger.warning(f"[正文] {i}/{total} 异常({exc}) {title_short!r}")

        _time.sleep(0.3)

    return items


# ============================================================
# ★ v2.13: 头条正文用 Playwright 抓（NewsCrawler 抓不动反爬）
# ============================================================
# 提取正文+标题+图片的 JS（在 article 标签下找）
# 头条文章页结构：<h1>标题</h1> ... <article><p>段落</p><img src=...></article>
_TOUTIAO_BODY_JS = r"""
() => {
    const h1 = document.querySelector('h1');
    const title = h1 ? (h1.innerText || '').trim() : '';
    const article = document.querySelector('article');
    if (!article) return { title, texts: [], images: [], ok: false };
    const texts = [];
    const images = [];
    for (const el of article.children) {
        const tag = el.tagName.toLowerCase();
        if (tag === 'p') {
            const t = (el.innerText || '').trim();
            if (t) texts.push(t);
        }
        if (tag === 'img') {
            const src = el.src || el.getAttribute('src') || '';
            if (src) images.push(src);
        } else {
            // p 或 div 里嵌套的 img
            for (const img of el.querySelectorAll('img')) {
                const src = img.src || img.getAttribute('src') || '';
                if (src) images.push(src);
            }
        }
    }
    return { title, texts, images, ok: true };
}
"""


async def fetch_toutiao_bodies_via_playwright(
    items: list,
    top_n: int = 50,
    headless: bool = False,
    logger=None,
) -> None:
    """
    用 Playwright 抓头条文章正文（NewsCrawler 抓不动反爬，改用真实浏览器）。
    复用 PROFILE_DIR + ANTI_DETECT_JS，跟主流程的头条抓取同一套防检测策略。
    单独开一个 Chromium，不影响已经关闭的主浏览器。

    成功：item["body_texts"], item["body_images"], item["body_fetched"]=True
    失败：item["body_fetched"]=False, item["body_error"]=...
    """
    toutiao_items = [
        x for x in items[:top_n]
        if x.get("source") == "toutiao" and x.get("body_fetched") is not True
    ]
    if not toutiao_items:
        return

    total = len(toutiao_items)
    if logger:
        logger.info(f"[正文-头条] 启动 Playwright 抓 {total} 条头条正文...")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        if logger:
            logger.warning("[正文-头条] Playwright 不可用，跳过")
        return

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            user_agent=random.choice(UAS),
            viewport={"width": 1366, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        await ctx.add_init_script(ANTI_DETECT_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        n_ok = 0
        for i, item in enumerate(toutiao_items, 1):
            url = item.get("url", "")
            title_short = item.get("title", "")[:20]
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # 等 h1 出现（最多 8s，没有就当反爬失败）
                try:
                    await page.wait_for_selector("h1", timeout=8000)
                except Exception:
                    pass
                # 滚动一次，触发图片懒加载
                try:
                    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    await asyncio.sleep(0.8)
                except Exception:
                    pass

                data = await page.evaluate(_TOUTIAO_BODY_JS)
                texts = data.get("texts", []) if data else []
                images = data.get("images", []) if data else []

                if not texts:
                    # 没拿到正文段落 → 视为失败
                    item["body_fetched"] = False
                    item["body_error"] = "no <article>/<p> found"
                    if logger:
                        logger.warning(f"[正文-头条] {i}/{total} 失败(无正文) {title_short!r}")
                else:
                    item["body_texts"] = texts
                    item["body_images"] = images
                    item["body_fetched"] = True
                    n_ok += 1
                    if logger:
                        logger.info(f"[正文-头条] {i}/{total} OK {len(texts)}段/{len(images)}图 {title_short!r}")
            except Exception as exc:
                item["body_fetched"] = False
                item["body_error"] = str(exc)[:80]
                if logger:
                    logger.warning(f"[正文-头条] {i}/{total} 异常({exc}) {title_short!r}")

            # 拟人节奏，避免触发反爬
            await asyncio.sleep(random.uniform(1.5, 3.0))

        try:
            await ctx.close()
        except Exception:
            pass

        if logger:
            logger.info(f"[正文-头条] 完成: {n_ok}/{total} 成功")


# ============================================================
# 主调度
# ============================================================
async def main_async(args):
    # ★ 铁律 4: 初始化日志，共享时间戳（log/raw/topics 三件套配对）
    ts = datetime.now().strftime("%Y-%m-%d-%H%M")
    log_path = None
    if setup_logger:
        _, log_path, ts = setup_logger(OUTPUT_DIR, timestamp=ts)
        logger.info(f"scraper.py v2.14 启动, 日志 → {log_path}")
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
        logger.info("scraper.py v2.14 启动 (logger_utils.py 未找到, 仅控制台输出)")

    items_all: list[dict] = []
    max_items_override = getattr(args, "max_items", 0) or 0
    use_m163 = bool(getattr(args, "m163", True)) and not bool(getattr(args, "no_m163", False))
    m163_url_override = getattr(args, "m163_url", None)
    if use_m163:
        logger.info("[网易] 移动端模式启用（v2.12 默认开启，--no-m163 可关闭）")
        if m163_url_override:
            logger.info(f"[网易] --m163-url 覆盖：候选 = [{m163_url_override}]")

    async with async_playwright() as pw:
        # 桌面 context：跑 toutiao/sohu，以及未启用 --m163 时的 163 老路径
        desktop_ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
            user_agent=random.choice(UAS),
            viewport={"width": 1366, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        await desktop_ctx.add_init_script(ANTI_DETECT_JS)
        desktop_page = desktop_ctx.pages[0] if desktop_ctx.pages else await desktop_ctx.new_page()

        # ★ v2.7: 移动端 context（仅在 --m163 启用时开）
        mobile_ctx = None
        mobile_page = None
        if use_m163:
            MOBILE_PROFILE_DIR.mkdir(exist_ok=True)
            iphone = dict(pw.devices["iPhone 13"])
            # default_browser_type 是 device descriptor 的元数据，
            # launch_persistent_context 不接受，必须剔掉
            iphone.pop("default_browser_type", None)
            # ★ v2.9: 用真实 iOS 17 UA 替换 Playwright 默认的"Version/26.0"假版本号
            # 保留 viewport / is_mobile / has_touch / device_scale_factor 不变
            iphone["user_agent"] = MOBILE_UA_IOS17
            try:
                mobile_ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(MOBILE_PROFILE_DIR),
                    headless=args.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    **iphone,
                )
                await mobile_ctx.add_init_script(ANTI_DETECT_JS)
                mobile_page = (
                    mobile_ctx.pages[0]
                    if mobile_ctx.pages
                    else await mobile_ctx.new_page()
                )
                logger.info(
                    f"[网易] mobile context 就绪 "
                    f"(UA={iphone.get('user_agent','?')[:60]}..., "
                    f"viewport={iphone.get('viewport')})"
                )
            except Exception as e:
                log_exception(logger, where="mobile context 启动")
                logger.error(f"[网易] mobile context 启动失败: {e}，将回退 PC 入口")
                mobile_ctx = None
                mobile_page = None

        platforms = ["toutiao", "sohu", "163"]
        if args.only:
            platforms = [args.only]
        if args.skip:
            platforms = [p for p in platforms if p != args.skip]

        scrapers = {
            "toutiao": scrape_toutiao,
            "sohu": scrape_sohu,
            "163": scrape_163,
        }

        for plat in platforms:
            try:
                kw = {}
                if max_items_override > 0:
                    kw["max_items"] = max_items_override

                # ★ v2.7: 163 在 --m163 启用且 mobile_page 就绪时走移动端
                if plat == "163" and use_m163 and mobile_page is not None:
                    kw["mobile"] = True
                    if m163_url_override:
                        kw["mobile_urls"] = [m163_url_override]
                    items = await scrapers[plat](mobile_page, **kw)
                else:
                    items = await scrapers[plat](desktop_page, **kw)
                items_all.extend(items)
            except Exception as e:
                log_scrape_fail(logger, plat, str(e))
                logger.error(f"[{plat}] 抓取异常: {e}")

        await desktop_ctx.close()
        if mobile_ctx:
            try:
                await mobile_ctx.close()
            except Exception:
                pass

    logger.info("=" * 40)
    logger.info(f"原始抓取总计: {len(items_all)} 条")

    # 热度分布速查（帮你判断 --min-pop 设多少合适）
    pops = [it["popularity"] for it in items_all if it.get("popularity", 0) > 0]
    if pops:
        pops_sorted = sorted(pops, reverse=True)
        logger.info(f"热度分布 (n={len(pops)}/{len(items_all)}): "
              f"最高 {pops_sorted[0]}, "
              f"中位 {pops_sorted[len(pops_sorted)//2]}, "
              f"最低 {pops_sorted[-1]}")
    else:
        logger.warning("没抓到任何热度数据，检查 output JSON 里 signals 字段")

    # 热度过滤（Bug4: 分平台阈值，更精准）
    min_pop_map = {
        "toutiao": getattr(args, "min_pop_toutiao", 0) or 0,
        "sohu": getattr(args, "min_pop_sohu", 0) or 0,
        "163": getattr(args, "min_pop_163", 0) or 0,
    }
    global_min = getattr(args, "min_pop", 0) or 0
    # 如果设了全局 --min-pop 但没设分平台的，全局值补位
    for k in min_pop_map:
        if min_pop_map[k] == 0 and global_min > 0:
            min_pop_map[k] = global_min

    has_filter = any(v > 0 for v in min_pop_map.values())
    if has_filter:
        before = len(items_all)
        items_all = [
            it for it in items_all
            if it.get("popularity", 0) >= min_pop_map.get(it.get("source", ""), 0)
        ]
        logger.info(
            f"[热度过滤] 分平台阈值 {min_pop_map}: {before} -> {len(items_all)}"
        )

    # 题材评分
    if score_title:
        for it in items_all:
            try:
                it["scores"] = score_title(it["title"], it.get("popularity", 0))
                # 铁律 4: 评分明细落日志
                sc = it["scores"]
                log_score_item(
                    logger, it["title"],
                    final_score=sc.get("final_score", 0),
                    topic_fit=sc.get("topic_fit", 0),
                    hook_bonus=sc.get("hook_bonus", 0),
                    view_factor=sc.get("view_factor", 0),
                    tier=sc.get("tier", ""),
                )
            except Exception as e:
                log_exception(logger, "score_title", item_hint=it.get("title","")[:30])
                it["scores"] = {}

    # 已发去重
    if PublishedIndex:
        try:
            idx = PublishedIndex.load_default(BASE_DIR)
            for it in items_all:
                it["published_match"] = idx.check(it["title"])
            n_dup = sum(1 for x in items_all if x.get("published_match", {}).get("is_published"))
            logger.info(f"[去重-已发] {n_dup} 条命中已发文章")
        except Exception as e:
            log_exception(logger, "已发去重")

    # 跨平台去重
    if cross_platform_dedup:
        try:
            items_all = cross_platform_dedup(items_all)
            # 铁律 4: 记录跨平台去重结果
            for it in items_all:
                if it.get("dup_count", 1) > 1:
                    log_dedup_crossplat(
                        logger, it.get("title", ""),
                        it.get("dup_count", 0),
                        it.get("dup_sources", []),
                        it.get("dup_factor", 1.0),
                    )
        except Exception as e:
            log_exception(logger, "跨平台去重")

    # 排序：未发 > 非噪音 > 综合分高 > 热度高
    def sort_key(it):
        published = it.get("published_match", {}).get("is_published", False)
        is_noise = it.get("scores", {}).get("is_noise", False)
        score = it.get("scores", {}).get("final_score", 0)
        pop = it.get("popularity", 0)
        return (published, is_noise, -score, -pop)

    items_all.sort(key=sort_key)

    # ★ v2.14: 古代硬配额（默认 ≤ 7 进 TOP 50，可 --ancient-quota N 覆盖，--no-ancient-quota 禁用）
    # 实测背景：v2.13 下 TOP 50 古代 28 条，单改 ANCIENT 权重无效（非古代候选不够 43 条），
    #         所以引入硬配额：按已排序结果，TOP 50 内非古代优先，古代最多 quota 条。
    ancient_quota = getattr(args, "ancient_quota", 7)
    quota_disabled = getattr(args, "no_ancient_quota", False)
    if ancient_quota > 0 and not quota_disabled:
        TOP_N = 50
        # 拆分（items_all 已按 sort_key 排好序，分数倒序、未发优先、非噪音优先）
        ancient_items = [it for it in items_all if it.get("scores", {}).get("tier") == "ancient"]
        non_ancient_items = [it for it in items_all if it.get("scores", {}).get("tier") != "ancient"]

        # TOP 50 = 非古代前 (50-quota) + 古代前 quota
        target_non = TOP_N - ancient_quota
        keep_non = non_ancient_items[:target_non]
        keep_anc = ancient_items[:ancient_quota]

        # 兜底：如果非古代不足 (50 - quota)，古代补位填满 TOP 50
        deficit = target_non - len(keep_non)
        if deficit > 0:
            keep_anc = ancient_items[:ancient_quota + deficit]

        # 候补区（剩下的）
        rest_non = non_ancient_items[len(keep_non):]
        rest_anc = ancient_items[len(keep_anc):]

        # TOP 50 内部按分数重排（古代非古代混在一起，按 sort_key 排）
        top50_pool = keep_non + keep_anc
        top50_pool.sort(key=sort_key)
        # 候补区也按分数排
        rest_pool = rest_non + rest_anc
        rest_pool.sort(key=sort_key)

        items_all = top50_pool + rest_pool

        # 日志：让用户每次跑都看到配额生效情况
        n_anc_top50 = sum(1 for x in items_all[:TOP_N] if x.get("scores", {}).get("tier") == "ancient")
        n_anc_total = len(ancient_items)
        logger.info("=" * 40)
        logger.info(
            f"[古代配额 v2.14] 配额={ancient_quota}, TOP {TOP_N} 古代={n_anc_top50}/{n_anc_total}, 候补区古代={len(rest_anc)}"
        )
        if deficit > 0:
            logger.info(f"[古代配额] 非古代候选不足，古代补位 {deficit} 条填满 TOP {TOP_N}")
    else:
        logger.info("[古代配额 v2.14] 已禁用（--no-ancient-quota 或 --ancient-quota 0）")

    # ★ v2.12: --fetch-content → 调 NewsCrawler 拿正文（feature flag）
    if getattr(args, "fetch_content", False):
        logger.info("=" * 40)
        logger.info("[正文] --fetch-content 已启用，开始抓 TOP 50 正文")
        logger.info("[正文] 搜狐+163 走 NewsCrawler，头条走 Playwright（v2.13）")
        logger.info("[正文] 确保 NewsCrawler 已启动：双击 StartNewsCrawler.bat")
        # 阶段1：搜狐+163 走 NewsCrawler HTTP（头条会被 _to_newscrawler_url 返回 None 跳过）
        fetch_content_for_items(items_all, top_n=50, logger=logger)
        # ★ v2.13 阶段2：头条用 Playwright 接管
        await fetch_toutiao_bodies_via_playwright(
            items_all, top_n=50, headless=args.headless, logger=logger,
        )
        n_ok = sum(1 for x in items_all[:50] if x.get("body_fetched") is True)
        n_skip = sum(1 for x in items_all[:50] if x.get("body_fetched") is None)
        n_fail = sum(1 for x in items_all[:50] if x.get("body_fetched") is False)
        logger.info(f"[正文] 完成: {n_ok} 成功, {n_skip} 跳过, {n_fail} 失败")

    # ★ v2.15: --analyze → 调 GLM 做选题精筛 + B5 主体类型判断（feature flag，铁律 2）
    if getattr(args, "analyze", False):
        logger.info("=" * 40)
        logger.info("[GLM] --analyze 已启用，准备调 GLM-4.7-Flash 分析 TOP 50")
        # B 方案：有 body_texts 用 body_texts，没有则降级 card_excerpt
        n_with_body = sum(1 for x in items_all[:50] if x.get("body_texts"))
        if n_with_body == 0:
            logger.info(f"[GLM] 本次无 body_texts（未跑 --fetch-content），GLM 将使用 card_excerpt 降级模式")
        else:
            logger.info(f"[GLM] {n_with_body}/50 条带 body_texts，其余用 card_excerpt 降级")
        try:
            from analyzer import analyze_top_n
            glm_meta = analyze_top_n(
                items_all,
                top_n=50,
                provider=args.analyze_provider,
                model=args.analyze_model,
                logger=logger,
            )
            if glm_meta.get("overall_observation"):
                logger.info(f"[GLM] 整体观察: {glm_meta['overall_observation']}")
        except Exception as e:
            # GLM 失败不影响主流程（铁律 1）
            logger.exception(f"[GLM] 分析过程异常，跳过 GLM 但不影响后续输出：{e}")

    # 保存 JSON（用 setup_logger 同一时间戳，三件套配对）
    out_json = OUTPUT_DIR / f"raw_{ts}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(items_all, f, ensure_ascii=False, indent=2)
    logger.info(f"[保存 JSON] {out_json}")

    # 干净的待写选题（v2.5：写 .txt 清单）
    fresh = [
        it for it in items_all
        if not it.get("published_match", {}).get("is_published")
        and not it.get("scores", {}).get("is_noise", False)
    ]
    out_txt = OUTPUT_DIR / f"topics_{ts}.txt"
    write_topics_txt(fresh, out_txt, top_n=50)
    logger.info(f"[保存 TXT] {out_txt}")

    # 控制台打印 TOP 20
    logger.info("=" * 40)
    logger.info("待写选题 TOP 20（已剔除已发+噪音）:")
    logger.info("=" * 40)
    for i, it in enumerate(fresh[:20], 1):
        sc = it.get("scores", {}).get("final_score", 0)
        tier = it.get("scores", {}).get("tier", "?")
        pop_raw = it.get("popularity_raw") or f'{it.get("popularity",0)}'
        src = it.get("source", "?")
        logger.info(f"{i:2d}. [{sc:5.1f} | {tier:8s} | {src:8s} | 热{pop_raw:>10s}] {it['title'][:50]}")


def main():
    p = argparse.ArgumentParser(description="百家号选题素材抓取器 v2.14")
    p.add_argument("--only", choices=["toutiao", "sohu", "163"])
    p.add_argument("--skip", choices=["toutiao", "sohu", "163"])
    p.add_argument("--headless", action="store_true")
    p.add_argument("--min-pop", type=int, default=0,
                   help="全局热度阈值（兜底），首跑用 0 看分布")
    p.add_argument("--min-pop-toutiao", type=int, default=0,
                   help="头条评论数阈值（建议 30）")
    p.add_argument("--min-pop-sohu", type=int, default=0,
                   help="搜狐阅读量阈值（建议 3000）")
    p.add_argument("--min-pop-163", type=int, default=0,
                   help="网易跟帖数阈值（建议 30）")
    p.add_argument("--max-items", type=int, default=0,
                   help="每平台最大抓取条数（0=使用函数默认值: 头条80/搜狐60/网易60）")
    # ★ v2.12: --m163 改为默认开启
    p.add_argument("--m163", action="store_true", default=True,
                   help="启用网易移动端入口（v2.12 起默认开启）")
    p.add_argument("--no-m163", action="store_true", default=False,
                   help="关闭移动端，回退到 PC 版 history.163.com")
    p.add_argument("--m163-url", type=str, default=None,
                   help="后门：直接 goto 这个 URL")
    # ★ v2.12: NewsCrawler 正文抓取（feature flag，需先启动 StartNewsCrawler.bat）
    p.add_argument("--fetch-content", action="store_true", default=False,
                   help="调 localhost:8000 NewsCrawler API 拿 TOP 50 正文+图片（需先启动 StartNewsCrawler.bat）")
    # ★ v2.14: 古代硬配额（默认 7，可临时覆盖；--no-ancient-quota 完全禁用）
    p.add_argument("--ancient-quota", type=int, default=7,
                   help="TOP 50 中古代题材最多保留 N 条，超出的进候补区（默认 7，0 等同禁用）")
    p.add_argument("--no-ancient-quota", action="store_true", default=False,
                   help="完全禁用古代配额，恢复 v2.13 旧排序行为")
    # ★ v2.15: GLM 选题分析（feature flag，需先 setx ZHIPU_API_KEY xxx）
    p.add_argument("--analyze", action="store_true", default=False,
                   help="TOP 50 调 GLM-4.7-Flash 做选题精筛+主体类型判断（需先设 ZHIPU_API_KEY 环境变量）")
    p.add_argument("--analyze-provider", type=str, default="glm",
                   choices=["glm", "deepseek", "kimi", "qwen", "doubao"],
                   help="--analyze 使用的模型供应商（默认 glm）")
    p.add_argument("--analyze-model", type=str, default=None,
                   help="覆盖模型名（默认用 provider 的 default_model，glm 默认 glm-4.7-flash）")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
