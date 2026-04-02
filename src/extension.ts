/**
 * PR Review MCP — VS Code Extension
 *
 * On activation, registers the bundled Python MCP server so that
 * Copilot Chat (Agent mode) can use its PR-review tools.
 *
 * Settings:
 *   prReviewMcp.githubToken      — PAT (falls back to $GITHUB_TOKEN)
 *   prReviewMcp.githubApiBase    — REST base URL
 *   prReviewMcp.githubGraphqlUrl — GraphQL endpoint
 *   prReviewMcp.pythonPath       — Python interpreter
 */

import * as vscode from "vscode";
import * as path from "path";
import * as cp from "child_process";

let serverProcess: cp.ChildProcess | undefined;
let outputChannel: vscode.OutputChannel;

// ── helpers ────────────────────────────────────────────────────────────────

function cfg<T>(key: string): T {
  return vscode.workspace.getConfiguration("prReviewMcp").get<T>(key) as T;
}

function serverScript(): string {
  return path.join(__dirname, "..", "mcp_server", "server.py");
}

function buildEnv(): NodeJS.ProcessEnv {
  const token = cfg<string>("githubToken") || process.env.GITHUB_TOKEN || "";
  return {
    ...process.env,
    GITHUB_TOKEN: token,
    GITHUB_API_BASE: cfg<string>("githubApiBase"),
    GITHUB_GRAPHQL_URL: cfg<string>("githubGraphqlUrl"),
  };
}

// ── server lifecycle ──────────────────────────────────────────────────────

function startServer(): void {
  stopServer();

  const pythonPath = cfg<string>("pythonPath") || "python3";
  const script = serverScript();

  outputChannel.appendLine(`Starting MCP server: ${pythonPath} ${script}`);

  serverProcess = cp.spawn(pythonPath, [script], {
    env: buildEnv(),
    cwd: path.dirname(script),
    stdio: ["pipe", "pipe", "pipe"],
  });

  serverProcess.stderr?.on("data", (data: Buffer) => {
    outputChannel.appendLine(`[server] ${data.toString().trimEnd()}`);
  });

  serverProcess.on("exit", (code) => {
    outputChannel.appendLine(`MCP server exited with code ${code}`);
    serverProcess = undefined;
  });

  outputChannel.appendLine("MCP server started (stdio transport).");
}

function stopServer(): void {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = undefined;
    outputChannel.appendLine("MCP server stopped.");
  }
}

// ── activation / deactivation ─────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext): void {
  outputChannel = vscode.window.createOutputChannel("PR Review MCP");

  // Validate Python + deps on first activation
  checkPythonDeps();

  // Register the restart command
  context.subscriptions.push(
    vscode.commands.registerCommand("prReviewMcp.restartServer", () => {
      startServer();
      vscode.window.showInformationMessage("PR Review MCP server restarted.");
    })
  );

  // React to config changes
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("prReviewMcp")) {
        outputChannel.appendLine("Configuration changed — restarting server.");
        startServer();
      }
    })
  );

  outputChannel.appendLine("PR Review MCP extension activated.");
  outputChannel.appendLine(
    "Use the MCP server via Copilot Chat (Agent mode) or configure in .vscode/mcp.json."
  );
  showSetupHints();
}

export function deactivate(): void {
  stopServer();
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
          "Run: pip install 'mcp[cli]>=1.0.0' httpx";
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
  const token = cfg<string>("githubToken") || process.env.GITHUB_TOKEN;
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
