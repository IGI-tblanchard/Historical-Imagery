$ErrorActionPreference = 'Stop'

$propy = 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat'
$scriptPath = 'C:\Users\tblanchard\Documents\Tracy\Code\Historical Imagery\Git-Hub\Historical-Imagery\generate_historical_boundaries.py'
$reportRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\Historical_Imagery_Reports'
$shareRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive'
$runStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$transcriptPath = Join-Path $reportRoot "historical_boundaries_scheduler_$runStamp.log"
$transcriptStarted = $false
$exitCode = 1

try {
    Start-Transcript -LiteralPath $transcriptPath -Force | Out-Null
    $transcriptStarted = $true
    Write-Output "Started historical boundary generation: $(Get-Date -Format o)"
    Write-Output "ArcGIS Pro Python: $propy"
    Write-Output "UNC data share visible: $(Test-Path -LiteralPath $shareRoot)"
    Write-Output "Reports folder: $reportRoot"

    if (-not (Test-Path -LiteralPath $propy -PathType Leaf)) {
        throw "ArcGIS Pro Python launcher was not found: $propy"
    }
    if (-not (Test-Path -LiteralPath $shareRoot -PathType Container)) {
        throw "UNC data share is unavailable to this scheduled task account: $shareRoot"
    }
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Historical boundary script was not found: $scriptPath"
    }

    & $propy $scriptPath
    $exitCode = $LASTEXITCODE
    Write-Output "Historical boundary script exit code: $exitCode"
    Write-Output "Finished: $(Get-Date -Format o)"
}
catch {
    Write-Error (($_ | Out-String).Trim())
    $exitCode = 1
}
finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
}

exit $exitCode
