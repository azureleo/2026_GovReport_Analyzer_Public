param(
    [string]$ServerUrl = "http://127.0.0.1:10000"
)

$ErrorActionPreference = "Stop"
$response = Invoke-RestMethod -Uri "$($ServerUrl.TrimEnd('/'))/v1/models" -Method Get -TimeoutSec 15
$response | ConvertTo-Json -Depth 8
