[CmdletBinding()]
param(
    [ValidateSet('Start', 'Status', 'ConfigureCodex')]
    [string]$Mode = 'Start',
    [switch]$Build,
    [ValidateRange(1, 65535)]
    [int]$ProxyPort = 4100,
    [ValidateRange(1, 65535)]
    [int]$PanelPort = 4101
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sourceBinary = Join-Path $projectRoot 'target\release\3api.exe'
$packagedBinary = Join-Path $projectRoot '3api.exe'
$rustBinary = if (Test-Path -LiteralPath $packagedBinary -PathType Leaf) { $packagedBinary } else { $sourceBinary }
$configPath = Join-Path $projectRoot 'providers.toml'
$runtimePath = Join-Path $projectRoot 'data\rust'
$workerPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Get-PanelHealth {
    # Unauthenticated loopback health contains no credentials or project data.
    try {
        $result = Invoke-RestMethod -Uri "http://127.0.0.1:$PanelPort/health" -TimeoutSec 2 -MaximumRedirection 0
        return ($result.status -eq 'ok' -and $result.application -eq '3api-rust-panel')
    } catch { return $false }
}

function Test-PublicPorts {
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return @($listeners | Where-Object { $_.Port -in @($ProxyPort, $PanelPort) }).Count -gt 0
}

try {
    if ($ProxyPort -eq $PanelPort) { throw 'Proxy i panel wymagaja roznych portow.' }
    if ($Mode -eq 'Status') {
        if (Get-PanelHealth) {
            Write-Host "Rust panel: http://127.0.0.1:$PanelPort/ui/ (online)"
            Write-Host "Rust API:   http://127.0.0.1:$ProxyPort/v1"
            Write-Host 'Stan providerow i zadan sprawdz w panelu. Sesja lokalna powstaje automatycznie.'
            exit 0
        }
        Write-Host "Panel Rust nie odpowiada na 127.0.0.1:$PanelPort. Uruchom start.bat."
        exit 1
    }

    if ($Mode -eq 'Start' -and (Test-PublicPorts)) {
        if (Get-PanelHealth) {
            Write-Host "Panel Rust juz dziala: http://127.0.0.1:$PanelPort/ui/"
            Write-Host 'Aby go zaktualizowac, zakoncz prace i nacisnij Ctrl+C w jego oknie.'
            exit 0
        }
        throw "Port $ProxyPort lub $PanelPort jest zajety. Sprawdz jego wlasciciela; skrypt nie zatrzymuje innych procesow."
    }

    Push-Location -LiteralPath $projectRoot
    try {
        # bootstrap repairs copied virtual environments and installs worker dependencies.
        & (Join-Path $projectRoot 'bootstrap.bat')
        if ($LASTEXITCODE -ne 0) { throw 'Przygotowanie prywatnego procesu Python nie powiodlo sie.' }
        & $workerPython (Join-Path $projectRoot 'manage.py') init
        if ($LASTEXITCODE -ne 0) { throw 'Przygotowanie konfiguracji i lokalnego tokenu nie powiodlo sie.' }

        if ($Mode -eq 'ConfigureCodex') {
            & $workerPython (Join-Path $projectRoot 'manage.py') configure-codex
            if ($LASTEXITCODE -ne 0) { throw 'Nie udalo sie skonfigurowac Codexa.' }
            exit 0
        }

        $sourceNeedsBuild = $false
        if ($rustBinary -eq $sourceBinary -and (Test-Path -LiteralPath $sourceBinary -PathType Leaf)) {
            $builtAt = (Get-Item -LiteralPath $sourceBinary).LastWriteTimeUtc
            $sourceInputs = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'src') -Filter '*.rs' -File -Recurse -ErrorAction SilentlyContinue)
            foreach ($inputName in @('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml')) {
                $inputPath = Join-Path $projectRoot $inputName
                if (Test-Path -LiteralPath $inputPath -PathType Leaf) { $sourceInputs += Get-Item -LiteralPath $inputPath }
            }
            $sourceNeedsBuild = @($sourceInputs | Where-Object { $_.LastWriteTimeUtc -gt $builtAt }).Count -gt 0
        }
        if ($Build -or $sourceNeedsBuild -or -not (Test-Path -LiteralPath $rustBinary -PathType Leaf)) {
            if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'Cargo.toml') -PathType Leaf)) {
                throw 'Wydanie nie zawiera zrodel Rust. Rozpakuj kompletne wydanie z 3api.exe lub buduj z katalogu zrodel.'
            }
            $rustBinary = $sourceBinary
            # Never overwrite an executable still owned by a running installation.
            foreach ($process in @(Get-Process -Name '3api' -ErrorAction SilentlyContinue)) {
                $processPath = $null
                try { $processPath = $process.Path } catch {}
                if ($processPath -and [System.String]::Equals($processPath, $rustBinary, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Ta binarka Rust jest uruchomiona. Najpierw zatrzymaj ja przez Ctrl+C, potem zbuduj aktualizacje.'
                }
            }
            $cargoCommand = Get-Command cargo -ErrorAction SilentlyContinue
            if (-not $cargoCommand) {
                $cargoCandidate = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cargo\bin\cargo.exe'
                if (Test-Path -LiteralPath $cargoCandidate -PathType Leaf) { $cargoCommand = Get-Command $cargoCandidate }
            }
            if (-not $cargoCommand) { throw 'Brak Rust/Cargo. Zainstaluj Rust przez rustup.rs (na Windows takze MSVC Build Tools), potem ponow start.bat.' }
            Write-Host 'Building Rust: cargo build --locked --release'
            & $cargoCommand.Source build --locked --release
            if ($LASTEXITCODE -ne 0) { throw 'Kompilacja Rust nie powiodla sie.' }
        }

        & $rustBinary check --config $configPath
        if ($LASTEXITCODE -ne 0) { throw 'Konfiguracja nie przeszla kontroli Rust.' }
        Write-Host "Panel: http://127.0.0.1:$PanelPort/ui/"
        Write-Host 'Panel otwiera sie bez tokenu; wewnetrzna autoryzacja API jest przygotowana automatycznie.'
        Write-Host 'Pozostaw okno otwarte. Ctrl+C zatrzymuje te instancje i jej prywatny proces roboczy.'
        & $rustBinary serve --config $configPath --data-dir $runtimePath --project-dir $projectRoot --proxy-port $ProxyPort --panel-port $PanelPort
        exit $LASTEXITCODE
    } finally { Pop-Location }
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
