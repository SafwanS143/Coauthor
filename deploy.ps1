<#
.SYNOPSIS
    Package this project, upload it to S3, and redeploy it on the EC2 instance.

.DESCRIPTION
    There is no SSH on the box by design, so code travels via a private S3
    bucket and the redeploy is triggered through SSM Run Command.

    Secrets are deliberately excluded from the tarball. The Chrome profile in
    particular contains live Google session cookies and is uploaded separately,
    once, by -PushProfile.

.EXAMPLE
    .\deploy.ps1
    .\deploy.ps1 -PushProfile      # also send chrome-profile/ and credentials
#>
[CmdletBinding()]
param(
    [switch]$PushProfile,
    [string]$TerraformDir = "$PSScriptRoot\terraform"
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 writes a UTF-8 byte-order mark with -Encoding utf8.
# The AWS CLI rejects a BOM outright, and Python's json.load chokes on one too,
# so every JSON file this script produces goes through here instead.
function Write-Utf8NoBom([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-TfOutput([string]$Name) {
    $v = terraform -chdir="$TerraformDir" output -raw $Name 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($v)) {
        throw "Could not read terraform output '$Name'. Has 'terraform apply' been run?"
    }
    return $v.Trim()
}

Write-Host "==> reading terraform outputs" -ForegroundColor Cyan
$bucket     = Get-TfOutput 'deploy_bucket'
$instanceId = Get-TfOutput 'instance_id'
$region     = terraform -chdir="$TerraformDir" output -raw region 2>$null
if ([string]::IsNullOrWhiteSpace($region)) { $region = 'us-east-1' }
Write-Host "    bucket=$bucket instance=$instanceId region=$region"

# --- package -------------------------------------------------------------
# Everything the app needs to run, and nothing that identifies anybody.
$staging = Join-Path $env:TEMP "coauthor-deploy"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging -Force | Out-Null

$include = @(
    'presence_watcher.py', 'activity_poller.py', 'report.py', 'resolve_people.py',
    'db.py', 'server.py', 'requirements.txt', 'config.json', 'people_map.json'
)
foreach ($f in $include) {
    $p = Join-Path $PSScriptRoot $f
    if (Test-Path $p) { Copy-Item $p $staging }
    else { Write-Warning "skipping missing $f" }
}
Copy-Item (Join-Path $PSScriptRoot 'templates') $staging -Recurse

# The instance runs headless behind xvfb and must bind only to localhost.
$cfgPath = Join-Path $staging 'config.json'
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$cfg.headless  = $false
$cfg.web_host  = '127.0.0.1'
Write-Utf8NoBom $cfgPath ($cfg | ConvertTo-Json -Depth 10)

Write-Host "==> building app.tar.gz" -ForegroundColor Cyan
$tarball = Join-Path $env:TEMP 'app.tar.gz'
tar -czf $tarball -C $staging .
"    $([math]::Round((Get-Item $tarball).Length / 1KB)) KB"

Write-Host "==> uploading" -ForegroundColor Cyan
aws s3 cp $tarball "s3://$bucket/app.tar.gz" --region $region

# --- credentials and browser profile, only when asked --------------------
if ($PushProfile) {
    Write-Host "==> uploading credentials and browser profile" -ForegroundColor Yellow
    Write-Host "    (chrome-profile contains live Google session cookies)"
    $secretsTar = Join-Path $env:TEMP 'secrets.tar.gz'
    $secretFiles = @('service_account.json', 'client_secret.json', 'token.json') |
        Where-Object { Test-Path (Join-Path $PSScriptRoot $_) }

    # chrome-profile is deliberately NOT shipped from Windows. Chrome seals the
    # cookie-encryption key with DPAPI, tied to the Windows account, so Linux
    # Chromium reads the cookies as garbage and lands on a sign-in page. Worse,
    # copying it up overwrites a working profile that was signed in on the
    # instance. Sign in there instead -- see DEPLOY.md step 6.
    if (Test-Path (Join-Path $PSScriptRoot 'chrome-profile')) {
        if ($IsWindows -eq $false) {
            $secretFiles += 'chrome-profile'
        } else {
            Write-Host "    skipping chrome-profile (Windows profiles do not work on Linux)" -ForegroundColor DarkYellow
        }
    }
    if (-not $secretFiles) { throw "Nothing to push: no credentials found." }
    tar -czf $secretsTar -C $PSScriptRoot @secretFiles
    aws s3 cp $secretsTar "s3://$bucket/secrets.tar.gz" --region $region --sse AES256
    Remove-Item $secretsTar -Force
}

# --- redeploy -------------------------------------------------------------
Write-Host "==> waiting for first-boot setup to finish" -ForegroundColor Cyan
# The two tokens must not be substrings of one another. PowerShell's -match is
# a substring regex match, so a READY / NOTREADY pair silently reports ready.
$deadline = (Get-Date).AddMinutes(12)
$bootstrapped = $false
while (-not $bootstrapped) {
    $ready = aws ssm send-command --region $region --instance-ids $instanceId `
        --document-name "AWS-RunShellScript" `
        --parameters "commands=test -f /etc/coauthor.bootstrapped && echo BOOTSTRAP_OK || echo BOOTSTRAP_PENDING" `
        --query "Command.CommandId" --output text
    Start-Sleep -Seconds 8
    $readyOut = aws ssm list-command-invocations --command-id $ready --region $region --details `
        --query "CommandInvocations[0].CommandPlugins[0].Output" --output text

    if ($readyOut -match 'BOOTSTRAP_OK') { $bootstrapped = $true; break }

    if ((Get-Date) -gt $deadline) {
        Write-Warning "First-boot setup has not completed after 12 minutes."
        Write-Warning "  aws ssm start-session --target $instanceId --region $region"
        Write-Warning "  sudo tail -40 /var/log/coauthor-bootstrap.log"
        throw "Bootstrap did not finish -- deploying now would fail."
    }
    Write-Host "    still installing (apt upgrade and a 70 MB AWS CLI download)..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 20
}
Write-Host "    ready" -ForegroundColor DarkGray

Write-Host "==> running coauthor-deploy on the instance" -ForegroundColor Cyan
$commands = @()
if ($PushProfile) {
    $commands += "aws s3 cp s3://$bucket/secrets.tar.gz /tmp/secrets.tar.gz"
    $commands += "tar xzf /tmp/secrets.tar.gz -C /opt/coauthor/app"
    $commands += "shred -u /tmp/secrets.tar.gz"
    $commands += "chown -R coauthor:coauthor /opt/coauthor/app"
    $commands += "chmod -R go-rwx /opt/coauthor/app/chrome-profile 2>/dev/null || true"
    $commands += "aws s3 rm s3://$bucket/secrets.tar.gz"
}
# Strip to ASCII on the instance. systemd prints bullets and arrows, and the
# AWS CLI cannot encode those to a Windows console codepage -- it fails the
# whole call with a 'charmap' error after the deploy has already succeeded.
$commands += "/usr/local/bin/coauthor-deploy 2>&1 | tr -cd '\11\12\15\40-\176'"

# The whole request goes via a file. Windows PowerShell 5.1 mangles quotes when
# handing a JSON string to a native executable -- aws receives {commands:[...]}
# with every double quote stripped -- and file:// avoids the argument parser
# entirely. Forward slashes because the AWS CLI treats backslashes as escapes.
$requestPath = Join-Path $env:TEMP 'coauthor-ssm.json'
Write-Utf8NoBom $requestPath (@{
        InstanceIds  = @($instanceId)
        DocumentName = 'AWS-RunShellScript'
        Parameters   = @{ commands = $commands }
    } | ConvertTo-Json -Depth 6)

$requestUri = 'file://' + $requestPath.Replace('\', '/')
$cmdId = aws ssm send-command --region $region `
    --cli-input-json $requestUri `
    --query "Command.CommandId" --output text
Remove-Item $requestPath -Force -ErrorAction SilentlyContinue

if ([string]::IsNullOrWhiteSpace($cmdId) -or $cmdId -eq 'None') {
    throw "send-command returned no command id. Is the SSM agent online? Check: aws ssm describe-instance-information --region $region"
}

Write-Host "    command id $cmdId -- waiting" -ForegroundColor DarkGray
# The invocation takes a moment to register, so an empty status early on means
# 'not yet', not 'finished'.
$elapsed = 0
do {
    Start-Sleep -Seconds 6
    $elapsed += 6
    $status = aws ssm list-command-invocations --command-id $cmdId --region $region `
        --query "CommandInvocations[0].Status" --output text
    if ([string]::IsNullOrWhiteSpace($status) -or $status -eq 'None') {
        $status = if ($elapsed -lt 60) { 'Pending' } else { 'Failed' }
    }
    Write-Host "    $status" -ForegroundColor DarkGray
} while ($status -in @('Pending', 'InProgress', 'Delayed'))

aws ssm list-command-invocations --command-id $cmdId --region $region --details `
    --query "CommandInvocations[0].CommandPlugins[0].Output" --output text

if ($status -ne 'Success') {
    aws ssm list-command-invocations --command-id $cmdId --region $region --details `
        --query "CommandInvocations[0].CommandPlugins[0].StandardErrorContent" --output text
    Write-Error "Deploy finished with status: $status"
    exit 1
}
Write-Host "`n==> done" -ForegroundColor Green
terraform -chdir="$TerraformDir" output -raw dashboard_url
Write-Host ""
