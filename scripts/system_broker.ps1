# Windows Helper Plus - privileged read-only evidence broker.
# Runs as NT AUTHORITY\SYSTEM from a protected install directory (Program Files).
# Protocol: operator writes request JSON into ProgramData requests dir; this broker
# processes fixed operations only, never executes arbitrary commands.
# ASCII-only source on purpose so Windows PowerShell 5.1 can parse it under any locale.

param(
    [string]$DataRoot = (Join-Path $env:ProgramData 'WindowsHelperPlus\SystemBroker'),
    [switch]$Once
)

$ErrorActionPreference = 'Continue'
$scriptVersion = '1.0'
$requestsDir = Join-Path $DataRoot 'requests'
$responsesDir = Join-Path $DataRoot 'responses'
$logsDir = Join-Path $DataRoot 'logs'
foreach ($dir in @($DataRoot, $requestsDir, $responsesDir, $logsDir)) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$brokerName = $identity.Name
$brokerSid = $identity.User.Value
$startedAt = (Get-Date).ToString('o')
$statusPath = Join-Path $responsesDir 'broker-status.json'
$logPath = Join-Path $logsDir 'broker.log'
$processed = 0

function Write-BrokerLog([string]$Message) {
    try {
        $line = (Get-Date).ToString('o') + ' ' + $Message
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    } catch { }
}

function Write-JsonAtomic([string]$Path, $Object) {
    $json = $Object | ConvertTo-Json -Depth 10 -Compress
    $tmp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Write-BrokerStatus {
    $status = [ordered]@{
        version = 1
        service = 'WindowsHelperPlusSystemBroker'
        identity = $brokerName
        sid = $brokerSid
        pid = $PID
        started_at = $startedAt
        updated_at = (Get-Date).ToString('o')
        processed = $processed
        computer = $env:COMPUTERNAME
    }
    try { Write-JsonAtomic $statusPath $status } catch { }
}

function Write-BrokerResponse([string]$Nonce, [bool]$Ok, $Results, [string]$ErrorMessage) {
    $payload = [ordered]@{
        version = 1
        nonce = $Nonce
        ok = $Ok
        broker = [ordered]@{
            identity = $brokerName
            sid = $brokerSid
            pid = $PID
            collected_at = (Get-Date).ToString('o')
        }
        results = @($Results)
        error = $ErrorMessage
    }
    $path = Join-Path $responsesDir ('resp-' + $Nonce + '.json')
    Write-JsonAtomic $path $payload
}

function Get-PriviledgedFileEvidence([string]$Target) {
    $file = Get-Item -LiteralPath $Target -ErrorAction Stop
    if ($file.PSIsContainer) {
        throw 'Target is a directory; only files are supported.'
    }
    $signature = $null
    $signatureError = $null
    try {
        $signature = Get-AuthenticodeSignature -LiteralPath $Target -ErrorAction Stop
    } catch {
        $signatureError = $_.Exception.Message
    }
    $hash = $null
    $hashError = $null
    try {
        $hash = (Get-FileHash -LiteralPath $Target -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    } catch {
        $hashError = $_.Exception.Message
    }
    $owner = $null
    $aclError = $null
    try {
        $acl = Get-Acl -LiteralPath $Target -ErrorAction Stop
        $owner = $acl.Owner
    } catch {
        $aclError = $_.Exception.Message
    }
    $zone = $null
    try {
        $zone = (Get-Content -LiteralPath $Target -Stream Zone.Identifier -ErrorAction Stop | Out-String).Trim()
    } catch {
        $zone = $null
    }
    $vi = $file.VersionInfo
    $data = [ordered]@{
        path = [string]$file.FullName
        name = [string]$file.Name
        extension = [string]$file.Extension
        size_bytes = [long]$file.Length
        created = if ($file.CreationTime) { $file.CreationTime.ToString('o') } else { $null }
        modified = if ($file.LastWriteTime) { $file.LastWriteTime.ToString('o') } else { $null }
        attributes = [string]$file.Attributes
        sha256 = $hash
        sha256_error = $hashError
        company = [string]$vi.CompanyName
        product = [string]$vi.ProductName
        description = [string]$vi.FileDescription
        file_version = [string]$vi.FileVersion
        product_version = [string]$vi.ProductVersion
        original_filename = [string]$vi.OriginalFilename
        signature_status = if ($signature) { [string]$signature.Status } else { 'CheckFailed' }
        signature_status_message = if ($signature) { [string]$signature.StatusMessage } else { $signatureError }
        signer_subject = if ($signature -and $signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { $null }
        signer_issuer = if ($signature -and $signature.SignerCertificate) { [string]$signature.SignerCertificate.Issuer } else { $null }
        signer_thumbprint = if ($signature -and $signature.SignerCertificate) { [string]$signature.SignerCertificate.Thumbprint } else { $null }
        cert_not_before = if ($signature -and $signature.SignerCertificate) { $signature.SignerCertificate.NotBefore.ToString('yyyy-MM-dd') } else { $null }
        cert_not_after = if ($signature -and $signature.SignerCertificate) { $signature.SignerCertificate.NotAfter.ToString('yyyy-MM-dd') } else { $null }
        is_os_binary = if ($signature) { [bool]$signature.IsOSBinary } else { $null }
        zone_identifier = $zone
        owner = $owner
        acl_error = $aclError
        collector_identity = $brokerName
        collector_sid = $brokerSid
    }
    $summary = 'SYSTEM broker: ' + $file.Name + '; signature=' + [string]$data.signature_status + '; sha256=' + [string]$hash
    return [ordered]@{
        tool = 'system_broker_inspect'
        ok = [bool]($hash -ne $null)
        summary = $summary
        error = if ($hashError) { [string]$hashError } else { $null }
        source = 'Windows Helper Plus SYSTEM Broker'
        native_command = 'Get-Item / Get-FileHash / Get-AuthenticodeSignature / Get-Acl (SYSTEM)'
        data = $data
    }
}

function Process-RequestFile([string]$RequestPath) {
    $fileName = [IO.Path]::GetFileName($RequestPath)
    if ($fileName -notmatch '^req-([0-9a-f]{32})\.json$') {
        try { Remove-Item -LiteralPath $RequestPath -Force } catch { }
        return
    }
    $nonce = $matches[1]
    try {
        $info = Get-Item -LiteralPath $RequestPath -ErrorAction Stop
        if ($info.Length -gt 65536) {
            throw 'Request too large.'
        }
        $raw = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 -ErrorAction Stop
        $request = $raw | ConvertFrom-Json -ErrorAction Stop
        if ([int]$request.version -ne 1) { throw 'Unsupported protocol version.' }
        if ([string]$request.nonce -ne $nonce) { throw 'Nonce mismatch.' }
        $operation = [string]$request.operation
        if ($operation -eq 'ping') {
            Write-BrokerResponse $nonce $true @() $null
            Write-BrokerLog ('ping from request ' + $nonce)
        } elseif ($operation -eq 'inspect_target') {
            $target = [string]$request.target
            if ([string]::IsNullOrWhiteSpace($target)) { throw 'Missing target.' }
            if ($target.Length -gt 1024) { throw 'Target path too long.' }
            if (-not [IO.Path]::IsPathRooted($target)) { throw 'Target must be an absolute path.' }
            $full = [IO.Path]::GetFullPath($target)
            if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw 'Target file does not exist.' }
            # Defense in depth: never expose metadata/hashes of credential or
            # audit-log stores through the SYSTEM channel.
            $winRoot = [IO.Path]::GetFullPath($env:SystemRoot).TrimEnd('\') + '\'
            $blockedPrefixes = @(
                ($winRoot + 'System32\config\'),
                ($winRoot + 'System32\LogFiles\'),
                ($winRoot + 'System32\winevt\Logs\'),
                ($winRoot + 'NTDS\')
            )
            foreach ($prefix in $blockedPrefixes) {
                if ($full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Target path is excluded from privileged inspection.'
                }
            }
            $evidence = Get-PriviledgedFileEvidence $full
            Write-BrokerResponse $nonce $true @($evidence) $null
            Write-BrokerLog ('inspected ' + $full + ' for request ' + $nonce)
        } else {
            throw 'Unsupported operation.'
        }
        $script:processed = $script:processed + 1
        try { Remove-Item -LiteralPath $RequestPath -Force } catch { }
    } catch {
        try {
            Write-BrokerResponse $nonce $false @() ([string]$_.Exception.Message)
            Write-BrokerLog ('request failed: ' + [string]$_.Exception.Message)
        } catch { }
        try { Remove-Item -LiteralPath $RequestPath -Force } catch { }
    }
}

function Remove-StaleFiles {
    $cutoff = (Get-Date).AddHours(-1)
    foreach ($set in @(
        @{ Dir = $requestsDir; Filter = 'req-*.json' },
        @{ Dir = $responsesDir; Filter = 'resp-*.json' }
    )) {
        try {
            Get-ChildItem -LiteralPath $set.Dir -Filter $set.Filter -File -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTime -lt $cutoff } |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
        } catch { }
    }
}

Write-BrokerLog ('broker starting; identity=' + $brokerName + '; sid=' + $brokerSid + '; pid=' + $PID)
Remove-StaleFiles
$lastCleanup = Get-Date
while ($true) {
    Write-BrokerStatus
    try {
        Get-ChildItem -LiteralPath $requestsDir -Filter 'req-*.json' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime |
            ForEach-Object { Process-RequestFile $_.FullName }
    } catch {
        Write-BrokerLog ('loop error: ' + [string]$_.Exception.Message)
    }
    if (((Get-Date) - $lastCleanup).TotalMinutes -gt 30) {
        Remove-StaleFiles
        $lastCleanup = Get-Date
    }
    if ($Once) { break }
    Start-Sleep -Seconds 1
}
Write-BrokerLog 'broker stopped'
