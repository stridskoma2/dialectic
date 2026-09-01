$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSCommandPath
$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
$pythonw = Join-Path $repositoryRoot '.venv\Scripts\pythonw.exe'

if ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $pythonw)) {
    & $python -c 'import PySide6' 2>$null
    if ($LASTEXITCODE -eq 0) {
        Start-Process -FilePath $pythonw -ArgumentList '-m dialectic.desktop' -WorkingDirectory $repositoryRoot
        exit 0
    }
    Start-Process -FilePath $pythonw -ArgumentList '-m dialectic.ui' -WorkingDirectory $repositoryRoot
    exit 0
}

if (Test-Path -LiteralPath $wsl) {
    & $wsl -d Ubuntu -- bash -lc 'test -x ~/.local/share/dialectic/venv/bin/python' 2>$null
    if ($LASTEXITCODE -eq 0) {
        $previousBridgeDirectory = [Environment]::GetEnvironmentVariable('DIALECTIC_WINDOWS_BRIDGE_DIR', 'Process')
        $previousBridgeToken = [Environment]::GetEnvironmentVariable('DIALECTIC_WINDOWS_BRIDGE_TOKEN', 'Process')
        $previousWslEnv = [Environment]::GetEnvironmentVariable('WSLENV', 'Process')
        try {
            $bridgeReady = $false
            if (Test-Path -LiteralPath $pythonw) {
                $bridgeDirectory = Join-Path $repositoryRoot ".git\dialectic-ui-bridge\$([Guid]::NewGuid().ToString('N'))"
                New-Item -ItemType Directory -Path $bridgeDirectory -Force | Out-Null
                if ($bridgeDirectory -notmatch '^([A-Za-z]):\\(.*)$') {
                    throw "Cannot translate the bridge directory for WSL: $bridgeDirectory"
                }
                $bridgeDirectoryWsl = "/mnt/$($Matches[1].ToLowerInvariant())/$($Matches[2].Replace('\', '/'))"
                $bridgeToken = [Guid]::NewGuid().ToString('N')
                $env:DIALECTIC_WINDOWS_BRIDGE_TOKEN = $bridgeToken
                $bridgeArguments = "-m dialectic.windows_bridge --directory `"$bridgeDirectory`""
                $bridgeProcess = Start-Process -FilePath $pythonw -ArgumentList $bridgeArguments -WorkingDirectory $repositoryRoot -PassThru
                for ($attempt = 0; $attempt -lt 30; $attempt++) {
                    if (Test-Path -LiteralPath (Join-Path $bridgeDirectory '.ready')) {
                        $bridgeReady = $true
                        break
                    }
                    Start-Sleep -Milliseconds 100
                }
                if ($bridgeReady) {
                    $env:DIALECTIC_WINDOWS_BRIDGE_DIR = $bridgeDirectoryWsl
                    $bridgeWslEnv = 'DIALECTIC_WINDOWS_BRIDGE_DIR/u:DIALECTIC_WINDOWS_BRIDGE_TOKEN/u'
                    $env:WSLENV = if ([string]::IsNullOrEmpty($previousWslEnv)) {
                        $bridgeWslEnv
                    } else {
                        "$previousWslEnv`:$bridgeWslEnv"
                    }
                } else {
                    Stop-Process -Id $bridgeProcess.Id -ErrorAction SilentlyContinue
                    Remove-Item -LiteralPath $bridgeDirectory -ErrorAction SilentlyContinue
                }
            }
            $arguments = '-d Ubuntu -- bash -lc "exec ~/.local/share/dialectic/venv/bin/python -m dialectic.ui"'
            Start-Process -FilePath $wsl -ArgumentList $arguments -WorkingDirectory $repositoryRoot -WindowStyle Hidden
        } finally {
            [Environment]::SetEnvironmentVariable('DIALECTIC_WINDOWS_BRIDGE_DIR', $previousBridgeDirectory, 'Process')
            [Environment]::SetEnvironmentVariable('DIALECTIC_WINDOWS_BRIDGE_TOKEN', $previousBridgeToken, 'Process')
            [Environment]::SetEnvironmentVariable('WSLENV', $previousWslEnv, 'Process')
        }
        exit 0
    }
}

$installedDesktop = Get-Command dialectic-desktop.exe -ErrorAction SilentlyContinue
if ($null -ne $installedDesktop) {
    & $installedDesktop.Source --check 2>$null
    if ($LASTEXITCODE -eq 0) {
        Start-Process -FilePath $installedDesktop.Source -WorkingDirectory $repositoryRoot
        exit 0
    }
}

$installedWeb = Get-Command dialectic-ui.exe -ErrorAction SilentlyContinue
if ($null -ne $installedWeb) {
    Start-Process -FilePath $installedWeb.Source -WorkingDirectory $repositoryRoot
    exit 0
}

Write-Error 'Dialectic is not installed in Ubuntu WSL or the Windows .venv. Follow the README install steps, including the desktop extra for the native UI, then launch it again.'
