# JobHelm launcher for Windows — stops any old instance, starts fresh, app opens your browser.
# Usage:  .\run.ps1        (bundled demo data)
#         $env:JOBHELM_CAREEROPS="C:\path\to\career-ops"; .\run.ps1   (your real data)
$port = if ($env:JOBHELM_PORT) { $env:JOBHELM_PORT } else { "8899" }
Write-Host "-> Stopping any old JobHelm on port $port..."
Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Write-Host "-> Starting JobHelm at http://127.0.0.1:$port  (Ctrl+C to stop)"
python jobhelm\mission-control.py
