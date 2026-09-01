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
        $bridgeEnvironment = ''
        if (Test-Path -LiteralPath $pythonw) {
            $bridgeDirectory = Join-Path $repositoryRoot ".git\dialectic-ui-bridge\$([Guid]::NewGuid().ToString('N'))"
            New-Item -ItemType Directory -Path $bridgeDirectory -Force | Out-Null
            if ($bridgeDirectory -notmatch '^([A-Za-z]):\\(.*)$') {
                throw "Cannot translate the bridge directory for WSL: $bridgeDirectory"
            }
            $bridgeDirectoryWsl = "/mnt/$($Matches[1].ToLowerInvariant())/$($Matches[2].Replace('\', '/'))"
            $bridgeToken = [Guid]::NewGuid().ToString('N')
            $bridgeArguments = "-m dialectic.windows_bridge --directory `"$bridgeDirectory`" --token $bridgeToken"
            $bridgeProcess = Start-Process -FilePath $pythonw -ArgumentList $bridgeArguments -WorkingDirectory $repositoryRoot -PassThru
            $bridgeReady = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                if (Test-Path -LiteralPath (Join-Path $bridgeDirectory '.ready')) {
                    $bridgeReady = $true
                    break
                }
                Start-Sleep -Milliseconds 100
            }
            if ($bridgeReady) {
                $quotedBridgeDirectory = "'" + $bridgeDirectoryWsl.Replace("'", "'`"'`"'") + "'"
                $bridgeEnvironment = "DIALECTIC_WINDOWS_BRIDGE_DIR=$quotedBridgeDirectory DIALECTIC_WINDOWS_BRIDGE_TOKEN=$bridgeToken "
            } else {
                Stop-Process -Id $bridgeProcess.Id -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $bridgeDirectory -ErrorAction SilentlyContinue
            }
        }
        $arguments = "-d Ubuntu -- bash -lc `"exec env $bridgeEnvironment~/.local/share/dialectic/venv/bin/python -m dialectic.ui`""
        Start-Process -FilePath $wsl -ArgumentList $arguments -WorkingDirectory $repositoryRoot -WindowStyle Hidden
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
