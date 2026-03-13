import json
import os
import re
import hashlib
from pathlib import Path
import argparse
import requests
import fitz  # PyMuPDF
from slugify import slugify
from playwright.sync_api import sync_playwright

DOWNLOAD_PAGE = "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/850/index/download"
TARGET_TITLE = "Ascend C算子开发"
STATE_PATH = Path("state.json")
DOWNLOAD_DIR = Path("downloads")
OUT_DIR = Path("ascend_dev_guide_sections")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text("utf-8"))
    return {}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

from urllib.parse import unquote
from playwright.sync_api import sync_playwright

def get_target_pdf_url_via_playwright(target_title: str = TARGET_TITLE) -> dict:
    """
    直接抓页面里所有 .pdf 链接，并筛选出 href 中包含 target_title 的那个。
    兼容 URL 编码、空格差异。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        page.goto(DOWNLOAD_PAGE, wait_until="domcontentloaded", timeout=120000)

        # 关键：不等 ul.list，直接等 pdf 链接出现（你截图里 href 直接是 .pdf）
        page.wait_for_selector("a[href$='.pdf']", timeout=120000)

        # 拿到所有 pdf href
        hrefs = page.locator("a[href$='.pdf']").evaluate_all(
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )

        # 做一个归一化：解码 + 去掉多余空格，方便匹配
        def norm(s: str) -> str:
            return unquote(s).replace("\u00a0", " ").replace(" ", "").strip()

        target_key = norm(target_title)

        # 先用 href 内容匹配（最稳）
        chosen = None
        for h in hrefs:
            if target_key in norm(h):
                chosen = h
                break

        # 兜底：如果 target_title 没出现在 href（少数情况），就按“opdevg + ascendc”特征筛
        if not chosen:
            for h in hrefs:
                nh = norm(h)
                if "opdevg" in nh.lower() and ("ascendc" in nh.lower() or "ascendc" in nh.lower()):
                    chosen = h
                    break

        browser.close()

        if not chosen:
            raise RuntimeError(f"未找到目标 PDF 链接。候选 pdf 链接数量={len(hrefs)}，示例={hrefs[:5]}")

        return {"pdf_url": chosen, "label": target_title}

def download_pdf(pdf_url: str, out_path: Path):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": DOWNLOAD_PAGE,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    r = requests.get(pdf_url, headers=headers, stream=True, timeout=60, allow_redirects=True)
    r.raise_for_status()
    with out_path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

def sanitize_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    # 文件名不宜太长
    if len(title) > 80:
        title = title[:80].rstrip()
    return title


def safe_filename_cn(title: str, max_len: int = 80) -> str:
    """
    保留中文/英文/数字，清理 macOS/Linux/Windows 都不允许的文件名字符。
    """
    t = sanitize_title(title)

    # 替换 Windows/macOS/Linux 不适合的字符
    t = re.sub(r'[\\/:*?"<>|]', "_", t)

    # 去掉控制字符
    t = re.sub(r"[\x00-\x1f\x7f]", "", t)

    # 末尾的点和空格在 Windows 下有坑
    t = t.strip(" .")

    # 太长就截断
    if len(t) > max_len:
        t = t[:max_len].rstrip()

    # 兜底
    return t or "untitled"

def export_sections_to_markdown(pdf_path: Path, out_dir: Path):
    """
    按 PDF 目录书签切分为 md，并在同一页内按“小节标题行”进一步切分。
    - 默认：导出 level>=2 的叶子节点（更细）
    - 切分逻辑：从本节标题行开始收集，到下节标题行之前停止（可跨页）
    """
    doc = fitz.open(pdf_path)
    toc = doc.get_toc(simple=True)
    if not toc:
        doc.close()
        raise RuntimeError("PDF 没有目录/书签（toc 为空），无法按章节拆分。")

    # ---------- 工具：清洗标题 / 标题匹配 ----------
    def strip_sec_no(title: str) -> str:
        # 去掉 "1.3.1.2 " 这种编号前缀
        m = re.match(r"^\s*\d+(?:\.\d+)*\s+(.*)$", title)
        return sanitize_title(m.group(1) if m else title)

    def norm(s: str) -> str:
        # 归一化用于匹配：去空白、去点号、大小写不敏感
        s = s.replace("\u00a0", " ")
        s = re.sub(r"\s+", "", s)
        s = s.replace(".", "")
        return s.lower()

    def find_title_line_index(lines: list[str], title: str) -> int | None:
        """
        在 lines 里找最像标题的行索引。优先精确包含（归一化后）。
        """
        key_full = norm(sanitize_title(title))
        key_plain = norm(strip_sec_no(title))

        # 先尝试 full title
        for i, ln in enumerate(lines):
            if key_full and key_full in norm(ln):
                return i
        # 再尝试去编号的 title
        for i, ln in enumerate(lines):
            if key_plain and key_plain in norm(ln):
                return i
        return None

    # ---------- 1) 清洗 toc ----------
    cleaned = []
    for lvl, title, page1 in toc:
        t = sanitize_title(title)
        if not t or t in {"目录", "Contents", "Table of Contents"}:
            continue
        cleaned.append((lvl, t, page1))

    if not cleaned:
        doc.close()
        raise RuntimeError("清洗后 toc 为空，无法拆分。")

    # ---------- 2) 标记是否有子节点 ----------
    has_child = [False] * len(cleaned)
    for i in range(len(cleaned) - 1):
        if cleaned[i + 1][0] > cleaned[i][0]:
            has_child[i] = True

    # ---------- 3) 选择要导出的条目：叶子 + level>=2 ----------
    MIN_LEVEL = 2
    LEAF_ONLY = True

    selected = []
    for i, (lvl, title, page1) in enumerate(cleaned):
        if lvl < MIN_LEVEL:
            continue
        if LEAF_ONLY and has_child[i]:
            continue
        selected.append((i, lvl, title, page1))

    if not selected:
        # 兜底：导出 level>=2 全部
        for i, (lvl, title, page1) in enumerate(cleaned):
            if lvl >= MIN_LEVEL:
                selected.append((i, lvl, title, page1))

    # ---------- 4) 对每节做“页内切分” ----------
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, (_, lvl, cur_title, cur_page1) in enumerate(selected):
        cur_start_page = max(0, cur_page1 - 1)

        next_title = None
        next_start_page = doc.page_count  # default end
        if idx + 1 < len(selected):
            next_title = selected[idx + 1][2]
            next_start_page = max(0, selected[idx + 1][3] - 1)

        # 文件名：0000_1.2 环境准备.md（保留中文）
        sec_no = ""
        m = re.match(r"^\s*(\d+(?:\.\d+)*)\s+(.*)$", cur_title)
        if m:
            sec_no = m.group(1)
            name_title = m.group(2).strip() or cur_title
        else:
            name_title = cur_title

        safe_name = safe_filename_cn(name_title)
        prefix = f"{sec_no} " if sec_no else ""
        filename = f"{idx:04d}_{prefix}{safe_name}.md"
        out_path = out_dir / filename

        parts = [f"# {prefix}{name_title}\n"]

        started = False

        # 遍历页：从本节起始页到“下一节起始页”之间（包含下一节起始页，因为可能同页）
        # --- 计算页范围：如果下一节起始页在后面，就只读到 next_start_page-1；
# --- 只有当 next_start_page == cur_start_page 才做“同页切分”。
# next_start_page 是下一节书签页（0-based）
        end_page = doc.page_count - 1 if next_title is None else max(cur_start_page, next_start_page - 1)
        #same_page = (next_title is not None and next_start_page == cur_start_page)
        boundary_page = next_start_page if next_title is not None else None


        def page_lines(pno: int) -> list[str]:
            page = doc.load_page(pno)
    # 你如果有 clip/过滤页眉页脚，就放在这里
            return page.get_text("text", sort=True).splitlines()

        boundary_has_next = False
        boundary_stop_pos = None

        if next_title is not None:
         # 先在 next_start_page 探测是否存在 next_title
            blines = page_lines(boundary_page)
            boundary_stop_pos = find_title_line_index(blines, next_title)
            if boundary_stop_pos is not None and boundary_stop_pos > 8:
                boundary_has_next = True
                # ✅ 只有确认边界页存在 next_title，才把边界页包含进来
                end_page = max(end_page, boundary_page)

        for pno in range(cur_start_page, end_page + 1):
    # 用 sort=True 更稳，正文不容易丢
            #lines = doc.load_page(pno).get_text("text", sort=True).splitlines()
            page = doc.load_page(pno)
            rect = page.rect  # 页面尺寸
            # 保留中间 8%~92% 的高度（你可以调整比例）
            clip = fitz.Rect(rect.x0, rect.y0 + rect.height * 0.08,
                             rect.x1, rect.y0 + rect.height * 0.92)

            text = page.get_text("text", sort=True, clip=clip)
            lines = text.splitlines()

            if not started:
                pos = find_title_line_index(lines, cur_title)
                if pos is None:
                   continue
                lines = lines[pos:]
                started = True

    # ✅ 只有“下一节也在同一页”才在本页里找 stop
            
            if boundary_has_next and next_title and (pno == boundary_page):
                lines_full = lines  # lines 是当前页（且从 cur_title 起可能已裁过）
    # 注意：如果这一页不是起始页，lines 没裁掉前面；这里用整页 lines 来截断
                stop_pos = find_title_line_index(lines_full, next_title)
                if stop_pos is not None and stop_pos > 8:
                    lines = lines_full[:stop_pos]
                    if lines:
                        parts.append("\n".join(lines).strip())
                        parts.append("")
                    break


            if lines:
                parts.append("\n".join(lines).strip())
                parts.append("")

        # 如果整个循环都没开始，给个提示（避免空文件悄悄生成）
        if not started:
            parts.append("> ⚠️ 未能在正文中定位到该小节标题（PDF 文本抽取可能丢失标题行）。\n")

        out_path.write_text("\n".join(parts).strip() + "\n", "utf-8")

    doc.close()

def main():
    state = load_state()
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-page", required=True, help="CANN 下载页 URL")
    parser.add_argument("--target-title", default="Ascend C算子开发")
    parser.add_argument("--force", action="store_true", help="force re-export even if unchanged")
    args = parser.parse_args()

    global DOWNLOAD_PAGE, TARGET_TITLE
    DOWNLOAD_PAGE = args.download_page
    TARGET_TITLE = args.target_title

    state = load_state()

    info = get_target_pdf_url_via_playwright(TARGET_TITLE)
    pdf_url = info["pdf_url"]
    label = info.get("label", "")

    # force 则不提前退出
    if (not args.force) and state.get("pdf_url") == pdf_url:
        print("No update: pdf_url unchanged.")
        return



    info = get_target_pdf_url_via_playwright(TARGET_TITLE)
    pdf_url = info["pdf_url"]
    label = info.get("label", "")

    # 用 pdf_url 做“是否更新”的第一判断（更稳的是下载后比 sha256）
    if state.get("pdf_url") == pdf_url:
        print("No update: pdf_url unchanged.")
        return

    # 下载到临时文件，再计算 hash
    pdf_name = slugify(label or "ascendc_doc", separator="_") or "ascendc_doc"
    pdf_path = DOWNLOAD_DIR / f"{pdf_name}.pdf"

    print(f"Downloading: {pdf_url}")
    download_pdf(pdf_url, pdf_path)

    file_hash = sha256_file(pdf_path)
    if state.get("sha256") == file_hash:
        print("No update: sha256 unchanged.")
        state["pdf_url"] = pdf_url  # URL可能变但内容未变，也记录一下
        save_state(state)
        return

    # 清空旧输出（可选：也可以按版本输出到子目录）
    for f in OUT_DIR.glob("*.md"):
        f.unlink()

    print("Exporting sections to markdown...")
    export_sections_to_markdown(pdf_path, OUT_DIR)

    state.update({
        "pdf_url": pdf_url,
        "label": label,
        "sha256": file_hash,
    })
    save_state(state)

    print("Done.")


if __name__ == "__main__":
    main()

