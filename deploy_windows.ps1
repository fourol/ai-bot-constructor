param(
    [switch]$AutoStart,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

function Write-Step($num, $total, $msg) {
    Write-Host ""
    Write-Host "[$num/$total] $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "  [OK] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "  [!] $msg" -ForegroundColor Yellow
}

function Write-Err($msg) {
    Write-Host "  [ОШИБКА] $msg" -ForegroundColor Red
}

function Download-File($Url, $OutFile) {
    $curlExe = Get-Command "curl.exe" -ErrorAction SilentlyContinue
    if ($curlExe) {
        & curl.exe -L -C - --retry 3 --retry-delay 3 --progress-bar $Url -o $OutFile
        if ($LASTEXITCODE -ne 0) {
            throw "curl.exe завершился с кодом $LASTEXITCODE при скачивании $Url"
        }
    } else {
        Write-Host "  Скачивание через Invoke-WebRequest..."
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    }
}

Write-Host "========================================================================" -ForegroundColor DarkCyan
Write-Host "   КС54 • AI-Интегратор — Авторазвёртывание для Windows 10 Server" -ForegroundColor White
Write-Host "   Целевая конфигурация: NVIDIA T1000 (8 GB) • 20+ пользователей" -ForegroundColor Gray
Write-Host "========================================================================" -ForegroundColor DarkCyan

# Проверка прав администратора
$IsAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    Write-Warn "Скрипт запущен без прав Администратора. Открытие порта в Брандмауэре может не сработать."
}

# ------------------------------------------------------------------------
# ШАГ 1: Проверка / установка Python 3.10+
# ------------------------------------------------------------------------
Write-Step 1 6 "Проверка Python 3.10+..."

function Get-WorkingPython {
    $candidates = @("python", "py")
    foreach ($cmd in $candidates) {
        $info = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($info -and $info.Source -notlike "*WindowsApps*") {
            try {
                if ($cmd -eq "py") {
                    $ver = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                } else {
                    $ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                }
                if ($ver -match '^(\d+)\.(\d+)$') {
                    $maj = [int]$Matches[1]
                    $min = [int]$Matches[2]
                    if ($maj -ge 3 -and $min -ge 10) {
                        return @{ Cmd = $cmd; Version = $ver }
                    }
                }
            } catch {}
        }
    }
    # Проверка стандартных путей установки
    $stdPaths = @(
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python310\python.exe",
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe"
    )
    foreach ($p in $stdPaths) {
        if (Test-Path $p) {
            return @{ Cmd = $p; Version = "3.1x" }
        }
    }
    return $null
}

$pyInfo = Get-WorkingPython
if (-not $pyInfo) {
    Write-Warn "Python 3.10+ не найден. Пробуем автоматическую установку Python 3.11..."
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    $installed = $false
    if ($winget) {
        try {
            & winget install -e --id Python.Python.3.11 --scope machine --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) { $installed = $true }
        } catch {}
    }
    if (-not $installed) {
        $pyInstaller = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
        Write-Host "  Скачивание официального установщика Python 3.11.9 (x64)..."
        Download-File "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" $pyInstaller
        Write-Host "  Тихая установка Python 3.11.9..."
        $proc = Start-Process -FilePath $pyInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait -PassThru
        Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
        if ($proc.ExitCode -ne 0) {
            throw "Не удалось установить Python 3.11 автоматически (код $($proc.ExitCode)). Установите вручную с python.org."
        }
    }
    # Обновление PATH в текущей сессии
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $pyInfo = Get-WorkingPython
    if (-not $pyInfo) {
        throw "Python установлен, но не обнаружен в PATH. Перезапустите PowerShell и повторите install_windows.bat."
    }
}
Write-Ok "Найден Python ($($pyInfo.Version)): $($pyInfo.Cmd)"

# ------------------------------------------------------------------------
# ШАГ 2: Виртуальное окружение venv и зависимости
# ------------------------------------------------------------------------
Write-Step 2 6 "Настройка виртуального окружения Python (venv) и зависимостей..."

if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Host "  Создание venv..."
    if ($pyInfo.Cmd -eq "py") {
        & py -3 -m venv venv
    } else {
        & $pyInfo.Cmd -m venv venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать venv" }
}

$venvPy = Join-Path $RepoRoot "venv\Scripts\python.exe"
$venvPip = Join-Path $RepoRoot "venv\Scripts\pip.exe"

Write-Host "  Обновление pip и установка библиотек из requirements.txt..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Ошибка установки зависимостей из requirements.txt" }
Write-Ok "Зависимости FastAPI/Uvicorn/OpenAI/RAG установлены."

# ------------------------------------------------------------------------
# ШАГ 3: Определение NVIDIA T1000 GPU и выбор сборки llama-server
# ------------------------------------------------------------------------
Write-Step 3 6 "Диагностика видеокарты NVIDIA и подбор оптимального движка LLM..."

$BuildMode = "cpu"
$GpuName = "Не обнаружена"
$DriverVer = "0.0"

$nvidiaSmi = Get-Command "nvidia-smi.exe" -ErrorAction SilentlyContinue
if (-not $nvidiaSmi -and (Test-Path "$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe")) {
    $nvidiaSmi = "$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
}

if ($nvidiaSmi) {
    try {
        $smiOut = & $nvidiaSmi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null | Select-Object -First 1
        if ($smiOut) {
            $parts = $smiOut -split ","
            $GpuName = $parts[0].Trim()
            $VramInfo = $parts[1].Trim()
            $DriverVer = $parts[2].Trim()
            Write-Ok "Обнаружена GPU: $GpuName ($VramInfo), драйвер NVIDIA $DriverVer"

            # CUDA 12.4 на Windows требует драйвер >= 550.54
            $drvParts = $DriverVer -split "\."
            $drvMaj = [int]$drvParts[0]
            $drvMin = if ($drvParts.Length -gt 1) { [int]$drvParts[1] } else { 0 }

            if ($drvMaj -gt 550 -or ($drvMaj -eq 550 -and $drvMin -ge 54)) {
                $BuildMode = "cuda12.4"
                Write-Ok "Драйвер подходит для CUDA 12.4 — будет установлена максимальная GPU-сборка (cuBLAS / Tensor Cores)."
            } else {
                $BuildMode = "vulkan"
                Write-Warn "Версия драйвера ($DriverVer) ниже 550.54 (требуется для CUDA 12.4)."
                Write-Warn "Выбрана GPU-сборка Vulkan (работает на текущем драйвере без ошибок; для CUDA 12.4 обновите драйвер с nvidia.com)."
            }
        }
    } catch {
        Write-Warn "Не удалось опросить nvidia-smi, проверяем альтернативы..."
    }
} else {
    Write-Warn "nvidia-smi не найден. Если стоит NVIDIA T1000 — проверьте установку драйвера NVIDIA."
    Write-Warn "Будет установлена CPU-сборка (можно позже перекачать CUDA через download_model.bat)."
}

# ------------------------------------------------------------------------
# ШАГ 4: Скачивание llama-server + CUDA DLL + модели Qwen 2.5 3B
# ------------------------------------------------------------------------
Write-Step 4 6 "Скачивание llama-server ($BuildMode) и модели Qwen 2.5 3B Instruct (GGUF)..."

$llmDir = Join-Path $RepoRoot "llm"
if (-not (Test-Path $llmDir)) { New-Item -ItemType Directory -Path $llmDir | Out-Null }

$llamaExe = Join-Path $llmDir "llama-server.exe"
$needDownloadServer = $true
if (Test-Path $llamaExe) {
    if ($BuildMode -eq "cuda12.4" -and -not (Test-Path (Join-Path $llmDir "ggml-cuda.dll"))) {
        Write-Warn "Найден старый llama-server без ggml-cuda.dll — обновляем на CUDA 12.4..."
        $needDownloadServer = $true
    } else {
        $needDownloadServer = $false
        Write-Ok "llama-server.exe уже установлен в папке llm\."
    }
}

if ($needDownloadServer) {
    $tempExtract = Join-Path $llmDir "_tmp_extract"
    if (Test-Path $tempExtract) { Remove-Item $tempExtract -Recurse -Force }
    New-Item -ItemType Directory -Path $tempExtract | Out-Null

    if ($BuildMode -eq "cuda12.4") {
        $zipMain = Join-Path $llmDir "llama-win-cuda.zip"
        $zipCudart = Join-Path $llmDir "cudart-win-cuda.zip"

        Write-Host "  [1/2] Скачивание llama-b12651-bin-win-cuda-12.4-x64.zip..."
        Download-File "https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-win-cuda-12.4-x64.zip" $zipMain
        Expand-Archive -Path $zipMain -DestinationPath $tempExtract -Force
        Remove-Item $zipMain -Force -ErrorAction SilentlyContinue

        Write-Host "  [2/2] Скачивание CUDA Runtime DLLs (cudart-llama-bin-win-cuda-12.4-x64.zip)..."
        Download-File "https://github.com/ggml-org/llama.cpp/releases/download/b12651/cudart-llama-bin-win-cuda-12.4-x64.zip" $zipCudart
        Expand-Archive -Path $zipCudart -DestinationPath $tempExtract -Force
        Remove-Item $zipCudart -Force -ErrorAction SilentlyContinue
    } elseif ($BuildMode -eq "vulkan") {
        $zipMain = Join-Path $llmDir "llama-win-vulkan.zip"
        Write-Host "  Скачивание llama-b12651-bin-win-vulkan-x64.zip..."
        Download-File "https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-win-vulkan-x64.zip" $zipMain
        Expand-Archive -Path $zipMain -DestinationPath $tempExtract -Force
        Remove-Item $zipMain -Force -ErrorAction SilentlyContinue
    } else {
        $zipMain = Join-Path $llmDir "llama-win-cpu.zip"
        Write-Host "  Скачивание llama-b12651-bin-win-cpu-x64.zip..."
        Download-File "https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-win-cpu-x64.zip" $zipMain
        Expand-Archive -Path $zipMain -DestinationPath $tempExtract -Force
        Remove-Item $zipMain -Force -ErrorAction SilentlyContinue
    }

    # Переносим все .exe и .dll прямо в папку llm\ (на случай вложенной папки в архиве)
    Get-ChildItem -Path $tempExtract -Recurse -File | Where-Object { $_.Extension -in @(".exe", ".dll") } | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination (Join-Path $llmDir $_.Name) -Force
    }
    Remove-Item $tempExtract -Recurse -Force -ErrorAction SilentlyContinue

    if (-not (Test-Path $llamaExe)) {
        throw "Не удалось найти llama-server.exe после распаковки!"
    }
    Write-Ok "Бинарники llama-server ($BuildMode) готовы в папке llm\."
}

$modelPath = Join-Path $llmDir "qwen2.5-3b-instruct-q4_k_m.gguf"
$minModelSize = 1800000000 # ~1.8 ГБ минимум для проверки целостности
$needModel = $true
if (Test-Path $modelPath) {
    $fileLen = (Get-Item $modelPath).Length
    if ($fileLen -ge $minModelSize) {
        $needModel = $false
        $gb = [math]::Round($fileLen / 1GB, 2)
        Write-Ok "Модель qwen2.5-3b-instruct-q4_k_m.gguf уже скачана ($gb ГБ)."
    } else {
        Write-Warn "Обнаружен неполный файл модели — докачиваем..."
    }
}

if ($needModel) {
    Write-Host "  Скачивание Qwen 2.5 3B Instruct Q4_K_M (~2.0 ГБ) с HuggingFace..."
    Download-File "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf" $modelPath
    $fileLen = (Get-Item $modelPath).Length
    if ($fileLen -lt $minModelSize) {
        throw "Файл модели скачался не полностью (размер $fileLen байт). Проверьте интернет и повторите запуск."
    }
    Write-Ok "Модель успешно скачана!"
}

# ------------------------------------------------------------------------
# ШАГ 5: Настройка Брандмауэра Windows (порт 3000 для 20+ участников)
# ------------------------------------------------------------------------
Write-Step 5 6 "Открытие порта 3000 в Брандмауэре Windows и проверка сети..."

if ($IsAdmin) {
    try {
        & netsh advfirewall firewall delete rule name="KS54-AI-Bot-Constructor" >$null 2>&1
        & netsh advfirewall firewall add rule name="KS54-AI-Bot-Constructor" dir=in action=allow protocol=TCP localport=3000 >$null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Правило Брандмауэра Windows для TCP порта 3000 добавлено."
        } else {
            Write-Warn "Не удалось автоматически добавить правило Брандмауэра (код $LASTEXITCODE)."
        }
    } catch {
        Write-Warn "Ошибка настройки Брандмауэра: $_"
    }
} else {
    Write-Warn "Для автооткрытия порта 3000 запустите скрипт от имени Администратора или выполните:"
    Write-Host '    netsh advfirewall firewall add rule name="KS54-AI-Bot-Constructor" dir=in action=allow protocol=TCP localport=3000' -ForegroundColor Gray
}

# Определение локальных IPv4-адресов сервера
$lanIPs = @()
try {
    $lanIPs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
        Select-Object -ExpandProperty IPAddress
} catch {}

# ------------------------------------------------------------------------
# ШАГ 6: Создание ярлыка на Рабочем столе и готовность к старту
# ------------------------------------------------------------------------
Write-Step 6 6 "Завершение настройки и создание ярлыка запуска..."

if (-not $SkipShortcut) {
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $lnkPath = Join-Path $desktop "КС54 AI-Интегратор.lnk"
        $wsh = New-Object -ComObject WScript.Shell
        $sc = $wsh.CreateShortcut($lnkPath)
        $sc.TargetPath = Join-Path $RepoRoot "start_all.bat"
        $sc.WorkingDirectory = $RepoRoot
        $sc.Description = "Запуск ИИ-Лаборатории КС54 (LLM + Веб-сервер)"
        $sc.Save()
        Write-Ok "Ярлык «КС54 AI-Интегратор» создан на Рабочем столе."
    } catch {
        Write-Warn "Не удалось создать ярлык на Рабочем столе: $_"
    }
}

Write-Host ""
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "   РАЗВЁРТЫВАНИЕ ЗАВЕРШЕНО УСПЕШНО!" -ForegroundColor Green
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "   Режим GPU       : $BuildMode ($GpuName)"
Write-Host "   Конфигурация LLM: -ngl 99 -np 24 -c 49152 (до 24 параллельных слотов)"
Write-Host "   Локальный адрес : http://127.0.0.1:3000"
if ($lanIPs.Count -gt 0) {
    foreach ($ip in $lanIPs) {
        Write-Host "   Адрес для класса: http://${ip}:3000" -ForegroundColor Cyan
    }
} else {
    Write-Host "   Адрес для класса: http://<IP_СЕРВЕРА>:3000 (узнать через ipconfig)"
}
Write-Host "========================================================================" -ForegroundColor Green
Write-Host ""

$startAllBat = Join-Path $RepoRoot "start_all.bat"
if ($AutoStart) {
    Start-Process -FilePath $startAllBat -WorkingDirectory $RepoRoot
} else {
    $ans = Read-Host "Запустить сервер сейчас? [Y/n]"
    if ($ans -eq "" -or $ans -match '^[YyДд]') {
        Start-Process -FilePath $startAllBat -WorkingDirectory $RepoRoot
    } else {
        Write-Host "Для запуска в любое время кликните дважды по start_all.bat или ярлыку на Рабочем столе." -ForegroundColor Gray
    }
}
