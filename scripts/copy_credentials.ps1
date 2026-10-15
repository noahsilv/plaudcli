$ErrorActionPreference = 'Stop'
$tokenPath = Join-Path $env:USERPROFILE '.plaud\tokens.json'
if (-not (Test-Path -LiteralPath $tokenPath)) {
    throw 'No Plaud CLI credentials found. Run plaud login first.'
}
$rawTokens = Get-Content -LiteralPath $tokenPath -Raw
$parsedTokens = $rawTokens | ConvertFrom-Json
if (-not $parsedTokens.access_token) {
    throw 'The credential file has no access_token. Run plaud login again.'
}
Set-Clipboard -Value $rawTokens
Write-Output 'Credentials copied privately to clipboard. Paste into the Horizon PLAUD_TOKENS_JSON secret, then clear your clipboard.'
