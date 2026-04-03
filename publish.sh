#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# PR Review MCP — Publish to VS Code Marketplace
# ─────────────────────────────────────────────────────────────────
#
# Usage:
#   ./publish.sh <YOUR_AZURE_DEVOPS_PAT>
#
# Pre-requisites (one-time setup):
#   1. Go to https://marketplace.visualstudio.com/manage
#      → Sign in with your Microsoft / GitHub account
#      → Click "Create Publisher"
#      → Publisher ID: apattnay
#      → Display Name: Aurodeepta Pattnayak
#      → Save
#
#   2. Go to https://dev.azure.com/apattnay/_usersSettings/tokens
#      (replace "apattnay" with your Azure DevOps org if different)
#      → New Token
#      → Name: vsce-publish
#      → Organization: All accessible organizations
#      → Scopes: Custom → check "Marketplace → Manage"
#      → Create → Copy the token
#
#   3. Run this script:
#      ./publish.sh <paste-token-here>
#
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <AZURE_DEVOPS_PAT>"
    echo ""
    echo "Get a PAT from: https://dev.azure.com/<your-org>/_usersSettings/tokens"
    echo "  → Scopes: Marketplace → Manage"
    exit 1
fi

PAT="$1"
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "═══════════════════════════════════════════════════"
echo "  Publishing PR Review MCP v1.0.0"
echo "═══════════════════════════════════════════════════"
echo ""

# Step 1: Verify the PAT works for publisher "apattnay"
echo "→ Verifying PAT for publisher 'apattnay'..."
npx @vscode/vsce verify-pat apattnay --pat "$PAT"

# Step 2: Publish
echo ""
echo "→ Publishing to VS Code Marketplace..."
npx @vscode/vsce publish --pat "$PAT" --no-dependencies

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✅ Published!"
echo ""
echo "  View at: https://marketplace.visualstudio.com/items?itemName=aurodeeptapattnayak.pr-review-mcp"
echo ""
echo "  Users can install with:"
echo "    code --install-extension aurodeeptapattnayak.pr-review-mcp"
echo "═══════════════════════════════════════════════════"
