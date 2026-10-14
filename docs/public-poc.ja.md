# 公開HTTPSで先に疎通確認するPoC

本番の閉域構築前に、Claude Code → Functions MCP → Foundry検索Agentの機能を確認する手順です。
`infra/main.bicep` は **既定でprivate**。承認したPoCだけ `networkMode=public` を明示します。
公開経路でもEntra ID、許可ユーザー、許可クライアント、search scopeの検査は維持し、匿名アクセスやStorage共有キーは許可しません。

## 作成・確認

1. [通常手順](deployment-guide.ja.md)の事前確認とEntraアプリ作成を実施します。利用者のObject IDはAPIアプリと同じテナントの値を使用します。ゲストではホームテナントのObject IDと異なります。
2. `infra/public.parameters.example.json` を `infra/public.parameters.local.json` にコピーし、既存Foundry・検索Agentの名前/固定バージョン、EntraアプリID、許可利用者IDを設定します。リージョンを確認します。
3. 専用RGを作成し、下記のvalidate / what-if後に承認した差分のみをデプロイします。VNet・PE・DNS・VPNは作成しません。
4. `scripts/Publish-Mcp.ps1` でコードを発行します。既存FoundryとAgentは変更しません。
5. デプロイ出力のMCP URLを、`New-EntraApps.ps1 -NamePrefix <作成時のPrefix> -McpResourceUri <MCP-URL>`で同じAPIアプリのApplication ID URIへ追加します。`/mcp`を含め、末尾のスラッシュは付けません。初回作成時のstateファイルを使用します。
6. `scripts/smoke_test.py` とClaude CodeのOAuth接続で確認します。認証に使うブラウザ操作は利用者本人が行います。

```powershell
$Rg = '<POC-RESOURCE-GROUP>'
az deployment group validate -g $Rg --template-file .\infra\main.bicep `
  --parameters '@infra\public.parameters.local.json'
az deployment group what-if -g $Rg --template-file .\infra\main.bicep `
  --parameters '@infra\public.parameters.local.json'
az deployment group create -g $Rg -n public-mcp --template-file .\infra\main.bicep `
  --parameters '@infra\public.parameters.local.json'
```

既存ProjectにはFunctions専用Managed IdentityのFoundry Userロールを追加します。
公開StorageにもEntra RBAC認証が必要で、コンテナ公開は無効です。ただしこの段階は**ネットワーク的な閉域ではありません**。
PoCの検索クエリもBing処理境界へ送られるため、機密情報を入力しないでください。

## 組織ポリシーによるStorage公開禁止

ARMのvalidate / what-if / createが成功しても、Azure Policyの`modify`により、
Storageの`publicNetworkAccess`が`Disabled`へ変更される場合があります。
発行前に実際の設定を確認してください。

```powershell
az storage account show -g $Rg -n '<STORAGE-ACCOUNT-NAME>' `
  --query '{publicNetworkAccess:publicNetworkAccess,networkRules:networkRuleSet}'
```

この状態でVNet Integration・Storage Private Endpointがないと、Functionsのコード発行が
`InaccessibleStorageException` / `BlobUploadFailedException` / HTTP 403で失敗します。
RBACを追加するだけではネットワーク拒否は解消しません。
ポリシーを上書き・無効化せず、次のいずれかを事前承認してから進めます。

- Functionsの受信は公開HTTPS＋Entra認証のまま、Functions → Storageだけ先にVNet Integration、Blob Private Endpoint、Private DNSを構成する。
- 組織の管理者に正式なポリシー例外を申請し、承認・適用後に公開PoCを再実行する。

前者では、まず以下のネットワークをvalidate / what-ifし、承認済みの差分を作成します。
アドレス空間は既存・将来の接続先と重複しないものを選択してください。

```powershell
az deployment group validate -g $Rg --template-file .\infra\network.bicep `
  --parameters name='<PREFIX>-vnet' dnsZoneScope=storage createDnsResolver=false
az deployment group what-if -g $Rg --template-file .\infra\network.bicep `
  --parameters name='<PREFIX>-vnet' dnsZoneScope=storage createDnsResolver=false
az deployment group create -g $Rg -n network --template-file .\infra\network.bicep `
  --parameters name='<PREFIX>-vnet' dnsZoneScope=storage createDnsResolver=false
```

出力の`functionsSubnetId`、`endpointsSubnetId`、`dnsZoneIds`をmainのパラメーターへ設定し、
`networkMode=public-with-private-storage`、`createFoundryPrivateEndpoint=false`でmainを再適用します。
このモードではStorageだけにPEを作成し、FunctionsにVNet Integrationを設定します。
Functionsの受信は公開HTTPS＋Entra認証、Foundryへの送信は既存の公開経路を維持します。
PC用VPN、Functions/FoundryのPE、DNS Resolverはこの段階では不要です。
後日の完全閉域化時は`dnsZoneScope=all`で残りのDNSゾーンも作成してください。

2026-10-01の検証環境では、組織のStorageポリシーによる公開アクセス無効化をActivity Logで確認しました。
初回の基盤作成とManaged Identityのロール割り当ては成功しましたが、コード発行は上記403で停止しました。
承認後にStorageだけの閉域構成を追加し、Blob PEの`Approved`、Private DNSのAレコード、
VNetリンクの`Completed`、Functionsの統合サブネットを確認しました。
直後はアプリ再起動・停止開始後もKuduのStorage検証段階で同じ403が継続しました。
Storageログの`PutBlob`は想定したManaged Identityによる`OAuth`要求で、
`AuthorizationFailure`でした。その後、設定を変えずに再発行したところ、下記の両方式で成功しました。
既存Foundry Agentへの直接検索と、後述のClaude CodeのOAuth認証は成功していますが、Functions経由の検索ツール実行はまだ未確認です。
ARMの成功をMCPの稼働成功として扱わないでください。

### リモートビルド有効／無効の比較結果

2026-10-01に同じFunctions・Storage・Managed Identityで比較しました。時刻はJSTです。
Firewall、RBAC、Private Endpoint、VNet設定は比較中に変更していません。

| 方式 | Azure側の開始～完了 | 結果 |
|---|---|---|
| リモートビルド有効（既定） | 15:56:25～15:59:24、約179秒 | 発行成功 |
| Linux依存パッケージ同梱、`-LocalBuild` | 16:02:55～16:03:57、約62秒 | Storage検証・アップロード・トリガー同期成功 |

上記時間にローカルの依存取得・zip作成は含みません。初回／後続の発行であるため、性能比較の結論には使いません。
比較後、利用者の指定により**リモートビルド版を正規の発行方式**としました。
2026-10-01 16:20:12 JSTにリモートビルドでの再発行が完了し、現在のアクティブなパッケージです。
通常は`Publish-Mcp.ps1`を`-LocalBuild`なしで実行します。`-LocalBuild`は比較・代替手段として残します。
比較に使用した同梱パッケージはPython 3.12互換のLinux wheelで、Windowsの`pywin32`／`.pyd`は含みません。
再現コマンドとuvの設定は[通常手順の発行節](deployment-guide.ja.md#52-functionsへ発行)に記載しています。

Storageログでは、以前の失敗要求がFunctions統合サブネット外のアドレスから送信されていたのに対し、
成功要求はFunctions統合サブネット内のアドレスから送信され、同じManaged Identityの
`OAuth`要求が`201 Success`となりました。Storageの公開アクセス・共有キーは引き続き無効です。
ネットワーク統合の反映やデプロイ環境の切り替わりが関係した可能性がありますが、内部原因は確定できません。
**この環境で「リモートビルドだけが失敗する」という仮説は再現しませんでした。**

発行後の`/mcp`は未認証で401、パス別OAuth Protected Resource Metadataは200を返しました。
これは認証入口の確認であり、利用者のOAuthログイン後の`tools/call`成功を意味しません。

### Claude CodeのOAuth resource不一致

初回ログインでは`AADSTS9010010 / invalid_target`を再現しました。
PRMの`resource`はHTTPSの`/mcp` URLでしたが、APIアプリには`api://<API-client-id>`しか登録されていませんでした。
同じAPIアプリへHTTPSのMCP URLを追加すると、同じresourceと既存scopeの非対話認証要求が
`invalid_target`ではなく、未ログイン時の期待結果である`login_required`へ変わりました。
スコープ、許可ユーザー、許可クライアント、Easy Authの検査は変更していません。
修正後は古いエラータブを閉じ、`claude mcp login foundry-search`からやり直します。
この非対話確認だけで利用者ログイン・トークン交換の成功を断定しません。

2026-10-01、利用者がログインをやり直し、Claude Codeのブラウザ画面に
`Authentication successful`が表示されたことを確認しました。
途中の`AADSTS50012`の原因は確定していません。テナントの認証ポリシー、
許可ユーザー、許可クライアント、シークレットなしの構成は変更していません。
次にClaude Codeで`foundry-search`の`search_web`を明示的に使わせ、
実際のツール呼び出しと引用URL付き回答を確認します。認証成功だけでは検索経路の完了とはしません。

### Python疎通確認のリダイレクトURI不一致

Claude Codeとは独立した認証確認で`AADSTS50011`が発生しました。
Azure Identityの`InteractiveBrowserCredential`は指定URIの`/callback`を使用せず、
`http://localhost:8765`を送信する一方、公開クライアントにはClaude Code用の
`http://localhost:8765/callback`だけを登録していたことが原因です。
更新版`New-EntraApps.ps1`は既存URIを保持して両方を登録し、`smoke_test.py`もパスなしURIを指定します。
同じstateファイルで登録を更新し、古いログイン処理を終了してから再試行してください。
これはPython検証用設定の修正であり、先にClaude Codeで発生した`AADSTS50012`の原因確定や解消を意味しません。
ブラウザが別のサインイン方式を求める場合は提示された「パスワードを使う」等に従い、
会社のポリシーやAuthenticatorの設定を変更して回避しないでください。

URI修正後のPython確認では、トークン取得時に`AADSTS7000218`が返りました。
公開クライアントのMobile/Desktop URI登録を確認しましたが、原因は未確定です。
`isFallbackPublicClient`の変更やシークレット追加は行っていません。
その後、利用者によるClaude Codeの認証成功を確認したため、Pythonの再試行は停止しました。
Pythonスクリプトの成功とClaude Codeの認証成功は区別して記録します。

### 発行失敗の診断・引き継ぎ

Blobの`StorageRead` / `StorageWrite`診断ログはLog Analyticsの`StorageBlobLogs`へ送信します。
リクエストのメタデータを記録し、ログ取り込み・保持には料金がかかります。
対象ワークスペースで次のKQLを実行して、認証方式・呼び出し元・エラーを確認します。

```kusto
StorageBlobLogs
| where TimeGenerated > ago(1h)
| where StatusCode == "403"
| project TimeGenerated, OperationName, StatusCode, StatusText,
          AuthenticationType, CallerIpAddress, RequesterObjectId
| order by TimeGenerated desc
| take 20
```

PE承認、DNS、VNet Integration、必要なRBACが揃っても発行できない場合は、
AzureサポートへFunction AppのリソースID、UTC時刻、Kudu deployment ID、
上記ログを添えてデプロイ処理のStorage到達経路の調査を依頼します。
Portalの **Diagnose and solve problems → Flex Consumption Deployment** でも失敗段階を確認できます。
VNet診断には反映遅延があるため、診断結果だけで設定反映や到達性を断定しません。
公開アクセスの再有効化、共有キーの有効化、不要なRBAC追加で回避しないでください。

## 検索モデルの変更と切り戻し

2026-10-01、費用削減を目的とする利用者の承認により検索モデルを変更しました。

| 項目 | 通常運用 | 切り戻し先 |
|---|---|---|
| Foundryアカウント / Project | `<FOUNDRY-ACCOUNT>` / `<SEARCH-PROJECT>` | 同じ |
| Agent名 | `<SEARCH-AGENT>` | 同じ |
| Agentバージョン | `2` | `1` |
| モデルデプロイメント | `gpt-5-mini` | `gpt-6-astra` |
| モデルバージョン | `2025-08-07` | `2026-09-03` |
| SKU / 容量 | GlobalStandard / 10（10,000 TPM、10 RPM） | GlobalStandard / 500（変更せず保持） |
| Functions設定 | `FOUNDRY_AGENT_VERSION=2` | `FOUNDRY_AGENT_VERSION=1` |
| Bicep設定 | `infra\public.parameters.local.json` の `agentVersion=2` | 同じファイルで `1` |

旧版の指示（空文字）・Web Searchツール・その他の定義を保持し、モデルだけを変更した不変バージョン2を作成しました。
既存のAstraデプロイメント・Agentバージョン1は削除していません。
Functionsの保存済み参照先がバージョン2であることを確認し、Bicep再適用でも戻らないようパラメーターファイルを更新しました。
MCP URL、Entra認証、ネットワーク、Functionsのリモートビルド版コードは変更していません。

### 変更時の確認結果

本番と同じ`FoundrySearch`コードからバージョン2へ検索を1回実施し、
完了したWeb検索、短い回答、Microsoft LearnのURL引用が返ることを確認しました（約11.1秒）。
これはAzure CLIユーザー資格情報によるFoundry直接呼び出しです。
FunctionsのManaged Identity経由やClaude Codeからの検索全体の成功を示すものではありません。
また、1回の結果は品質・応答速度・費用の比較評価ではありません。
検索ツールの料金はモデル料金とは別で、具体的な費用削減率は未測定です。

### Astraへ切り戻す

PowerShell 7でリポジトリのルートから実行します。
Functionsの設定変更により再起動・一時的な接続断が発生し得ます。
コードの再発行やAgentの再作成、Claude CodeのOAuth再登録は不要です。

```powershell
$Subscription = '<SUBSCRIPTION-ID>'
$Rg = '<POC-RESOURCE-GROUP>'
$FunctionApp = '<FUNCTION-APP>'
$TargetVersion = '1'  # GPT-5-miniへ再切り替えする場合は '2'

az functionapp config appsettings set --subscription $Subscription `
  --resource-group $Rg --name $FunctionApp `
  --settings "FOUNDRY_AGENT_VERSION=$TargetVersion" --output none
if ($LASTEXITCODE -ne 0) { throw 'FunctionsのAgentバージョン変更に失敗しました。' }

$ParameterPath = '.\infra\public.parameters.local.json'
$p = Get-Content -LiteralPath $ParameterPath -Raw | ConvertFrom-Json
$p.parameters.agentVersion.value = $TargetVersion
$p | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $ParameterPath -Encoding utf8

az functionapp config appsettings list --subscription $Subscription `
  --resource-group $Rg --name $FunctionApp `
  --query "[?name=='FOUNDRY_AGENT_NAME' || name=='FOUNDRY_AGENT_VERSION'].{name:name,value:value}" `
  --output table
if ($LASTEXITCODE -ne 0) { throw '変更後の設定確認に失敗しました。' }
```

表示が`<SEARCH-AGENT>` / `1`であることを確認し、Claude Codeから`foundry-search`の`search_web`を実行して引用付き回答を確認します。
GPT-5-miniへ戻す場合は`$TargetVersion='2'`で同じ手順を実施します。
別のCI/CDパラメーターファイルを使っている場合も同じバージョンへ揃えてください。
切り戻し可能な間は、Agentバージョン1および`gpt-6-astra`モデルデプロイメントを削除しないでください。

### モデル・Agentの作成記録

今回のモデルはEast US 2で提供と空きクォータを確認後、次の条件で新規作成しました。
モデルデプロイメントが既にある現在の環境では再作成不要です。

```powershell
az cognitiveservices account deployment create `
  --subscription '<SUBSCRIPTION-ID>' `
  --resource-group '<FOUNDRY-RESOURCE-GROUP>' --name '<FOUNDRY-ACCOUNT>' `
  --deployment-name gpt-5-mini --model-format OpenAI `
  --model-name gpt-5-mini --model-version 2025-08-07 `
  --sku-name GlobalStandard --sku-capacity 10

.\.venv\Scripts\python.exe .\scripts\create_agent.py `
  --endpoint 'https://<FOUNDRY-ACCOUNT>.services.ai.azure.com/api/projects/<SEARCH-PROJECT>' `
  --name '<SEARCH-AGENT>' --from-version 1 --model gpt-5-mini
```

2つ目のコマンドは実行のたびに新しいAgentバージョンを作成します。
今回は戻り値が`2`でしたが、別環境・再実行では実際の戻り値を使用してください。
モデルカタログではこのモデル版の推論廃止予定は2027-02-09と表示されました。運用継続時は最新の提供・廃止予定を再確認します。

## 後から閉域化する順序

1. 同じリージョンにVNet、Functions専用サブネット、PE用サブネット、Private DNSを用意し、PC・配布端末のVPN等の経路を構成します。
2. 現在のFunctions・Storage・Foundryを対象にPEを事前作成し、DNSと接続を確認します。`infra/private-endpoint.bicep` を単独適用できます。mainと同じPE名・DNS zone group名を使用すれば、後続main適用で管理を引き継げます。
3. 現在のFunctionsにVNet Integrationを設定して、Storage／FoundryのPEへ到達できることを確認します。移行作業の影響時間を確保します。
4. **同じRG・namePrefix・Entraアプリ・Agent設定を維持したまま**mainの `networkMode=private`、サブネットID・DNSゾーンIDを設定し、validate / what-if後に適用します。FunctionsとStorageのpublic accessが無効になります。
5. 既存Foundryのpublic accessは自動変更されません。他の利用元への影響を承認してから無効化します。
6. 社内経路で成功、外部経路で拒否、匿名401、Claudeからの検索成功を再確認します。

これはPrivate Endpointによる**受信側**の閉域化です。Foundry Agent実行環境のVNet注入を後付けする手順ではありません。
Web Search自体は公開エンドポイント経由のままです。
