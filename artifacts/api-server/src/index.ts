import { spawn, type ChildProcess } from "node:child_process";
import app from "./app";
import { logger } from "./lib/logger";

const rawPort = process.env["PORT"];

if (!rawPort) throw new Error("PORT environment variable is required but was not provided.");

const port = Number(rawPort);
const apiPort = Number(process.env.RAG_API_PORT ?? "18081");
const workerPort = Number(process.env.RAG_WORKER_PORT ?? "18082");
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("PORT must be a valid TCP port.");
}
if (!Number.isInteger(apiPort) || apiPort < 1 || apiPort > 65535 || apiPort === port) {
  throw new Error("RAG_API_PORT must be a valid private TCP port distinct from PORT.");
}
if (
  !Number.isInteger(workerPort) ||
  workerPort < 1 ||
  workerPort > 65535 ||
  workerPort === port ||
  workerPort === apiPort
) {
  throw new Error("RAG_WORKER_PORT must be a valid private TCP port distinct from the API ports.");
}

const basePythonEnv = {
  ...process.env,
  PYTHONUNBUFFERED: "1",
  RAG_INTERNAL_STORAGE_BASE_URL: `http://127.0.0.1:${port}/api/internal/storage`,
  RAG_WORKER_URL: `http://127.0.0.1:${workerPort}`,
};

function startPythonService(modulePath: string, servicePort: number): ChildProcess {
  const child = spawn(
    "uv",
    [
      "run",
      "--locked",
      "python",
      "-m",
      "uvicorn",
      modulePath,
      "--host",
      "127.0.0.1",
      "--port",
      String(servicePort),
      "--log-level",
      process.env.LOG_LEVEL === "debug" ? "debug" : "info",
    ],
    { cwd: process.cwd(), env: basePythonEnv, stdio: "inherit" },
  );
  child.once("error", (error) =>
    logger.error({ errorName: error.name, service: modulePath }, "Could not start Python service"),
  );
  return child;
}

async function waitForService(child: ChildProcess, servicePort: number): Promise<void> {
  const deadline = Date.now() + 90_000;
  const healthUrl = `http://127.0.0.1:${servicePort}/healthz`;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`Python service on port ${servicePort} exited before becoming healthy.`);
    }
    try {
      const response = await fetch(healthUrl, { signal: AbortSignal.timeout(1500) });
      if (response.ok) return;
    } catch {
      // Wait briefly while uv syncs the locked Python dependencies and Uvicorn starts.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Python service on port ${servicePort} did not become healthy within 90 seconds.`);
}

let shuttingDown = false;
let worker: ChildProcess | undefined;
let workerRestartDelayMs = 5000;
let restartTimer: NodeJS.Timeout | undefined;

function startWorker(): void {
  if (shuttingDown) return;
  const startedAt = Date.now();
  worker = startPythonService("python_api.worker:app", workerPort);
  worker.once("error", (error) => logger.error({ errorName: error.name }, "Could not start ingestion worker"));
  worker.on("exit", (code, signal) => {
    if (shuttingDown) return;
    if (Date.now() - startedAt >= 60_000) workerRestartDelayMs = 5000;
    logger.error({ code, signal, restartDelayMs: workerRestartDelayMs }, "Ingestion worker exited");
    restartTimer = setTimeout(() => {
      workerRestartDelayMs = Math.min(workerRestartDelayMs * 2, 60_000);
      startWorker();
    }, workerRestartDelayMs);
  });
}

async function main(): Promise<void> {
  const api = startPythonService("python_api.main:app", apiPort);
  api.on("exit", (code, signal) => {
    if (!shuttingDown) {
      logger.error({ code, signal }, "FastAPI process exited");
      process.exit(1);
    }
  });
  try {
    await waitForService(api, apiPort);
  } catch (error) {
    logger.error({ errorName: error instanceof Error ? error.name : "UnknownError" }, "FastAPI startup failed");
    api.kill("SIGTERM");
    process.exit(1);
  }
  startWorker();
  if (worker) await waitForService(worker, workerPort);

  const server = app.listen(port, () => logger.info({ port, apiPort }, "RAG edge server listening"));
  server.on("error", (error) => {
    logger.error({ errorName: error.name }, "Error listening on port");
    process.exit(1);
  });

  const shutdown = (signal: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    logger.info({ signal }, "Shutting down RAG services");
    if (restartTimer) clearTimeout(restartTimer);
    worker?.kill("SIGTERM");
    api.kill("SIGTERM");
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 10_000).unref();
  };
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
}

void main().catch((error: unknown) => {
  logger.error({ errorName: error instanceof Error ? error.name : "UnknownError" }, "RAG services failed to start");
  process.exit(1);
});
