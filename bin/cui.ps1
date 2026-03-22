[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$scriptDir = Split-Path -Parent $PSCommandPath
& (Join-Path $scriptDir "caliper.ps1") @RemainingArgs
exit $LASTEXITCODE

