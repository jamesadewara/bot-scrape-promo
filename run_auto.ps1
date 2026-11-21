# Starts PromoScout in auto mode with the project's .venv. Output goes to data\auto.log.
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force data | Out-Null
& "$PSScriptRoot\.venv\Scripts\python.exe" -m promoscout auto --quiet *>> "$PSScriptRoot\data\auto.log"
