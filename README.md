# discovery — NotebookLM pipeline

**Дослідження в Google NotebookLM однією командою.** Агент (Claude Code або інший) лише формує команду, а скрипт виконує роботу сам:
1. завантажує теку, файли, URL чи Google Drive у блокнот;
2. задає промпт у «Налаштування чату»;
3. ставить питання й просить продовжувати, доки відповідь не буде повною;
4. запускає Deep Research;
5. зберігає результат у DOCX з посиланнями на джерела.

Агент отримує лише підсумок у кілька рядків, тож токенів витрачається мінімум.

> One-command research pipelines for Google NotebookLM, driven by an AI agent (Claude Code skill / plugin). UI and prompts are in Ukrainian.

## Можливості
- **Джерела:**
  - локальні теки, обхід рекурсивний;
  - OneDrive: хмарні файли довантажуються автоматично;
  - Google Drive через Drive for Desktop або за посиланням;
  - файли й URL.

  Завантажуються лише формати, які підтримує NotebookLM. Excel і HTML скрипт конвертує в Markdown. Повторний запуск не дублює джерела.
- **Типи досліджень:**
  - `oglyad` — огляд матеріалів;
  - `pytannia` — відповіді на перелік питань;
  - `research` — Deep Research в інтернеті;
  - `kandydat` — аналіз доброчесності кандидата на посаду судді;
  - власні типи: `nlm types add`.
- **Бібліотека промптів:** готові промпти (група `vrp`, аналітика щодо суддів) плюс ваші власні. Додати промпт можна командою `nlm prompts add` або імпортувати з DOCX за заголовками командою `nlm prompts import`.
- **Результат:** DOCX з нормальним форматуванням (заголовки, таблиці, список джерел), а також MD або JSON.

## Встановлення
### Claude Code (рекомендовано)
```
/plugin marketplace add teraxis/discovery
/plugin install discovery@discovery
```
Потім напишіть агенту: «Налаштуй discovery». Він виконає [AGENT_SETUP.md](AGENT_SETUP.md):
1. встановить залежності;
2. попросить вас увійти в Google командою `nlm login`;
3. перевірить усе командою `nlm doctor`.

### Інші агенти (Codex, Gemini CLI, Cursor…)
Скажіть агенту: «Встанови https://github.com/teraxis/discovery і налаштуй за AGENT_SETUP.md». Інструкції для агента лежать в [AGENTS.md](AGENTS.md).

### Вручну
```
git clone https://github.com/teraxis/discovery
powershell -ExecutionPolicy Bypass -File discovery\skills\discovery\install.ps1   # або: sh discovery/skills/discovery/install.sh
discovery\skills\discovery\nlm.cmd login
discovery\skills\discovery\nlm.cmd doctor
```

## Використання
У Claude Code:
```
/discovery зроби огляд матеріалів у D:\Справи\Договір-17, результат у D:\Звіти
/discovery проаналізуй кандидата Іваненко Іван Іванович, матеріали в D:\Кандидати\Іваненко
/discovery досліди в інтернеті практику ЄСПЛ щодо строків розгляду справ
```
З терміналу, без агента і без токенів:
```
nlm types
nlm start oglyad --materials "D:\Справи\Договір-17" --out "D:\Звіти" --var "ТЕМА=договір оренди"
nlm kandydat --pib "Іваненко Іван Іванович" --materials "D:\Кандидати\Іваненко" --out-dir "D:\Звіти"
nlm prompts
```

## Власні промпти й типи
- **Промпти.** Покладіть `.md` у `~/.nlm-pipeline/prompts/` або виконайте `nlm prompts add назва --file текст.md --use persona`. Щоб розібрати свій чекліст на промпти, виконайте `nlm prompts import чекліст.docx`: документ ділиться за заголовками.
- **Типи.** Виконайте `nlm types add "Огляд договору" --from oglyad`, відредагуйте створений YAML і запускайте `nlm start ohliad_dohovoru …`.
- **Синхронізація між ПК.** Шляхи `prompts_dir` і `types_dir` у `config.yaml` можна направити в OneDrive чи Google Drive.

## Застереження
- Пакет використовує [notebooklm-py](https://pypi.org/project/notebooklm-py/), **неофіційну** бібліотеку. Вона працює через внутрішній інтерфейс NotebookLM, тож Google може його змінити, і бібліотека зламається. Для NotebookLM радимо окремий Google-акаунт.
- Сесія входу, токени Drive й журнали зберігаються лише локально, у `~/.nlm-pipeline/`. Нікуди, крім Google, вони не передаються.
- Відповідальність за зміст матеріалів, які ви завантажуєте в NotebookLM, і за дотримання вимог щодо персональних даних несе користувач.

## Ліцензія
MIT
