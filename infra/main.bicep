param location string = resourceGroup().location
@minLength(3)
@maxLength(16)
param namePrefix string
@allowed(['private', 'public', 'public-with-private-storage'])
@description('Public modes are for approved connectivity PoCs. public-with-private-storage keeps Storage private; authentication remains required.')
param networkMode string = 'private'
param functionsSubnetId string = ''
param endpointsSubnetId string = ''
@description('Existing zones: functions, blob, and foundry (array of three resource IDs).')
param dnsZoneIds object = {}
param foundryResourceGroup string
param foundryAccountName string
param foundryProjectName string
param foundryProjectEndpoint string
param agentName string = 'shared-web-search'
param agentVersion string = '1'
param mcpApiClientId string
param mcpPublicClientId string
@minLength(1)
@description('Entra user object IDs authorized to invoke MCP. No tenant-wide default.')
param allowedUserObjectIds array
param createFoundryPrivateEndpoint bool = true

var suffix = uniqueString(resourceGroup().id, namePrefix)
var appName = '${namePrefix}-mcp-${suffix}'
var storageName = 'stmcp${suffix}'
var privateNetworking = networkMode == 'private'
var privateStorage = networkMode != 'public'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-identity'
  location: location
}
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: privateStorage ? 'Disabled' : 'Enabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: privateStorage ? 'Deny' : 'Allow'
    }
  }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}
resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'deployment'
  properties: { publicAccess: 'None' }
}
resource storageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, 'blob-owner')
  scope: storage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
  }
}
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}
resource storageDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'deployment-storage-diagnostics'
  scope: blobs
  properties: {
    workspaceId: workspace.id
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      { category: 'StorageWrite', enabled: true }
      { category: 'StorageRead', enabled: true }
    ]
  }
}
resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
  }
}
resource monitoringRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, identity.id, 'monitoring-publisher')
  scope: insights
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${namePrefix}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: { reserved: true }
}
resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: appName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: privateNetworking ? 'Disabled' : 'Enabled'
    virtualNetworkSubnetId: privateStorage ? functionsSubnetId : null
    functionAppConfig: {
      runtime: {
        name: 'python'
        version: '3.12'
      }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${packageContainer.name}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
        triggers: { http: { perInstanceConcurrency: 4 } }
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        { name: 'AzureWebJobsFeatureFlags', value: 'EnableMcpCustomHandlerPreview' }
        { name: 'PYTHONPATH', value: '/home/site/wwwroot/.python_packages/lib/site-packages' }
        { name: 'AzureWebJobsStorage__blobServiceUri', value: storage.properties.primaryEndpoints.blob }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: identity.properties.clientId }
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
        { name: 'FOUNDRY_AGENT_NAME', value: agentName }
        { name: 'FOUNDRY_AGENT_VERSION', value: agentVersion }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'ClientId=${identity.properties.clientId};Authorization=AAD' }
        { name: 'WEBSITE_AUTH_PRM_DEFAULT_WITH_SCOPES', value: 'api://${mcpApiClientId}/search' }
      ]
    }
  }
  dependsOn: [storageRole, monitoringRole, storageEndpoint]
}
resource auth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: app
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    httpSettings: { requireHttps: true }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: mcpApiClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [mcpApiClientId, 'api://${mcpApiClientId}']
          defaultAuthorizationPolicy: {
            allowedApplications: [mcpPublicClientId]
            allowedPrincipals: { identities: allowedUserObjectIds }
          }
        }
      }
    }
    login: { tokenStore: { enabled: false } }
  }
}
resource scm 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'scm'
  properties: { allow: false }
}
resource ftp 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'ftp'
  properties: { allow: false }
}
module storageEndpoint 'private-endpoint.bicep' = if (privateStorage) {
  name: 'storage-endpoint'
  params: {
    name: '${namePrefix}-blob-pe'
    location: location
    subnetId: endpointsSubnetId
    targetId: storage.id
    groupId: 'blob'
    dnsZoneIds: [dnsZoneIds.blob]
  }
}
module functionsEndpoint 'private-endpoint.bicep' = if (privateNetworking) {
  name: 'functions-endpoint'
  params: {
    name: '${namePrefix}-functions-pe'
    location: location
    subnetId: endpointsSubnetId
    targetId: app.id
    groupId: 'sites'
    dnsZoneIds: [dnsZoneIds.functions]
  }
}
module foundryEndpoint 'private-endpoint.bicep' = if (privateNetworking && createFoundryPrivateEndpoint) {
  name: 'foundry-endpoint'
  params: {
    name: '${namePrefix}-foundry-pe'
    location: location
    subnetId: endpointsSubnetId
    targetId: resourceId(foundryResourceGroup, 'Microsoft.CognitiveServices/accounts', foundryAccountName)
    groupId: 'account'
    dnsZoneIds: dnsZoneIds.foundry
  }
}
module foundryRole 'foundry-role.bicep' = {
  name: '${namePrefix}-foundry-role'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    accountName: foundryAccountName
    projectName: foundryProjectName
    principalId: identity.properties.principalId
  }
}
output functionAppName string = app.name
output mcpUrl string = 'https://${app.properties.defaultHostName}/mcp'
output storageAccountName string = storage.name
output managedIdentityPrincipalId string = identity.properties.principalId
output managedIdentityClientId string = identity.properties.clientId
output networkMode string = networkMode
