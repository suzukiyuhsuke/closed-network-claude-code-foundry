[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$FunctionAppName,
    [switch]$LocalBuild,
    [ValidateSet('Entra', 'FunctionKey')][string]$AuthenticationMode = 'Entra',
    [string]$UvCommand = 'uv'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Path $dist -Force | Out-Null
$zip = Join-Path $dist $(if ($LocalBuild) { 'mcp-local.zip' } else { 'mcp.zip' })
$source = Join-Path $root 'src\mcp_server'
$hostConfig = if ($AuthenticationMode -eq 'FunctionKey') {
    Join-Path $source 'host.function-key.json'
} else {
    Join-Path $source 'host.json'
}
$files = @('server.py', 'search.py', 'requirements.txt') |
    ForEach-Object { Join-Path $source $_ }
$buildMode = if ($LocalBuild) { 'local Linux dependency packaging' } else { 'remote build' }
if ($PSCmdlet.ShouldProcess("$SubscriptionId/$ResourceGroup/$FunctionAppName", "Deploy MCP package with $buildMode")) {
    $staging = Join-Path $dist "mcp-stage-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        Copy-Item -LiteralPath $files -Destination $staging
        Copy-Item -LiteralPath $hostConfig -Destination (Join-Path $staging 'host.json')
        if ($LocalBuild) {
            $packages = Join-Path $staging '.python_packages\lib\site-packages'
            # uv evaluates dependency markers for Linux, unlike pip --platform on Windows.
            & $UvCommand pip install --target $packages `
                --python-platform x86_64-manylinux2014 --python-version 3.12 `
                --only-binary=:all: --no-python-downloads `
                --requirement (Join-Path $source 'requirements.txt') --quiet
            if ($LASTEXITCODE -ne 0) { throw 'Linux Python 3.12 dependency packaging failed; nothing was deployed.' }
        }
        if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }
        # CreateFromDirectory includes .python_packages and keeps the selected auth host config as host.json.
        [System.IO.Compression.ZipFile]::CreateFromDirectory($staging, $zip)
    } finally {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
    $remoteBuild = if ($LocalBuild) { 'false' } else { 'true' }
    & az functionapp deployment source config-zip --subscription $SubscriptionId `
        --resource-group $ResourceGroup --name $FunctionAppName --src $zip `
        --build-remote $remoteBuild --only-show-errors --output none
    if ($LASTEXITCODE -ne 0) { throw 'Functions deployment failed. Check Storage policy, Private Endpoint/DNS, VNet integration, RBAC and deployment logs. See docs/public-poc.ja.md.' }
    Write-Host 'Package deployed. Complete the authentication and MCP acceptance checks in the runbook.'
}
