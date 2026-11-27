# Встановлення notebooklm-pipeline (Windows). Повторний запуск безпечний (оновлює залежності).
# Використання: powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$NlmHome = if ($env:NLM_HOME) { $env:NLM_HOME } else { Join-Path $HOME ".nlm-pipeline" }
New-Item -ItemType Directory -Force $NlmHome, "$NlmHome\prompts", "$NlmHome\types" | Out-Null

# 1. Python 3.10+
$py = $null
foreach ($c in @("py -3.12", "py -3.11", "py -3.10", "python")) {
    try {
        $v = & cmd /c "$c -c ""import sys;print(sys.version_info>=(3,10))"" 2>nul"
        if ($v -eq "True") { $py = $c; break }
    } catch {}
}
if (-not $py) {
    Write-Host "X Потрібен Python 3.10+. Встановіть:  winget install -e --id Python.Python.3.12  і повторіть." -ForegroundColor Red
    exit 1
}
Write-Host "✓ Python: $py"

# 2. Віртуальне середовище й залежності (у теці користувача — оновлення пакета його не зачіпають)
$venvPy = "$NlmHome\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { & cmd /c "$py -m venv ""$NlmHome\.venv""" }
& $venvPy -m pip install --upgrade pip -q
& $venvPy -m pip install -q -r "$Here\requirements.txt"
Write-Host "✓ Залежності встановлено"

# 3. Браузер для входу: Chrome/Edge системний, інакше — Chromium від Playwright
$hasChrome = (Test-Path "$env:ProgramFiles\Google\Chrome\Application\chrome.exe") -or
             (Test-Path "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe")
if (-not $hasChrome) { & $venvPy -m playwright install chromium }
Write-Host "✓ Браузер для входу готовий"

# 4. Налаштування за замовчуванням
if (-not (Test-Path "$NlmHome\config.yaml")) { Copy-Item "$Here\config.example.yaml" "$NlmHome\config.yaml" }
Set-Content "$NlmHome\installed_from.txt" $Here -Encoding utf8

Write-Host ""
Write-Host "Готово. Далі:" -ForegroundColor Green
Write-Host "  1) Вхід у NotebookLM (робить людина):  & ""$Here\nlm.cmd"" login"
Write-Host "  2) Перевірка:                          & ""$Here\nlm.cmd"" doctor"
