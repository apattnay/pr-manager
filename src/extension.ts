/**
 * PR Review MCP — VS Code Extension (v1.1.1)
 *
 * Registers the bundled Python MCP server via the standard
 * `vscode.lm.registerMcpServerDefinitionProvider` API so that
 * Copilot Chat (Agent mode) can use its PR-review tools.
 *
 * **Auto-setup on activation:**
 * 1. Discovers a **system** Python >= 3.11 (skips project venvs)
 * 2. Creates a dedicated venv inside the extension directory
 * 3. Installs mcp + httpx into that isolated venv
 * 4. Persists the venv Python path in settings
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
  isVenv: boolean;
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

/** Run a longer command (venv creation, pip install). */
function execLong(cmd: string, timeoutMs = 120_000): Promise<{ ok: boolean; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    cp.exec(cmd, { timeout: timeoutMs }, (err, stdout, stderr) => {
      resolve({
        ok: !err,
        stdout: (stdout || "").trim(),
        stderr: (stderr || "").trim(),
      });
    });
  });
}

/**
 * Check if a resolved Python path lives inside a virtual environment.
 */
function isVenvPath(pythonPath: string): boolean {
  const normalized = pythonPath.replace(/\\/g, "/").toLowerCase();
  return (
    normalized.includes("/.venv/") ||
    normalized.includes("/venv/") ||
    normalized.includes("/virtualenvs/") ||
    normalized.includes("/envs/") ||
    normalized.includes("\\.venv\\") ||
    normalized.includes("\\venv\\")
  );
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
  const resolved = realPath || candidate;
  return {
    path: resolved,
    version: `${major}.${minor}`,
    major,
    minor,
    isVenv: isVenvPath(resolved),
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
    // Linux / macOS — prefer versioned names, then common absolute paths
    candidates.push(
      "python3.13",
      "python3.12",
      "python3.11",
      "python3",
      "python",
    );
    candidates.push(
      "/usr/local/bin/python3.13",
      "/usr/local/bin/python3.12",
      "/usr/local/bin/python3.11",
      "/usr/local/bin/python3",
      "/usr/bin/python3",
      "/opt/homebrew/bin/python3",
    );
  }
  return candidates;
}

/**
 * Discover the best available Python >= 3.11 on the system.
 * Prefers system Pythons over venv Pythons.
 * If the user already configured prReviewMcp.pythonPath, try that first.
 */
async function discoverPython(): Promise<PythonInfo | null> {
  const userConfigured = cfg<string>("pythonPath") || "";

  // 1. If user explicitly configured a path, trust it unconditionally
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

  // 2. Probe all candidates — collect system and venv Pythons separately
  outputChannel.appendLine("Searching for Python >= 3.11 on the system...");
  const systemHits: PythonInfo[] = [];
  const venvHits: PythonInfo[] = [];

  for (const candidate of pythonCandidates()) {
    const info = await probePython(candidate);
    if (!info) {
      continue;
    }
    if (info.isVenv) {
      outputChannel.appendLine(
        `SKIP  ${candidate} -> ${info.path} (${info.version}) [project venv — skipped]`,
      );
      venvHits.push(info);
    } else {
      outputChannel.appendLine(
        `OK  Found: ${candidate} -> ${info.path} (${info.version})`,
      );
      systemHits.push(info);
    }
  }

  // Prefer system Python; fall back to venv Python only as last resort
  if (systemHits.length > 0) {
    return systemHits[0];
  }
  if (venvHits.length > 0) {
    outputChannel.appendLine(
      "WARN  No system Python >= 3.11 found; falling back to venv Python.",
    );
    return venvHits[0];
  }

  outputChannel.appendLine("FAIL  No Python >= 3.11 found on the system.");
  return null;
}

// ── dedicated venv & dependency install ───────────────────────────────────

/**
 * Path to the extension's own isolated venv.
 */
function mcpVenvDir(context: vscode.ExtensionContext): string {
  // Use globalStorageUri so it survives extension updates
  return path.join(context.globalStorageUri.fsPath, "mcp-venv");
}

/**
 * Python executable inside the dedicated venv.
 */
function mcpVenvPython(context: vscode.ExtensionContext): string {
  const isWindows = process.platform === "win32";
  const venvDir = mcpVenvDir(context);
  return isWindows
    ? path.join(venvDir, "Scripts", "python.exe")
    : path.join(venvDir, "bin", "python");
}

/**
 * Check if the given Python has `mcp` and `httpx` importable.
 */
async function hasDeps(pythonPath: string): Promise<boolean> {
  const result = await execProbe(
    `${pythonPath} -c "import mcp; import httpx; print('ok')"`,
  );
  return result === "ok";
}

/**
 * Check if pip is available for a Python.
 */
async function hasPip(pythonPath: string): Promise<boolean> {
  const result = await execProbe(`${pythonPath} -m pip --version`);
  return result.startsWith("pip ");
}

/**
 * Check if uv is available on the system.
 */
async function hasUv(): Promise<boolean> {
  const result = await execProbe("uv --version");
  return result.length > 0;
}

/**
 * Create a dedicated venv and install deps into it.
 * Returns the venv Python path on success, or "" on failure.
 */
async function createMcpVenv(
  basePython: string,
  context: vscode.ExtensionContext,
): Promise<string> {
  const venvDir = mcpVenvDir(context);
  const venvPy = mcpVenvPython(context);

  // Ensure the parent directory exists
  const parentDir = path.dirname(venvDir);
  if (!fs.existsSync(parentDir)) {
    fs.mkdirSync(parentDir, { recursive: true });
  }

  // Try creating venv with uv first (faster, always includes pip-compatible installer)
  const uvAvailable = await hasUv();
  if (uvAvailable) {
    outputChannel.appendLine(`Creating venv with uv: uv venv --python ${basePython} "${venvDir}"`);
    const uvResult = await execLong(`uv venv --python ${basePython} "${venvDir}"`);
    if (uvResult.ok && fs.existsSync(venvPy)) {
      outputChannel.appendLine("OK  venv created with uv.");
      // Install deps with uv pip
      outputChannel.appendLine("Installing dependencies with uv pip...");
      const installResult = await execLong(
        `uv pip install --python "${venvPy}" "mcp[cli]>=1.0.0" "httpx>=0.27.0"`,
      );
      if (installResult.stdout) { outputChannel.appendLine(installResult.stdout); }
      if (installResult.stderr) { outputChannel.appendLine(installResult.stderr); }
      if (installResult.ok) {
        outputChannel.appendLine("OK  Dependencies installed via uv pip.");
        return venvPy;
      }
      outputChannel.appendLine("WARN  uv pip install failed.");
    } else {
      if (uvResult.stderr) { outputChannel.appendLine(uvResult.stderr); }
      outputChannel.appendLine("WARN  uv venv creation failed.");
    }
  }

  // Fallback: create venv with stdlib venv module
  outputChannel.appendLine(`Creating venv: ${basePython} -m venv "${venvDir}"`);
  const venvResult = await execLong(`${basePython} -m venv "${venvDir}"`);
  if (!venvResult.ok || !fs.existsSync(venvPy)) {
    if (venvResult.stderr) { outputChannel.appendLine(venvResult.stderr); }
    outputChannel.appendLine("FAIL  Could not create venv.");
    return "";
  }
  outputChannel.appendLine("OK  venv created.");

  // Install deps with pip inside the fresh venv
  const pipAvail = await hasPip(venvPy);
  if (pipAvail) {
    outputChannel.appendLine("Installing dependencies with pip...");
    const pipResult = await execLong(
      `"${venvPy}" -m pip install "mcp[cli]>=1.0.0" "httpx>=0.27.0"`,
    );
    if (pipResult.stdout) { outputChannel.appendLine(pipResult.stdout); }
    if (pipResult.stderr) { outputChannel.appendLine(pipResult.stderr); }
    if (pipResult.ok) {
      outputChannel.appendLine("OK  Dependencies installed via pip.");
      return venvPy;
    }
    outputChannel.appendLine("WARN  pip install inside venv failed.");
  }

  // Last try: uv pip into the stdlib-created venv
  if (uvAvailable) {
    outputChannel.appendLine("Trying uv pip install into stdlib venv...");
    const uvPipResult = await execLong(
      `uv pip install --python "${venvPy}" "mcp[cli]>=1.0.0" "httpx>=0.27.0"`,
    );
    if (uvPipResult.stdout) { outputChannel.appendLine(uvPipResult.stdout); }
    if (uvPipResult.stderr) { outputChannel.appendLine(uvPipResult.stderr); }
    if (uvPipResult.ok) {
      outputChannel.appendLine("OK  Dependencies installed via uv pip into stdlib venv.");
      return venvPy;
    }
  }

  outputChannel.appendLine("FAIL  Could not install dependencies into venv.");
  return "";
}

/**
 * Full auto-setup flow:
 * 1. Check if we already have a working venv from a previous run
 * 2. Discover a system Python >= 3.11
 * 3. Create a dedicated venv and install deps
 * 4. Persist the venv Python path in settings
 * 5. Fire the change emitter so VS Code restarts the MCP server
 */
async function autoSetup(
  context: vscode.ExtensionContext,
  didChangeEmitter: vscode.EventEmitter<void>,
): Promise<void> {
  // Fast path: if the configured Python already works, nothing to do
  const currentSetting = cfg<string>("pythonPath") || "";
  if (currentSetting) {
    const depsOk = await hasDeps(currentSetting);
    if (depsOk) {
      outputChannel.appendLine(
        `OK  Configured Python has all deps: ${currentSetting}`,
      );
      return;
    }
  }

  // Check if our dedicated venv already exists and works
  const venvPy = mcpVenvPython(context);
  if (fs.existsSync(venvPy)) {
    const venvOk = await hasDeps(venvPy);
    if (venvOk) {
      outputChannel.appendLine(`OK  Existing MCP venv works: ${venvPy}`);
      if (currentSetting !== venvPy) {
        await vscode.workspace
          .getConfiguration("prReviewMcp")
          .update("pythonPath", venvPy, vscode.ConfigurationTarget.Global);
        outputChannel.appendLine(`Updated prReviewMcp.pythonPath -> "${venvPy}"`);
      }
      return;
    }
    outputChannel.appendLine("WARN  Existing MCP venv missing deps, will recreate.");
  }

  // Discover a base Python >= 3.11
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

  // If the discovered Python already has deps (e.g. user installed globally), use it directly
  const depsOk = await hasDeps(python.path);
  if (depsOk) {
    outputChannel.appendLine(`OK  ${python.path} already has all deps.`);
    if (currentSetting !== python.path) {
      await vscode.workspace
        .getConfiguration("prReviewMcp")
        .update("pythonPath", python.path, vscode.ConfigurationTarget.Global);
      outputChannel.appendLine(`Updated prReviewMcp.pythonPath -> "${python.path}"`);
    }
    return;
  }

  // Deps missing — create a dedicated venv (with user consent)
  outputChannel.appendLine(
    `Python ${python.version} at ${python.path} — deps missing, will create isolated venv.`,
  );

  const choice = await vscode.window.showWarningMessage(
    `PR Review MCP: Python ${python.version} found. ` +
      "Need to install packages (mcp, httpx) in an isolated environment.",
    "Install Now",
    "Install in Terminal",
    "Skip",
  );

  if (choice === "Install Now") {
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "PR Review MCP: Setting up Python environment...",
        cancellable: false,
      },
      async () => {
        const resultPy = await createMcpVenv(python.path, context);
        if (resultPy) {
          await vscode.workspace
            .getConfiguration("prReviewMcp")
            .update("pythonPath", resultPy, vscode.ConfigurationTarget.Global);
          outputChannel.appendLine(`Updated prReviewMcp.pythonPath -> "${resultPy}"`);
          vscode.window.showInformationMessage(
            "PR Review MCP: Environment ready! Server starting...",
          );
          didChangeEmitter.fire();
        } else {
          vscode.window.showErrorMessage(
            "PR Review MCP: Setup failed. Check Output panel (PR Review MCP) for details.",
          );
        }
      },
    );
  } else if (choice === "Install in Terminal") {
    const terminal = vscode.window.createTerminal("PR Review MCP Setup");
    terminal.show();
    const venvDir = mcpVenvDir(context);
    const isWindows = process.platform === "win32";
    const activateCmd = isWindows
      ? `"${venvDir}\\Scripts\\activate"`
      : `source "${venvDir}/bin/activate"`;
    terminal.sendText(`${python.path} -m venv "${venvDir}"`);
    terminal.sendText(activateCmd);
    terminal.sendText(`pip install "mcp[cli]>=1.0.0" "httpx>=0.27.0"`);
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
        "1.1.1",
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

  // Auto-discover Python >= 3.11, create venv, install deps
  autoSetup(context, didChangeEmitter);

  showSetupHints();

  outputChannel.appendLine("PR Review MCP extension activated (v1.1.1).");
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
