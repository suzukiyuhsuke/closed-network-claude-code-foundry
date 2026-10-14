param location string = resourceGroup().location
param name string
param addressPrefix string = '10.70.0.0/16'
param functionsSubnetPrefix string = '10.70.1.0/24'
param endpointsSubnetPrefix string = '10.70.2.0/24'
param resolverSubnetPrefix string = '10.70.3.0/28'
param createDnsResolver bool = false
param createDnsZones bool = true
@allowed(['all', 'storage'])
@description('Use storage for a public Functions PoC with private Storage only.')
param dnsZoneScope string = 'all'

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: name
  location: location
  properties: {
    addressSpace: { addressPrefixes: [addressPrefix] }
    subnets: [
      {
        name: 'functions'
        properties: {
          addressPrefix: functionsSubnetPrefix
          delegations: [
            {
              name: 'flex'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: endpointsSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'dns-resolver'
        properties: {
          addressPrefix: resolverSubnetPrefix
          delegations: [
            {
              name: 'dns'
              properties: { serviceName: 'Microsoft.Network/dnsResolvers' }
            }
          ]
        }
      }
    ]
  }
}

var zoneNames = dnsZoneScope == 'storage' ? [
  'privatelink.blob.${environment().suffixes.storage}'
] : [
  'privatelink.azurewebsites.net'
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
]

resource zones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for zone in zoneNames: if (createDnsZones) {
  name: zone
  location: 'global'
}]

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (zone, i) in zoneNames: if (createDnsZones) {
  parent: zones[i]
  name: name
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}]

resource resolver 'Microsoft.Network/dnsResolvers@2022-07-01' = if (createDnsResolver) {
  name: '${name}-resolver'
  location: location
  properties: { virtualNetwork: { id: vnet.id } }
}

resource inbound 'Microsoft.Network/dnsResolvers/inboundEndpoints@2022-07-01' = if (createDnsResolver) {
  parent: resolver
  name: 'inbound'
  location: location
  properties: {
    ipConfigurations: [
      {
        privateIpAllocationMethod: 'Dynamic'
        subnet: { id: '${vnet.id}/subnets/dns-resolver' }
      }
    ]
  }
}

output vnetId string = vnet.id
output functionsSubnetId string = '${vnet.id}/subnets/functions'
output endpointsSubnetId string = '${vnet.id}/subnets/private-endpoints'
output dnsZoneIds object = createDnsZones ? (dnsZoneScope == 'storage' ? {
  blob: zones[0].id
} : {
  functions: zones[0].id
  blob: zones[1].id
  foundry: [zones[2].id, zones[3].id, zones[4].id]
}) : {}
output resolverIp string = createDnsResolver ? inbound!.properties.ipConfigurations[0].privateIpAddress! : ''
