[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9-]{3,40}$')][string]$NamePrefix,
    [string]$StatePath = '.\entra.local.json',
    [string]$McpResourceUri
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($McpResourceUri) {
    $uri = $null
    if (-not [uri]::TryCreate($McpResourceUri, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
        $McpResourceUri.EndsWith('/') -or $uri.AbsolutePath -eq '/') {
        throw 'McpResourceUri must be the exact HTTPS MCP endpoint with a path, no credentials, query, fragment or trailing slash.'
    }
}

function Invoke-AzJson {
    param([string[]]$Arguments)
    $result = & az @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI failed: $($Arguments[0..1] -join ' ')" }
    if ($result) { return ($result -join "`n" | ConvertFrom-Json) }
}
function Set-GraphApplication {
    param([string]$ObjectId, [hashtable]$Body)
    $file = [IO.Path]::GetTempFileName()
    try {
        $Body | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $file -Encoding utf8
        $null = Invoke-AzJson @('rest', '--method', 'PATCH', '--url',
            "https://graph.microsoft.com/v1.0/applications/$ObjectId", '--body', "@$file")
    } finally {
        Remove-Item -LiteralPath $file
    }
}

$account = Invoke-AzJson @('account', 'show')
if (Test-Path -LiteralPath $StatePath) {
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json -AsHashtable
    if ($state.tenantId -ne $account.tenantId -or $state.namePrefix -ne $NamePrefix) {
        throw 'State file tenant/name mismatch. Use the original context or a different state file.'
    }
} else {
    foreach ($name in @("$NamePrefix-mcp-api", "$NamePrefix-claude-client")) {
        $existing = @(Invoke-AzJson @('ad', 'app', 'list', '--filter', "displayName eq '$name'"))
        if ($existing.Count -gt 0) {
            throw "Application $name already exists without this state file; do not modify it automatically."
        }
    }
    $state = @{
        tenantId = $account.tenantId
        namePrefix = $NamePrefix
        scopeId = [guid]::NewGuid().ToString()
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding utf8
}

foreach ($kind in @('api', 'client')) {
    if (-not $state.ContainsKey($kind)) {
        $displayName = if ($kind -eq 'api') { "$NamePrefix-mcp-api" } else { "$NamePrefix-claude-client" }
        $app = Invoke-AzJson @('ad', 'app', 'create', '--display-name', $displayName,
            '--sign-in-audience', 'AzureADMyOrg')
        $state[$kind] = @{ objectId = $app.id; clientId = $app.appId }
        $state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $StatePath -Encoding utf8
    }
    $appId = $state[$kind].clientId
    $existingSp = @(Invoke-AzJson @('ad', 'sp', 'list', '--filter', "appId eq '$appId'"))
    if ($existingSp.Count -eq 0) { $null = Invoke-AzJson @('ad', 'sp', 'create', '--id', $appId) }
}

$currentApi = Invoke-AzJson @('ad', 'app', 'show', '--id', $state.api.clientId)
$identifierUris = @($currentApi.identifierUris) + @("api://$($state.api.clientId)")
if ($state.ContainsKey('mcpResourceUri')) { $identifierUris += $state.mcpResourceUri }
if ($McpResourceUri) { $identifierUris += $McpResourceUri }
Set-GraphApplication $state.api.objectId @{
    identifierUris = @($identifierUris | Select-Object -Unique)
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @(@{
            id = $state.scopeId
            value = 'search'
            type = 'Admin'
            isEnabled = $true
            adminConsentDisplayName = 'Search the public web through Foundry'
            adminConsentDescription = 'Allow approved users to call the organization MCP search API.'
        })
    }
}
$currentClient = Invoke-AzJson @('ad', 'app', 'show', '--id', $state.client.clientId)
$redirectUris = @($currentClient.publicClient.redirectUris) +
    @('http://localhost:8765/callback', 'http://localhost:8765')
Set-GraphApplication $state.client.objectId @{
    publicClient = @{ redirectUris = @($redirectUris | Select-Object -Unique) }
    requiredResourceAccess = @(@{
        resourceAppId = $state.api.clientId
        resourceAccess = @(@{ id = $state.scopeId; type = 'Scope' })
    })
}
Set-GraphApplication $state.api.objectId @{
    api = @{
        preAuthorizedApplications = @(@{
            appId = $state.client.clientId
            delegatedPermissionIds = @($state.scopeId)
        })
    }
}

if ($McpResourceUri) {
    $state.mcpResourceUri = $McpResourceUri
    $state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $StatePath -Encoding utf8
}
Write-Host "Application configuration saved in $StatePath (IDs only; no secrets)."
Write-Host "API client ID: $($state.api.clientId)"
Write-Host "Public client ID: $($state.client.clientId)"
Write-Host 'Review preauthorization with your Entra administrator before use.'
