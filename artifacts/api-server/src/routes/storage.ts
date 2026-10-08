import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { RequestUploadUrlBody, RequestUploadUrlResponse } from "@workspace/api-zod";
import { Router, type IRouter, type Request, type Response } from "express";
import { ObjectNotFoundError, ObjectStorageService } from "../lib/objectStorage";

const router: IRouter = Router();
const storage = new ObjectStorageService();
const allowedTypes = new Set([
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/html",
  "text/markdown",
  "text/plain",
]);
const maxUploadBytes = Number(process.env.UPLOAD_MAX_BYTES ?? 20 * 1024 * 1024);

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

function ownerHash(userId: string): string {
  return createHash("sha256").update(userId).digest("hex");
}

function safeEqualHex(left: string, right: string): boolean {
  if (!/^[a-f0-9]+$/i.test(left) || left.length !== right.length) return false;
  return timingSafeEqual(Buffer.from(left, "hex"), Buffer.from(right, "hex"));
}

router.post("/storage/uploads/request-url", async (req: Request, res: Response): Promise<void> => {
  if (!isAuthenticated(req)) {
    problem(req, res, 401, "authentication_required", "Sign in to upload documents");
    return;
  }
  const parsed = RequestUploadUrlBody.safeParse(req.body);
  if (!parsed.success) {
    problem(req, res, 422, "invalid_upload_metadata", "Upload metadata is invalid");
    return;
  }
  const { name, size, contentType } = parsed.data;
  if (size > maxUploadBytes) {
    problem(req, res, 413, "upload_too_large", "The document exceeds the upload limit");
    return;
  }
  if (!allowedTypes.has(contentType.toLowerCase())) {
    problem(req, res, 415, "unsupported_media_type", "This document type is not supported");
    return;
  }
  try {
    const { uploadURL, objectPath } = await storage.getObjectEntityUploadURL(ownerHash(req.user.id));
    res.json(RequestUploadUrlResponse.parse({
      uploadURL,
      objectPath,
      metadata: { name, size, contentType },
    }));
  } catch (error) {
    req.log.error({ errorName: error instanceof Error ? error.name : "UnknownError" }, "Could not issue upload URL");
    problem(req, res, 503, "storage_unavailable", "Private file storage is unavailable");
  }
});

router.get("/storage/public-objects/*filePath", async (req: Request, res: Response): Promise<void> => {
  try {
    const raw = req.params.filePath;
    const filePath = Array.isArray(raw) ? raw.join("/") : raw;
    const file = await storage.searchPublicObject(filePath);
    if (!file) {
      problem(req, res, 404, "object_not_found", "File not found");
      return;
    }
    const [metadata] = await file.getMetadata();
    res.setHeader("Content-Type", String(metadata.contentType || "application/octet-stream"));
    res.setHeader("Cache-Control", "public, max-age=3600");
    if (metadata.size) res.setHeader("Content-Length", String(metadata.size));
    file.createReadStream().on("error", () => {
      if (!res.headersSent) problem(req, res, 500, "storage_read_failed", "Could not read file");
      else res.destroy();
    }).pipe(res);
  } catch (error) {
    req.log.error({ errorName: error instanceof Error ? error.name : "UnknownError" }, "Could not serve public object");
    problem(req, res, 500, "storage_read_failed", "Could not read file");
  }
});

// Internal-only bridge: FastAPI/worker can read private objects, but callers
// need a short-lived HMAC tied to the exact method, path, user, and owner path.
router.get("/internal/storage/objects/*path", async (req: Request, res: Response): Promise<void> => {
  const raw = req.params.path;
  const storagePath = Array.isArray(raw) ? raw.join("/") : raw;
  const subject = req.header("x-rag-subject") ?? "";
  const timestamp = req.header("x-rag-timestamp") ?? "";
  const signature = req.header("x-rag-signature") ?? "";
  const owner = storagePath.split("/")[1] ?? "";
  const secret = process.env.SESSION_SECRET;
  const age = Math.abs(Date.now() / 1000 - Number(timestamp));
  if (
    !secret ||
    !/^\d+$/.test(timestamp) ||
    age > 60 ||
    !subject ||
    !/^[a-f0-9]{64}$/.test(owner) ||
    ownerHash(subject) !== owner
  ) {
    problem(req, res, 403, "internal_signature_invalid", "Internal file access denied");
    return;
  }
  const canonical = `${timestamp}:GET:/api/internal/storage/objects/${storagePath}:${owner}:${subject}`;
  const expected = createHmac("sha256", secret).update(canonical).digest("hex");
  if (!safeEqualHex(signature, expected)) {
    problem(req, res, 403, "internal_signature_invalid", "Internal file access denied");
    return;
  }
  try {
    const file = await storage.getObjectEntityFile(`/objects/${storagePath}`);
    await storage.setPrivateOwner(file, subject);
    const [metadata] = await file.getMetadata();
    res.setHeader("Content-Type", String(metadata.contentType || "application/octet-stream"));
    if (metadata.size) res.setHeader("Content-Length", String(metadata.size));
    file.createReadStream().on("error", () => {
      if (!res.headersSent) problem(req, res, 500, "storage_read_failed", "Could not read document");
      else res.destroy();
    }).pipe(res);
  } catch (error) {
    if (error instanceof ObjectNotFoundError) {
      problem(req, res, 404, "object_not_found", "Uploaded file was not found");
      return;
    }
    req.log.error({ errorName: error instanceof Error ? error.name : "UnknownError" }, "Could not read private object");
    problem(req, res, 503, "storage_unavailable", "Private file storage is unavailable");
  }
});

export default router;
