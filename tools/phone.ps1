# Подготовка входа с телефона (вызывается из phone.bat).
#  1) находит IPv4-адрес ноутбука в локальной сети (интерфейс со шлюзом по умолчанию);
#  2) записывает его в .env (PHONE_HOST) — на этот адрес Caddy выпустит HTTPS-сертификат;
#  3) открывает порт HTTPS в брандмауэре Windows (один раз, с запросом прав администратора).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"
$port = 8443

$cfg = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
       Sort-Object { $_.IPv4DefaultGateway.RouteMetric + $_.NetAdapter.InterfaceMetric } | Select-Object -First 1
if (-not $cfg) {
    Write-Host "Ноутбук не подключён к сети (Wi-Fi или кабель). Подключитесь к той же сети, что и телефон."
    exit 1
}
$ip = ($cfg.IPv4Address | Select-Object -First 1).IPAddress
Write-Host "Адрес ноутбука в сети «$($cfg.InterfaceAlias)»: $ip"

# PHONE_HOST в .env
$lines = @()
if (Test-Path $envFile) { $lines = Get-Content $envFile -Encoding UTF8 }
$found = $false
$lines = $lines | ForEach-Object {
    if ($_ -match '^\s*PHONE_HOST\s*=') { $found = $true; "PHONE_HOST=$ip" }
    elseif ($_ -match '^\s*HTTPS_PORT\s*=\s*(\d+)') { $script:port = [int]$Matches[1]; $_ }
    else { $_ }
}
if (-not $found) { $lines += "PHONE_HOST=$ip" }
# UTF-8 без BOM — docker compose читает .env построчно
[IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object Text.UTF8Encoding $false))

# Правило брандмауэра
$rule = "ГИС ВОЛС HTTPS $port"
if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
    Write-Host "Открываю порт $port в брандмауэре (Windows спросит разрешение администратора)..."
    $cmd = "New-NetFirewallRule -DisplayName '$rule' -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Any"
    try {
        Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "-NoProfile", "-Command", $cmd
    } catch {
        Write-Host "Порт не открыт (нет прав администратора). Телефон может не достучаться до ноутбука."
    }
}
Write-Output "IP=$ip"
