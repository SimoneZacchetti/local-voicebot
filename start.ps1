<#
One command from a fresh clone to a running voicebot on Windows 10/11 x64: .\start.cmd

Idempotent: installs what is missing (VC++ runtime, uv, Ollama, the LLM, Python deps), creates .env,
runs the environment check the first time, then starts the server and opens the browser.
  .\start.cmd            setup if needed, then run
  .\start.cmd -Check     also re-run scripts/check_env.py   (alias: --check)
  .\start.cmd -NoOpen    do not open the browser            (alias: --no-open)
#>
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoOpen,
    [switch]$Help,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # PS 5.1: Invoke-WebRequest is very slow with the progress bar
Set-Location -LiteralPath $PSScriptRoot
[Environment]::CurrentDirectory = $PSScriptRoot   # .NET file APIs resolve relative paths against this
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$Utf8 = New-Object System.Text.UTF8Encoding $false
try { [Console]::OutputEncoding = $Utf8 } catch { }
$env:PYTHONUTF8 = '1'   # pocket-tts reads its UTF-8 YAML configs with the locale encoding (cp1252) otherwise
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'   # without Developer Mode, huggingface_hub copies instead of symlinking

# Ollama on Windows runs GGUF models (the Mac default *-nvfp4 needs MLX). The config default for
# Windows is qwen3.6:35b-a3b; smaller PCs get these, written into a new .env.
$MidModel = 'qwen3.5:9b'    # 16 GB RAM or 8-12 GB VRAM
$SmallModel = 'qwen3.5:4b'  # low end

$doCheck = [bool]$Check
$doOpen = -not $NoOpen
foreach ($a in @($Rest | Where-Object { $_ })) {
    switch ($a) {
        '--check' { $doCheck = $true }
        '--no-open' { $doOpen = $false }
        { $_ -in '-h', '--help', '/?' } { $Help = $true }
        default { Write-Host "Unknown option: $a"; exit 2 }
    }
}
if ($Help) { Get-Content -LiteralPath $PSCommandPath -TotalCount 8 | Select-Object -Skip 1; exit 0 }

function Step([string]$m) { Write-Host ''; Write-Host '==> ' -ForegroundColor Blue -NoNewline; Write-Host $m }
function Warn([string]$m) { Write-Host 'WARN ' -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Die([string]$m) { Write-Host 'ERROR ' -ForegroundColor Red -NoNewline; Write-Host $m; exit 1 }
function Have([string]$name) { [bool](Get-Command $name -CommandType Application -ErrorAction SilentlyContinue) }
function Add-PathFront([string]$dir) {
    if ((Test-Path -LiteralPath $dir) -and -not (($env:Path -split ';') -contains $dir)) { $env:Path = "$dir;$env:Path" }
}
function Update-Path {
    # winget and installers update the registry PATH, not this process: re-read it.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user, $env:Path) | Where-Object { $_ }) -join ';'
    Add-PathFront (Join-Path $HOME '.local\bin')
}
function Invoke-Quiet([scriptblock]$sb) {
    # PS 5.1 turns redirected native stderr into errors that 'Stop' makes fatal: relax it locally.
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $sb 2>$null } catch { $null } finally { $ErrorActionPreference = $old }
}
function Test-Url([string]$u) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $u | Out-Null; $true } catch { $false }
}
function Install-WingetPackage([string]$id) {
    if (-not (Have 'winget')) { return $false }
    # Out-Host: native output inside a function would otherwise become part of its return value.
    & winget install -e --id $id --source winget --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $code = $LASTEXITCODE
    Update-Path
    return ($code -eq 0 -or $code -eq -1978335189)   # 0x8A15002B: already installed, no newer version
}
function Protect-File([string]$path) {
    # Equivalent of chmod 600: drop inherited ACEs (e.g. Authenticated Users on C:\src), keep only this user.
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $path /inheritance:r /grant:r "*${sid}:(M)" | Out-Null
    if ($LASTEXITCODE -ne 0) { Warn "could not restrict the permissions of $path (icacls exit $LASTEXITCODE)." }
}
function Get-RegValue([string]$key, [string]$name) {
    $p = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $null }
    $prop = $p.PSObject.Properties[$name]
    if ($prop) { $prop.Value } else { $null }
}
function Get-OsArch {
    # Native OS architecture, also from an x64 PowerShell emulated on ARM64.
    try { return [string][Runtime.InteropServices.RuntimeInformation]::OSArchitecture } catch { }
    $a = Get-RegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' 'PROCESSOR_ARCHITECTURE'
    if ($a -eq 'AMD64') { 'X64' } elseif ($a) { [string]$a } else { 'Unknown' }
}
function Get-RamGB {
    $bytes = [double](Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
    # TotalPhysicalMemory excludes hardware-reserved memory (32 GB shows as ~31.7): prefer the installed modules.
    try {
        $sum = (Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum).Sum
        if ($sum -and [double]$sum -gt $bytes) { $bytes = [double]$sum }
    } catch { }
    [int][math]::Round($bytes / 1GB)
}
function Get-GpuVramGB {
    # Best effort. Win32_VideoController.AdapterRAM is a uint32 (caps at 4 GB): ask nvidia-smi, then the registry.
    if (Have 'nvidia-smi') {
        $lines = Invoke-Quiet { nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits }
        $mb = @($lines | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ })
        if ($mb.Count -gt 0) { return [int][math]::Round(($mb | Measure-Object -Maximum).Maximum / 1024) }
    }
    $best = 0
    $cls = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    foreach ($k in @(Get-ChildItem -LiteralPath $cls -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^\d{4}$' })) {
        $v = Get-RegValue $k.PSPath 'HardwareInformation.qwMemorySize'
        if ($v -is [byte[]] -and $v.Length -ge 8) { $v = [BitConverter]::ToUInt64($v, 0) }
        if ($v -is [ValueType]) { $best = [math]::Max($best, [math]::Round([double]$v / 1GB)) }
    }
    [int]$best
}
function Find-Ollama {
    Add-PathFront (Join-Path $env:LOCALAPPDATA 'Programs\Ollama')
    $c = Get-Command 'ollama' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { $c.Source } else { $null }
}

# --- Hardware ---
if (-not [Environment]::Is64BitOperatingSystem) { Die 'needs 64-bit Windows 10/11 (x64).' }
$arch = Get-OsArch
if ($arch -eq 'Arm64') {
    Warn 'Windows on ARM is untested: Python packages and Ollama run as x64 under emulation (slow). Trying anyway.'
} elseif ($arch -ne 'X64') {
    Die "needs Windows 10/11 on x64 (this is $arch)."
}
$build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
if ($build -lt 17763) { Die 'needs Windows 10 1809 or newer.' }
$ramGB = Get-RamGB
$vramGB = 0
try { $vramGB = Get-GpuVramGB } catch { }
Write-Host ("RAM {0} GB, GPU memory {1}" -f $ramGB, $(if ($vramGB) { "$vramGB GB" } else { 'unknown/none (CPU only)' }))
$suggested = $null   # $null: keep the config default (qwen3.6:35b-a3b)
if ($ramGB -lt 32 -and $vramGB -lt 24) {
    $suggested = if ($ramGB -ge 16 -or $vramGB -ge 8) { $MidModel } else { $SmallModel }
    Warn "$ramGB GB of RAM: the default LLM needs >= 32 GB. A new .env gets LLM_MODEL=$suggested (less accurate)."
}

$root = $PSScriptRoot
$pathHint = ''   # repeated if 'uv sync' fails
foreach ($od in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
    if ($od -and $root.StartsWith($od.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        # Keep the multi-GB .venv out of the synced folder; every later step uses 'uv run' from this process.
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA 'voicebot\.venv'
        $pathHint = " The project is inside OneDrive ($od): move it (e.g. C:\src\voicebot)."
        Warn ("the project is inside OneDrive ($od): .venv goes to $env:UV_PROJECT_ENVIRONMENT instead. " +
              "Running 'uv run' from another terminal needs the same UV_PROJECT_ENVIRONMENT. Better: move it (e.g. C:\src\voicebot).")
        break
    }
}
if ($root.Length -gt 80) {
    $long = (Get-RegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' 'LongPathsEnabled') -eq 1
    Warn ("long project path ($($root.Length) chars)" + $(if ($long) { '' } else { ' and Windows long paths disabled' }) +
          ": if 'uv sync' fails, move the folder (e.g. C:\src\voicebot).")
    $pathHint += " Long project path ($($root.Length) chars): move it (e.g. C:\src\voicebot)."
}

# --- Visual C++ runtime ---
# onnxruntime and PyTorch DLLs need msvcp140.dll >= 14.40: older ones crash natively inside std::mutex (no traceback).
function Test-VcRedist {
    foreach ($k in 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                   'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64') {
        $major = Get-RegValue $k 'Major'; $minor = Get-RegValue $k 'Minor'
        if ((Get-RegValue $k 'Installed') -eq 1 -and ($major -gt 14 -or ($major -eq 14 -and $minor -ge 40))) { return $true }
    }
    $false
}
if (-not (Test-VcRedist)) {
    Step 'Installing or updating the Microsoft Visual C++ runtime (accept the administrator prompt)'
    $null = Install-WingetPackage 'Microsoft.VCRedist.2015+.x64'
    if (-not (Test-VcRedist)) {
        Die 'install the Visual C++ runtime from https://aka.ms/vs/17/release/vc_redist.x64.exe, then run .\start.cmd again.'
    }
}

# --- uv ---
Add-PathFront (Join-Path $HOME '.local\bin')
if (-not (Have 'uv')) {
    Step 'Installing uv'
    $null = Install-WingetPackage 'astral-sh.uv'
    if (-not (Have 'uv')) {
        # Separate process: the installer script must not run under this script's StrictMode.
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; irm https://astral.sh/uv/install.ps1 | iex"
        Update-Path
    }
    if (-not (Have 'uv')) { Die 'uv is not on PATH: open a new terminal and run .\start.cmd again.' }
}

# --- .env ---
$envFile = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Step 'Creating .env from .env.example (every value has a default; edit it later if you want)'
    $text = [IO.File]::ReadAllText((Join-Path $root '.env.example'), $Utf8)
    # MLX ids only work on Apple Silicon: let the config pick the ONNX defaults.
    $text = $text -replace '(?m)^((?:STT|WHISPER)_MODEL=mlx-community/)', '# $1'
    $text = $text -replace '(?m)^LLM_BASE_URL=http://localhost:', 'LLM_BASE_URL=http://127.0.0.1:'
    if ($suggested) {
        $line = "LLM_MODEL=$suggested   # chosen by start.ps1 for $ramGB GB of RAM"
        if ($text -match '(?m)^#?[ \t]*LLM_MODEL=') { $text = $text -replace '(?m)^#?[ \t]*LLM_MODEL=[^\r\n]*', $line }
        else { $text += "`n$line`n" }
    }
    if ($ramGB -lt 32) {
        # Each Pocket TTS language is a separate model: preload two, the others load on first use.
        $lang = $env:DEFAULT_LANG
        if (-not $lang -and $text -match '(?m)^DEFAULT_LANG=([A-Za-z]+)') { $lang = $Matches[1] }
        if (-not $lang) { $lang = 'it' }
        $preload = (@($lang.ToLower(), 'en') | Select-Object -Unique) -join ','
        $text = $text -replace '(?m)^POCKET_PRELOAD=[^\r\n]*', "POCKET_PRELOAD=$preload   # start.ps1: $ramGB GB of RAM"
    }
    [IO.File]::WriteAllText($envFile, $text, $Utf8)   # never Out-File/Set-Content: UTF-16/ANSI in PS 5.1
    Protect-File $envFile
    $doCheck = $true
}

# --- Python dependencies ---
Step 'Syncing Python dependencies'
& uv sync --locked
if ($LASTEXITCODE -ne 0) { Die "'uv sync --locked' failed (exit $LASTEXITCODE).$pathHint" }

# --- Ollama ---
$ollama = Find-Ollama
if (-not $ollama) {
    Step 'Installing Ollama'
    $null = Install-WingetPackage 'Ollama.Ollama'
    $ollama = Find-Ollama
    if (-not $ollama) { Die 'install Ollama from https://ollama.com/download/windows, then run .\start.cmd again.' }
}

# No double quotes in the code: PS 5.1 mangles them in arguments to native programs.
$py = 'from server.config import cfg; print(cfg.llm_provider, cfg.llm_model, ' +
      'cfg.llm_base_url.rstrip(''/'').removesuffix(''/v1''), cfg.port, cfg.remote_port if cfg.remote_enabled else 0, ' +
      'cfg.stt_backend, cfg.stt_model, sep=chr(10))'
$conf = @(& uv run --no-sync python -c $py)
if ($LASTEXITCODE -ne 0 -or $conf.Count -lt 7) { Die 'could not read the configuration (see the error above; check .env).' }
$conf = @($conf | Select-Object -Last 7 | ForEach-Object { "$_".Trim() })
$provider, $model, $llmUrl, $port, $remotePort, $sttBackend, $sttModel = $conf
$probe = $llmUrl -replace '//localhost([:/]|$)', '//127.0.0.1$1'

if ($model -match 'nvfp4|mxfp8|-mlx') {
    Die ("LLM_MODEL=$model only runs on Ollama for Mac (MLX). In .env, remove the LLM_MODEL line " +
         "(default qwen3.6:35b-a3b, >= 32 GB RAM) or set LLM_MODEL=$MidModel (16 GB) or $SmallModel.")
}
if ($sttBackend -eq 'onnx' -and $sttModel -like 'mlx-community/*') {
    Warn "STT_MODEL=$sttModel is an MLX model: remove the STT_MODEL line from .env to use the ONNX default."
}

if (-not (Test-Url "$probe/api/version")) {
    Step 'Starting Ollama'
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
    for ($i = 0; $i -lt 60 -and -not (Test-Url "$probe/api/version"); $i++) { Start-Sleep -Seconds 1 }
    if (-not (Test-Url "$probe/api/version")) { Die "Ollama is not answering at $llmUrl." }
}

if ($model -notmatch ':') { $model = "$model`:latest" }
if ($provider -eq 'ollama') {
    $names = @(& $ollama list | Select-Object -Skip 1 | ForEach-Object { ("$_".Trim() -split '\s+')[0] })
    if ($names -notcontains $model) {
        Step "Downloading $model (first time only; the default model is ~24 GB)"
        & $ollama pull $model
        if ($LASTEXITCODE -ne 0) { Die "'ollama pull $model' failed." }
    }
    # server/llm.py sends tools (home control) and think (hard questions): Ollama answers 400 if unsupported.
    try {
        $body = @{ model = $model } | ConvertTo-Json -Compress
        $show = Invoke-RestMethod -UseBasicParsing -Method Post -ContentType 'application/json' -Body $body -Uri "$probe/api/show"
        if ($show.PSObject.Properties['capabilities']) {
            $caps = @($show.capabilities)
            if ($caps -notcontains 'tools') { Warn "$model does not support tools: home control will not work." }
            if ($caps -notcontains 'thinking') { Warn "$model does not support thinking: set LLM_THINK=off in .env." }
        }
    } catch { }
}

# --- Environment check (downloads the STT/TTS models the first time) ---
if ($doCheck) {
    Step 'Checking the environment'
    & uv run --no-sync python scripts/check_env.py
    if ($LASTEXITCODE -ne 0) { Die 'fix the FAIL lines above, then run .\start.cmd again.' }
}

# --- Run ---
foreach ($p in @(@($port, $remotePort) | Where-Object { $_ -and $_ -ne '0' })) {
    $busy = $null
    try { $busy = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$p) -ErrorAction SilentlyContinue) } catch { }
    if ($busy) {
        $owner = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue)
        $who = if ($owner) { "$($owner.ProcessName), PID $($owner.Id)" } else { "PID $($busy[0].OwningProcess)" }
        Die "port $p is already in use by $who (another voicebot?). Stop it, or set PORT / REMOTE_PORT in .env."
    }
}
# 127.0.0.1, not localhost: the server listens on IPv4 only and Windows tries ::1 first.
$url = "http://127.0.0.1:$port"
$opener = $null
if ($doOpen) {
    $opener = Start-Job -ArgumentList $url -ScriptBlock {
        param($url)
        $ProgressPreference = 'SilentlyContinue'
        for ($i = 0; $i -lt 300; $i++) {
            try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "$url/healthz" | Out-Null } catch { Start-Sleep -Seconds 1; continue }
            $browser = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                         "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                         "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
                         "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
                         "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
                       Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
            if ($browser) { Start-Process -FilePath $browser -ArgumentList $url } else { Start-Process $url }
            return
        }
    }
}
Step "Starting the voicebot: $url opens when the models are loaded (Ctrl+C to stop)"
$code = 1
try {
    & uv run --no-sync python -m server
    $code = $LASTEXITCODE
} finally {
    if ($opener) { Remove-Job -Job $opener -Force -ErrorAction SilentlyContinue }
}
exit $code
