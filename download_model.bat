@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Скачивание llama-server (CUDA/GPU) и модели Qwen 2.5 3B

echo ================================================================
echo    Скачивание GPU llama-server (CUDA 12.4) и модели Qwen 2.5 3B
echo ================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "New-Item -ItemType Directory -Force -Path 'llm' | Out-Null;" ^
  "$mode='cpu';" ^
  "$smi=Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue;" ^
  "if(-not $smi -and (Test-Path \"$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe\")){$smi=\"$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe\"};" ^
  "if($smi){try{$out=(& $smi --query-gpu=driver_version --format=csv,noheader 2>$null|Select-Object -First 1).Trim();if($out){$p=$out -split '\.';if([int]$p[0] -gt 550 -or ([int]$p[0] -eq 550 -and [int]$p[1] -ge 54)){$mode='cuda12.4'}else{$mode='vulkan'}}}catch{}};" ^
  "Write-Host \"[ИНФО] Выбранный режим сборки llama-server: $mode\" -ForegroundColor Cyan;" ^
  "if(-not (Test-Path 'llm\llama-server.exe') -or ($mode -eq 'cuda12.4' -and -not (Test-Path 'llm\ggml-cuda.dll'))){" ^
  "  New-Item -ItemType Directory -Force -Path 'llm\_tmp' | Out-Null;" ^
  "  if($mode -eq 'cuda12.4'){" ^
  "    curl.exe -L 'https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-win-cuda-12.4-x64.zip' -o 'llm\llama.zip';" ^
  "    Expand-Archive -Path 'llm\llama.zip' -DestinationPath 'llm\_tmp' -Force; Remove-Item 'llm\llama.zip' -Force;" ^
  "    curl.exe -L 'https://github.com/ggml-org/llama.cpp/releases/download/b12651/cudart-llama-bin-win-cuda-12.4-x64.zip' -o 'llm\cudart.zip';" ^
  "    Expand-Archive -Path 'llm\cudart.zip' -DestinationPath 'llm\_tmp' -Force; Remove-Item 'llm\cudart.zip' -Force;" ^
  "  }elseif($mode -eq 'vulkan'){" ^
  "    curl.exe -L 'https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-win-vulkan-x64.zip' -o 'llm\llama.zip';" ^
  "    Expand-Archive -Path 'llm\llama.zip' -DestinationPath 'llm\_tmp' -Force; Remove-Item 'llm\llama.zip' -Force;" ^
  "  }else{" ^
  "    curl.exe -L 'https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-win-cpu-x64.zip' -o 'llm\llama.zip';" ^
  "    Expand-Archive -Path 'llm\llama.zip' -DestinationPath 'llm\_tmp' -Force; Remove-Item 'llm\llama.zip' -Force;" ^
  "  }" ^
  "  Get-ChildItem 'llm\_tmp' -Recurse -File | Where-Object {$_.Extension -in '.exe','.dll'} | ForEach-Object {Copy-Item $_.FullName (Join-Path 'llm' $_.Name) -Force};" ^
  "  Remove-Item 'llm\_tmp' -Recurse -Force;" ^
  "};" ^
  "if(-not (Test-Path 'llm\qwen2.5-3b-instruct-q4_k_m.gguf')){" ^
  "  Write-Host '[2/2] Скачивание модели Qwen 2.5 3B Instruct Q4_K_M (~2.0 ГБ)...' -ForegroundColor Cyan;" ^
  "  curl.exe -L -C - 'https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf' -o 'llm\qwen2.5-3b-instruct-q4_k_m.gguf';" ^
  "}"

if %errorlevel% neq 0 (
    echo [ОШИБКА] Ошибка скачивания файлов!
    pause
    exit /b 1
)

echo.
echo ================================================================
echo  Все файлы готовы! Запустите start_all.bat для старта сервера.
echo ================================================================
pause
