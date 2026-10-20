param(
    [string]$DataDir = '',
    [string]$ReportsDir = '',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$runRoot = (Get-Location).Path
if (-not $DataDir) { $DataDir = Join-Path $runRoot 'work\tibo-monitor-runtime' }
if (-not $ReportsDir) { $ReportsDir = Join-Path $runRoot 'outputs\tibo-monitor-reports' }
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
$ReportsDir = [System.IO.Path]::GetFullPath($ReportsDir)
if (-not $Python) { $Python = (Get-Command python -ErrorAction Stop).Source }
$script = Join-Path $PSScriptRoot 'monitor.py'
$sharedArgs = @('--data-dir', $DataDir, '--reports-dir', $ReportsDir)
$statusText = & $Python -X utf8 $script status @sharedArgs
if ($LASTEXITCODE -ne 0) { throw 'Cannot read monitor status' }
$status = $statusText | ConvertFrom-Json
if ($status.running) { $statusText; exit 0 }
$processArgs = @('-X', 'utf8', ('"' + $script + '"'), 'run', '--data-dir', ('"' + $DataDir + '"'), '--reports-dir', ('"' + $ReportsDir + '"'))
$process = Start-Process -FilePath $Python -ArgumentList $processArgs -WorkingDirectory $runRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $DataDir 'process.stdout.log') -RedirectStandardError (Join-Path $DataDir 'process.stderr.log') -PassThru
Start-Sleep -Seconds 2
if ($process.HasExited) { throw ('Monitor exited. Check ' + (Join-Path $DataDir 'process.stderr.log')) }
& $Python -X utf8 $script status @sharedArgs
