import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

import fitz  # PyMuPDF
import requests
from playwright.sync_api import sync_playwright
from slugify import slugify

DOWNLOAD_PAGE = "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/850/index/download"
TARGET_TITLE = "Ascend C算子开发"
STATE_PATH = Path("state.json")
DOWNLOAD_DIR = Path("downloads")
OUT_DIR = Path("ascend_dev_guide_sections")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

SEC_NO_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+(.*)$")
STEP_RE = re.compile(r"^步骤\s*([0-9]+)\s*(.*)$")
ORDERED_RE = re.compile(r"^([0-9]+)[\.\)、)]\s*(.+)$")
LETTER_RE = re.compile(r"^([a-zA-Z])[\.\)]\s*(.+)$")
BULLET_RE = re.compile(r"^[\-\*\+•●▪◦–—]+\s*(.+)$")
CODE_HEAD_RE = re.compile(
    r"^(#\s*include|#\s*define|#\s*if|#\s*endif|extern\b|template\b|typedef\b|using\b|class\b|struct\b|enum\b|namespace\b|if\s*\(|for\s*\(|while\s*\(|switch\s*\(|return\b|auto\b|const\b|static\b|void\b)"
)
SHELL_RE = re.compile(r"^(?:\$|cmake\b|python3?\b|bash\b|sh\b|make\b|pip\b|export\b|source\b|msopgen\b)")
FIGURE_RE = re.compile(r"^(图|表)\s*\d")
CJK_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


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


def squeeze_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def sanitize_title(title: str) -> str:
    title = squeeze_ws(title)
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


def split_sec_no(title: str) -> tuple[str, str]:
    m = SEC_NO_RE.match(sanitize_title(title))
    if not m:
        t = sanitize_title(title)
        return "", t
    sec_no = m.group(1)
    name = sanitize_title(m.group(2)) or sanitize_title(title)
    return sec_no, name


def strip_sec_no(title: str) -> str:
    return split_sec_no(title)[1]


def norm_for_match(s: str) -> str:
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", "", s)
    s = s.replace(".", "")
    return s.lower()


def find_title_line_index(lines: list[str], title: str) -> int | None:
    """
    在 lines 里找最像标题的行索引。优先精确包含（归一化后）。
    """
    key_full = norm_for_match(sanitize_title(title))
    key_plain = norm_for_match(strip_sec_no(title))

    for i, ln in enumerate(lines):
        nln = norm_for_match(ln)
        if key_full and key_full in nln:
            return i
    for i, ln in enumerate(lines):
        nln = norm_for_match(ln)
        if key_plain and key_plain in nln:
            return i
    return None


def normalize_prose_line(line: str) -> str:
    s = squeeze_ws(line)
    s = CJK_SPACE_RE.sub("", s)
    s = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", s)
    s = re.sub(r"\s+([，。；：！？、）】》])", r"\1", s)
    s = re.sub(r"([（【《])\s+", r"\1", s)
    return s


def join_prose(prev: str, cur: str) -> str:
    if not prev:
        return cur
    if re.search(r"[A-Za-z0-9_]$", prev) and re.match(r"^[A-Za-z0-9_]", cur):
        return f"{prev} {cur}"
    return f"{prev}{cur}"


def should_start_new_paragraph(prev_text: str, cur_text: str, prev_indent: int, cur_indent: int) -> bool:
    if prev_text.endswith(("。", "！", "？", "；")) and cur_indent >= prev_indent + 4:
        return True
    if prev_text.endswith(("。", "！", "？")) and re.match(r"^(说明|注意|提示|例如|如下|常见|执行配置|步骤)", cur_text):
        return True
    return False


def normalize_list_item(line: str) -> str | None:
    t = squeeze_ws(line)
    m = STEP_RE.match(t)
    if m:
        num, rest = m.group(1), squeeze_ws(m.group(2))
        return f"{num}. 步骤{num} {rest}".rstrip()
    m = ORDERED_RE.match(t)
    if m:
        num, rest = m.group(1), squeeze_ws(m.group(2))
        if re.match(r"^[，。；：:!?！？]", rest):
            return None
        if re.match(r"^\d+(?:\.\d+)+", rest):
            return None
        return f"{num}. {rest}".rstrip()
    m = BULLET_RE.match(t)
    if m:
        return f"- {squeeze_ws(m.group(1))}"
    m = LETTER_RE.match(t)
    if m:
        return f"- {m.group(1)}. {squeeze_ws(m.group(2))}"
    return None


def is_code_like(line: str, prev_is_code: bool) -> bool:
    t = line.strip()
    if not t:
        return False
    if normalize_list_item(t) is not None:
        return False
    if FIGURE_RE.match(t):
        return False
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", t))
    if CODE_HEAD_RE.search(t) or SHELL_RE.search(t):
        return True
    if t.startswith("//") or t.startswith("/*") or t.endswith("*/"):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_:<>]*\s*\(.*\)\s*;?\s*(//.*)?$", t):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(.*\)\s*(//.*)?$", t):
        return True
    if has_cjk and ";" in t and re.search(r"[A-Za-z_][A-Za-z0-9_:.>]*\s*\(", t):
        return True
    if any(sym in t for sym in ("{", "}", ";", "::", "->", "<<<", ">>>", "#include", "#define", "├──", "└──", "│")) and not has_cjk:
        return True
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)", t) and re.search(r"[=<>]", t) and not has_cjk:
        return True
    if prev_is_code and re.fullmatch(r"[.·…]{3,}", t):
        return True
    if prev_is_code and re.search(r"[(){}\[\];,<>#=]", t):
        return True
    return False


def normalize_heading_text(line: str) -> str:
    t = squeeze_ws(line)
    m = SEC_NO_RE.match(t)
    if m:
        t = squeeze_ws(m.group(2))
    t = t.rstrip("：:").strip()
    return t


def looks_like_heading(line: str) -> bool:
    t = squeeze_ws(line)
    if not t:
        return False
    if re.fullmatch(r"[.·…]{3,}", t):
        return False
    if normalize_list_item(t) is not None:
        return False
    if is_code_like(t, prev_is_code=False):
        return False
    if FIGURE_RE.match(t):
        return False
    if re.search(r"[，。；：！？?,!]", t):
        return False
    if SEC_NO_RE.match(t):
        return True
    if t in {"说明", "实现流程", "约束条件", "完整样例", "常见问题"}:
        return True
    if len(t) <= 14:
        return True
    if len(t) <= 28 and re.search(r"(实现|流程|定义|调用|参数|设置|概述|介绍|场景|规则|模板|说明|步骤|格式|获取|输出)", t):
        return True
    return False


def is_strong_heading(line: str) -> bool:
    t = squeeze_ws(line)
    if not t or re.search(r"[，。；：！？?,!]", t):
        return False
    return len(t) <= 36 and bool(re.search(r"(实现|流程|定义|调用|参数|设置|概述|介绍|场景|规则|模板|说明|格式|获取|输出)", t))


def should_attach_code_comment_continuation(line: str, last_code_line: str) -> bool:
    curr = squeeze_ws(line)
    prev = last_code_line.strip()
    if not prev.startswith("//"):
        return False
    if not curr or normalize_list_item(curr) is not None:
        return False
    if re.search(r"[{};<>#]", curr):
        return False
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", curr):
        return False
    if len(curr) > 60:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", curr))


def normalize_code_lines(lines: list[str]) -> list[str]:
    cleaned = [ln.replace("\u00a0", " ").replace("\t", "    ").rstrip() for ln in lines]
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    if not cleaned:
        return []

    indents = [len(ln) - len(ln.lstrip(" ")) for ln in cleaned if ln.strip()]
    cut = min(indents) if indents else 0
    return [ln[cut:] if len(ln) >= cut else ln for ln in cleaned]


def render_section_markdown(cur_title: str, raw_lines: list[str]) -> str:
    _, heading_title = split_sec_no(cur_title)
    title_norms = {
        norm_for_match(cur_title),
        norm_for_match(strip_sec_no(cur_title)),
        norm_for_match(heading_title),
    }

    blocks: list[str] = [f"## {heading_title}"]
    para_buf: list[tuple[str, int]] = []
    code_buf: list[str] = []
    prev_raw_blank = True

    def append_blank():
        if blocks and blocks[-1] != "":
            blocks.append("")

    def flush_para():
        nonlocal para_buf
        if not para_buf:
            return

        merged: list[str] = []
        curr = ""
        curr_indent = 0
        for line, indent in para_buf:
            text = normalize_prose_line(line)
            if not text:
                continue
            if not curr:
                curr = text
                curr_indent = indent
                continue
            if should_start_new_paragraph(curr, text, curr_indent, indent):
                merged.append(curr.strip())
                curr = text
            else:
                curr = join_prose(curr, text)
            curr_indent = indent
        if curr:
            merged.append(curr.strip())

        for p in merged:
            if p:
                blocks.append(p)
                append_blank()

        para_buf = []

    def flush_code():
        nonlocal code_buf
        if not code_buf:
            return
        lines = normalize_code_lines(code_buf)
        if lines:
            blocks.append("```cpp")
            blocks.extend(lines)
            blocks.append("```")
            append_blank()
        code_buf = []

    for raw in raw_lines:
        raw = raw.replace("\u00a0", " ").rstrip("\n")
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))

        if not stripped:
            flush_para()
            flush_code()
            append_blank()
            prev_raw_blank = True
            continue

        if norm_for_match(stripped) in title_norms:
            prev_raw_blank = False
            continue

        list_item = normalize_list_item(stripped)
        if (prev_raw_blank or SEC_NO_RE.match(stripped) or is_strong_heading(stripped)) and looks_like_heading(stripped):
            flush_para()
            flush_code()
            blocks.append(f"### {normalize_heading_text(stripped)}")
            append_blank()
            prev_raw_blank = False
            continue
        if list_item is not None:
            flush_para()
            flush_code()
            blocks.append(list_item)
            prev_raw_blank = False
            continue
        if code_buf and should_attach_code_comment_continuation(stripped, code_buf[-1]):
            code_buf.append(raw)
            prev_raw_blank = False
            continue
        if is_code_like(raw, prev_is_code=bool(code_buf)):
            flush_para()
            code_buf.append(raw)
            prev_raw_blank = False
            continue
        if code_buf:
            flush_code()

        para_buf.append((raw, indent))
        prev_raw_blank = False

    flush_para()
    flush_code()

    while blocks and not blocks[-1]:
        blocks.pop()

    compact: list[str] = []
    for line in blocks:
        if line == "" and compact and compact[-1] == "":
            continue
        compact.append(line)

    return "\n".join(compact) + "\n"

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

    for idx, (toc_idx, lvl, cur_title, cur_page1) in enumerate(selected):
        cur_start_page = max(0, cur_page1 - 1)

        # 边界按“清洗后的 toc 中，下一个同级/上级标题”确定，避免把下一大节带进当前文件。
        next_title = None
        next_start_page = doc.page_count  # default end
        for j in range(toc_idx + 1, len(cleaned)):
            n_lvl, n_title, n_page1 = cleaned[j]
            if n_lvl <= lvl:
                next_title = n_title
                next_start_page = max(0, n_page1 - 1)
                break

        # 文件名：0000_1.2 环境准备.md（保留中文）
        sec_no, name_title = split_sec_no(cur_title)

        safe_name = safe_filename_cn(name_title)
        prefix = f"{sec_no} " if sec_no else ""
        filename = f"{idx:04d}_{prefix}{safe_name}.md"
        out_path = out_dir / filename

        started = False
        section_lines: list[str] = []

        # 计算页范围：如果下一节起始页在后面，就只读到 next_start_page-1；
        # 只有当 next_start_page == cur_start_page 才做同页切分。
        end_page = doc.page_count - 1 if next_title is None else max(cur_start_page, next_start_page - 1)
        boundary_page = next_start_page if next_title is not None else None


        def page_lines(pno: int) -> list[str]:
            page = doc.load_page(pno)
    # 你如果有 clip/过滤页眉页脚，就放在这里
            return page.get_text("text", sort=True).splitlines()

        boundary_has_next = False
        if next_title is not None:
            # 先在 next_start_page 探测是否存在 next_title
            blines = page_lines(boundary_page)
            boundary_stop_pos = find_title_line_index(blines, next_title)
            if boundary_stop_pos is not None and boundary_stop_pos > 0:
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
                # lines 是当前页（且从 cur_title 起可能已裁过）
                # 注意：如果这一页不是起始页，lines 没裁掉前面；这里用整页 lines 来截断
                lines_full = lines
                stop_pos = find_title_line_index(lines_full, next_title)
                if stop_pos is not None and stop_pos > 0:
                    lines = lines_full[:stop_pos]
                    if lines:
                        section_lines.extend(lines)
                    break


            if lines:
                section_lines.extend(lines)

        # 如果整个循环都没开始，给个提示（避免空文件悄悄生成）
        if not started:
            content = f"## {name_title}\n\n> ⚠️ 未能在正文中定位到该小节标题（PDF 文本抽取可能丢失标题行）。\n"
            out_path.write_text(content, "utf-8")
            continue

        out_path.write_text(render_section_markdown(cur_title, section_lines), "utf-8")

    doc.close()

def main():
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



    # 下载到临时文件，再计算 hash
    pdf_name = slugify(label or "ascendc_doc", separator="_") or "ascendc_doc"
    pdf_path = DOWNLOAD_DIR / f"{pdf_name}.pdf"

    print(f"Downloading: {pdf_url}")
    download_pdf(pdf_url, pdf_path)

    file_hash = sha256_file(pdf_path)
    if (not args.force) and state.get("sha256") == file_hash:
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
