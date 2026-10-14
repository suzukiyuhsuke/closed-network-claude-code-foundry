# 方式2：Azure Functions MCP + Foundry 共通 Web 検索エージェント構築手順

確認日：2026-10-01。対象：Azure Public Cloud。コマンド：PowerShell 7。

## 1. 実現する構成と対象外

Claude Code の推論先は既存の Foundry Claude デプロイメントのままにします。Claude が検索を必要と判断すると、**PC上の Claude Code MCP クライアント**が Functions に `tools/call` を送ります。Claudeモデルが直接Functionsに接続するわけではありません。

| 区間 | 接続と認証 |
|---|---|
| PC → 既存 Claude 推論 | Foundry Private Endpoint、利用者の Entra ID |
| PC → MCP ラッパー | Functions Private Endpoint、公開クライアントの OAuth Authorization Code + PKCE |
| Functions → Foundry Project / 検索 Agent | VNet Integration → Foundry Private Endpoint、専用 User Assigned Managed Identity |
| Foundry 検索 Agent → Web Search | Foundry側のサーバーツール、Bing の公開エンドポイント |
| 検索結果 → PC → Claude 推論 | 引用付き回答を MCP ツール結果として返す |

本実装は**検索・根拠付き要約**です。任意URLの原文取得やブラウザ操作、クライアント `WebFetch` の完全互換ではありません。Functions自身はWebページを取得しません。

### 導入前の必須承認

- Web Search は Bing Grounding の利用条件・課金の対象です。Bing に送られるデータには Microsoft DPA が適用されず、コンプライアンス・地理的境界外への転送があります。[公式説明][web-search]
- 「PCから任意インターネットへの通信を禁止し、検索をFoundryに集約する」構成であり、**検索も含めて全通信が閉域内に完結する構成ではありません**。
- 検索クエリに秘密情報、個人情報、社内コード、内部URLを含めないでください。MCPツールの説明とエージェント指示はDLP制御の代替ではありません。必要なら審査済みDLP処理をFunctions手前に追加してから本番利用します。
- Functions SDK型MCPホスティングはプレビュー、Flex Consumption・stateless Streamable HTTP限定です。Easy AuthのProtected Resource Metadataもプレビューです。[Functions][functions-mcp] / [認証][mcp-auth]
- 本手順ではAzureへの変更は利用者が実施します。既存Foundryの削除・再作成やpublic accessの自動変更はしません。

## 2. 追加リソースと再利用方針

| リソース | 必要性 / 用途 | 作成方法 |
|---|---|---|
| 既存 Foundry account / Claudeモデル | 再利用。新しいAIServices型のProject API対応が必要 | 事前確認 |
| Foundry Project | 検索用既存Projectを再利用、なければ追加 | Portal または `infra/project.bicep` |
| GPT系モデルデプロイメント | Web Search対応モデル。Claudeとは別。例：利用リージョンで対応する `gpt-5-mini` | Foundry Portal |
| Prompt Agent の固定バージョン | 共通の検索指示とWeb Searchツール | `scripts/create_agent.py` |
| VNet / Functions用サブネット / PE用サブネット | 新規または既存を使用。Flex用は `Microsoft.App/environments` に委譲 | `infra/network.bicep` または既存ネットワーク |
| Private DNS Zone / VNet link | Functions、Storage blob、Foundryの名前解決 | 同上。既存の中央DNSゾーンは再利用 |
| Functions Flex Consumption / Function App | MCPラッパー。Python 3.12 | `infra/main.bicep` |
| StorageV2 / deploymentコンテナ | Functionsホスト・コードパッケージ。Blob PE、共有キー禁止 | 同上 |
| User Assigned Managed Identity | Storage・Foundryへの秘密情報なしのアクセス | 同上 |
| Functions PE / Foundry PE | Functionsは必須、Foundryは既存PEがあれば再利用 | 同上 |
| Entra APIアプリ / 公開クライアントアプリ | それぞれMCP APIとClaude Code用。クライアントシークレット不要 | `scripts/New-EntraApps.ps1` |
| Log Analytics / Application Insights | プラットフォーム監視。30日保持 | `infra/main.bicep` |
| DNS Private Resolver inbound | オンプレからAzure Private DNSを引く場合。既存DNS転送基盤があれば不要 | `network.bicep` の任意パラメーター |
| VPN / ExpressRoute / VNet Peering | PC・デプロイ端末からPEへの到達経路 | 既存ネットワーク管理者による接続（後述） |

**この検索だけなら、Cosmos DB・Azure AI Search・別のBingリソース・APIM・Key Vaultは必須ではありません。** Basic agent setupと直接の `WebSearchTool` を使用します。Functions StorageとFoundryエージェント保存基盤は別物です。

Basicではエージェントデータの保存基盤はMicrosoft管理です。データを自社管理のStorage / Cosmos DB / AI Searchに置く必要がある場合は、[Standard setup][private-network]を別途設計してください。ネットワーク注入やcapability設定を既存リソースへ後付けできないケースがあります。標準構成の新規テンプレートを既存Foundryへそのまま適用しないでください。

**PEは受信を閉域化するもの、VNet注入はAgentの送信側の構成**です。本手順は既存FoundryにPEを追加する最小構成であり、Foundry側のVNet注入は追加しません。Web SearchはVNet注入環境でも公開エンドポイントを使用します。[ネットワーク選択][network-options] / [ツール対応表][private-link]

## 3. 事前確認と作業用変数

### 3.1 ツール・権限

用意するもの：

- PowerShell 7、Azure CLIとBicep CLI、Python 3.12（ローカルテストは3.13も可）、現行のClaude Code。
- ローカルFunctions起動を行う場合のみ、MCPプレビュー対応のAzure Functions Core Tools v4。
- ARMリソース作成者：新規RGのContributorとRBAC割当権限、既存Foundry ProjectへのRBAC割当権限、ネットワーク・DNSへの変更権限。
- Entraアプリ作成者：アプリ登録・APIスコープ・事前承認を構成できる権限。テナントの管理者承認ポリシーを確認。
- エージェント作成者：対象Projectの **Foundry User（旧Azure AI User）**。
- コード発行者：Function Appへの発行権限。Contributor等。SCMへのネットワーク到達性も必要。

新規リソースは既存Foundryと**同じサブスクリプション・テナント**に作成する前提です。リージョンはFoundry・GPT・Flex Consumptionの共通対応地域を選びます。`japaneast`は例であり、利用可能性やクォータを保証しません。

```powershell
az login --tenant '<TENANT-ID>'
$SubscriptionId = '<SUBSCRIPTION-ID>'
$Location = 'japaneast'
$FoundryRg = '<EXISTING-FOUNDRY-RG>'
$FoundryName = '<EXISTING-FOUNDRY-ACCOUNT>'
$ProjectName = '<SEARCH-PROJECT>'
$Rg = 'rg-foundry-search'
$Prefix = 'websearch'
$VnetName = 'vnet-foundry-search'
$AgentName = 'shared-web-search'
$ModelDeployment = '<GPT-DEPLOYMENT-NAME>'
az account set --subscription $SubscriptionId
az account show --query '{subscription:id,tenant:tenantId,name:name}' -o table
if ($LASTEXITCODE -ne 0) { throw 'Azure account selection failed' }
$AuthorObjectId = az ad signed-in-user show --query id -o tsv
```

作業用PCは管理プレーンとパッケージ取得先に接続できるようにします。最終利用端末の許可先とは分離してください。依存ライブラリは組織で承認したミラーから配布可能です。TLS検証を無効化しないでください。

### 3.2 既存Foundryを分類する

```powershell
az cognitiveservices account show -g $FoundryRg -n $FoundryName `
  --query '{id:id,kind:kind,location:location,properties:properties}' -o json
az cognitiveservices account deployment list -g $FoundryRg -n $FoundryName -o table
$AccountId = az cognitiveservices account show -g $FoundryRg -n $FoundryName --query id -o tsv
az rest --method get `
  --url "https://management.azure.com$AccountId/projects?api-version=2025-06-01"
az functionapp list-flexconsumption-locations -o table
```

| 結果 | 次の対応 |
|---|---|
| AIServices account、Projectあり、PEもあり | ProjectとPEを再利用。Functionsから同じPrivate DNSで解決できることを確認 |
| AIServices account、Projectなし | Project管理が有効なら次節でProject追加 |
| `allowProjectManagement` が未有効 / Project API失敗 | PortalでProject作成可否と既存リソースの対応を確認。変更影響を承認してから実施 |
| 旧Hub/Project、OpenAI単独アカウント等 | このBicep/APIへそのまま入力しない。検索用の対応Foundryが別途必要か判定。既存Claudeは維持 |
| GPTモデルの容量・地域非対応 | 別地域の検索用Foundryが必要な場合がある。この同一アカウント手順を止め、変更設計を先に承認 |

新しいFoundry PortalからProjectの**実際のEndpoint**を取得します。Claude用 `/anthropic` や旧接続文字列を使いません。

```powershell
$ProjectEndpoint = "https://$FoundryName.services.ai.azure.com/api/projects/$ProjectName"
# 上記は標準形。必ずPortal表示の値と一致させる。
$ProjectId = "$AccountId/projects/$ProjectName"
```

## 4. Azureリソースを構築する

### 4.1 Provider登録とRG作成

```powershell
foreach ($provider in @(
  'Microsoft.Web', 'Microsoft.Storage', 'Microsoft.Network',
  'Microsoft.ManagedIdentity', 'Microsoft.Insights',
  'Microsoft.OperationalInsights', 'Microsoft.CognitiveServices', 'Microsoft.App'
)) {
  az provider register --namespace $provider --wait
  if ($LASTEXITCODE -ne 0) { throw "Provider registration failed: $provider" }
}
az group create --name $Rg --location $Location --output none
```

### 4.2 Projectがない場合だけ追加し、GPTをデプロイ

既存Projectにはこのテンプレートを適用しません。既存Foundryの設定を変更・置換するテンプレートではありません。

```powershell
az deployment group what-if -g $FoundryRg --template-file .\infra\project.bicep `
  --parameters accountName=$FoundryName projectName=$ProjectName location=$Location
# 内容を確認・承認した後だけ実行
az deployment group create -g $FoundryRg -n search-project --template-file .\infra\project.bicep `
  --parameters accountName=$FoundryName projectName=$ProjectName location=$Location --output none
```

Foundry Portal → 対象Project → Models / DeploymentsからWeb Search対応のGPT系モデルをデプロイします。**デプロイメント名**を `$ModelDeployment` に設定します。モデルバージョン、提供リージョン、SKU、TPMクォータはその時点のカタログで確認し、組織で固定してください。既存Claudeのデプロイメント名を検索Agentに指定しません。

必要に応じ、作成担当者にProject単位で権限を割り当てます。同じロールが既にある場合は追加不要です。

```powershell
az role assignment create --assignee-object-id $AuthorObjectId --assignee-principal-type User `
  --role '53ca6127-db72-4b80-b1b0-d745d6d5456d' --scope $ProjectId --output none
```

### 4.3 ネットワークとDNS

既存VNet・中央Private DNSがある場合は、それを優先します。Functions用サブネットはPE用やFoundry Agent用と共用せず、`Microsoft.App/environments`へ委譲します。PE用サブネットは委譲しません。目安はFunctions `/24`、PE `/24`。

新規VNetを作る場合の例です。`10.70.0.0/16`が社内・Hub・他Spokeと重複しないことを先に確認してください。

```powershell
az deployment group what-if -g $Rg --template-file .\infra\network.bicep `
  --parameters name=$VnetName location=$Location createDnsResolver=true
# 新規VNet・ゾーン・Resolverの課金を承認した後
az deployment group create -g $Rg -n search-network --template-file .\infra\network.bicep `
  --parameters name=$VnetName location=$Location createDnsResolver=true --output none
$Network = az deployment group show -g $Rg -n search-network --query properties.outputs -o json | ConvertFrom-Json
```

既存DNS転送サーバがあれば `createDnsResolver=false`。既存の中央DNSゾーンがあれば `createDnsZones=false` として新規ゾーンを作らず、中央ゾーンにこのVNetのリンクを追加し、そのゾーンIDをmainパラメーターに設定します。**同じ名前のゾーンを二重に作成して同じVNetへリンクしないでください。**

#### PCからVNetへの経路

Private Endpointを作るだけではオンプレPCから到達できません。以下のどちらかを実施します。

1. 既存VPN/ExpressRouteのHubにこのVNetを接続する：ネットワーク管理者にアドレス範囲を払い出してもらい、双方向Peeringを作成。Hub側のGateway transit、Spoke側のUse remote gateways、社内への経路広告と戻り経路を構成します。既存Hubの変更は承認を得て実施します。
2. 接続基盤がない：PortalでVPN Gateway用の専用 `GatewaySubnet`（例 `/27`以上）を追加し、route-basedのVPN GatewayとPublic IPを作成。P2Sでは非重複クライアントアドレスプール・組織承認済み認証方式を設定し、VPNクライアントを配布。インターネットVPN自体を許可できない場合はExpressRouteを回線事業者と手配します。ゲートウェイSKU・認証・回線は環境依存のため本Bicepでは作成しません。

デプロイ端末も同じ経路を使用します。Azure Cloud ShellやMicrosoft-hosted CI runnerは、通常このPEへ到達しません。必要ならVNet内の管理VMやself-hosted runnerを使います。

#### オンプレDNS

社内DNSの条件付きフォワーダーに次を設定します。転送先は `$Network.resolverIp.value`（または既存Azure DNS転送サーバ）。UDP/TCP 53の双方向到達性が必要です。

| 転送する公開DNSゾーン | Azure内のPrivate DNS Zone |
|---|---|
| `azurewebsites.net` | `privatelink.azurewebsites.net` |
| `blob.core.windows.net` | `privatelink.blob.core.windows.net` |
| `services.ai.azure.com` | `privatelink.services.ai.azure.com` |
| `openai.azure.com` | `privatelink.openai.azure.com` |
| `cognitiveservices.azure.com` | `privatelink.cognitiveservices.azure.com` |

オンプレDNSから `168.63.129.16` へ直接転送しません。Azure内のResolver/転送VMを経由します。独自DNSをVNetに指定している場合もPrivate DNSへ転送してください。

### 4.4 Entra IDアプリを構成

```powershell
.\scripts\New-EntraApps.ps1 -NamePrefix $Prefix
$Entra = Get-Content .\entra.local.json -Raw | ConvertFrom-Json
```

スクリプトは新しいAPIアプリと公開クライアントだけを作成・構成します。生成したIDをローカルファイルに保存するため、再実行時はそのファイルを保持してください。途中失敗は保存済みIDから再開できます。同名アプリが既にありstateファイルがない場合は停止します。作成直後のdirectory反映遅延で失敗した場合は数分後に同じstateで再実行します。

設定内容：

| 対象 | 設定 |
|---|---|
| APIアプリ | Single tenant、Access token v2、Application ID URI `api://<API-client-id>` |
| Delegated scope | `search`（Admin consent）、公開クライアントをpre-authorize |
| 公開クライアント | Single tenant、Mobile/Desktop redirect URI `http://localhost:8765/callback`（Claude Code）と `http://localhost:8765`（Python疎通確認）、APIのsearch権限 |
| シークレット | 作成しない。PKCEを使用 |
| Easy Auth | tenant・audience・公開クライアントID・許可ユーザーObject IDを検査。未認証は401 |
| アプリ内検査 | Easy Authが検証後に渡すclaimsの `search` scopeを検査。認証済みでもscopeなしは403 |

EntraはDynamic Client Registrationをサポートしないため、Claude Codeに公開クライアントのIDを指定します。`allowedUserObjectIds`には**ユーザーのObject ID**を入れ、アプリIDやグループIDを入れないでください。初期構成は少人数の明示許可方式です。全社展開ではグループ・アプリロール等への変更を別途設計します。

本サーバのclaimsヘッダーは**Easy Auth配下でのみ信頼できる**情報です。Functions以外へそのまま公開したり、Easy Authを無効化したりしないでください。Foundryへはこのユーザートークンを転送せず、専用Managed Identityで別トークンを取得します。

### 4.5 Functions等の構築

```powershell
Copy-Item .\infra\main.parameters.example.json .\infra\main.parameters.local.json
$p = Get-Content .\infra\main.parameters.local.json -Raw | ConvertFrom-Json
$p.parameters.location.value = $Location
$p.parameters.namePrefix.value = $Prefix
$p.parameters.functionsSubnetId.value = $Network.functionsSubnetId.value
$p.parameters.endpointsSubnetId.value = $Network.endpointsSubnetId.value
$p.parameters.dnsZoneIds.value = $Network.dnsZoneIds.value
$p.parameters.foundryResourceGroup.value = $FoundryRg
$p.parameters.foundryAccountName.value = $FoundryName
$p.parameters.foundryProjectName.value = $ProjectName
$p.parameters.foundryProjectEndpoint.value = $ProjectEndpoint
$p.parameters.mcpApiClientId.value = $Entra.api.clientId
$p.parameters.mcpPublicClientId.value = $Entra.client.clientId
$p.parameters.allowedUserObjectIds.value = @($AuthorObjectId)
$p | ConvertTo-Json -Depth 20 | Set-Content .\infra\main.parameters.local.json -Encoding utf8
```

既存VNetを使う場合は `$Network` の代わりに実際のサブネット・DNSゾーンIDを設定してください。Foundry PEが既にあり到達可能なら `createFoundryPrivateEndpoint.value=false`。重複PEを作らず、Private DNS・Peeringで共有します。初回の `agentVersion=1` は仮値で、検索Agent作成後に必ず置換します。

```powershell
az bicep build --file .\infra\main.bicep --stdout > $null
az deployment group what-if -g $Rg --template-file .\infra\main.bicep `
  --parameters '@infra\main.parameters.local.json'
# 差分・権限・料金を確認した後
az deployment group create -g $Rg -n search-app --template-file .\infra\main.bicep `
  --parameters '@infra\main.parameters.local.json' --output none
if ($LASTEXITCODE -ne 0) { throw 'Infrastructure deployment failed' }
$Outputs = az deployment group show -g $Rg -n search-app --query properties.outputs -o json | ConvertFrom-Json
$FunctionApp = $Outputs.functionAppName.value
$McpUrl = $Outputs.mcpUrl.value
$McpHost = ([uri]$McpUrl).Host
```

URL確定後、同じEntraアプリのApplication ID URIへ**正確なMCP URL（`/mcp`を含む）**も登録します。
既存の`api://<API-client-id>`と`search`スコープは維持します。

```powershell
.\scripts\New-EntraApps.ps1 -NamePrefix $Prefix -McpResourceUri $McpUrl
```

Easy AuthのPRMは`resource`にHTTPSのMCP URLを返し、Claude CodeはこれをOAuth要求に送信します。
HTTPS URLがAPIアプリの識別子として登録されていないと、
`api://<API-client-id>/search`との対応が取れず`AADSTS9010010 (invalid_target)`になります。
追加後は両識別子が同じAPIアプリへ解決されるため、既存のスコープを変更する必要はありません。
スクリプトは既存の識別子を保持し、追加したURLをstateへ保存するため、再実行でも消えません。
APIはv2アクセストークンを使用します。テナントポリシーでURL追加が拒否される場合は、
ポリシーを解除せず管理者へ確認してください。

PE接続はすべて `Approved` であることを確認します。別RGのFoundryへの接続承認権限がない場合は、Foundry管理者に承認を依頼してください。

RBACは次を作成します。反映には数分かかる場合があります。

| ID | スコープ | ロール |
|---|---|---|
| Functions専用Managed Identity | 専用Storage | Storage Blob Data Owner（ホスト用コンテナ・blob管理） |
| 同上 | 対象Foundry Projectのみ | Foundry User（旧Azure AI User、ID `53ca6127-db72-4b80-b1b0-d745d6d5456d`） |
| 同上 | 専用Application Insights | Monitoring Metrics Publisher |

Foundry Userは「このAgentの実行だけ」に限定された権限ではありません。検索専用Projectを推奨します。コードは実行時にAgentを作成・変更しません。

### 4.6 既存Foundryの公開アクセスを閉じる

まずPCおよびFunctionsネットワークからFoundry名がPEのプライベートIPに解決されることを確認します。DNS名は通常のFQDNを使い、URLをPEのIPや`privatelink`名に書き換えません。

```powershell
Resolve-DnsName ([uri]$ProjectEndpoint).Host
Resolve-DnsName $McpHost
$ScmHost = $McpHost -replace '\.azurewebsites\.net$', '.scm.azurewebsites.net'
Resolve-DnsName $ScmHost
Resolve-DnsName "$($Outputs.storageAccountName.value).blob.core.windows.net"
Test-NetConnection $McpHost -Port 443
Test-NetConnection $ScmHost -Port 443
```

公開アクセスが現在有効なFoundryは、**既存利用者・パイプラインへの影響を確認し、変更承認を取ってから**次を実施します。本Bicepは既存Foundryの設定を上書きしません。

```powershell
az resource update --ids $AccountId --api-version 2025-06-01 `
  --set properties.publicNetworkAccess=Disabled --output none
```

既存Foundryがpublicのままでは、本構成全体の閉域受入条件を満たしません。FunctionsとStorageはBicepで最初からpublicを無効化しています。

## 5. 検索AgentとFunctionsコードを配置

### 5.1 エージェントの作成

PEへ到達できる作業端末で実行します。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements-dev.txt
.\.venv\Scripts\python.exe .\scripts\create_agent.py `
  --endpoint $ProjectEndpoint --model $ModelDeployment --name $AgentName
```

出力の `name` / `version` を控えます。実行のたびに**新しいバージョン**が作られます。自動削除・latestへの自動追従はしません。

```powershell
$AgentVersion = '<RETURNED-VERSION>'
$p.parameters.agentName.value = $AgentName
$p.parameters.agentVersion.value = $AgentVersion
$p | ConvertTo-Json -Depth 20 | Set-Content .\infra\main.parameters.local.json -Encoding utf8
az functionapp config appsettings set -g $Rg -n $FunctionApp `
  --settings "FOUNDRY_AGENT_NAME=$AgentName" "FOUNDRY_AGENT_VERSION=$AgentVersion" --output none
```

手順の途中で同じPowerShellを閉じた場合は `$p` をファイルから読み直してください。将来Bicepを再適用して古いAgentへ戻らないよう、パラメーターファイルも更新します。

### 5.2 Functionsへ発行

```powershell
.\scripts\Publish-Mcp.ps1 -SubscriptionId $SubscriptionId `
  -ResourceGroup $Rg -FunctionAppName $FunctionApp
```

既定では必要な4ファイルだけをzip化し、Linux側remote buildで依存関係をインストールします。Windowsの `.venv`、認証state、local settings、テストを配布しません。

**Private SCMへのHTTPS接続が必要**です。リモートビルド環境にはPython依存の配布先への到達性が必要です。組織のネットワーク制約でremote buildが使えない場合は、Azure Functionsの[パッケージ配布手順][package-deploy]に従い、同じLinux/Python 3.12環境と承認済みミラーで `.python_packages/lib/site-packages` を含むready-to-runパッケージを作成し、remote buildなしで配布する設計に切り替えます。Windowsバイナリを含むパッケージは使用しません。

`uv`を導入済みの端末では、次のオプションでLinux x86_64・Python 3.12用のwheelを同梱できます。
AzureのランタイムもPython 3.12であることを確認してください。

```powershell
.\scripts\Publish-Mcp.ps1 -SubscriptionId $SubscriptionId `
  -ResourceGroup $Rg -FunctionAppName $FunctionApp -LocalBuild
```

`-LocalBuild`では`uv pip install --python-platform x86_64-manylinux2014 --python-version 3.12 --only-binary=:all:`
で依存関係を解決し、`.python_packages`を含むzipを`--build-remote false`で発行します。
Windows上の`pip --platform`だけでは依存関係のOS条件がWindowsとして評価され、
`pywin32`などの解決に失敗するため、ここではターゲットOSの条件を評価できる`uv`を使用します。
承認済みミラーを使う場合は`UV_DEFAULT_INDEX`、OSの信頼ストアが必要なら`UV_NATIVE_TLS=true`を
実行プロセスに設定します。pipのミラー設定はuvには自動継承されません。TLS検証は無効化しません。
依存解決に失敗した場合は発行せず終了します。

Flexでは `WEBSITE_RUN_FROM_PACKAGE`、`WEBSITE_CONTENTAZUREFILECONNECTIONSTRING`、従来型Consumption用設定を追加しません。共有キー、Functionキー、publish profileに依存しません。

### 5.3 検索モデルだけを変更する

変更先のWeb Search対応モデルを同じFoundryアカウントにデプロイしてから、
`--from-version`で既存の固定バージョンを複製します。
このオプションは指示・ツール・その他のAgent定義とメタデータを保持し、モデルだけを変更します。
省略すると5.1の標準検索指示を使用します。既存Agentのモデル変更では省略しないでください。

```powershell
.\.venv\Scripts\python.exe .\scripts\create_agent.py `
  --endpoint $ProjectEndpoint --name $AgentName `
  --from-version $AgentVersion --model '<NEW-MODEL-DEPLOYMENT>'
```

返された新バージョンの検索結果・引用を確認してから、5.1と同様に
Functionsの`FOUNDRY_AGENT_VERSION`と使用中のBicepパラメーターファイルの`agentVersion`を更新します。
毎回新しいバージョンを作るため、接続先切り替えだけの目的でこのコマンドを再実行しないでください。
アプリ設定変更に伴いFunctionsの再起動・一時的な接続断が発生し得ます。
MCP URL・Entra設定・Functionsコードの再発行は変更不要です。

切り戻すときは同じ2か所のバージョンを旧値へ戻します。旧Agentバージョンだけでなく、
その版が参照する旧モデルデプロイメントも残してください。
従量課金モデルの容量はスループット枠で、月額予算の上限ではありません。
このPoCの実際の切り替え値・切り戻しコマンドは[PoC手順書](public-poc.ja.md#検索モデルの変更と切り戻し)に記録しています。

## 6. Claude Codeを設定

### 6.1 Foundryの推論接続

既存の正常な設定があれば維持します。Entra認証の例：

```powershell
$env:CLAUDE_CODE_USE_FOUNDRY = '1'
$env:ANTHROPIC_FOUNDRY_RESOURCE = $FoundryName
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = '<EXISTING-OPUS-DEPLOYMENT>'
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = '<EXISTING-SONNET-DEPLOYMENT>'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = '<EXISTING-HAIKU-DEPLOYMENT>'
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'
$env:DISABLE_AUTOUPDATER = '1'
az login --tenant $Entra.tenantId
```

利用していないモデル変数は設定しません。必要な推論権限は既存Foundry管理者が付与します。MCP用のsearchトークンとは別のaudienceです。[Claude Code Foundry設定][claude-foundry]

### 6.2 MCPを追加しOAuthログイン

先に4.5のMCP URLのApplication ID URI追加を完了してください。

```powershell
claude mcp add --transport http --scope user --client-id $Entra.client.clientId `
  --callback-port 8765 foundry-search $McpUrl
claude
```

Claude Code内で `/mcp` → `foundry-search` → 認証を実施。最新の公開クライアント対応版を使い、クライアントシークレットは設定しません。Entra認証時のブラウザとlocalhostへの戻り通信を許可します。既に同名サーバがある場合は重複追加せず既存設定を確認します。

スコープを明示する構成は `examples/claude-mcp.example.json` を参照し、**URLと各IDを実値へ置き換えてから**使用します。`oauth.scopes` は `api://<API-ID>/search offline_access`。URL・IDのプレースホルダーやトークンをそのままコミットしないでください。

スコープ自動検出ではEasy AuthのPRMが `search` を通知します。MCPメタデータ取得や認証が失敗する場合は、`/.well-known/oauth-protected-resource` と401の `WWW-Authenticate` を確認します。[MCP認証][mcp-auth] / [Claude MCP OAuth][claude-mcp]

### 6.3 管理者ポリシーと通信制御

`examples/managed-settings.example.json` を既存の管理ポリシーへ**マージ**します。Windowsの管理ファイルは通常 `C:\Program Files\ClaudeCode\managed-settings.json`。既存設定を上書きしないでください。組織の端末管理で配布し、ユーザーが書き換えられないようにします。

`WebFetch` と `WebSearch` をdenyし、`mcp__foundry-search__search_web` は初期段階で都度承認にします。承認時に検索クエリに非公開データがないか確認します。標準ツール名のdenyだけでは別名のMCPやBashでの通信は止まりません。

| 発信元 | 許可先 | 注意 |
|---|---|---|
| 利用PC | FoundryとFunctionsの実FQDN / PE、社内DNS | 任意公開Web、直接Bing、anthropic.com / claude.ai等への不要な通信はNW側で拒否 |
| 利用PC・ブラウザ | Entraログイン・MFA・組織のFederation先、必要な証明書検証先 | 認証まで完全に閉域化できるという意味ではない |
| Functions | Foundry PE、Storage blob PE、Managed Identity、プラットフォーム依存 | 任意Web取得コードなし。ただしVNet Integrationだけでは送信denyにならない |
| Functions / platform | Azure Monitor取込先 | 本テンプレートはMonitor Private Link Scopeを作らない。監視通信も閉域必須ならAMPLSを追加設計 |
| 配布端末・ビルド環境 | ARM / Entra / SCM PE / 承認済みパッケージ配布先 | 利用PCの通信規則とは分ける |

端末の既定送信を拒否し、承認したFQDN/宛先だけを許可する企業FW/プロキシルールを適用します。許可FQDNの実IPがPEであることも確認します。Functionsの外向き通信も制限したい場合は、Flexのルーティング仕様とプラットフォーム依存を踏まえてNSG / UDR / Firewallを追加します。本Bicepは既存FWを上書きしません。

プロキシを使う場合、PE宛ては通常 `NO_PROXY` に実FQDNを登録します。インターネット側プロキシへPE宛てを送らないでください。TLS検査が必要なら社内CAをOS・Node・Pythonの信頼ストアへ配布し、必要に応じ `NODE_EXTRA_CA_CERTS` / `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` を設定します。`NODE_TLS_REJECT_UNAUTHORIZED=0` や `--insecure` は使いません。

## 7. 受入確認（省略不可）

### 7.1 MCPの実接続

```powershell
.\.venv\Scripts\python.exe .\scripts\smoke_test.py `
  --url $McpUrl --tenant-id $Entra.tenantId `
  --client-id $Entra.client.clientId --api-client-id $Entra.api.clientId
```

このスクリプトはブラウザでOAuth認証し、未認証401、`initialize`、`tools/list`、`tools/call`、引用付き回答を確認します。トークンをファイルやstdoutへ出しません。ポート8765で別のログイン処理を同時実行しないでください。

Azure Identityの`InteractiveBrowserCredential`はリダイレクトURIのパスを使用せず、`http://localhost:8765`を送信します。
Claude Code用の`/callback`だけではPythonの確認が`AADSTS50011`で失敗するため、公開クライアントに上記2つのURIを登録します。
既存環境は同じstateファイルで更新版`New-EntraApps.ps1`を再実行してください。既存のリダイレクトURIは保持されます。

`tool_choice=required` に加え、サーバで完了した `web_search_call` とURL引用を確認します。モデル回答だけ・引用なし・未完了応答は `isError=true` となり、検索成功に見せかけません。検索結果が得られない問いは言い換えて再実行してください。

### 7.2 全受入項目

| 項目 | 合格条件 |
|---|---|
| DNS | Foundry・Functions・SCM・Blobの通常FQDNが期待するPE IPへ解決 |
| PE承認 | 対象PEがすべてApproved |
| 公開無効化 | Functions・Storage・既存Foundryのpublic network accessがDisabled |
| インターネット側からの拒否 | 社内VPN等を使わない別端末から、認証情報があってもFunctionデータプレーンにアクセス不可 |
| 未認証 | 閉域からでも `/mcp` は401 |
| 認可 | 許可リストにないユーザーは拒否。別tenant / audience / client IDのトークンも拒否 |
| スコープ | 有効なAPIトークンでも `search` scopeなしは403 |
| MCPプロトコル | initialize・tools/list成功、公開ツールは `search_web` のみ |
| 正常検索 | `answer`、`citations`（URL・タイトル・文字位置）、`response_id` を返す |
| 失敗時 | 403/429/タイムアウト/引用なしでMCPの `isError=true`。秘密を含む内部例外本文は返さない |
| 利用者分離 | 別ユーザー/連続検索で会話履歴を共有しない。conversation ID / previous_response_idを渡さない |
| Claude実利用 | 「foundry-searchで公開情報を検索し、出典URLとともに答えて」と依頼し、ツール呼び出し承認と引用を確認 |
| クライアント通信 | 検索時のPCのFW/Proxyログに公開Webへの直接アクセスがない |
| 迂回経路 | PCからcurl/Python等で管理者指定の公開テストURLへ接続してもNWポリシーで拒否 |
| 再認証 | トークン更新後・Claude再起動後もOAuth接続が維持/更新される |

**検索元WebへのPC通信がないことは、ツールdeny表示ではなくPC側の通信ログで確認します。** 引用リンクをブラウザで開く動作は別の通信であり、閉域PCでは開けない場合があります。

`store=False` はResponsesの保存指定であり、サービス側の監査・不正利用検知・モデル提供条件・Bingの保持条件まで無効化する保証ではありません。

### 7.3 ローカル検証

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

実Azureへアクセスしないテストです。認証claims処理、入力長1〜2000文字、空白拒否、MCPの初期化・ツール一覧・結果形、引用位置、SDKの実際のHTTPリクエスト、403/429/500の処理を確認します。Easy Auth自体のJWT検証はAzure受入試験の対象です。

実Foundryへローカル起動して確認する場合は `src\mcp_server\local.settings.example.json` を同ディレクトリの `local.settings.json` へコピーして編集し、仮想環境を有効にして同ディレクトリで `func start --port 7071` を実行します。`LOCAL_DEVELOPMENT=true` はCLI認証・ローカル認証省略用で、**Azureへ配布禁止**です。Azure環境変数がある場所では起動時に拒否します。バックエンドはloopbackにbindしますが、Functionsホストの7071ポートもOS Firewallで外部から遮断してください。

## 8. 運用・費用・障害対応

### 運用

- Agentは固定バージョンで参照します。更新時は `create_agent.py` → 新バージョンの受入確認 → Functions app setting / パラメーターファイルを更新。旧版は復旧用に保持します。
- 検索ごとに独立したリクエストとし、ユーザー間で会話を共有しません。
- 入力は2000文字、モデル出力は3000トークン。認証を含む呼び出し全体を180秒で打ち切り、HTTPフロントエンドの時間制限より短くします。長時間Deep Researchは対象外です。
- SDKの自動リトライは無効です。429等の際に意図せず検索料金が重複しないよう、利用者へエラーを返します。タイムアウト後もサービス側処理が完了して課金される場合があります。
- HTTPアクセスログ・SDK詳細ログ・クエリ/回答ログはアプリ側では出しません。失敗カテゴリのみをstderrへ出力します。カスタムハンドラーのstdout/stderrの収集先・保持方針はAzureで確認してください。Application Insightsを作るだけではPython独自処理のトレースが自動的にすべて収集されるわけではありません。
- Application Insightsの失敗率/応答時間、Functionsメトリクス、Entraサインインログ、Azure Activity Logを監視。利用者ごとの厳密なレート制限・課金配賦は未実装です。必要ならAPIM等を別途導入します。
- 依存関係の直接バージョンを固定しています。配布前に組織の承認済み依存ロック・脆弱性検査・パッケージミラー運用を適用してください。

課金対象はGPT推論、Web Search、Functions、Storage、Private Endpoint、監視、必要ならDNS Resolver / VPN / ExpressRouteです。Foundryで検索してもClaude側には結果の入力トークン料金が発生します。`maximumInstanceCount=40` はAzure Flexの設定値であり予算上限ではありません。実価格はリージョン・契約・利用量に依存するため、料金計算とAzure Budgetアラートを設定します。

### 障害切り分け

| 症状 | 調査先 / 対応 |
|---|---|
| DNSが公開IP / timeout | VPN/ER、Peering、VNet link、条件付き転送、独自DNS、経路・戻り経路を確認。public有効化で回避しない |
| `AADSTS50011` | 要求のredirect URIと公開クライアントの登録を比較。Claude Codeは`http://localhost:8765/callback`、Python疎通確認は`http://localhost:8765`。両方をMobile/Desktopに登録し、新しいログインを開始 |
| `AADSTS9010010` / `invalid_target` | PRMの`resource`とOAuth要求のURLを確認し、APIアプリのApplication ID URIに同じMCP URLを登録。既存scopeと同じAPIを示す必要がある。修正後は`claude mcp login foundry-search`で新しい認証を開始 |
| DCR unsupported | `--client-id` を指定。Entraで動的登録を前提にしない |
| 未認証401が続く | PRM、audience、issuer、search scope、ブラウザ認証、許可client IDを確認 |
| 有効トークンでも403 | ユーザーObject ID許可リストとscope。MCP API用でなくFoundry用トークンを使っていないか |
| Foundryで403 | Functions Managed IdentityのProject RBAC、反映待ち、Foundry PE / public disabledを確認 |
| Agent API / `agent_reference` が非対応 | 新Foundry Projectか、固定SDK/API/リージョン/モデルの対応を確認。旧Assistants APIと混在させない |
| Web Searchが拒否 | モデル対応、利用条件、サブスクリプションの `OpenAI.BlockedTools.web_search` 制限、クォータを管理者が確認。勝手にブロックを解除しない |
| 引用なし / incomplete | モデル・検索実行状況・出力トークン上限を確認。単純な公開情報の問いで再試行 |
| 429 | GPT/Web Search容量、Functions同時実行、利用量を確認。負荷を下げて時間を置く |
| Function起動失敗 | preview feature flag、host.json、Python/runtime、Storage PE/DNS/Blob RBAC、PYTHONPATH、ビルド出力を確認 |
| 発行失敗 | `<app>.scm.azurewebsites.net` のPE解決、発行者のEntra権限、CLI対応版、remote buildの配布先通信を確認 |

### 復旧・停止

Agent更新だけを戻す場合、Functionsの `FOUNDRY_AGENT_VERSION` とパラメーターファイルを直前の動作確認済み版へ戻します。コード更新は直前のパッケージ/コミットから再発行します。

緊急停止はFunction App停止、または管理者ポリシーで `mcp__foundry-search__search_web` をdeny。利用者を外す場合は `allowedUserObjectIds` を変更し、トークンキャッシュ・再認証を含めて拒否を確認します。

環境を撤去するときは、この構成専用のリソース・アプリ登録・RBAC・DNSリンクのみを台帳と照合して削除します。**既存Foundry、Claude、既存Project、共有DNS・Hub・回線は削除しません。** RG一括削除の前に既存資産が含まれていないことを必ず確認してください。

## 9. 公式資料

検索結果の要約ではなく以下の一次資料を基に構成しています。API/プレビュー仕様は変更されるため本番展開時に再確認してください。

- [Foundry Web Search：設定、Bing条件、対応モデル/API][web-search]
- [Foundryネットワーク選択：受信PEと送信注入の違い][network-options]
- [Foundry Private Link：隔離環境のWeb Searchは公開通信][private-link]
- [Standard setup with private networking][private-network]
- [Functions SDK型MCPホスティング、Flex、プレビュー制約][functions-mcp]
- [公式Python Functions MCPサンプル][functions-sample]
- [Easy Auth MCP認証、PRM、DCR非対応、トークン転送禁止][mcp-auth]
- [Functionsのパッケージ配布とprivate SCM][package-deploy]
- [Claude Code on Microsoft Foundry][claude-foundry]
- [Claude Code MCP、OAuth client ID、callback、scopes][claude-mcp]

[web-search]: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search
[network-options]: https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/networking-options
[private-link]: https://learn.microsoft.com/en-us/azure/foundry/how-to/configure-private-link
[private-network]: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks
[functions-mcp]: https://learn.microsoft.com/en-us/azure/azure-functions/self-hosted-mcp-servers
[functions-sample]: https://github.com/Azure-Samples/mcp-sdk-functions-hosting-python
[mcp-auth]: https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-mcp
[package-deploy]: https://learn.microsoft.com/en-us/azure/azure-functions/deployment-zip-push
[claude-foundry]: https://code.claude.com/docs/en/microsoft-foundry
[claude-mcp]: https://code.claude.com/docs/en/mcp
