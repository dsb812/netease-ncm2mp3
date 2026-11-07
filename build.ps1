# 打包成单文件 exe（需要先 pip install pyinstaller）
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "== 清理旧产物 =="
Remove-Item build, dist -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item *.spec -Force -ErrorAction SilentlyContinue

Write-Host "== PyInstaller 打包 =="
pyinstaller --noconfirm --clean --onefile --windowed --name netease-ncm2mp3 --hidden-import Crypto.Cipher.AES --exclude-module numpy --exclude-module pandas --exclude-module matplotlib app.py

if (Test-Path "$here\bin\ffmpeg.exe") {
  Write-Host "== 复制 ffmpeg =="
  Copy-Item "$here\bin\ffmpeg.exe" "$here\dist\ffmpeg.exe" -Force
}

Write-Host "== 完成: dist\netease-ncm2mp3.exe =="