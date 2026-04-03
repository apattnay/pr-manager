/**
 * PR Review MCP — VS Code Extension (v0.3.0)
 *
 * Registers the bundled Python MCP server via the standard
 * `vscode.lm.registerMcpServerDefinitionProvider` API so that
 * Copilot Chat (Agent mode) can use its PR-review tools.
 *
 * No manual `.vscode/mcp.json` is needed — the extension handles
 * everything automatically on activation.
 *
 * Settings (VS Code Settings UI → "PR Review MCP"):
 *   prReviewMcp.githubToken      — PAT (falls back to $GITHUB_TOKEN / ~/.netrc)
 *   prReviewMcp.githubApiBase    — REST base URL
 *   prReviewMcp.githubGraphqlUrl — GraphQL endpoint
 *   prReviewMcp.pythonPath       — Python interpreter
 */

import * as vscode from "vscode";
import * as path from "path";
import * as fs from "fs";
import * as os from "os";
import * as cp from "child_process";

let outputChannel: vscode.OutputChannel;

// ── helpers ────────────────────────────────────────────────────────────────

function cfg<T>(key: string): T {
  return vscode.workspace.getConfiguration("prReviewMcp").get<T>(key) as T;
}

/** Absolute path to the bundled server.py inside the installed extension. */
function serverScript(context: vscode.ExtensionContext): string {
  return path.join(context.extensionPath, "mcp_server", "server.py");
}

function parseHostFromApiBase(apiBase: string): string {
  try {
    return new URL(apiBase).hostname;
  } catch {
    return "";
  }
}

function extractTokenFromNetrc(hosts: string[]): string {
  const netrcPath = path.join(os.homedir(), ".netrc");
  if (!fs.existsSync(netrcPath)) {
    return "";
  }
  try {
    const content = fs.readFileSync(netrcPath, "utf8");
    for (const host of hosts) {
      const escapedHost = host.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const machineBlock = new RegExp(
        `(?:^|\\n)\\s*machine\\s+${escapedHost}([\\s\\S]*?)(?=\\n\\s*machine\\s+|$)`,
        "i"
      );
      const blockMatch = content.match(machineBlock);
      if (!blockMatch) {
        continue;
      }
      const passwordMatch = blockMatch[1].match(/(?:^|\s)password\s+([^\s]+)/i);
      if (passwordMatch?.[1]) {
        return passwordMatch[1].trim();
      }
    }
  } catch {
    return "";
  }
  return "";
}

/** Resolve a GitHub token from settings → env → ~/.netrc. */
function resolveToken(): string {
  const fromSettings = cfg<string>("githubToken");
  if (fromSettings) {
    return fromSettings;
  }
  const fromEnv = process.env.GITHUB_TOKEN || "";
  if (fromEnv) {
    return fromEnv;
  }
  const apiHost = parseHostFromApiBase(cfg<string>("githubApiBase") || "");
  const hostsToTry = [apiHost, "github.com", "api.github.com", "github.intel.com"]
    .filter(Boolean);
  return extractTokenFromNetrc(hostsToTry);
}

/** Build the env vars object for the MCP server process. */
function buildServerEnv(): Record<string, string> {
  return {
    GITHUB_TOKEN: resolveToken(),
    GITHUB_API_BASE: cfg<string>("githubApiBase") || "https://api.github.com",
    GITHUB_GRAPHQL_URL:
      cfg<string>("githubGraphqlUrl") || "https://api.github.com/graphql",
  };
}

// ── MCP server provider (standard VS Code API) ───────────────────────────

function createMcpProvider(
  context: vscode.ExtensionContext,
  didChangeEmitter: vscode.EventEmitter<void>
): vscode.McpServerDefinitionProvider {
  return {
    onDidChangeMcpServerDefinitions: didChangeEmitter.event,

    provideMcpServerDefinitions: async () => {
      const pythonPath = cfg<string>("pythonPath") || "python3";
      const script = serverScript(context);

      outputChannel.appendLine(
        `Providing MCP server: ${pythonPath} ${script}`
      );

      // Constructor: (label, command, args?, env?, version?)
      const server = new vscode.McpStdioServerDefinition(
        "PR Review MCP",
        pythonPath,
        [script],
        buildServerEnv(),
        "0.3.0"
      );
      server.cwd = vscode.Uri.file(path.dirname(script));

      return [server];
    },

    resolveMcpServerDefinition: async (
      server: vscode.McpStdioServerDefinition
    ) => {
      // Refresh the token at resolve time (it may have been set after discovery)
      const token = resolveToken();
      if (!token) {
        const choice = await vscode.window.showWarningMessage(
          "PR Review MCP: No GitHub token found. Configure in settings, set GITHUB_TOKEN, or add to ~/.netrc.",
          "Open Settings"
        );
        if (choice === "Open Settings") {
          await vscode.commands.executeCommand(
            "workbench.action.openSettings",
            "prReviewMcp.githubToken"
          );
        }
      }
      // Update env with latest values
      server.env = buildServerEnv();
      return server;
    },
  };
}

// ── activation / deactivation ─────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext): void {
  outputChannel = vscode.window.createOutputChannel("PR Review MCP");

  // 1. Register MCP server via the standard provider API
  const didChangeEmitter = new vscode.EventEmitter<void>();
  context.subscriptions.push(didChangeEmitter);

  context.subscriptions.push(
    vscode.lm.registerMcpServerDefinitionProvider(
      "pr-review-mcp.server",
      createMcpProvider(context, didChangeEmitter)
    )
  );

  // 2. Register restart command → fires change event so VS Code re-discovers
  context.subscriptions.push(
    vscode.commands.registerCommand("prReviewMcp.restartServer", () => {
      didChangeEmitter.fire();
      vscode.window.showInformationMessage("PR Review MCP server restarted.");
    })
  );

  // 3. Re-register on config changes
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("prReviewMcp")) {
        outputChannel.appendLine("Configuration changed — refreshing MCP server.");
        didChangeEmitter.fire();
      }
    })
  );

  // 4. Validate Python + deps
  checkPythonDeps();

  outputChannel.appendLine("PR Review MCP extension activated (v0.3.0).");
  outputChannel.appendLine(
    `Server script: ${serverScript(context)}`
  );
  showSetupHints();
}

export function deactivate(): void {
  // VS Code manages the server lifecycle via the provider API
}

// ── setup helpers ─────────────────────────────────────────────────────────

function checkPythonDeps(): void {
  const pythonPath = cfg<string>("pythonPath") || "python3";
  cp.exec(
    `${pythonPath} -c "import mcp; import httpx; print('ok')"`,
    (err, stdout) => {
      if (err || stdout.trim() !== "ok") {
        const msg =
          "PR Review MCP: Python dependencies (mcp, httpx) not found. " +
          'Run: pip install "mcp[cli]>=1.0.0" httpx';
        outputChannel.appendLine(msg);
        vscode.window.showWarningMessage(msg, "Install Now").then((choice) => {
          if (choice === "Install Now") {
            const terminal = vscode.window.createTerminal("PR Review MCP Setup");
            terminal.show();
            terminal.sendText(
              `${pythonPath} -m pip install "mcp[cli]>=1.0.0" "httpx>=0.27.0"`
            );
          }
        });
      } else {
        outputChannel.appendLine("Python dependencies OK.");
      }
    }
  );
}

function showSetupHints(): void {
  const token = resolveToken();
  if (!token) {
    vscode.window
      .showWarningMessage(
        "PR Review MCP: No GitHub token configured. Set `prReviewMcp.githubToken` in settings or export GITHUB_TOKEN.",
        "Open Settings"
      )
      .then((choice) => {
        if (choice === "Open Settings") {
          vscode.commands.executeCommand(
            "workbench.action.openSettings",
            "prReviewMcp.githubToken"
          );
        }
      });
  }
}
