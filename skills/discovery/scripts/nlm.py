"""nlm — оркестратор досліджень у NotebookLM поверх notebooklm-py.

Агент лише запускає команду; усю роботу робить скрипт, а в stdout потрапляє
тільки короткий підсумок. Повний журнал і сирі відповіді лежать у runs/<час>_<назва>/.

Команди:
  nlm types                                 типи досліджень (вбудовані + власні)
  nlm start ТИП [--materials …] [--out …] [--var К=З]   запустити дослідження за типом
  nlm types add НАЗВА [--from ТИП]          створити власний тип (копія шаблону для редагування)
  nlm run JOB.yaml [--dry-run]              виконати файл завдання
  nlm quick --notebook NAME [опції]         те саме з прапорців, без YAML
  nlm prompts [add|import|show]             бібліотека промптів
  nlm login | check | doctor | drive-auth   вхід і перевірка встановлення

Дані користувача (налаштування, власні промпти й типи, журнали) лежать у NLM_HOME
(за замовчуванням ~/.nlm-pipeline), тож оновлення пакета їх не зачіпає.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent          # skills/discovery: код, вбудовані промпти й типи
HOME = Path(os.environ.get("NLM_HOME") or Path.home() / ".nlm-pipeline").expanduser()
STATE = HOME / "state"
RUNS = HOME / "runs"
CONFIG = HOME / "config.yaml"
VENV = HOME / ".venv"
NLM_EXE = VENV / ("Scripts/notebooklm.exe" if os.name == "nt" else "bin/notebooklm")
BUILTIN_PROMPTS = ROOT / "prompts"
BUILTIN_TYPES = ROOT / "types"

DEFAULT_EXT = ["pdf", "docx", "pptx", "odt", "epub", "rtf", "csv", "txt", "md",
               "mp3", "m4a", "wav", "png", "jpg", "jpeg"]
PERSONA_LIMIT = 10_000
ADD_PAUSE = 3.2  # ~20 джерел/хв — ліміт NotebookLM

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


class NlmError(RuntimeError):
    pass


# ---------------------------------------------------------------- utils
def load_config() -> dict:
    if CONFIG.exists():
        return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    return {}


def user_dir(key: str) -> Path:
    v = load_config().get(f"{key}_dir")
    return Path(v).expanduser() if v else HOME / key


def prompt_dirs() -> list[Path]:
    return [user_dir("prompts"), BUILTIN_PROMPTS]


def type_dirs() -> list[Path]:
    return [user_dir("types"), BUILTIN_TYPES]


FM_RX = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def split_fm(text: str) -> tuple[dict, str]:
    """Заголовок YAML між рядками --- (необов'язковий) → (meta, текст)."""
    m = FM_RX.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, text[m.end():]


def find_named(name: str, dirs: list[Path], ext: str) -> Path | None:
    for d in dirs:
        f = d / (name if name.endswith(ext) else name + ext)
        if f.is_file():
            return f
    return None


def slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\-]+", "_", s, flags=re.U).strip("_")
    return s[:n] or "job"


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fill_vars(text: str, vars_: dict) -> str:
    """Підставляє {{КЛЮЧ}} і [КЛЮЧ] лише для переданих ключів."""
    for k, v in (vars_ or {}).items():
        text = text.replace("{{" + k + "}}", str(v)).replace("[" + k + "]", str(v))
    return text


def resolve_prompt(ref: str | None, vars_: dict | None = None) -> str | None:
    """ref — назва з prompts/, шлях до файлу або сам текст."""
    if not ref:
        return None
    ref = str(ref)
    cand = find_named(ref, prompt_dirs(), ".md") if "\n" not in ref and len(ref) < 120 else None
    if cand:
        text = split_fm(cand.read_text(encoding="utf-8"))[1]
    elif "\n" not in ref and len(ref) < 260 and Path(ref).expanduser().is_file():
        text = Path(ref).expanduser().read_text(encoding="utf-8")
    else:
        text = ref
    return fill_vars(text, vars_ or {}).strip()


# ---------------------------------------------------------------- runner
class Runner:
    def __init__(self, profile: str | None, log_path: Path, dry: bool = False):
        self.profile = profile
        self.log_path = log_path
        self.dry = dry
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", NO_COLOR="1")

    def log(self, msg: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%H:%M:%S}] {msg}\n")

    def nlm(self, *args: str, json_out: bool = True, timeout: int = 900, mutate: bool = True):
        cmd = [str(NLM_EXE)]
        if self.profile:
            cmd += ["-p", self.profile]
        cmd += [a for a in args if a is not None]
        if json_out:
            cmd.append("--json")
        shown = " ".join(a if len(a) < 120 else a[:117] + "..." for a in cmd[1:])
        self.log("$ notebooklm " + shown)
        if self.dry and mutate:
            return {}
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=self.env, timeout=timeout)
        if r.stderr.strip():
            self.log("stderr: " + r.stderr.strip()[-2000:])
        if r.returncode != 0:
            raise NlmError(f"notebooklm {args[0]} {args[1] if len(args) > 1 else ''} → код {r.returncode}: "
                           f"{(r.stdout or r.stderr).strip()[-400:]}")
        if not json_out:
            return r.stdout
        out = r.stdout.strip()
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            m = re.search(r"(\{.*\}|\[.*\])\s*$", out, re.S)
            if m:
                return json.loads(m.group(1))
            raise NlmError(f"Не JSON від notebooklm {args[0]}: {out[-300:]}")


# ---------------------------------------------------------------- steps
def ensure_notebook(rn: Runner, spec: str, create: bool) -> tuple[str, str]:
    try:
        data = rn.nlm("list", mutate=False)
    except NlmError:
        if not rn.dry:
            raise
        data = {}
    nbs = data.get("notebooks", []) if isinstance(data, dict) else data
    for nb in nbs:
        if nb.get("id") == spec or nb.get("id", "").startswith(spec) and len(spec) >= 8:
            return nb["id"], nb.get("title", spec)
    for nb in nbs:
        if (nb.get("title") or "").strip().casefold() == spec.strip().casefold():
            return nb["id"], nb["title"]
    if not create:
        raise NlmError(f"Блокнот «{spec}» не знайдено (create: false)")
    if rn.dry:
        rn.log(f"[dry] створив би блокнот «{spec}»")
        return "DRY-RUN", spec
    res = rn.nlm("create", spec)
    nb = res.get("notebook", res)
    rn.log(f"створено блокнот {nb['id']}")
    return nb["id"], spec


def collect_files(src: dict) -> list[Path]:
    exts = {e.lower().lstrip(".") for e in (src.get("ext") or DEFAULT_EXT)}
    excl = [re.compile(x, re.I) for x in src.get("exclude", [])]
    if "file" in src:
        files = [Path(src["file"]).expanduser()]
    else:
        root = Path(src["folder"]).expanduser()
        if not root.is_dir():
            raise NlmError(f"Теки немає: {root}")
        it = root.rglob("*") if src.get("recursive", True) else root.glob("*")
        files = sorted(p for p in it if p.is_file())
    out = []
    for p in files:
        if p.name.startswith(("~$", ".")) or p.suffix.lower().lstrip(".") not in exts:
            continue
        if any(rx.search(str(p)) for rx in excl):
            continue
        if p.stat().st_size > 200 * 1024 * 1024:
            continue
        out.append(p)
    return out


def add_sources(rn: Runner, nb_id: str, sources: list[dict], stats: dict) -> None:
    state_file = STATE / f"{nb_id}.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    existing = {}
    if not rn.dry:
        data = rn.nlm("source", "list", "-n", nb_id, mutate=False)
        existing = {(s.get("title") or "").casefold(): s["id"] for s in data.get("sources", [])}
    live_ids = set(existing.values())
    pending: list[str] = []

    def save_state():
        if not rn.dry:
            STATE.mkdir(exist_ok=True)
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    def known(key: str, title: str) -> bool:
        """Вже є в блокноті — тоді не конвертувати й не завантажувати."""
        prev = state.get(key)
        return bool(prev and (rn.dry or prev in live_ids)) or title.casefold() in existing

    def drop(*paths):
        """Тимчасові копії (конвертовані / завантажені з Drive) видаляються одразу після вивантаження."""
        for x in paths:
            try:
                if x and Path(x).is_relative_to(STATE):
                    Path(x).unlink(missing_ok=True)
                    if Path(x).parent != STATE and not any(Path(x).parent.iterdir()):
                        Path(x).parent.rmdir()
            except OSError:
                pass

    def add(key: str, title: str, *args: str):
        prev = state.get(key)
        if prev and (rn.dry or prev in live_ids):
            stats["skipped"] += 1
            return
        if title.casefold() in existing:
            state[key] = existing[title.casefold()]
            stats["skipped"] += 1
            return
        try:
            res = rn.nlm(*args, "-n", nb_id, timeout=600)
            sid = (res.get("source") or {}).get("id")
            if sid:
                state[key] = sid
                pending.append(sid)
            stats["added"] += 1
            save_state()
        except NlmError as e:
            stats["failed"].append(f"{title}: {e}")
            rn.log(f"ПОМИЛКА {title}: {e}")
        if not rn.dry:
            time.sleep(ADD_PAUSE)

    for src in sources:
        if "folder" in src or "file" in src:
            import convert
            excl = [re.compile(x, re.I) for x in src.get("exclude", [])]
            only = {e.lower().lstrip(".") for e in src["ext"]} if src.get("ext") else None
            if "file" in src:
                fp = Path(src["file"]).expanduser()
                native, conv, skipped = convert.scan(fp.parent, False, only, excl)
                native, conv = [x for x in native if x == fp], [x for x in conv if x == fp]
            else:
                root = Path(src["folder"]).expanduser()
                if not root.is_dir():
                    raise NlmError(f"Теки немає: {root}")
                native, conv, skipped = convert.scan(root, src.get("recursive", True), only, excl)
            stats["formats_skipped"].update(skipped)
            if "folder" in src and not rn.dry:
                try:
                    convert.ensure_local(root, native + conv, log=lambda m: (print(m, flush=True), rn.log(m)))
                except RuntimeError as e:
                    raise NlmError(str(e))
            names = Counter(x.name.casefold() for x in native + conv)

            def title_of(x: Path) -> str:  # однакові імена в різних підтеках → «підтека - ім'я»
                return f"{x.parent.name} - {x.name}" if names[x.name.casefold()] > 1 else x.name

            for x in native:
                add("sha256:" + file_hash(x), title_of(x), "source", "add", str(x), "--type", "file",
                    "--title", title_of(x), "--request-timeout", "300")
            for x in conv:
                key = "sha256:" + file_hash(x)
                if known(key, title_of(x)):
                    add(key, title_of(x))  # лише облік «пропущено»
                    continue
                try:
                    md, _ = convert.to_markdown(x, STATE / "converted")
                except Exception as e:
                    stats["failed"].append(f"конвертація {x.name}: {e}")
                    continue
                stats["converted"] += 1
                add(key, title_of(x), "source", "add", str(md), "--type", "file",
                    "--title", title_of(x), "--mime-type", "text/markdown", "--request-timeout", "300")
                drop(md)
        elif "drive_folder" in src:
            import convert, gdrive
            try:
                svc = gdrive.service()
            except RuntimeError as e:
                raise NlmError(str(e)) from e
            files = list(gdrive.walk(svc, gdrive.folder_id(src["drive_folder"])))
            names = Counter(f["name"].casefold() for f in files)
            cache = STATE / "drive_cache"
            for f in files:
                title = (f"{f['path'].rsplit('/', 2)[-2]} - {f['name']}"
                         if names[f["name"].casefold()] > 1 and "/" in f["path"] else f["name"])
                ext = Path(f["name"]).suffix.lower().lstrip(".")
                kind = gdrive.BY_REF.get(f["mimeType"])
                if kind:  # NotebookLM бере файл з Drive сам
                    add("drive:" + f["id"], title, "source", "add-drive", f["id"], title, "--mime-type", kind)
                    continue
                if f["mimeType"].startswith("application/vnd.google-apps") or (
                        ext not in convert.NATIVE and ext not in convert.CONVERT):
                    stats["formats_skipped"][ext or f["mimeType"].rsplit(".", 1)[-1]] += 1
                    continue
                if known("drive:" + f["id"], title):
                    add("drive:" + f["id"], title)
                    continue
                if rn.dry:
                    stats["added"] += 1
                    continue
                orig = None
                try:
                    local = orig = gdrive.download(svc, f, cache)
                    if ext in convert.CONVERT:
                        local, _ = convert.to_markdown(local, STATE / "converted")
                        stats["converted"] += 1
                except Exception as e:
                    stats["failed"].append(f"завантаження {f['path']}: {e}")
                    continue
                add("drive:" + f["id"], title, "source", "add", str(local), "--type", "file", "--title", title,
                    *(["--mime-type", "text/markdown"] if local.suffix == ".md" else []), "--request-timeout", "300")
                drop(local, orig)
        elif "url" in src or "urls" in src or "urls_file" in src:
            urls = []
            if "url" in src:
                urls.append(src["url"])
            urls += src.get("urls", []) if isinstance(src.get("urls"), list) else []
            if "urls_file" in src:
                urls += [u.strip() for u in Path(src["urls_file"]).read_text(encoding="utf-8").splitlines()
                         if u.strip() and not u.startswith("#")]
            for u in urls:
                add("url:" + u, u, "source", "add", u, "--request-timeout", "120")
        elif "drive" in src:
            items = src["drive"] if isinstance(src["drive"], list) else [src["drive"]]
            for d in items:
                d = d if isinstance(d, dict) else {"id": d}
                title = d.get("title") or d["id"]
                add("drive:" + d["id"], title, "source", "add-drive", d["id"], title,
                    "--mime-type", d.get("type", "pdf"))
        elif "text" in src:
            t = src["text"]
            content = Path(t["file"]).read_text(encoding="utf-8") if "file" in t else t["content"]
            key = "text:" + hashlib.sha256(content.encode()).hexdigest()
            add(key, t.get("title", "Текст"), "source", "add", content, "--type", "text",
                "--title", t.get("title", "Текст"))
        else:
            stats["failed"].append(f"невідомий тип джерела: {src}")

    for sid in pending:
        try:
            rn.nlm("source", "wait", sid, "-n", nb_id, "--timeout", "900", "--interval", "3",
                   timeout=960, mutate=False)
        except NlmError as e:
            stats["failed"].append(f"обробка {sid}: {e}")
    save_state()


def configure_chat(rn: Runner, nb_id: str, chat: dict, stats: dict) -> None:
    if not chat:
        return
    if chat.get("mode"):
        rn.nlm("configure", "-n", nb_id, "--mode", chat["mode"])
        stats["chat"] = f"mode={chat['mode']}"
        return
    args = ["configure", "-n", nb_id]
    persona = resolve_prompt(chat.get("persona"), chat.get("vars"))
    if persona:
        if len(persona) > PERSONA_LIMIT:
            raise NlmError(f"persona {len(persona)} символів > {PERSONA_LIMIT}. "
                           "Скоротіть промпт або перенесіть частину в перше питання.")
        args += ["--persona", persona]
    args += ["--response-length", chat.get("response_length", "longer")]
    rn.nlm(*args)
    stats["chat"] = f"persona={chat.get('persona') or '-'} ({len(persona or '')} симв.), " \
                    f"довжина={chat.get('response_length', 'longer')}"


# Типові значення для `continue`; тип дослідження може задати власні (prompt / until / max).
CONTINUE_PROMPT = ("Продовжуй з того місця, де зупинився, відповідно до налаштувань чату. "
                   "Коли відповідь повністю завершена, останнім рядком напиши: «Аналіз завершено».")
DONE_RX = r"аналіз (повністю )?завершено"


def is_done(answer: str, rx: re.Pattern) -> bool:
    return bool(rx.search(answer or ""))


def ask(rn: Runner, nb_id: str, question: str, run_dir: Path, idx: int, title: str | None,
        new: bool = False) -> dict:
    qf = run_dir / f"q{idx:02d}.txt"
    qf.write_text(question, encoding="utf-8")
    args = ["ask", "-n", nb_id, "--prompt-file", str(qf), "--request-timeout", "600"]
    if new:
        args += ["--new", "-y"]
    res = rn.nlm(*args, timeout=900)
    return {"kind": "ask", "title": title or question.splitlines()[0][:120], "question": question,
            "answer": res.get("answer", ""), "references": res.get("references", []),
            "conversation_id": res.get("conversation_id")}


def run_tasks(rn: Runner, nb_id: str, tasks: list, run_dir: Path, base_vars: dict,
              stats: dict) -> list[dict]:
    results, idx = [], 0
    for t in tasks or []:
        if isinstance(t, str):
            t = {"ask": t}
        vars_ = {**base_vars, **(t.get("vars") or {})}
        try:
            if "ask" in t or "prompt" in t:
                q = resolve_prompt(t.get("ask") or t.get("prompt"), vars_)
                if t.get("target"):
                    q = f"Цільова особа: {fill_vars(t['target'], vars_)}.\n\n{q}"
                idx += 1
                if rn.dry:
                    rn.log(f"[dry] ask #{idx}: {q[:200]}")
                    continue
                first = ask(rn, nb_id, q, run_dir, idx, t.get("title"), t.get("new", False))
                results.append(first)
                cont = t.get("continue")
                if cont:
                    # Промпт видає результат частинами («по 1 блоку за відповідь») — просимо продовжувати,
                    # доки не з'явиться фінальна ознака (за замовчуванням — таблиця майна).
                    cont = cont if isinstance(cont, dict) else {}
                    done_rx = re.compile(cont.get("until", DONE_RX), re.I | re.S)
                    prev = first["answer"]
                    n = 0
                    while not is_done(prev, done_rx) and n < int(cont.get("max", 15)):
                        n += 1
                        idx += 1
                        nxt = ask(rn, nb_id, cont.get("prompt", CONTINUE_PROMPT), run_dir, idx, None)
                        if len(nxt["answer"].strip()) < 80 or nxt["answer"].strip() == prev.strip():
                            break
                        nxt["kind"] = "continue"
                        results.append(nxt)
                        prev = nxt["answer"]
                    stats["continues"] = stats.get("continues", 0) + n
                    if not is_done(prev, done_rx):
                        stats["failed"].append(f"аналіз не завершено після {n} продовжень (немає ознаки завершення)")
            elif "ask_file" in t:
                text = Path(t["ask_file"]).read_text(encoding="utf-8")
                sep = "\n---\n" if "\n---\n" in text else "\n"
                for q in [x.strip() for x in text.split(sep) if x.strip() and not x.startswith("#")]:
                    idx += 1
                    if not rn.dry:
                        results.append(ask(rn, nb_id, fill_vars(q, vars_), run_dir, idx, None))
            elif "research" in t:
                r = t["research"] if isinstance(t["research"], dict) else {"query": t["research"]}
                args = ["source", "add-research", fill_vars(r["query"], vars_), "-n", nb_id,
                        "--mode", r.get("mode", "deep"), "--from", r.get("from", "web"),
                        "--timeout", str(r.get("timeout", 2400))]
                imp = r.get("import", "cited")
                if imp in ("all", "cited"):
                    args.append("--import-all")
                if imp == "cited":
                    args.append("--cited-only")
                res = rn.nlm(*args, timeout=int(r.get("timeout", 2400)) * 2 + 60)
                n = len(res.get("sources", []) or res.get("imported", []) or [])
                stats["research"].append(f"«{r['query'][:60]}»: {n} джерел")
                results.append({"kind": "research", "title": f"Deep Research: {r['query']}",
                                "raw": res})
            elif "explore" in t:
                # Автономне дослідження: NotebookLM сам пропонує наступні питання.
                e = t["explore"] if isinstance(t["explore"], dict) else {"steps": int(t["explore"])}
                if rn.dry:
                    rn.log(f"[dry] explore: {e.get('steps', 5)} кроків")
                    continue
                if e.get("start"):
                    idx += 1
                    results.append(ask(rn, nb_id, resolve_prompt(e["start"], vars_), run_dir, idx, None))
                asked = set()
                for _ in range(int(e.get("steps", 5))):
                    sug = rn.nlm("suggest-next-steps", "-n", nb_id, mutate=False).get("suggestions", [])
                    if not sug:
                        sug = [{"question": s["prompt"]} for s in
                               rn.nlm("suggest-prompts", "-n", nb_id, mutate=False).get("suggestions", [])]
                    q = next((s["question"] for s in sug if s.get("question") not in asked), None)
                    if not q:
                        break
                    asked.add(q)
                    idx += 1
                    results.append(ask(rn, nb_id, q, run_dir, idx, None))
            elif "report" in t:
                r = t["report"] if isinstance(t["report"], dict) else {"prompt": t["report"]}
                fmt = r.get("format", "custom")
                args = ["generate", "report", "-n", nb_id, "--format", fmt, "--wait",
                        "--timeout", str(r.get("timeout", 900)), "--retry", "2",
                        "--language", r.get("language", "uk")]
                if r.get("prompt"):
                    pf = run_dir / f"report{idx:02d}.txt"
                    pf.write_text(resolve_prompt(r["prompt"], vars_), encoding="utf-8")
                    args += ["--prompt-file", str(pf)] if fmt == "custom" else ["--append", pf.read_text("utf-8")]
                res = rn.nlm(*args, timeout=int(r.get("timeout", 900)) + 120)
                md = run_dir / f"report_{res.get('task_id', idx)}.md"
                sel = ["-a", res["task_id"]] if res.get("task_id") else ["--latest"]
                rn.nlm("download", "report", str(md), "-n", nb_id, *sel, "--force")
                results.append({"kind": "report", "title": r.get("title", "Звіт NotebookLM"),
                                "answer": md.read_text(encoding="utf-8") if md.exists() else ""})
            elif "generate" in t:
                g = t["generate"]
                kind = g.pop("type")
                args = ["generate", kind, "-n", nb_id, "--wait"]
                for k, v in g.items():
                    if k != "download":
                        args += [f"--{k}", str(v)]
                res = rn.nlm(*args, timeout=3600)
                out = Path(g.get("download") or run_dir / f"{kind}_{res.get('task_id', '')}")
                sel = ["-a", res["task_id"]] if res.get("task_id") else ["--latest"]
                rn.nlm("download", kind, str(out), "-n", nb_id, *sel, "--force")
                results.append({"kind": "file", "title": kind, "path": str(out)})
            else:
                stats["failed"].append(f"невідоме завдання: {t}")
        except NlmError as e:
            stats["failed"].append(str(e))
            rn.log(f"ПОМИЛКА завдання: {e}")
    return results


# ---------------------------------------------------------------- output
def source_titles(rn: Runner, nb_id: str) -> dict:
    try:
        return {s["id"]: s.get("title") or s["id"]
                for s in rn.nlm("source", "list", "-n", nb_id, mutate=False).get("sources", [])}
    except NlmError:
        return {}


def compact_nums(nums: list[int]) -> str:
    """[1,2,3,5,7,8] -> '1–3, 5, 7–8'"""
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}–{nums[j]}")
        i = j + 1
    return ", ".join(out)


def to_markdown(job_title: str, results: list[dict], titles: dict, cfg_out: dict) -> str:
    cites = cfg_out.get("citations", "list")  # list | quotes | none
    parts = [f"# {job_title}", "", f"_Сформовано {dt.datetime.now():%d.%m.%Y %H:%M} з NotebookLM_", ""]
    for r in results:
        if r["kind"] == "research":
            continue
        if r["kind"] == "file":
            parts += [f"## {r['title']}", "", f"Файл: {r['path']}", ""]
            continue
        show = cfg_out.get("show_questions", len(results) > 1)
        if show:
            parts += [f"## {r['title']}", ""]
        body = r["answer"].strip()
        if show:  # заголовки всередині відповіді — на рівень нижче за заголовок розділу
            body = re.sub(r"(?m)^(#{1,5})(?=\s)", r"#\1", body)
        parts += [body, ""]
        refs = r.get("references") or []
        if refs and cites != "none":
            parts += ["**Джерела:**", ""]
            if cites == "list":  # один рядок на джерело: [1, 3–5] Назва
                by_src: dict = {}
                for ref in refs:
                    by_src.setdefault(ref.get("source_id"), set()).add(ref.get("citation_number") or 0)
                for sid, nums in sorted(by_src.items(), key=lambda kv: min(kv[1])):
                    parts.append(f"[{compact_nums(sorted(nums))}] {titles.get(sid, sid)}")
                parts.append("")
                continue
            seen = set()
            for ref in sorted(refs, key=lambda x: x.get("citation_number") or 0):
                n = ref.get("citation_number")
                if (n, ref.get("source_id")) in seen:
                    continue
                seen.add((n, ref.get("source_id")))
                line = f"[{n}] {titles.get(ref.get('source_id'), ref.get('source_id'))}"
                if cites == "quotes" and ref.get("cited_text"):
                    q = " ".join(ref["cited_text"].split())
                    line += f" — «{q[:300]}{'…' if len(q) > 300 else ''}»"
                parts.append(line)
            parts.append("")
    return "\n".join(parts).strip() + "\n"


def md_to_docx(md: str, path: Path, template: str | None) -> None:
    import docx
    from docx.shared import Pt

    d = docx.Document(template) if template else docx.Document()
    if template:  # прибрати вміст шаблону, лишити стилі/колонтитули
        body = d.element.body
        for el in list(body):
            if not el.tag.endswith("sectPr"):
                body.remove(el)

    def add_runs(par, text):
        for i, chunk in enumerate(re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_)", text)):
            if not chunk:
                continue
            if chunk.startswith("**") and chunk.endswith("**"):
                par.add_run(chunk[2:-2]).bold = True
            elif (chunk[0] in "*_") and chunk[-1] == chunk[0] and len(chunk) > 2:
                par.add_run(chunk[1:-1]).italic = True
            else:
                par.add_run(chunk)

    def style(name, fallback="Normal"):
        try:
            d.styles[name]
            return name
        except KeyError:
            return fallback

    table_buf: list[str] = []

    def flush_table():
        rows = [r for r in table_buf if not re.match(r"^\s*\|?\s*:?-{2,}", r)]
        table_buf.clear()
        if not rows:
            return
        cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
        w = max(len(r) for r in cells)
        t = d.add_table(rows=len(cells), cols=w)
        if style("Table Grid", None):
            t.style = "Table Grid"
        for i, r in enumerate(cells):
            for j in range(w):
                cell = t.cell(i, j)
                cell.text = ""
                add_runs(cell.paragraphs[0], r[j] if j < len(r) else "")
                if i == 0:
                    for run in cell.paragraphs[0].runs:
                        run.bold = True

    for line in md.splitlines():
        if line.strip().startswith("|"):
            table_buf.append(line)
            continue
        if table_buf:
            flush_table()
        s = line.rstrip()
        if not s.strip() or re.fullmatch(r"\s*([-*_])\1{2,}\s*", s):
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            lvl = len(m.group(1))
            name = "Title" if lvl == 1 else f"Heading {min(lvl - 1, 4)}"
            p = d.add_paragraph(style=style(name, style("Heading 1")))
            add_runs(p, m.group(2).strip())
            continue
        m = re.match(r"^(\s*)([-*•])\s+(.*)", s)
        if m:
            p = d.add_paragraph(style=style("List Bullet"))
            add_runs(p, m.group(3))
            continue
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", s)
        if m:
            p = d.add_paragraph(style=style("List Number"))
            add_runs(p, m.group(3))
            continue
        add_runs(d.add_paragraph(), s)
    if table_buf:
        flush_table()
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))


def write_output(md: str, results: list[dict], out: dict, run_dir: Path, job_title: str,
                 titles: dict | None = None) -> Path:
    path = Path(out.get("path") or run_dir / f"{slug(job_title)}.{out.get('format', 'md')}").expanduser()
    if path.is_dir() or out.get("filename") or path.suffix.lower() not in (".md", ".json", ".docx"):
        name = out.get("filename") or f"{slug(job_title)}_{dt.datetime.now():%Y%m%d_%H%M}"
        path = path / f"{name}.{out.get('format', 'docx')}"
    fmt = path.suffix.lower().lstrip(".")
    (run_dir / "result.md").write_text(md, encoding="utf-8")
    if fmt == "md":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
    elif fmt == "json":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    elif fmt == "docx" and out.get("style") == "dossier":
        import dossier
        dossier.render(results, titles or {}, path, out.get("title") or job_title, template=out.get("template"))
    elif fmt == "docx":
        md_to_docx(md, path, out.get("template"))
    else:
        raise NlmError(f"Невідомий формат виводу: {fmt}")
    return path


# ---------------------------------------------------------------- main flow
def deep_fill(obj, vars_: dict):
    if isinstance(obj, str):
        return fill_vars(obj, vars_)
    if isinstance(obj, list):
        return [deep_fill(x, vars_) for x in obj]
    if isinstance(obj, dict):
        return {k: deep_fill(v, vars_) for k, v in obj.items()}
    return obj


def run_job(job: dict, dry: bool = False) -> int:
    cfg = load_config()
    all_vars = {**(job.get("vars") or {}), **((job.get("chat") or {}).get("vars") or {})}
    job = deep_fill(job, all_vars)
    missing = sorted(set(re.findall(r"\{\{([^}]+)\}\}", json.dumps(job, ensure_ascii=False))))
    if missing:
        print(f"✗ Не задано змінні: {', '.join(missing)} (додайте --var КЛЮЧ=значення)")
        return 2
    title = job.get("title") or job.get("notebook") or "Дослідження"
    run_dir = RUNS / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{slug(title)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "job.yaml").write_text(yaml.safe_dump(job, allow_unicode=True, sort_keys=False), encoding="utf-8")
    rn = Runner(job.get("profile") or cfg.get("profile"), run_dir / "log.txt", dry)
    stats = {"added": 0, "skipped": 0, "failed": [], "research": [], "chat": None,
             "converted": 0, "formats_skipped": Counter()}
    t0 = time.time()
    try:
        if not dry:
            rn.nlm("auth", "check", mutate=False)
    except NlmError:
        print("✗ Немає входу в NotebookLM. Людина має виконати: nlm login")
        return 2
    try:
        nb_id, nb_title = ensure_notebook(rn, str(job["notebook"]), job.get("create", True))
        if job.get("sources"):
            add_sources(rn, nb_id, job["sources"], stats)
        configure_chat(rn, nb_id, job.get("chat") or {}, stats)
        vars_ = (job.get("chat") or {}).get("vars") or job.get("vars") or {}
        results = run_tasks(rn, nb_id, job.get("tasks") or [], run_dir, vars_, stats)
        (run_dir / "answers.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        out_path = None
        if results and not dry:
            titles = source_titles(rn, nb_id)
            md = to_markdown(job.get("output", {}).get("title") or title, results, titles, job.get("output") or {})
            out_path = write_output(md, results, job.get("output") or {}, run_dir, title, titles)
    except NlmError as e:
        if re.search(r"AUTH_|Authentication|Auth not found", str(e)):
            print("✗ Немає входу в NotebookLM (сесію прострочено). Людина має виконати: nlm login")
            return 2
        print(f"✗ {e}\n  журнал: {run_dir / 'log.txt'}")
        return 1

    mins = (time.time() - t0) / 60
    asks = sum(1 for r in results if r["kind"] == "ask")
    print(f"{'[DRY] ' if dry else ''}✓ «{nb_title}» ({nb_id[:8]}) за {mins:.1f} хв")
    print(f"  джерела: +{stats['added']}, пропущено {stats['skipped']}"
          + (f", помилок {len(stats['failed'])}" if stats["failed"] else ""))
    if stats["converted"]:
        print(f"  сконвертовано в Markdown (xlsx/html): {stats['converted']}")
    if stats["formats_skipped"]:
        print("  пропущено непідтримувані: " + ", ".join(f"{k} ×{v}" for k, v in stats["formats_skipped"].most_common()))
    if stats["chat"]:
        print(f"  чат: {stats['chat']}")
    for r in stats["research"]:
        print(f"  research: {r}")
    if asks:
        print(f"  відповідей: {asks}" + (f" (+{stats['continues']} продовжень)" if stats.get("continues") else ""))
    if out_path:
        print(f"  результат: {out_path}")
    for f in stats["failed"][:5]:
        print(f"  ! {f[:200]}")
    print(f"  журнал: {run_dir}")
    return 0 if not stats["failed"] else 3


def job_from_flags(a) -> dict:
    job: dict = {"notebook": a.notebook, "create": not a.no_create}
    if a.profile:
        job["profile"] = a.profile
    src = []
    for f in a.folder or []:
        src.append({"folder": f})
    for f in a.file or []:
        src.append({"file": f})
    for u in a.url or []:
        src.append({"url": u})
    for d in a.drive or []:
        src.append({"drive": {"id": d}})
    if src:
        job["sources"] = src
    vars_ = dict(v.split("=", 1) for v in a.var or [])
    if a.persona or a.length:
        job["chat"] = {"persona": a.persona, "response_length": a.length or "longer", "vars": vars_}
    tasks = []
    for r in a.research or []:
        tasks.append({"research": {"query": r, "mode": "deep"}})
    for q in a.ask or []:
        tasks.append({"ask": q})
    if a.ask_file:
        tasks.append({"ask_file": a.ask_file})
    if a.explore:
        tasks.append({"explore": {"steps": a.explore}})
    if a.report:
        tasks.append({"report": {"prompt": a.report}})
    job["tasks"] = tasks
    job["vars"] = vars_
    if a.out:
        job["output"] = {"path": a.out, "template": a.template, "citations": a.citations}
    return job


TRANSLIT = dict(zip("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя",
                    ["a", "b", "v", "h", "g", "d", "e", "ie", "zh", "z", "y", "i", "i", "i", "k", "l", "m", "n", "o",
                     "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "iu", "ia"]))


def name_slug(s: str, n: int = 40) -> str:
    s = "".join(TRANSLIT.get(c, c) for c in s.lower())
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:n] or "prompt"


def materials_to_sources(items: list[str]) -> list[dict]:
    out = []
    for m in items or []:
        if "drive.google.com/drive/folders" in m or re.fullmatch(r"[\w-]{25,}", m):
            out.append({"drive_folder": m})
        elif re.match(r"https?://", m):
            out.append({"url": m})
        elif Path(m).expanduser().is_file():
            out.append({"file": m})
        else:
            out.append({"folder": m})
    return out


# ---------------------------------------------------------------- types
def load_type(name: str) -> tuple[dict, Path]:
    f = find_named(name, type_dirs(), ".yaml")
    if not f:
        raise SystemExit(f"✗ Тип «{name}» не знайдено. Список: nlm types")
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}, f


def cmd_types(a) -> int:
    if a.action == "add":
        if not a.name:
            raise SystemExit("✗ nlm types add НАЗВА [--from ТИП]")
        src = load_type(a.src)[1] if a.src else BUILTIN_TYPES / "_template.yaml"
        dst = user_dir("types") / f"{name_slug(a.name)}.yaml"
        if dst.exists():
            raise SystemExit(f"✗ Уже є: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"✓ Створено {dst}\n  Відредагуйте description, params, chat і tasks; запуск: nlm start {dst.stem}")
        return 0
    seen = set()
    for d in type_dirs():
        for f in sorted(d.glob("*.yaml")):
            if f.stem.startswith("_") or f.stem in seen:
                continue
            seen.add(f.stem)
            t = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            params = " ".join(f"{k}{'*' if (v or {}).get('required') else ''}" for k, v in (t.get("params") or {}).items())
            own = " [свій]" if d != BUILTIN_TYPES else ""
            print(f"- {f.stem}{own} — {t.get('description', '')}" + (f"  (параметри: {params})" if params else ""))
    print("\nЗапуск: nlm start ТИП --materials ТЕКА_або_URL --out ТЕКА [--var КЛЮЧ=значення]")
    return 0


def cmd_start(a) -> int:
    t, f = load_type(a.type)
    params = t.pop("params", None) or {}
    t.pop("description", None)
    vars_ = {k: (v or {}).get("default") for k, v in params.items() if (v or {}).get("default") is not None}
    vars_.update(dict(v.split("=", 1) for v in a.var or []))
    missing = [k for k, v in params.items() if (v or {}).get("required") and not vars_.get(k)]
    if missing:
        print(f"✗ Тип «{a.type}» потребує: " + ", ".join(f"--var \"{k}=…\" ({params[k].get('help', '')})" for k in missing))
        return 2
    job = t
    job.setdefault("vars", {}).update(vars_)
    if job.get("chat"):
        job["chat"].setdefault("vars", {}).update(vars_)
    src = list(job.get("sources") or []) + materials_to_sources(a.materials)
    if src:
        job["sources"] = src
    if a.notebook:
        job["notebook"] = a.notebook
    job.setdefault("notebook", job.get("title") or a.type)
    out = job.setdefault("output", {})
    out["path"] = a.out or out.get("path") or load_config().get("output_dir") or str(Path.cwd())
    if a.max_continue:
        for task in job.get("tasks") or []:
            if isinstance(task, dict) and isinstance(task.get("continue"), dict):
                task["continue"]["max"] = a.max_continue
    return run_job(job, a.dry_run)


# ---------------------------------------------------------------- prompts
def cmd_prompts(a) -> int:
    if a.action == "add":
        if not (a.name and a.file):
            raise SystemExit("✗ nlm prompts add НАЗВА --file текст.md [--use persona|ask|report] [--description …]")
        meta, body = split_fm(Path(a.file).expanduser().read_text(encoding="utf-8"))
        return save_prompt(name_slug(a.name), body, a.use or meta.get("use", "ask"),
                           a.description or meta.get("description", a.name), a.group or meta.get("group", "own"))
    if a.action == "import":
        if not a.file:
            raise SystemExit("✗ nlm prompts import документ.docx [--level 1|2] [--group назва]")
        return import_docx(Path(a.file).expanduser(), a.level, a.group or "own", a.use)
    if a.action == "show":
        f = find_named(a.name or "", prompt_dirs(), ".md")
        if not f:
            raise SystemExit("✗ Промпт не знайдено")
        meta, body = split_fm(f.read_text(encoding="utf-8"))
        print(f"{f}\n{meta}\n{len(body)} символів\n---\n{body[:1500]}{'…' if len(body) > 1500 else ''}")
        return 0
    seen, rows = set(), []
    for d in prompt_dirs():
        for f in sorted(d.glob("*.md")):
            if f.stem in seen or f.stem.upper() == "README":
                continue
            seen.add(f.stem)
            meta, body = split_fm(f.read_text(encoding="utf-8"))
            rows.append((meta.get("group", "own" if d != BUILTIN_PROMPTS else "-"), f.stem, meta.get("use", "ask"),
                         len(body), meta.get("description", ""), d != BUILTIN_PROMPTS))
    group = None
    for g, n, use, chars, desc, own in sorted(rows):
        if g != group:
            print(f"\n[{g}]")
            group = g
        warn = " ⚠>10k, лише як питання" if use == "persona" and chars > PERSONA_LIMIT else ""
        print(f"- {n}{' [свій]' if own else ''} ({use}, {chars} симв.){warn} — {desc}")
    print(f"\nВласні промпти: {user_dir('prompts')}  (nlm prompts add|import)")
    return 0


def save_prompt(name: str, body: str, use: str, desc: str, group: str) -> int:
    d = user_dir("prompts")
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.md"
    meta = {"name": name, "description": desc, "use": use, "group": group}
    f.write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + body.strip() + "\n",
                 encoding="utf-8")
    warn = f"  ⚠ {len(body)} символів > {PERSONA_LIMIT}: у «Налаштування чату» не влізе" if use == "persona" and len(body) > PERSONA_LIMIT else ""
    print(f"✓ {name} ({use}, {len(body)} симв.) → {f}{warn}")
    return 0


def import_docx(path: Path, level: int | None, group: str, use: str | None) -> int:
    """Ділить документ на промпти за заголовками (Heading 1/2 або «Заголовок 1/2»)."""
    import docx as _docx
    d = _docx.Document(str(path))

    def lvl(p):
        m = re.search(r"(?:Heading|Заголовок)\s*(\d)", p.style.name or "")
        return int(m.group(1)) if m and p.text.strip() else None
    levels = [x for x in (lvl(p) for p in d.paragraphs) if x]
    if not levels:
        raise SystemExit("✗ У документі немає заголовків (стилі «Заголовок 1/2»). Розмітьте розділи заголовками.")
    level = level or max(set(levels), key=levels.count)
    sections, cur, buf = [], None, []
    for p in d.paragraphs:
        lv = lvl(p)
        if lv and lv <= level:
            if cur and "".join(buf).strip():
                sections.append((cur, "\n".join(buf)))
            cur, buf = (p.text.strip() if lv == level else None), []
        elif cur:
            buf.append(p.text)
    if cur and "".join(buf).strip():
        sections.append((cur, "\n".join(buf)))
    for title, body in sections:
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        save_prompt(name_slug(title), body, use or ("persona" if len(body) <= PERSONA_LIMIT else "ask"), title, group)
    print(f"✓ Імпортовано {len(sections)} промптів (рівень заголовків {level})")
    return 0


# ---------------------------------------------------------------- setup / checks
def nlm_base(profile: str | None) -> list[str]:
    return [str(NLM_EXE)] + (["-p", profile] if profile else [])


def cmd_login(a) -> int:
    prof = a.profile or load_config().get("profile")
    if prof:
        subprocess.run(nlm_base(None) + ["profile", "create", prof], capture_output=True)
    args = nlm_base(prof) + ["login"] + (["--browser", a.browser] if a.browser else [])
    print("Відкриється браузер — увійдіть у свій Google-акаунт (для NotebookLM краще окремий).")
    return subprocess.run(args).returncode


def cmd_check(a) -> int:
    cfg = load_config()
    rn = Runner(cfg.get("profile"), RUNS / "check.log")
    RUNS.mkdir(parents=True, exist_ok=True)
    try:
        rn.nlm("auth", "check", "--test", mutate=False)
        print(f"✓ вхід активний (профіль: {cfg.get('profile') or 'default'})")
        return 0
    except NlmError:
        print(f"✗ немає входу в NotebookLM (профіль: {cfg.get('profile') or 'default'}). Людина має виконати: nlm login")
        return 2


def cmd_doctor(a) -> int:
    ok = True

    def line(good, text, hint=""):
        nonlocal ok
        ok &= bool(good) or good is None
        mark = "✓" if good else ("·" if good is None else "✗")
        print(f"{mark} {text}" + (f" — {hint}" if hint and not good else ""))
    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "потрібен 3.10+")
    for mod in ("docx", "yaml", "openpyxl", "xlrd", "bs4"):
        try:
            __import__(mod)
            line(True, f"модуль {mod}")
        except ImportError:
            line(False, f"модуль {mod}", "запустіть install.ps1 / install.sh")
    line(NLM_EXE.exists(), f"notebooklm-py ({NLM_EXE})", "запустіть install.ps1 / install.sh")
    line(HOME.exists(), f"дані користувача: {HOME}")
    up = len(list(user_dir("prompts").glob("*.md"))) if user_dir("prompts").exists() else 0
    ut = len(list(user_dir("types").glob("*.yaml"))) if user_dir("types").exists() else 0
    line(True, f"промпти: {len(list(BUILTIN_PROMPTS.glob('*.md')))} вбудованих + {up} власних; "
               f"типи: {len([f for f in BUILTIN_TYPES.glob('*.yaml') if not f.stem.startswith('_')])} + {ut}")
    if NLM_EXE.exists():
        line(cmd_check(a) == 0, "вхід у NotebookLM", "людина має виконати: nlm login")
    # необов'язкові
    od = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or os.environ.get("OneDriveCommercial")
    line(None, f"OneDrive: {'виявлено ' + od if od else 'не виявлено'} (необов'язково; хмарні файли довантажуються самі)")
    tok = HOME / "drive_token.json"
    line(None, f"Google Drive API: {'підключено' if tok.exists() else 'не підключено'} "
               "(необов'язково; потрібен лише для посилань на теки Drive — nlm drive-auth)")
    if a.smoke:
        smoke = ROOT / "tests" / "smoke"
        print("\nТест на вигаданих даних (створить блокнот «nlm smoke test»)…")
        ns = argparse.Namespace(type="oglyad", var=[], materials=[str(smoke)], notebook="nlm smoke test",
                                out=str(HOME / "smoke_result"), max_continue=None, dry_run=False)
        line(cmd_start(ns) == 0, "тестове дослідження")
    print("\nГотово." if ok else "\nЄ проблеми — див. ✗ вище.")
    return 0 if ok else 1


def cmd_drive_auth(a) -> int:
    import gdrive
    return gdrive.auth()


def main() -> int:
    ap = argparse.ArgumentParser(prog="nlm")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("job")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--var", action="append", help="КЛЮЧ=значення (перекриває vars у YAML)")
    q = sp.add_parser("quick")
    q.add_argument("--notebook", required=True)
    q.add_argument("--profile")
    q.add_argument("--no-create", action="store_true")
    q.add_argument("--folder", action="append")
    q.add_argument("--file", action="append")
    q.add_argument("--url", action="append")
    q.add_argument("--drive", action="append", help="ID файлу Google Drive")
    q.add_argument("--persona")
    q.add_argument("--length", choices=["default", "longer", "shorter"])
    q.add_argument("--var", action="append")
    q.add_argument("--ask", action="append")
    q.add_argument("--ask-file")
    q.add_argument("--research", action="append")
    q.add_argument("--explore", type=int)
    q.add_argument("--report")
    q.add_argument("--out")
    q.add_argument("--template")
    q.add_argument("--citations", default="list", choices=["list", "quotes", "none"])
    q.add_argument("--dry-run", action="store_true")
    s = sp.add_parser("start", help="дослідження за типом (nlm types)")
    s.add_argument("type")
    s.add_argument("--materials", action="append", help="тека, файл, URL або посилання на теку Drive (можна кілька)")
    s.add_argument("--out", help="тека для результату")
    s.add_argument("--notebook", help="назва або ID блокнота")
    s.add_argument("--var", action="append", help="КЛЮЧ=значення")
    s.add_argument("--max-continue", type=int)
    s.add_argument("--dry-run", action="store_true")
    k = sp.add_parser("kandydat", help="скорочення: nlm start kandydat")
    k.add_argument("--pib", required=True)
    k.add_argument("--materials", action="append", required=True)
    k.add_argument("--out-dir", required=True)
    k.add_argument("--notebook")
    k.add_argument("--max-continue", type=int)
    k.add_argument("--dry-run", action="store_true")
    t = sp.add_parser("types")
    t.add_argument("action", nargs="?", choices=["list", "add"], default="list")
    t.add_argument("name", nargs="?")
    t.add_argument("--from", dest="src")
    pr = sp.add_parser("prompts")
    pr.add_argument("action", nargs="?", choices=["list", "add", "import", "show"], default="list")
    pr.add_argument("name", nargs="?")
    pr.add_argument("--file")
    pr.add_argument("--use", choices=["persona", "ask", "report"])
    pr.add_argument("--description")
    pr.add_argument("--group")
    pr.add_argument("--level", type=int)
    lg = sp.add_parser("login")
    lg.add_argument("--profile")
    lg.add_argument("--browser", choices=["chrome", "msedge", "chromium"])
    sp.add_parser("check")
    dc = sp.add_parser("doctor")
    dc.add_argument("--smoke", action="store_true", help="ще й реальне тестове дослідження на вигаданих даних")
    sp.add_parser("drive-auth")
    a = ap.parse_args()

    if a.cmd == "prompts" and a.action == "import":
        a.file = a.file or a.name
    if a.cmd == "run":
        job = yaml.safe_load(Path(a.job).read_text(encoding="utf-8"))
        if a.var:
            extra = dict(v.split("=", 1) for v in a.var)
            job.setdefault("vars", {}).update(extra)
            if job.get("chat"):
                job["chat"].setdefault("vars", {}).update(extra)
        return run_job(job, a.dry_run)
    if a.cmd == "kandydat":
        a = argparse.Namespace(type="kandydat", var=[f"ПІБ={a.pib}"], materials=a.materials, out=a.out_dir,
                               notebook=a.notebook, max_continue=a.max_continue, dry_run=a.dry_run)
        return cmd_start(a)
    return {"quick": lambda x: run_job(job_from_flags(x), x.dry_run), "start": cmd_start, "types": cmd_types,
            "prompts": cmd_prompts, "login": cmd_login, "check": cmd_check, "doctor": cmd_doctor,
            "drive-auth": cmd_drive_auth}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
