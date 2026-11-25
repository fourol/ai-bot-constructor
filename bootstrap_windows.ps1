$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

Write-Host "========================================================================" -ForegroundColor DarkCyan
Write-Host "   КС54 • AI-Интегратор — Быстрая установка с GitHub (Windows 10)" -ForegroundColor White
Write-Host "========================================================================" -ForegroundColor DarkCyan

$TargetDir = "C:\ai-bot-constructor"
$RepoUrl = "https://github.com/fourol/ai-bot-constructor.git"
$ZipUrl = "https://github.com/fourol/ai-bot-constructor/archive/refs/heads/main.zip"

# Пробуем обновить PATH на случай свежеустановленного Git
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

if (-not (Get-Command "git" -ErrorAction SilentlyContinue)) {
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "[*] Git не найден. Устанавливаем Git через winget..." -ForegroundColor Cyan
        try {
            & winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        } catch {}
    }
}

if (Get-Command "git" -ErrorAction SilentlyContinue) {
    if (Test-Path (Join-Path $TargetDir ".git")) {
        Write-Host "[*] Папка $TargetDir уже содержит репозиторий. Обновляем (git pull)..." -ForegroundColor Cyan
        & git -C $TargetDir pull origin main
    } else {
        Write-Host "[*] Клонируем репозиторий в $TargetDir..." -ForegroundColor Cyan
        & git clone $RepoUrl $TargetDir
    }
} else {
    Write-Host "[*] Git не установлен — скачиваем архив проекта напрямую с GitHub..." -ForegroundColor Cyan
    if (-not (Test-Path $TargetDir)) { New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null }
    $zipFile = Join-Path $env:TEMP "ai-bot-constructor.zip"
    $tmpExtract = Join-Path $env:TEMP "ai-bot-extract"
    if (Test-Path $tmpExtract) { Remove-Item $tmpExtract -Recurse -Force }
    & curl.exe -L $ZipUrl -o $zipFile
    Expand-Archive -Path $zipFile -DestinationPath $tmpExtract -Force
    Copy-Item -Path (Join-Path $tmpExtract "ai-bot-constructor-main\*") -Destination $TargetDir -Recurse -Force
    Remove-Item $zipFile -Force -ErrorAction SilentlyContinue
    Remove-Item $tmpExtract -Recurse -Force -ErrorAction SilentlyContinue
}

Set-Location $TargetDir
Write-Host "[*] Переходим к полной настройке и скачиванию компонентов..." -ForegroundColor Green
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $TargetDir "deploy_windows.ps1")
