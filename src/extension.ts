/**
 * PR Review MCP — VS Code Extension (v1.1.0)
 *
 * Registers the bundled Python MCP server via the standard
 * `vscode.lm.registerMcpServerDefinitionProvider` API so that
 * Copilot Chat (Agent mode) can use its PR-review tools.
 *
 * **Auto-setup on activation:**
 * 1. Discovers Python >= 3.11 on the system (python3, python, py, etc.)
 * 2. If `mcp` / `httpx` are missing, offers one-click install
 * 3. Persists the discovered Python path in settings
 *
 * Settings (VS Code Settings UI -> "PR Review MCP"):
 *   prReviewMcp.githubToken      — PAT (falls back to $GITHUB_TOKEN / ~/.netrc)
 *   prReviewMcp.githubApiBase    — REST base URL
 *   prReviewMcp.githubGraphqlUrl — GraphQL endpoint
 *   prReviewMcp.pythonPath       — Python interpreter (auto-detected if empty)
 */

import * as vscode from "vscode";
import * as path from "path";
import * as fs from "fs";
import * as os from "os";
import * as cp from "child_process";

let outputChannel: vscode.OutputChannel;

/* Minimum required Python version for the `mcp` SDK. */
const MIN_PYTHON_MAJOR = 3;
const MIN_PYTHON_MINOR = 11;

// ── helpers ───────────────────────────────────────────────────────────────

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
      const passwordMatch = blockMatch[1].match(
        /(?:^|\s)password\s+([^\s]+)/i
      );
      if (passwordMatch?.[1]) {
        return passwordMatch[1].trim();
      }
    }
  } catch {
    return "";
  }
  return "";
}

/** Resolve a GitHub token from settings -> env -> ~/.netrc. */
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
  const hostsToTry = [
    apiHost,
    "github.com",
    "api.github.com",
    "github.intel.com",
  ].filter(Boolean);
  return extractTokenFromNetrc(hostsToTry);
}

/** Build the env vars object for the MCP server process. */
function buildServerEnv(): Record<string, string> {
  return {
    GITHUB_TOKEN: resolveToken(),
    GITHUB_API_BASE:
      cfg<string>("githubApiBase") || "https://api.github.com",
    GITHUB_GRAPHQL_URL:
      cfg<string>("githubGraphqlUrl") || "https://api.github.com/graphql",
  };
}

// ── Python discovery ─────────────────────────────────────────────────────

interface PythonInfo {
  path: string;
  version: string;
  major: number;
  minor: number;
}

/**
 * Run a command and return trimmed stdout, or "" on failure.
 * Short timeout because we are just probing.
 */
function execProbe(cmd: string): Promise<string> {
  return new Promise((resolve) => {
    cp.exec(cmd, { timeout: 10_000 }, (err, stdout) => {
      if (err) {
        resolve("");
      } else {
        resolve((stdout || "").trim());
      }
    });
  });
}

/**
 * Probe a single candidate to see if it is a usable Python >= 3.11.
 * Returns PythonInfo or null.
 */
async function probePython(candidate: string): Promise<PythonInfo | null> {
  const versionStr = await execProbe(
    `${candidate} -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"`,
  );
  if (!versionStr) {
    return null;
  }
  const parts = versionStr.split(".");
  if (parts.length < 2) {
    return null;
  }
  const major = parseInt(parts[0], 10);
  const minor = parseInt(parts[1], 10);
  if (isNaN(major) || isNaN(minor)) {
    return null;
  }
  if (
    major < MIN_PYTHON_MAJOR ||
    (major === MIN_PYTHON_MAJOR && minor < MIN_PYTHON_MINOR)
  ) {
    return null;
  }
  const realPath = await execProbe(
    `${candidate} -c "import sys; print(sys.executable)"`,
  );
  return {
    path: realPath || candidate,
    version: `${major}.${minor}`,
    major,
    minor,
  };
}

/**
 * Return an ordered list of Python commands to try.
 * Platform-aware: on Windows we include `py -3` and `python`; on Unix
 * we prefer `python3` and versioned variants.
 */
function pythonCandidates(): string[] {
  const isWindows = process.platform === "win32";
  const candidates: string[] = [];

  if (isWindows) {
    candidates.push("py -3.13", "py -3.12", "py -3.11", "py -3");
    candidates.push("python3", "python");
    const localAppData = process.env.LOCALAPPDATA || "";
    const programFiles = process.env.ProgramFiles || "C:\\Program Files";
    for (const ver of ["313", "312", "311"]) {
      if (localAppData) {
        candidates.push(
          `"${localAppData}\\Programs\\Python\\Python${ver}\\python.exe"`,
        );
      }
      candidates.push(
        `"${programFiles}\\Python${ver}\\python.exe"`,
      );
    }
  } else {
    candidates.push(
      "python3.13",
      "python3.12",
      "python3.11",
      "python3",
      "python",
    );
    candidates.push("/usr/local/bin/python3", "/opt/homebrew/bin/python3");
  }
  return candidates;
}

/**
 * Discover the best available Python >= 3.11 on the system.
 * If the user already configured prReviewMcp.pythonPath, try that first.
 */
async function discoverPython(): Promise<PythonInfo | null> {
  const userConfigured = cfg<string>("pythonPath") || "";

  if (userConfigured) {
    const info = await probePython(userConfigured);
    if (info) {
      outputChannel.appendLine(
        `OK  Configured Python: ${info.path} (${info.version})`,
      );
      return info;
    }
    outputChannel.appendLine(
      `WARN  Configured pythonPath "${userConfigured}" is not Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} or not found.`,
    );
  }

  outputChannel.appendLine("Searching for Python >= 3.11 on the system...");
  for (const candidate of pythonCandidates()) {
    const info = await probePython(candidate);
    if (info) {
      outputChannel.appendLine(
        `OK  Found: ${candidate} -> ${info.path} (${info.version})`,
      );
      return info;
    }
  }

  outputChannel.appendLine("FAIL  No Python >= 3.11 found on the system.");
  return null;
}

// ── dependency checking & auto-install ────────────────────────────────────

async function hasDeps(pythonPath: string): Promise<boolean> {
  const result = await execProbe(
    `${pythonPath} -c "import mcp; import httpx; print('ok')"`,
  );
  return result === "ok";
}

function installDeps(pythonPath: string): Promise<boolean> {
  return new Promise((resolve) => {
    const pipCmd = `${pythonPath} -m pip install "mcp[cli]>=1.0.0" "httpx>=0.27.0"`;
    outputChannel.appendLine(`Installing dependencies: ${pipCmd}`);
    cp.exec(pipCmd, { timeout: 120_000 }, (err, stdout, stderr) => {
      if (stdout) {
        outputChannel.appendLine(stdout);
      }
      if (stderr) {
        outputChannel.appendLine(stderr);
      }
      if (err) {
        outputChannel.appendLine(`FAIL  pip install failed: ${err.message}`);
        resolve(false);
      } else {
        outputChannel.appendLine("OK  Dependencies installed successfully.");
        resolve(true);
      }
    });
  });
}

/**
 * Full auto-setup flow:
 * 1. Discover Python >= 3.11
 * 2. Check deps, offer to install if missing
 * 3. Persist the working pythonPath in settings
 * 4. Fire the change emitter so VS Code restarts the MCP server
 */
async function autoSetup(
  didChangeEmitter: vscode.EventEmitter<void>,
): Promise<void> {
  const python = await discoverPython();

  if (!python) {
    const msg =
      `PR Review MCP: No Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} found. ` +
      "Please install Python 3.11+ from https://www.python.org/downloads/ " +
      "and restart VS Code.";
    outputChannel.appendLine(msg);
    const choice = await vscode.window.showErrorMessage(
      msg,
      "Download Python",
      "Set Path Manually",
    );
    if (choice === "Download Python") {
      vscode.env.openExternal(
        vscode.Uri.parse("https://www.python.org/downloads/"),
      );
    } else if (choice === "Set Path Manually") {
      vscode.commands.executeCommand(
        "workbench.action.openSettings",
        "prReviewMcp.pythonPath",
      );
    }
    return;
  }

  // Persist discovered Python path in user settings
  const currentSetting = cfg<string>("pythonPath") || "";
  if (currentSetting !== python.path) {
    await vscode.workspace
      .getConfiguration("prReviewMcp")
      .update("pythonPath", python.path, vscode.ConfigurationTarget.Global);
    outputChannel.appendLine(
      `Updated prReviewMcp.pythonPath -> "${python.path}"`,
    );
  }

  // Check dependencies
  const depsOk = await hasDeps(python.path);
  if (depsOk) {
    outputChannel.appendLine("OK  All dependencies satisfied.");
    return;
  }

  outputChannel.appendLine(
    "Dependencies (mcp, httpx) missing - prompting for install...",
  );

  const choice = await vscode.window.showWarningMessage(
    `PR Review MCP: Python ${python.version} found at ${python.path}, ` +
      "but required packages (mcp, httpx) are not installed.",
    "Install Now",
    "Install in Terminal",
    "Skip",
  );

  if (choice === "Install Now") {
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "PR Review MCP: Installing dependencies...",
        cancellable: false,
      },
      async () => {
        const ok = await installDeps(python.path);
        if (ok) {
          vscode.window.showInformationMessage(
            "PR Review MCP: Dependencies installed! Server starting...",
          );
          didChangeEmitter.fire();
        } else {
          vscode.window.showErrorMessage(
            "PR Review MCP: pip install failed. Check Output panel (PR Review MCP) for details.",
          );
        }
      },
    );
  } else if (choice === "Install in Terminal") {
    const terminal = vscode.window.createTerminal("PR Review MCP Setup");
    terminal.show();
    terminal.sendText(
      `${python.path} -m pip install "mcp[cli]>=1.0.0" "httpx>=0.27.0"`,
    );
    vscode.window.showInformationMessage(
      "PR Review MCP: After install completes, run 'PR Review MCP: Restart Server' from the Command Palette.",
    );
  }
}

// ── MCP server provider (standard VS Code API) ──────────────────────────

function createMcpProvider(
  context: vscode.ExtensionContext,
  didChangeEmitter: vscode.EventEmitter<void>,
): vscode.McpServerDefinitionProvider {
  return {
    onDidChangeMcpServerDefinitions: didChangeEmitter.event,

    provideMcpServerDefinitions: async () => {
      const pythonPath = cfg<string>("pythonPath") || "python3";
      const script = serverScript(context);

      outputChannel.appendLine(
        `Providing MCP server: ${pythonPath} ${script}`,
      );

      const server = new vscode.McpStdioServerDefinition(
        "PR Review MCP",
        pythonPath,
        [script],
        buildServerEnv(),
        "1.1.0",
      );
      server.cwd = vscode.Uri.file(path.dirname(script));

      return [server];
    },

    resolveMcpServerDefinition: async (
      server: vscode.McpStdioServerDefinition,
    ) => {
      const token = resolveToken();
      if (!token) {
        const choice = await vscode.window.showWarningMessage(
          "PR Review MCP: No GitHub token found. Configure in settings, set GITHUB_TOKEN, or add to ~/.netrc.",
          "Open Settings",
        );
        if (choice === "Open Settings") {
          await vscode.commands.executeCommand(
            "workbench.action.openSettings",
            "prReviewMcp.githubToken",
          );
        }
      }
      server.env = buildServerEnv();
      return server;
    },
  };
}

// ── activation / deactivation ────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext): void {
  outputChannel = vscode.window.createOutputChannel("PR Review MCP");

  const didChangeEmitter = new vscode.EventEmitter<void>();
  context.subscriptions.push(didChangeEmitter);

  context.subscriptions.push(
    vscode.lm.registerMcpServerDefinitionProvider(
      "pr-review-mcp.server",
      createMcpProvider(context, didChangeEmitter),
    ),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("prReviewMcp.restartServer", () => {
      didChangeEmitter.fire();
      vscode.window.showInformationMessage(
        "PR Review MCP server restarted.",
      );
    }),
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("prReviewMcp")) {
        outputChannel.appendLine(
          "Configuration changed - refreshing MCP server.",
        );
        didChangeEmitter.fire();
      }
    }),
  );

  // Auto-discover Python >= 3.11 and install deps
  autoSetup(didChangeEmitter);

  showSetupHints();

  outputChannel.appendLine("PR Review MCP extension activated (v1.1.0).");
  outputChannel.appendLine(`Server script: ${serverScript(context)}`);
}

export function deactivate(): void {
  // VS Code manages the server lifecycle via the provider API
}

// ── setup helpers ────────────────────────────────────────────────────────

function showSetupHints(): void {
  const token = resolveToken();
  if (!token) {
    vscode.window
      .showWarningMessage(
        "PR Review MCP: No GitHub token configured. " +
          "Set prReviewMcp.githubToken in settings or export GITHUB_TOKEN.",
        "Open Settings",
      )
      .then((choice) => {
        if (choice === "Open Settings") {
          vscode.commands.executeCommand(
            "workbench.action.openSettings",
            "prReviewMcp.githubToken",
          );
        }
      });
  }
}
