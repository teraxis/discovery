"""Відбір файлів для NotebookLM: рекурсивно, лише підтримувані формати; xlsx/xls/html → Markdown."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

# Завантажуються як є (офіційний перелік NotebookLM / Gemini Notebook API)
NATIVE = {"pdf", "docx", "pptx", "odt", "epub", "rtf", "hwp", "csv", "txt", "md",
          "mp3", "m4a", "wav", "aac", "ogg", "oga", "flac", "opus", "3gp", "3gpp",
          "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic"}
# Конвертуються в Markdown перед завантаженням
CONVERT = {"xlsx", "xlsm", "xls", "html", "htm", "mhtml", "xml"}
MAX_BYTES = 200 * 1024 * 1024
MAX_MD_CHARS = 900_000  # ліміт NotebookLM ~1 МБ тексту на джерело


def scan(root: Path, recursive: bool = True, only: set[str] | None = None,
         exclude: list[re.Pattern] | None = None):
    """→ (native: [Path], convert: [Path], skipped: Counter{ext: n})"""
    it = root.rglob("*") if recursive else root.glob("*")
    native, conv, skipped = [], [], Counter()
    for p in sorted(x for x in it if x.is_file()):
        if p.name.startswith(("~$", ".")) or any(rx.search(str(p)) for rx in exclude or []):
            continue
        ext = p.suffix.lower().lstrip(".")
        if only and ext not in only:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            skipped["недоступні"] += 1
            continue
        if size > MAX_BYTES:
            skipped[f"{ext} >200МБ"] += 1
        elif ext in NATIVE:
            native.append(p)
        elif ext in CONVERT:
            conv.append(p)
        else:
            skipped[ext or "без розширення"] += 1
    return native, conv, skipped


RECALL = 0x400000 | 0x40000  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN (хмарна заглушка)


def _attrs(p: Path) -> int:
    try:
        return getattr(p.stat(), "st_file_attributes", 0)
    except OSError:
        return 0


def ensure_local(root: Path, files: list[Path], log=print, timeout: int = 3600) -> None:
    """OneDrive Files On-Demand: закріпити теку («Завжди зберігати на пристрої») і дочекатися завантаження."""
    import subprocess
    import time
    cloud = [f for f in files if _attrs(f) & RECALL]
    if not cloud:
        return
    log(f"  OneDrive: {len(cloud)} файлів лише в хмарі — закріплюю теку й чекаю завантаження…")
    subprocess.run(["attrib", "+P", "-U", "/S", "/D", str(root / "*")], capture_output=True)
    t0, last = time.time(), -1
    while True:
        left = [f for f in cloud if _attrs(f) & RECALL]
        if not left:
            return
        if len(left) != last:
            log(f"  OneDrive: лишилось {len(left)}")
            last = len(left)
        if time.time() - t0 > timeout:
            raise RuntimeError(f"OneDrive не завантажив {len(left)} файлів за {timeout // 60} хв. "
                               "Перевірте, що OneDrive запущений і синхронізацію не призупинено.")
        if time.time() - t0 > 300 and last == len(cloud):
            raise RuntimeError("OneDrive не завантажує файли («cloud file provider is not running»?). "
                               "Відкрийте OneDrive, відновіть синхронізацію і повторіть.")
        time.sleep(5)


def _cell(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%d.%m.%Y") if not getattr(v, "hour", 0) else v.strftime("%d.%m.%Y %H:%M")
    s = str(v).replace("|", "/").replace("\n", " ").strip()
    return s[:-2] if re.fullmatch(r"-?\d+\.0", s) else s


def _md_table(rows: list[list[str]]) -> str:
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    keep = [j for j in range(w) if any(j < len(r) and r[j] for r in rows)]
    rows = [[r[j] if j < len(r) else "" for j in keep] for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(keep)]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def xlsx_to_md(p: Path) -> str:
    parts = [f"# {p.name}"]
    if p.suffix.lower() == ".xls":
        import xlrd
        wb = xlrd.open_workbook(str(p))
        for sh in wb.sheets():
            rows = [[_cell(sh.cell_value(i, j)) for j in range(sh.ncols)] for i in range(sh.nrows)]
            t = _md_table(rows)
            if t:
                parts += [f"\n## Аркуш: {sh.name}\n", t]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = [[_cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
            t = _md_table(rows)
            if t:
                parts += [f"\n## Аркуш: {ws.title}\n", t]
        wb.close()
    return "\n".join(parts)


def html_to_md(p: Path) -> str:
    from bs4 import BeautifulSoup
    raw = p.read_bytes()
    soup = BeautifulSoup(raw, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "head"]):
        t.decompose()
    for tbl in soup.find_all("table"):
        rows = [[" ".join(c.get_text(" ", strip=True).split()).replace("|", "/")
                 for c in tr.find_all(["td", "th"])] for tr in tbl.find_all("tr")]
        tbl.replace_with(soup.new_string("\n\n" + _md_table(rows) + "\n\n"))
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return f"# {p.name}\n\n" + text.strip()


def to_markdown(p: Path, cache: Path) -> tuple[Path, str]:
    """Конвертує файл у .md (кеш за хешем вмісту). → (шлях до .md, sha256 оригіналу)"""
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    out = cache / f"{h[:20]}.md"
    if not out.exists():
        ext = p.suffix.lower().lstrip(".")
        md = xlsx_to_md(p) if ext in ("xlsx", "xlsm", "xls") else html_to_md(p)
        if len(md) > MAX_MD_CHARS:
            md = md[:MAX_MD_CHARS] + "\n\n[… обрізано: перевищено ліміт NotebookLM на одне джерело]"
        cache.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
    return out, h
