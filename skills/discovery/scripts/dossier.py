"""Досьє кандидата: усі відповіді NotebookLM → один DOCX з нормальним форматуванням.

- ## / ### / #### → заголовки 2/3/4 рівня; рядок, що повністю жирний і починається з номера
  («**1. Назва обставини**»), → заголовок 4 рівня;
- маркеровані й нумеровані списки, таблиці markdown → таблиці Word;
- посилання [1], [1, 2], [3-5] з різних відповідей перенумеровуються наскрізно за джерелом,
  наприкінці — «Перелік джерел»;
- репліки NotebookLM на кшталт «Бажаєте, щоб я продовжив?» прибираються.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

CITE_RX = re.compile(r"\[(\d+(?:\s*[,;–\-]\s*\d+)*)\]")
CHATTER_RX = re.compile(
    r"(бажаєте|чи хочете|чи продовж|готов(ий|а) продовж|напишіть\s*[«\"]?продовж|якщо бажаєте|"
    r"можу (також )?(звести|підготувати|продовжити|надати)|переходжу до наступного блоку\?|"
    r"дайте знати|повідомте,? (коли|якщо))", re.I)
EMOJI_RX = re.compile(r"^[\s\W]*[\U0001F300-\U0001FAFF☀-➿]")


def _expand(spec: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*[,;]\s*", spec):
        m = re.fullmatch(r"(\d+)\s*[–\-]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out += list(range(a, b + 1)) if 0 < b - a < 50 else [a, b]
        elif part.strip().isdigit():
            out.append(int(part))
    return out


def clean_answer(text: str) -> str:
    lines = text.strip().splitlines()
    # прибрати хвіст-звертання до користувача (останні 1–2 абзаци)
    for _ in range(2):
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and len(lines[-1]) < 400 and (CHATTER_RX.search(lines[-1]) or EMOJI_RX.match(lines[-1])):
            lines.pop()
    lines = [l for l in lines if not (EMOJI_RX.match(l) and CHATTER_RX.search(l))]
    return "\n".join(lines).strip()


class Renumberer:
    def __init__(self, titles: dict):
        self.titles = titles
        self.glob: dict[str, int] = {}

    def apply(self, answer: str, refs: list[dict]) -> str:
        local = {}
        for r in refs or []:
            if r.get("citation_number") is not None and r.get("source_id"):
                local[int(r["citation_number"])] = r["source_id"]

        def sub(m):
            nums = []
            for n in _expand(m.group(1)):
                sid = local.get(n)
                if not sid:
                    continue
                g = self.glob.setdefault(sid, len(self.glob) + 1)
                if g not in nums:
                    nums.append(g)
            return "".join(f"[{g}]" for g in nums) if nums else ""

        return CITE_RX.sub(sub, answer)

    def listing(self) -> list[str]:
        return [f"[{g}] {self.titles.get(sid, sid)}" for sid, g in sorted(self.glob.items(), key=lambda kv: kv[1])]


def _set_base_font(d) -> None:
    st = d.styles["Normal"]
    st.font.name = "Arial"
    st.font.size = Pt(10)
    rpr = st.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for k in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(k), "Arial")
    st.paragraph_format.space_after = Pt(4)
    for name, size in (("Heading 2", 13), ("Heading 3", 12), ("Heading 4", 10.5)):
        h = d.styles[name]
        h.font.name = "Arial"
        h.font.size = Pt(size)
        h.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)
        h.paragraph_format.space_before = Pt(10 if name != "Heading 4" else 6)
        h.paragraph_format.space_after = Pt(4)


def _runs(par, text: str) -> None:
    for chunk in re.split(r"(\*\*[^*]+\*\*|(?<!\*)\*[^*\s][^*]*\*(?!\*))", text):
        if not chunk:
            continue
        if chunk.startswith("**") and chunk.endswith("**") and len(chunk) > 4:
            par.add_run(chunk[2:-2]).bold = True
        elif chunk.startswith("*") and chunk.endswith("*") and len(chunk) > 2:
            par.add_run(chunk[1:-1]).italic = True
        else:
            par.add_run(chunk)


def _table(d, rows: list[str]) -> None:
    rows = [r for r in rows if not re.match(r"^\s*\|?\s*:?-{2,}", r)]
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if not cells:
        return
    w = max(len(r) for r in cells)
    t = d.add_table(rows=len(cells), cols=w)
    t.style = "Table Grid"
    for i, r in enumerate(cells):
        for j in range(w):
            c = t.cell(i, j)
            c.text = ""
            p = c.paragraphs[0]
            for k, part in enumerate((r[j] if j < len(r) else "").split("<br>")):
                if k:
                    p = c.add_paragraph()
                _runs(p, part.strip())
            for p in c.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(9)
                    if i == 0:
                        run.bold = True
            if i == 0:
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear")
                shd.set(qn("w:fill"), "D9E2F3")
                c._tc.get_or_add_tcPr().append(shd)
    d.add_paragraph()


def _body(d, md: str) -> None:
    buf: list[str] = []
    for raw in md.splitlines() + [""]:
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            buf.append(line)
            continue
        if buf:
            _table(d, buf)
            buf = []
        s = line.strip()
        if not s or re.fullmatch(r"([-*_])\1{2,}", s):
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            lvl = min(max(len(m.group(1)), 2), 4)
            d.add_heading(m.group(2).strip().strip("*").strip(), level=lvl)
            continue
        m = re.fullmatch(r"\*\*(\d+[.)]\s*.+?)\*\*:?", s)
        if m:  # «**1. Назва обставини**» → заголовок обставини
            d.add_heading(m.group(1).strip(), level=4)
            continue
        m = re.match(r"^(\s*)[-*•]\s+(.*)", line)
        if m:
            p = d.add_paragraph(style="List Bullet 2" if len(m.group(1)) >= 2 else "List Bullet")
            _runs(p, m.group(2))
            continue
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if m:
            p = d.add_paragraph(style="List Number")
            _runs(p, m.group(3))
            continue
        p = d.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        _runs(p, s)


def render(results: list[dict], titles: dict, path: Path, pib: str, subtitle: str | None = None,
           template: str | None = None) -> Path:
    d = docx.Document(template) if template else docx.Document()
    if template:
        body = d.element.body
        for el in list(body):
            if not el.tag.endswith("sectPr"):
                body.remove(el)
    else:
        sec = d.sections[0]
        sec.left_margin = sec.right_margin = Cm(2)
        sec.top_margin = sec.bottom_margin = Cm(1.8)
    _set_base_font(d)

    t = d.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = t.add_run(pib)
    r.bold, r.font.size = True, Pt(15)
    s = d.add_paragraph()
    s.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = s.add_run(subtitle or f"Аналіз матеріалів у NotebookLM · {dt.date.today():%d.%m.%Y}")
    r.font.size, r.font.color.rgb = Pt(9.5), RGBColor(0x59, 0x59, 0x59)

    rn = Renumberer(titles)
    for res in results:
        if res.get("kind") not in ("ask", "continue", "report"):
            continue
        _body(d, rn.apply(clean_answer(res.get("answer", "")), res.get("references")))

    src = rn.listing()
    if src:
        d.add_heading("Перелік джерел", level=3)
        for line in src:
            p = d.add_paragraph()
            r = p.add_run(line)
            r.font.size = Pt(9)
            p.paragraph_format.space_after = Pt(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    return path
