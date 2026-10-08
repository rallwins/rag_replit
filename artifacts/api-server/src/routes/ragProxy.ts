import { createHmac } from "node:crypto";
import { Router, type Request, type Response } from "express";

const router = Router();

function isAuthenticated(req: Request): req is Request & { user: Express.User } {
  return typeof req.isAuthenticated === "function" && req.isAuthenticated();
}

function problem(req: Request, res: Response, status: number, code: string, title: string): void {
  res.status(status).type("application/problem+json").json({
    type: "about:blank",
    title,
    status,
    code,
    request_id: String(req.id ?? ""),
  });
}

router.use("/v1", async (req: Request, res: Response): Promise<void> => {
  const readiness = req.method === "GET" && req.path === "/health/ready";
  if (!readiness && !isAuthenticated(req)) {
    problem(req, res, 401, "authentication_required", "Sign in to access this workspace");
    return;
  }

  const secret = process.env.SESSION_SECRET;
  if (!readiness && !secret) {
    req.log.error("SESSION_SECRET is not configured");
    problem(req, res, 503, "auth_gateway_misconfigured", "The API gateway is not configured");
    return;
  }
  const rawPort = process.env.RAG_API_PORT ?? "18081";
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    problem(req, res, 503, "api_gateway_misconfigured", "The API gateway is not configured");
    return;
  }

  const upstreamPath = req.originalUrl.replace(/^\/api(?=\/)/, "");
  const url = `http://127.0.0.1:${port}${upstreamPath}`;
  const headers = new Headers();
  const contentType = req.header("content-type");
  if (contentType) headers.set("content-type", contentType);
  const accept = req.header("accept");
  if (accept) headers.set("accept", accept);
  const idempotencyKey = req.header("idempotency-key");
  if (idempotencyKey) headers.set("idempotency-key", idempotencyKey);
  headers.set("x-request-id", String(req.id ?? ""));

  if (!readiness && isAuthenticated(req)) {
    const subject = req.user.id;
    const issuer = process.env.ISSUER_URL ?? "https://replit.com/oidc";
    const timestamp = String(Math.floor(Date.now() / 1000));
    const canonical = `${timestamp}:${req.method}:${upstreamPath}:${issuer}:${subject}`;
    const signature = createHmac("sha256", secret!)
      .update(canonical)
      .digest("hex");
    headers.set("x-rag-subject", subject);
    headers.set("x-rag-issuer", issuer);
    headers.set("x-rag-timestamp", timestamp);
    headers.set("x-rag-signature", signature);
  }

  try {
    const body =
      req.method === "GET" || req.method === "HEAD" || req.method === "DELETE"
        ? undefined
        : JSON.stringify(req.body ?? {});
    const response = await fetch(url, {
      method: req.method,
      headers,
      body,
      signal: AbortSignal.timeout(60_000),
    });
    res.status(response.status);
    response.headers.forEach((value, key) => {
      if (key.toLowerCase() !== "content-length" && key.toLowerCase() !== "transfer-encoding") {
        res.setHeader(key, value);
      }
    });
    if (response.status === 204 || req.method === "HEAD") {
      res.end();
      return;
    }
    res.send(Buffer.from(await response.arrayBuffer()));
  } catch (error) {
    req.log.error(
      { errorName: error instanceof Error ? error.name : "UnknownError" },
      "FastAPI request failed",
    );
    problem(req, res, 503, "api_unavailable", "The RAG API is temporarily unavailable");
  }
});

export default router;
