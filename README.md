# 閉域クライアントから使う Foundry Web 検索 MCP（方式2）

Claude Code のエージェントループはPCに残し、**Web検索だけを Azure Functions の MCP → Foundry Prompt Agent → Web Search に委譲**します。既存の Foundry / Claude デプロイメントを利用します。

**[構築・運用手順書（日本語）](docs/deployment-guide.ja.md)** に、追加リソース、Entra ID、閉域ネットワーク、構築コマンド、Claude Code 設定、受入確認、復旧手順をまとめています。

先に機能だけ確認する場合は **[公開HTTPS PoCと後日の閉域化](docs/public-poc.ja.md)** を使用します。公開モードでもEntra ID認証は必須です。Storageの公開を禁止する組織では、Functionsの入口は公開のままStorageだけ先に閉域化できます。

| 成果物 | 場所 |
|---|---|
| Functions MCP サーバ（Python、Streamable HTTP） | `src/mcp_server/` |
| VNet / Private DNS / DNS Resolver（任意） | `infra/network.bicep` |
| Functions Flex / Storage / Private Endpoint / Managed Identity / RBAC / 監視 | `infra/main.bicep` |
| 既存 Foundry 内のプロジェクト追加（必要な場合のみ） | `infra/project.bicep` |
| Entra API / 公開クライアントの登録 | `scripts/New-EntraApps.ps1` |
| 共通検索エージェントのバージョン作成 | `scripts/create_agent.py` |
| Functions 発行 / 実環境受入確認 | `scripts/Publish-Mcp.ps1`、`scripts/smoke_test.py` |
| Claude Code / 管理者ポリシー例 | `examples/` |
| オフライン単体・MCPプロトコル・SDK契約テスト | `tests/` |

## 重要な境界

- PC→Functions / Foundry は Private Endpoint。PC→Entra ID の認証通信は別途必要です。
- Foundry Web Search は **公開エンドポイントを利用**します。検索クエリは Bing の処理境界に送られ、Azure の DPA・地理的境界内に完結しません。ソースコード、秘密情報、個人情報を検索に渡さない運用が必要です。
- `WebFetch` / `WebSearch` の deny はネットワーク遮断ではありません。Bash、Python、別MCPなどからの迂回は、端末・ネットワーク側の送信制御で防ぎます。
- Functions の SDK 型 MCP ホスティングと Easy Auth の MCP メタデータはプレビューです。本番採用前に組織の承認と実環境受入確認が必要です。
- このリポジトリは **構築用成果物**です。実環境での確認状況・制約は[PoC手順](docs/public-poc.ja.md)に記載しています。

## ローカル検証

PowerShell 7、Python 3.12 または 3.13（Azure は 3.12）を使用します。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
az bicep build --file .\infra\main.bicep --stdout > $null
az bicep build --file .\infra\network.bicep --stdout > $null
```

テストは実際のWeb検索をせず、Foundry応答をモックします。Azure上のDNS、Easy Auth、RBAC、モデル対応は手順書の受入確認で検証してください。