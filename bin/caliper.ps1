[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"

function Get-InstallEnvPath {
    if ($env:CALIPER_INSTALL_ENV) {
        return [System.IO.Path]::GetFullPath($env:CALIPER_INSTALL_ENV)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $HOME ".config\caliper\install.env"))
}

function Read-InstallEnv {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $values
    }
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        if (($value.StartsWith("'") -and $value.EndsWith("'")) -or ($value.StartsWith('"') -and $value.EndsWith('"'))) {
            if ($value.Length -ge 2) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $values[$key] = $value
    }
    return $values
}

function Find-CliRoot {
    param([string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if (-not $candidate) { continue }
        try {
            $resolved = [System.IO.Path]::GetFullPath($candidate)
        }
        catch {
            continue
        }
        $entry = Join-Path $resolved "cli\caliper.py"
        if (Test-Path -LiteralPath $entry -PathType Leaf) {
            return $resolved
        }
    }
    return $null
}

function Find-RuntimeRoot {
    param([string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if (-not $candidate) { continue }
        try {
            $resolved = [System.IO.Path]::GetFullPath($candidate)
        }
        catch {
            continue
        }
        $entry = Join-Path $resolved "util\utils.py"
        if (Test-Path -LiteralPath $entry -PathType Leaf) {
            return $resolved
        }
    }
    return $null
}

$installEnvPath = Get-InstallEnvPath
$installValues = Read-InstallEnv -Path $installEnvPath

foreach ($key in @("CALIPER_CLI_ROOT", "CALIPER_ROOT", "OMNIPARSER_ROOT", "CALIPER_RUNTIME_ROOT", "UIED_ROOT", "CALIPER_PYTHON", "CALIPER_MODEL_DIR")) {
    if (-not (Get-Item "Env:$key" -ErrorAction SilentlyContinue) -and $installValues.ContainsKey($key)) {
        Set-Item -Path "Env:$key" -Value $installValues[$key]
    }
}

if ($RemainingArgs.Count -gt 0 -and $RemainingArgs[0] -eq "setup") {
    $setCliRoot = $env:CALIPER_CLI_ROOT
    $setRuntimeRoot = if ($env:OMNIPARSER_ROOT) { $env:OMNIPARSER_ROOT } else { $env:CALIPER_RUNTIME_ROOT }
    $setUiedRoot = $env:UIED_ROOT
    $setPython = $env:CALIPER_PYTHON
    $showOnly = $false
    $clearOnly = $false

    $i = 1
    while ($i -lt $RemainingArgs.Count) {
        $arg = $RemainingArgs[$i]
        switch ($arg) {
            "--cli-root" {
                $i++
                if ($i -ge $RemainingArgs.Count) { throw "caliper setup: --cli-root requires a value" }
                $setCliRoot = $RemainingArgs[$i]
            }
            "--runtime-root" {
                $i++
                if ($i -ge $RemainingArgs.Count) { throw "caliper setup: --runtime-root requires a value" }
                $setRuntimeRoot = $RemainingArgs[$i]
            }
            "--python" {
                $i++
                if ($i -ge $RemainingArgs.Count) { throw "caliper setup: --python requires a value" }
                $setPython = $RemainingArgs[$i]
            }
            "--uied-root" {
                $i++
                if ($i -ge $RemainingArgs.Count) { throw "caliper setup: --uied-root requires a value" }
                $setUiedRoot = $RemainingArgs[$i]
            }
            "--show" { $showOnly = $true }
            "--clear" { $clearOnly = $true }
            "--help" {
                Write-Output "Usage: caliper setup [--cli-root <path>] [--runtime-root <path>] [--uied-root <path>] [--python <path>] [--show] [--clear]"
                exit 0
            }
            default {
                throw "caliper setup: unknown option '$arg'"
            }
        }
        $i++
    }

    if ($showOnly) {
        Write-Output "install_env=$installEnvPath"
        if (Test-Path -LiteralPath $installEnvPath -PathType Leaf) {
            Get-Content -LiteralPath $installEnvPath | Write-Output
        }
        else {
            Write-Output "(not configured)"
        }
        exit 0
    }

    if ($clearOnly) {
        if (Test-Path -LiteralPath $installEnvPath -PathType Leaf) {
            Remove-Item -LiteralPath $installEnvPath -Force
        }
        Write-Output "Cleared persistent config at $installEnvPath"
        exit 0
    }

    if (-not $setCliRoot -or -not $setRuntimeRoot) {
        throw "caliper setup requires --cli-root and --runtime-root (or pre-set env values)."
    }

    $resolvedCli = [System.IO.Path]::GetFullPath($setCliRoot)
    $resolvedRuntime = [System.IO.Path]::GetFullPath($setRuntimeRoot)
    if (-not (Test-Path -LiteralPath (Join-Path $resolvedCli "cli\caliper.py") -PathType Leaf)) {
        throw "caliper setup: invalid --cli-root (missing cli/caliper.py): $resolvedCli"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $resolvedRuntime "util\utils.py") -PathType Leaf)) {
        throw "caliper setup: invalid --runtime-root (missing util/utils.py): $resolvedRuntime"
    }

    $resolvedPython = $null
    if ($setPython) {
        $resolvedPython = [System.IO.Path]::GetFullPath($setPython)
        if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
            throw "caliper setup: invalid --python path: $resolvedPython"
        }
    }

    $resolvedUied = $null
    if ($setUiedRoot) {
        $resolvedUied = [System.IO.Path]::GetFullPath($setUiedRoot)
        if (-not (Test-Path -LiteralPath (Join-Path $resolvedUied "detect_compo\ip_region_proposal.py") -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $resolvedUied "detect_merge\merge.py") -PathType Leaf)) {
            throw "caliper setup: invalid --uied-root (missing detect_compo/ip_region_proposal.py or detect_merge/merge.py): $resolvedUied"
        }
    }

    $installDir = Split-Path -Parent $installEnvPath
    New-Item -ItemType Directory -Force -Path $installDir | Out-Null

    $lines = @(
        "CALIPER_CLI_ROOT='$resolvedCli'",
        "OMNIPARSER_ROOT='$resolvedRuntime'"
    )
    if ($resolvedUied) {
        $lines += "UIED_ROOT='$resolvedUied'"
    }
    if ($resolvedPython) {
        $lines += "CALIPER_PYTHON='$resolvedPython'"
    }
    Set-Content -LiteralPath $installEnvPath -Value $lines -Encoding UTF8

    Write-Output "Saved persistent caliper config to $installEnvPath"
    Write-Output "CALIPER_CLI_ROOT=$resolvedCli"
    Write-Output "OMNIPARSER_ROOT=$resolvedRuntime"
    if ($resolvedUied) {
        Write-Output "UIED_ROOT=$resolvedUied"
    }
    if ($resolvedPython) {
        Write-Output "CALIPER_PYTHON=$resolvedPython"
    }
    exit 0
}

$scriptDir = Split-Path -Parent $PSCommandPath

$cliRootCandidates = @(
    $env:CALIPER_CLI_ROOT,
    $env:CALIPER_ROOT,
    (Join-Path $scriptDir ".."),
    (Join-Path $scriptDir "..\OmniParser"),
    (Join-Path $scriptDir "..\omni-parser\OmniParser"),
    (Join-Path $scriptDir "..\caliper-parser\OmniParser"),
    (Join-Path $HOME "ai\op-cli"),
    (Join-Path $HOME "ai\omni-parser\OmniParser"),
    (Join-Path $HOME "ai\caliper-parser\OmniParser"),
    (Join-Path $HOME "OmniParser"),
    (Join-Path $HOME "src\OmniParser"),
    (Join-Path $HOME "projects\OmniParser")
)

$cliRoot = Find-CliRoot -Candidates $cliRootCandidates
if (-not $cliRoot) {
    Write-Error "caliper: unable to locate CLI root (expected <root>/cli/caliper.py)"
    exit 2
}

$runtimeRootCandidates = @(
    $env:OMNIPARSER_ROOT,
    $env:CALIPER_RUNTIME_ROOT,
    $cliRoot,
    (Join-Path $cliRoot "..\OmniParser"),
    (Join-Path $cliRoot "..\omni-parser\OmniParser"),
    (Join-Path $cliRoot "..\caliper-parser\OmniParser"),
    (Join-Path $HOME "ai\omni-parser\OmniParser"),
    (Join-Path $HOME "ai\caliper-parser\OmniParser"),
    (Join-Path $HOME "OmniParser"),
    (Join-Path $HOME "src\OmniParser"),
    (Join-Path $HOME "projects\OmniParser")
)

$runtimeRoot = Find-RuntimeRoot -Candidates $runtimeRootCandidates
if (-not $runtimeRoot) {
    Write-Error "caliper: unable to locate OmniParser runtime root (expected <root>/util/utils.py)"
    exit 2
}

$cliEntry = Join-Path $cliRoot "cli\caliper.py"

$pythonBin = $null
if ($env:CALIPER_PYTHON -and (Test-Path -LiteralPath $env:CALIPER_PYTHON -PathType Leaf)) {
    $pythonBin = $env:CALIPER_PYTHON
}
elseif ($env:VIRTUAL_ENV -and (Test-Path -LiteralPath (Join-Path $env:VIRTUAL_ENV "Scripts\python.exe") -PathType Leaf)) {
    $pythonBin = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
}
elseif ($env:CONDA_PREFIX -and (Test-Path -LiteralPath (Join-Path $env:CONDA_PREFIX "python.exe") -PathType Leaf)) {
    $pythonBin = Join-Path $env:CONDA_PREFIX "python.exe"
}
elseif (Test-Path -LiteralPath (Join-Path $runtimeRoot ".venv\Scripts\python.exe") -PathType Leaf) {
    $pythonBin = Join-Path $runtimeRoot ".venv\Scripts\python.exe"
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonBin = (Get-Command python).Source
}
elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonBin = (Get-Command py).Source
}
else {
    Write-Error "caliper: unable to find a usable Python interpreter"
    exit 2
}

if (-not $env:CALIPER_MODEL_DIR) {
    $defaultModelDir = Join-Path $runtimeRoot "weights"
    if (Test-Path -LiteralPath $defaultModelDir -PathType Container) {
        $env:CALIPER_MODEL_DIR = $defaultModelDir
    }
}

$env:OMNIPARSER_ROOT = $runtimeRoot
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$cliRoot;$runtimeRoot;$($env:PYTHONPATH)"
}
else {
    $env:PYTHONPATH = "$cliRoot;$runtimeRoot"
}

& $pythonBin $cliEntry @RemainingArgs
exit $LASTEXITCODE
