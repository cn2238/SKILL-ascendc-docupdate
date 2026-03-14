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
    r"^(#\s*include|#\s*define|#\s*if|#\s*endif|extern\b|template\b|typedef\b|using\b|class\b|struct\b|enum\b|namespace\b|if\s*\(|for\s*\(|while\s*\(|switch\s*\(|return\b|auto\b|const\b|static\b|void\b|inline\b|constexpr\b|public\s*:|private\s*:|protected\s*:|__global__\b|__aicore__\b|__aicpu__\b)"
)
SHELL_RE = re.compile(r"^(?:\$|cmake\b|python3?\b|bash\b|sh\b|make\b|pip\b|export\b|source\b|msopgen\b|bisheng(?:\s|-))")
FIGURE_RE = re.compile(r"^(图|表)\s*\d")
CJK_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
CPP_QUALIFIER_RE = re.compile(r"^(?:extern|inline|static|constexpr|virtual|friend|__global__|__aicore__|__aicpu__)$")
CPP_TYPE_HINT_RE = re.compile(
    r"^(?:void|bool|char|short|int|long|float|double|size_t|int\d+_t|uint\d+_t|acl\w*|AscendC::\w*|Kernel\w*)$"
)
CODE_FILE_RE = re.compile(r"(?:[A-Za-z0-9_./${}-]+)?\.(?:c|cc|cpp|cxx|h|hpp|asc|aicpu|o|so|a)(?![A-Za-z0-9_])")
CLI_OPTION_RE = re.compile(r"(?:^|\s)--?[A-Za-z][A-Za-z0-9_-]*")
CLI_FLAG_LINE_RE = re.compile(r"^--?[A-Za-z][A-Za-z0-9_]*(?:[=:\-][A-Za-z0-9_./${}-]+)?$")
CODE_WORD_SEQ_RE = re.compile(r"^[A-Za-z0-9_./${}<>\-+=:]+(?:\s+[A-Za-z0-9_./${}<>\-+=:]+)*$")
OPTION_TABLE_ROW_RE = re.compile(r"^(?P<option>.+?)\s+(?P<required>是|否)\s+(?P<desc>.+)$")
TABLE_TITLE_START_RE = re.compile(r"^表\d+-\d+\s+\S")
TABLE_TITLE_INLINE_RE = re.compile(r"表\d+-\d+\s+\S")
CMAKE_VAR_HEAD_RE = re.compile(r"^(CMAKE_[A-Z0-9_]+)\s+(.*)$")
LIB_HEAD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.+-]*)\s+(.*)$")
API_TABLE_ROW_START_RE = re.compile(r"^(?:基础API|Utils API|高阶API)\s*>")
API_TABLE_HEADER_NOISE_RE = re.compile(r"接口分类接口名称(?:备注)?")
API_NAME_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)?")
API_CLASS_TAIL_WORDS = [
    "基础算术",
    "逻辑计算",
    "复合计算",
    "比较与选择",
    "类型转换",
    "归约计算",
    "数据转换",
    "数据填充",
    "排序组合",
    "离散与聚合",
    "掩码操作",
    "量化设置",
    "基础数据搬运",
    "增强数据搬运",
    "切片数据搬运",
    "核内同步",
    "核间同步",
    "缓存控制",
    "系统变量访问",
    "系统变量访 问",
    "算法",
    "容器函数",
    "类型特性",
    "type_traits",
]
CMAKE_CONTINUATION_WORDS = {
    "PROPERTIES",
    "LANGUAGE",
    "PRIVATE",
    "PUBLIC",
    "STATIC",
    "SHARED",
    "ASC",
    "AICPU",
    "REQUIRED",
}


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
    # 避免正文里的 "#" 被 markdown 解释成标题
    if s.startswith("#"):
        s = "\\" + s
    return s


def normalize_option_cell(text: str) -> str:
    t = squeeze_ws(text)
    if not t:
        return t
    if t.startswith("-"):
        return t
    if t.startswith("cce-aicpu-"):
        return f"--{t}"
    if "，--" in t:
        return f"-{t}"
    if t in {"help", "x", "o", "c", "shared", "lib", "g", "fPIC", "O"}:
        return f"-{t}"
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:\s+<[^>]+>)?", t):
        return f"-{t}"
    return t


def escape_table_cell(text: str) -> str:
    return text.replace("|", r"\|")


def is_block_boundary(line: str) -> bool:
    t = line.strip()
    return t.startswith("## ") or t.startswith("### ") or bool(TABLE_TITLE_START_RE.match(t))


def split_embedded_table_titles(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        m = TABLE_TITLE_INLINE_RE.search(line)
        if not m or m.start() == 0:
            out.append(line)
            continue
        prev = line[m.start() - 1]
        if prev not in "。；:：）】》":
            out.append(line)
            continue
        head = line[:m.start()].rstrip()
        tail = line[m.start():].strip()
        if head:
            out.append(head)
        if tail:
            out.append(tail)
    return out


def repair_cmake_var_and_desc(var_name: str, desc: str) -> tuple[str, str]:
    name = var_name
    d = squeeze_ws(desc)
    if name.startswith("CMAKE_BUILD_T"):
        name = "CMAKE_BUILD_TYPE"
        d = d.replace("：YPE", "：")
    elif name.startswith("CMAKE_INSTALL_"):
        name = "CMAKE_INSTALL_PREFIX"
    elif name.startswith("CMAKE_CXX_CO"):
        name = "CMAKE_CXX_COMPILER_LAUNCHER"
        d = d.replace("程序MPILER_LAUNCH 为", "程序为")
        d = d.replace("并ER 提高", "并提高")
    d = d.replace("●", "<br>- ")
    d = d.replace("：<br>-", "：<br>-")
    return name, d


def repair_lib_name(name: str, desc: str) -> tuple[str, str]:
    n = name.strip()
    d = squeeze_ws(desc)
    special = {
        "libascendc_runti": "libascendc_runtime.a",
        "libascend_dump.s": "libascend_dump.so",
        "liberror_manager.": "liberror_manager.so",
    }
    if n in special:
        fixed = special[n]
        m = re.search(r"。([A-Za-z0-9_.+-]+)$", d)
        if m:
            d = d[: m.start() + 1].strip()
        return fixed, d

    m = re.search(r"。([A-Za-z0-9_.+-]+)$", d)
    if m and not re.search(r"\.(?:so|a)$", n):
        suffix = m.group(1)
        cand = n + suffix
        if re.search(r"\.(?:so|a)$", cand):
            n = cand
            d = d[: m.start() + 1].strip()
    return n, d


def append_desc_cell(base: str, extra: str) -> str:
    e = squeeze_ws(extra)
    if not e:
        return base
    return f"{base}<br>{e}" if base else e


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
    if looks_like_cli_option_line(t):
        return None
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


def looks_like_cli_option_line(line: str) -> bool:
    t = squeeze_ws(line)
    if not t:
        return False
    compact = t
    if re.match(r"^--?\s+\S+$", t):
        compact = re.sub(r"^(-{1,2})\s+", r"\1", t)
    if " " in compact:
        return False
    return bool(CLI_FLAG_LINE_RE.match(compact))


def looks_like_code_continuation_line(line: str) -> bool:
    t = squeeze_ws(line)
    if not t:
        return False
    if re.search(r"[\u4e00-\u9fff]", t):
        return False
    if len(t) > 180:
        return False
    if not CODE_WORD_SEQ_RE.fullmatch(t):
        return False
    if any(sym in t for sym in ("_", ".", "/", "$", "=", "<", ">")):
        return True
    tokens = t.split()
    if any(tok in CMAKE_CONTINUATION_WORDS for tok in tokens):
        return True
    return t.isupper()


def is_code_like(line: str, prev_is_code: bool) -> bool:
    t = line.strip()
    if not t:
        return False
    if normalize_list_item(t) is not None:
        return False
    if FIGURE_RE.match(t):
        return False
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", t))
    if t.startswith("${") and has_cjk:
        return False
    if prev_is_code and looks_like_cli_option_line(t):
        return True
    if is_hash_comment_code_line(t, prev_is_code):
        return True
    if CODE_HEAD_RE.search(t) or SHELL_RE.search(t):
        return True
    if t.startswith("//") or t.startswith("/*") or t.endswith("*/"):
        return True
    if re.fullmatch(r"(public|private|protected)\s*:", t):
        return True
    if looks_like_cpp_signature_line(t):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_:<>]*\s*\(.*\)\s*;?\s*(//.*)?$", t):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(.*\)\s*(//.*)?$", t):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_:<>\s*&]+\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^;]+;\s*(//.*)?$", t):
        return True
    if not has_cjk and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(.*$", t):
        return True
    if has_cjk and ";" in t and re.search(r"[A-Za-z_][A-Za-z0-9_:.>]*\s*\(", t):
        return True
    if any(sym in t for sym in ("├──", "└──", "│")):
        return True
    if any(sym in t for sym in ("{", "}", ";", "::", "->", "<<<", ">>>", "#include", "#define")) and not has_cjk:
        return True
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)", t) and re.search(r"[=<>]", t) and not has_cjk:
        return True
    if prev_is_code and looks_like_code_continuation_line(t):
        return True
    if prev_is_code and looks_like_mixed_token_cjk_continuation(t):
        return True
    if prev_is_code and CODE_FILE_RE.search(t):
        return True
    if prev_is_code and not has_cjk and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
        return True
    if prev_is_code and re.fullmatch(r"[.·…]{3,}", t):
        return True
    if prev_is_code and (not has_cjk) and re.search(r"[(){}\[\];,<>#=]", t):
        return True
    return False


def is_hash_comment_code_line(line: str, prev_is_code: bool) -> bool:
    t = line.strip()
    if not t.startswith("#"):
        return False
    body = squeeze_ws(t.lstrip("#"))
    if not body:
        return False
    if re.search(r"\b(?:bisheng|cmake|make|g\+\+|gcc|python3?|bash|sh)\b", body):
        return True
    if body.startswith("-"):
        return True
    if CODE_FILE_RE.search(body):
        return True
    if re.search(r"\.(?:c|cc|cpp|cxx|h|hpp|asc|aicpu|so|a)(?![A-Za-z0-9_])", body):
        return True
    if CLI_OPTION_RE.search(body):
        return True
    if prev_is_code:
        return True
    return False


def looks_like_cpp_signature_line(line: str) -> bool:
    t = squeeze_ws(line)
    if not t:
        return False
    if normalize_list_item(t) is not None:
        return False
    if re.search(r"[\u4e00-\u9fff]", t):
        return False
    if "(" not in t or ")" not in t:
        return False

    prefix = t.split("(", 1)[0].strip()
    if not prefix:
        return False

    raw_tokens = [tok for tok in re.split(r"\s+", prefix) if tok]
    tokens = [tok.strip("*&") for tok in raw_tokens if tok.strip("*&")]
    if len(tokens) < 2:
        return False
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tokens[-1]):
        return False

    qualifier_or_type = False
    for tok in tokens[:-1]:
        if CPP_QUALIFIER_RE.match(tok) or CPP_TYPE_HINT_RE.match(tok):
            qualifier_or_type = True
            break

    return qualifier_or_type or len(tokens) >= 3


def looks_like_mixed_token_cjk_continuation(line: str) -> bool:
    t = squeeze_ws(line)
    if not t:
        return False
    if normalize_list_item(t) is not None:
        return False
    if t.startswith(("●", "-", "步骤", "表", "图")):
        return False
    if not re.match(r"^[A-Za-z0-9_]+[\u4e00-\u9fff]", t):
        return False
    if re.search(r"[{};#]", t):
        return False
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", t):
        return False
    if len(t) > 100:
        return False
    return True


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
    if t.startswith("#"):
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
    if len(t) <= 28 and re.search(r"(实现|流程|定义|调用|参数|设置|概述|介绍|场景|规则|模板|说明|步骤|格式|获取|输出|选项|变量|链接库)", t):
        return True
    return False


def is_strong_heading(line: str) -> bool:
    t = squeeze_ws(line)
    if not t or re.search(r"[，。；：！？?,!]", t):
        return False
    if t.startswith("#"):
        return False
    return len(t) <= 36 and bool(re.search(r"(实现|流程|定义|调用|参数|设置|概述|介绍|场景|规则|模板|说明|格式|获取|输出|选项|变量|链接库)", t))


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


def should_attach_hash_comment_continuation(line: str, last_code_line: str) -> bool:
    curr = squeeze_ws(line)
    prev = last_code_line.strip()
    if not prev.startswith("#"):
        return False
    if not curr or normalize_list_item(curr) is not None:
        return False
    if curr.startswith(("●", "-", "步骤")):
        return False
    if curr.startswith("#"):
        return False
    if len(curr) > 120:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", curr))


def should_attach_inline_comment_continuation(line: str, last_code_line: str) -> bool:
    curr = squeeze_ws(line)
    prev = last_code_line.strip()
    if "//" not in prev:
        return False
    if prev.startswith("//"):
        return False
    comment_part = prev.split("//", 1)[1].strip()
    if comment_part.endswith(("。", ".", "！", "!", "？", "?", "；", ";")):
        return False
    if not curr or normalize_list_item(curr) is not None:
        return False
    if curr.startswith(("●", "-", "步骤", "表", "图")):
        return False
    if not (
        re.match(r"^[\u4e00-\u9fff]", curr)
        or looks_like_mixed_token_cjk_continuation(curr)
    ):
        return False
    if re.search(r"[{};#]", curr):
        return False
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", curr):
        return False
    if len(curr) > 80:
        return False
    return True


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


def postprocess_option_table_blocks(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_code = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            in_code = not in_code
            out.append(line)
            i += 1
            continue
        if in_code or line.strip() != "### 选项 是否 说明":
            out.append(line)
            i += 1
            continue

        j = i + 1
        while j < len(lines) and lines[j].strip() == "":
            j += 1

        if j < len(lines) and lines[j].strip() == "必需":
            j += 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1

        body: list[str] = []
        k = j
        while k < len(lines):
            cur = lines[k]
            if cur.startswith("## ") or cur.startswith("### ") or cur.startswith("```"):
                break
            body.append(cur)
            k += 1

        rows: list[list[str]] = []
        for raw in body:
            t = raw.strip()
            if not t:
                continue
            if t.startswith("- "):
                content = squeeze_ws(t[2:])
                m = OPTION_TABLE_ROW_RE.match(content)
                if not m:
                    continue
                opt = normalize_option_cell(m.group("option"))
                req = m.group("required")
                desc = m.group("desc")
                rows.append([opt, req, desc])
            elif rows:
                rows[-1][2] = f"{rows[-1][2]}<br>{squeeze_ws(t)}"

        if len(rows) < 1:
            out.append(line)
            i += 1
            continue

        out.append("### 选项 是否必需 说明")
        out.append("")
        out.append("| 选项 | 是否必需 | 说明 |")
        out.append("| --- | --- | --- |")
        for opt, req, desc in rows:
            out.append(
                f"| {escape_table_cell(opt)} | {escape_table_cell(req)} | {escape_table_cell(desc)} |"
            )
        out.append("")
        i = k

    return out


def postprocess_common_table_blocks(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            in_code = not in_code
            out.append(line)
            i += 1
            continue
        if in_code:
            out.append(line)
            i += 1
            continue

        head = line.strip()
        if head == "### 接口分类 接口名称":
            block, nxt = parse_api_scope_table_block(lines, i, with_remark=False)
            if block:
                out.extend(block)
                i = nxt
                continue
        if head == "### 接口分类 接口名称 备注":
            block, nxt = parse_api_scope_table_block(lines, i, with_remark=True)
            if block:
                out.extend(block)
                i = nxt
                continue
        if head == "### 变量 配置说明":
            block, nxt = parse_var_table_block(lines, i)
            if block:
                out.extend(block)
                i = nxt
                continue
        if head == "### 名称 作用描述 使用场景":
            block, nxt = parse_lib_table_3col_block(lines, i)
            if block:
                out.extend(block)
                i = nxt
                continue
        if head == "### 名称 作用描述":
            block, nxt = parse_lib_table_2col_block(lines, i)
            if block:
                out.extend(block)
                i = nxt
                continue

        out.append(line)
        i += 1

    return out


def parse_var_table_block(lines: list[str], start: int) -> tuple[list[str] | None, int]:
    j = start + 1
    while j < len(lines) and lines[j].strip() == "":
        j += 1

    rows: list[list[str]] = []
    k = j
    while k < len(lines):
        cur = lines[k]
        t = cur.strip()
        if is_block_boundary(cur):
            break

        if cur.startswith("```"):
            code_lines: list[str] = []
            k += 1
            while k < len(lines) and not lines[k].startswith("```"):
                code_line = squeeze_ws(lines[k])
                if code_line:
                    code_lines.append(f"`{escape_table_cell(code_line)}`")
                k += 1
            if k < len(lines) and lines[k].startswith("```"):
                k += 1
            if rows and code_lines:
                rows[-1][1] = append_desc_cell(rows[-1][1], "<br>".join(code_lines))
            continue

        if not t:
            k += 1
            continue

        m = CMAKE_VAR_HEAD_RE.match(t)
        if m:
            var_name, desc = repair_cmake_var_and_desc(m.group(1), m.group(2))
            rows.append([var_name, desc])
            k += 1
            continue

        if rows:
            rows[-1][1] = append_desc_cell(rows[-1][1], t)
        k += 1

    if not rows:
        return None, start + 1

    table_lines = [
        "### 变量 配置说明",
        "",
        "| 变量 | 配置说明 |",
        "| --- | --- |",
    ]
    for var_name, desc in rows:
        table_lines.append(
            f"| {escape_table_cell(var_name)} | {escape_table_cell(desc)} |"
        )
    table_lines.append("")
    return table_lines, k


def parse_lib_table_3col_block(lines: list[str], start: int) -> tuple[list[str] | None, int]:
    j = start + 1
    while j < len(lines) and lines[j].strip() == "":
        j += 1

    rows: list[list[str]] = []
    k = j
    while k < len(lines):
        cur = lines[k]
        t = cur.strip()
        if is_block_boundary(cur):
            break
        if not t:
            k += 1
            continue

        m = LIB_HEAD_RE.match(t)
        if m and "." in m.group(1):
            name, rest = repair_lib_name(m.group(1), m.group(2))
            desc, scene = squeeze_ws(rest), ""
            if "。" in desc:
                pos = desc.find("。")
                scene = squeeze_ws(desc[pos + 1 :])
                desc = desc[: pos + 1]
            rows.append([name, desc, scene])
            k += 1
            continue

        if rows:
            if rows[-1][2]:
                rows[-1][2] = append_desc_cell(rows[-1][2], t)
            else:
                rows[-1][2] = squeeze_ws(t)
        k += 1

    if not rows:
        return None, start + 1

    table_lines = [
        "### 名称 作用描述 使用场景",
        "",
        "| 名称 | 作用描述 | 使用场景 |",
        "| --- | --- | --- |",
    ]
    for name, desc, scene in rows:
        table_lines.append(
            f"| {escape_table_cell(name)} | {escape_table_cell(desc)} | {escape_table_cell(scene)} |"
        )
    table_lines.append("")
    return table_lines, k


def parse_lib_table_2col_block(lines: list[str], start: int) -> tuple[list[str] | None, int]:
    j = start + 1
    while j < len(lines) and lines[j].strip() == "":
        j += 1

    rows: list[list[str]] = []
    k = j
    while k < len(lines):
        cur = lines[k]
        t = cur.strip()
        if is_block_boundary(cur):
            break
        if not t:
            k += 1
            continue

        m = LIB_HEAD_RE.match(t)
        if m and m.group(1).startswith("lib"):
            name, desc = repair_lib_name(m.group(1), m.group(2))
            rows.append([name, squeeze_ws(desc)])
            k += 1
            continue

        if rows:
            rows[-1][1] = append_desc_cell(rows[-1][1], t)
        k += 1

    if not rows:
        return None, start + 1

    table_lines = [
        "### 名称 作用描述",
        "",
        "| 名称 | 作用描述 |",
        "| --- | --- |",
    ]
    for name, desc in rows:
        table_lines.append(
            f"| {escape_table_cell(name)} | {escape_table_cell(desc)} |"
        )
    table_lines.append("")
    return table_lines, k


def clean_api_table_text(text: str) -> str:
    t = squeeze_ws(text)
    if not t:
        return t
    t = API_TABLE_HEADER_NOISE_RE.sub("", t)
    t = t.replace("接口分类 接口名称 备注", "")
    t = t.replace("接口分类 接口名称", "")
    t = t.replace("系统变量访 问", "系统变量访问")
    return squeeze_ws(t)


def looks_like_api_table_row_start(text: str) -> bool:
    return bool(API_TABLE_ROW_START_RE.match(squeeze_ws(text)))


def add_api_class_tail(api_class: str, tail: str) -> str:
    c = squeeze_ws(api_class)
    t = squeeze_ws(tail)
    if not t or t in c:
        return c
    if t in {"算法", "容器函数", "类型特性", "type_traits"} and not c.endswith(">"):
        if c.endswith(("C++标准库", "模板库函数")):
            return f"{c} > {t}".strip()
    return f"{c} {t}".strip()


def join_api_name_parts(left: str, right: str) -> str:
    l = squeeze_ws(left)
    r = squeeze_ws(right)
    if not l:
        return r
    if not r:
        return l
    if l.endswith(("、", "，", ",", "/", "(", "（")):
        return f"{l}{r}"
    return join_prose(l, r)


def split_api_class_and_name(text: str) -> tuple[str | None, str | None]:
    t = clean_api_table_text(text)
    if not t:
        return None, None
    for m in API_NAME_TOKEN_RE.finditer(t):
        token = m.group(0)
        prefix = t[: m.start()].strip()
        if prefix.count(">") < 1:
            continue
        if not re.search(r"[\u4e00-\u9fff]", prefix):
            continue
        if token in {"API", "Utils", "Atlas", "Core"}:
            continue
        return squeeze_ws(prefix), squeeze_ws(t[m.start() :])
    return None, None


def extract_api_class_tail_prefix(text: str) -> tuple[str, str]:
    t = squeeze_ws(text)
    for w in sorted(API_CLASS_TAIL_WORDS, key=len, reverse=True):
        if t.startswith(w):
            return w, squeeze_ws(t[len(w) :])
    return "", t


def looks_like_api_remark_text(text: str) -> bool:
    t = squeeze_ws(text)
    if not t:
        return False
    if t.startswith(">") or "不支持" in t or "TSCM" in t:
        return True
    if re.search(r"[A-Za-z_]", t):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", t))


def should_arrow_be_class_tail(api_class: str, right: str) -> bool:
    cls = squeeze_ws(api_class)
    r = squeeze_ws(right)
    if not r:
        return False
    tail, _ = extract_api_class_tail_prefix(r)
    if not tail:
        return False
    if "不支持" in r or "TSCM" in r:
        return False
    if cls.startswith(("Utils API >", "高阶API >")):
        return True
    return bool(API_NAME_TOKEN_RE.search(r))


def repair_api_class_and_name(api_class: str, api_name: str) -> tuple[str, str]:
    cls = clean_api_table_text(api_class)
    name = clean_api_table_text(api_name).lstrip("-").strip()
    cls = cls.replace("基础API > 数据搬运增强数据搬运", "基础API > 数据搬运 > 增强数据搬运")

    if cls.endswith("访") and name.startswith("问"):
        cls = f"{cls}问"
        name = name[1:].lstrip()
    cls = cls.replace("系统变量访", "系统变量访问")
    if "系统变量访问" in cls:
        name = re.sub(r"^问\s+", "", name)
        name = re.sub(r"([、,，])\s*问\s+", r"\1", name)

    for w in sorted(API_CLASS_TAIL_WORDS, key=len, reverse=True):
        p = re.compile(rf"([、,，])\s*{re.escape(w)}\s+")
        if p.search(name):
            name = p.sub(r"\1", name, count=1)
            cls = add_api_class_tail(cls, w)
        if name.startswith(f"{w} "):
            name = squeeze_ws(name[len(w) :])
            cls = add_api_class_tail(cls, w)
        if name.endswith(w) and re.search(r"[A-Za-z0-9_)\]）]$", name[: -len(w)]):
            name = name[: -len(w)].rstrip(" 、,，")
            cls = add_api_class_tail(cls, w)
        m = re.match(rf"^([A-Za-z_][A-Za-z0-9_:/()<>+-]*){re.escape(w)}$", name)
        if m:
            name = m.group(1)
            cls = add_api_class_tail(cls, w)

    name = squeeze_ws(name)
    name = re.sub(r"\s+([，。；：！？、）】》])", r"\1", name)
    name = re.sub(r"([（【《])\s+", r"\1", name)
    if (cls == "高阶API >" or cls == "高阶API") and name.startswith("C++标准库 "):
        cls = "高阶API > C++标准库"
        name = squeeze_ws(name[len("C++标准库 ") :])
    if cls == "高阶API > 类型特性" and name.startswith("C++标准库 "):
        cls = "高阶API > C++标准库 > 类型特性"
        name = squeeze_ws(name[len("C++标准库 ") :])
    if cls == "高阶API > 模板库函数 type_traits":
        cls = "高阶API > 模板库函数 > type_traits"
    if cls == "基础API >" and (
        name.startswith("Copy、DataCopyPad")
        or name.startswith("DataCopy")
        or name.startswith("VECIN/")
    ):
        cls = "基础API > 数据搬运"
    if cls.endswith(">") and name == "SetDeqScale":
        cls = add_api_class_tail(cls, "量化设置")
    elif cls.endswith(">"):
        cls = cls.rstrip(">").rstrip()
    return cls, name


def parse_api_scope_row(line: str, with_remark: bool) -> list[str] | None:
    t = clean_api_table_text(line)
    api_class, rest = split_api_class_and_name(t)
    if not api_class or not rest:
        return None

    api_name = rest
    remark = "-" if with_remark else ""

    if with_remark:
        if "不支持" in api_class:
            before, rem = api_class.split("不支持", 1)
            api_class = squeeze_ws(before)
            remark = f"不支持{squeeze_ws(rem)}" if squeeze_ws(rem) else "不支持"

        arrow = re.search(r"\s*->\s*", rest)
        if arrow:
            api_name = squeeze_ws(rest[: arrow.start()])
            right = clean_api_table_text(rest[arrow.end() :])
            if right:
                if should_arrow_be_class_tail(api_class, right):
                    tail, tail_rest = extract_api_class_tail_prefix(right)
                    api_class = add_api_class_tail(api_class, tail)
                    if tail_rest:
                        api_name = join_api_name_parts(api_name, tail_rest)
                    remark = "-"
                else:
                    remark = right
        else:
            m = re.search(r"\s+-(?!>)\s*", rest)
            if m:
                left = squeeze_ws(rest[: m.start()])
                right = clean_api_table_text(rest[m.end() :])
                api_name = left
                if not right:
                    remark = "-"
                else:
                    tail, tail_rest = extract_api_class_tail_prefix(right)
                    if tail:
                        api_class = add_api_class_tail(api_class, tail)
                        if tail_rest:
                            api_name = join_api_name_parts(api_name, tail_rest)
                        remark = "-"
                    elif looks_like_api_remark_text(right):
                        remark = right
                    else:
                        api_name = join_api_name_parts(api_name, right)
                        remark = "-"

    api_class, api_name = repair_api_class_and_name(api_class, api_name)
    if not api_class or not api_name:
        return None
    if with_remark:
        return [api_class, api_name, clean_api_table_text(remark) or "-"]
    return [api_class, api_name]


def parse_api_scope_table_block(
    lines: list[str], start: int, with_remark: bool
) -> tuple[list[str] | None, int]:
    j = start + 1
    while j < len(lines) and lines[j].strip() == "":
        j += 1

    rows: list[list[str]] = []
    pending_tail = ""
    trailing_table_title = ""
    k = j
    while k < len(lines):
        stop_after_current = False
        cur = lines[k]
        t = cur.strip()
        if cur.startswith("```"):
            break
        if cur.startswith("## "):
            break
        if TABLE_TITLE_START_RE.match(t):
            break
        if cur.startswith("### "):
            heading_text = clean_api_table_text(cur[4:].strip().rstrip("-").strip())
            if not heading_text:
                k += 1
                continue
            if heading_text in API_CLASS_TAIL_WORDS:
                pending_tail = heading_text
                k += 1
                continue
            if heading_text.startswith("接口分类"):
                k += 1
                continue
            break
        if not t:
            k += 1
            continue

        t = clean_api_table_text(t)
        if not t:
            k += 1
            continue
        m_title = TABLE_TITLE_INLINE_RE.search(t)
        if m_title and m_title.start() > 0:
            trailing_table_title = squeeze_ws(t[m_title.start() :])
            t = squeeze_ws(t[: m_title.start()])
            stop_after_current = True
            if not t:
                break

        if looks_like_api_table_row_start(t):
            row = parse_api_scope_row(t, with_remark)
            if row:
                if pending_tail:
                    if row[0].endswith(">"):
                        row[0] = add_api_class_tail(row[0], pending_tail)
                    pending_tail = ""
                rows.append(row)
            k += 1
            if stop_after_current:
                break
            continue

        if with_remark and rows and re.match(
            r"^(基础数据搬运|增强数据搬运|随路转换ND2NZ搬运|随路转换NZ2ND搬运|随路量化激活搬运|Copy、DataCopyPad、)",
            t,
        ):
            base = rows[-1][0]
            m = re.match(r"^(.*>)\s*", base)
            base_prefix = m.group(1) if m else base
            synthetic = f"{base_prefix} {t}"
            row = parse_api_scope_row(synthetic, with_remark)
            if row:
                if pending_tail:
                    if row[0].endswith(">"):
                        row[0] = add_api_class_tail(row[0], pending_tail)
                    pending_tail = ""
                rows.append(row)
            k += 1
            if stop_after_current:
                break
            continue

        if not rows:
            k += 1
            if stop_after_current:
                break
            continue

        if with_remark and (t.startswith("- >") or t.startswith("-")):
            extra = squeeze_ws(t.lstrip("-").lstrip(">"))
            if extra:
                rows[-1][2] = append_desc_cell(rows[-1][2], extra)
            k += 1
            if stop_after_current:
                break
            continue

        if with_remark and looks_like_api_remark_text(t):
            rows[-1][2] = append_desc_cell(rows[-1][2], t.lstrip("> ").strip())
        else:
            rows[-1][1] = append_desc_cell(rows[-1][1], t)
        k += 1
        if stop_after_current:
            break

    if len(rows) < 2:
        return None, start + 1

    if with_remark:
        rows = normalize_api_scope_special_rows(rows)

    if with_remark:
        table_lines = [
            "### 接口分类 接口名称 备注",
            "",
            "| 接口分类 | 接口名称 | 备注 |",
            "| --- | --- | --- |",
        ]
        for api_class, api_name, remark in rows:
            table_lines.append(
                f"| {escape_table_cell(api_class)} | {escape_table_cell(api_name)} | {escape_table_cell(remark)} |"
            )
    else:
        table_lines = [
            "### 接口分类 接口名称",
            "",
            "| 接口分类 | 接口名称 |",
            "| --- | --- |",
        ]
        for api_class, api_name in rows:
            table_lines.append(
                f"| {escape_table_cell(api_class)} | {escape_table_cell(api_name)} |"
            )
    table_lines.append("")
    if trailing_table_title:
        table_lines.append(trailing_table_title)
        table_lines.append("")
    return table_lines, k


def normalize_api_scope_special_rows(rows: list[list[str]]) -> list[list[str]]:
    out: list[list[str]] = []
    inserted_slice_row = False

    for cls, name, remark in rows:
        cls = squeeze_ws(cls)
        name = squeeze_ws(name)
        remark = squeeze_ws(remark).replace("> TSCM", "-> TSCM")

        m_move = re.fullmatch(r"基础API > 数据搬运 > (基础数据搬运|增强数据搬运)", cls)
        if m_move and "VECIN/" in name:
            out.append(
                [
                    "基础API > 数据搬运 > DataCopy",
                    m_move.group(1),
                    "不支持VECIN/VECCALC/VECOUT<br>-> TSCM通路的数据搬运。",
                ]
            )
            continue

        if cls == "基础API > 数据搬运 > 随路转换":
            if not inserted_slice_row:
                out.append(["基础API > 数据搬运 > DataCopy", "切片数据搬运", "-"])
                inserted_slice_row = True
            out.append(
                [
                    "基础API > 数据搬运 > DataCopy",
                    "随路转换ND2NZ搬运<br>随路转换NZ2ND搬运<br>随路量化激活搬运",
                    "不支持VECIN/VECCALC/VECOUT<br>-> TSCM通路的数据搬运。",
                ]
            )
            continue

        if cls == "基础API > 数据搬运 >" and (
            name.startswith("Copy、DataCopyPad")
            or name.startswith("DataCopy")
            or name.startswith("VECIN/")
        ):
            cls = "基础API > 数据搬运"

        out.append([cls, name, remark])
        if cls.startswith("基础API > 数据搬运") and name == "切片数据搬运":
            inserted_slice_row = True

    return out


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
    pending_blank_after_code = False

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
            if code_buf:
                pending_blank_after_code = True
                prev_raw_blank = True
                continue
            flush_para()
            append_blank()
            prev_raw_blank = True
            continue

        if pending_blank_after_code:
            if is_code_like(raw, prev_is_code=True):
                code_buf.append("")
            else:
                flush_code()
            pending_blank_after_code = False

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
        if code_buf and (
            should_attach_code_comment_continuation(stripped, code_buf[-1])
            or should_attach_hash_comment_continuation(stripped, code_buf[-1])
            or should_attach_inline_comment_continuation(stripped, code_buf[-1])
        ):
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

    compact = split_embedded_table_titles(compact)
    compact = postprocess_option_table_blocks(compact)
    compact = postprocess_common_table_blocks(compact)

    final_compact: list[str] = []
    for line in compact:
        if line == "" and final_compact and final_compact[-1] == "":
            continue
        final_compact.append(line)

    return "\n".join(final_compact) + "\n"

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
