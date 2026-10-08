import { randomUUID } from "node:crypto";
import { Readable } from "node:stream";
import { File, Storage } from "@google-cloud/storage";
import { canAccessObject, type ObjectAclPolicy } from "./objectAcl";

const SIDECAR = "http://127.0.0.1:1106";

// Replit sidecar credentials are the supported App Storage authentication path.
export const objectStorageClient = new Storage({
  credentials: {
    audience: "replit",
    subject_token_type: "access_token",
    token_url: `${SIDECAR}/token`,
    type: "external_account",
    credential_source: {
      url: `${SIDECAR}/credential`,
      format: { type: "json", subject_token_field_name: "access_token" },
    },
    universe_domain: "googleapis.com",
  },
  projectId: "",
});

export class ObjectNotFoundError extends Error {}

export class ObjectStorageService {
  getPrivateObjectDir(): string {
    const path = process.env.PRIVATE_OBJECT_DIR;
    if (!path) throw new Error("PRIVATE_OBJECT_DIR is not configured");
    return path;
  }

  getPublicObjectSearchPaths(): string[] {
    return Array.from(
      new Set(
        (process.env.PUBLIC_OBJECT_SEARCH_PATHS ?? "")
          .split(",")
          .map((path) => path.trim())
          .filter(Boolean),
      ),
    );
  }

  async getObjectEntityUploadURL(ownerHash: string): Promise<{ uploadURL: string; objectPath: string }> {
    const fullPath = `${this.getPrivateObjectDir()}/uploads/${ownerHash}/${randomUUID()}`;
    const { bucketName, objectName } = parseObjectPath(fullPath);
    const uploadURL = await signObjectURL(bucketName, objectName, "PUT", 900);
    const privatePrefix = `${this.getPrivateObjectDir()}/`;
    const normalized = new URL(uploadURL).pathname;
    if (!normalized.startsWith(privatePrefix)) throw new Error("Signed upload URL is outside the private object directory");
    return {
      uploadURL,
      objectPath: `/objects/${normalized.slice(privatePrefix.length)}`,
    };
  }

  async getObjectEntityFile(objectPath: string): Promise<File> {
    if (!/^\/objects\/uploads\/[a-f0-9]{64}\/[0-9a-f-]{36}$/.test(objectPath)) {
      throw new ObjectNotFoundError();
    }
    const entityId = objectPath.slice("/objects/".length);
    const { bucketName, objectName } = parseObjectPath(`${this.getPrivateObjectDir()}/${entityId}`);
    const file = objectStorageClient.bucket(bucketName).file(objectName);
    const [exists] = await file.exists();
    if (!exists) throw new ObjectNotFoundError();
    return file;
  }

  async downloadObject(file: File): Promise<{ stream: Readable; contentType: string; size?: number }> {
    const [metadata] = await file.getMetadata();
    return {
      stream: file.createReadStream(),
      contentType: String(metadata.contentType || "application/octet-stream"),
      size: metadata.size ? Number(metadata.size) : undefined,
    };
  }

  async searchPublicObject(filePath: string): Promise<File | null> {
    if (!filePath || filePath.includes("..")) return null;
    for (const root of this.getPublicObjectSearchPaths()) {
      const { bucketName, objectName } = parseObjectPath(`${root}/${filePath}`);
      const file = objectStorageClient.bucket(bucketName).file(objectName);
      const [exists] = await file.exists();
      if (exists) return file;
    }
    return null;
  }

  async setPrivateOwner(file: File, userId: string): Promise<void> {
    const policy: ObjectAclPolicy = { owner: userId, visibility: "private" };
    const [exists] = await file.exists();
    if (exists) {
      const existing = await file.getMetadata();
      if (existing[0].metadata?.["custom:aclPolicy"]) return;
    }
    const { setObjectAclPolicy } = await import("./objectAcl");
    await setObjectAclPolicy(file, policy);
  }

  async canRead(file: File, userId: string | undefined): Promise<boolean> {
    return canAccessObject(file, userId);
  }
}

function parseObjectPath(raw: string): { bucketName: string; objectName: string } {
  const normalized = raw.startsWith("/") ? raw : `/${raw}`;
  const [_, bucketName, ...objectParts] = normalized.split("/");
  if (!bucketName || objectParts.length === 0) throw new Error("Invalid object path");
  return { bucketName, objectName: objectParts.join("/") };
}

async function signObjectURL(
  bucketName: string,
  objectName: string,
  method: "PUT" | "GET" | "DELETE" | "HEAD",
  ttlSec: number,
): Promise<string> {
  const response = await fetch(`${SIDECAR}/object-storage/signed-object-url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      bucket_name: bucketName,
      object_name: objectName,
      method,
      expires_at: new Date(Date.now() + ttlSec * 1000).toISOString(),
    }),
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`Object storage URL signing failed (${response.status})`);
  const payload = (await response.json()) as { signed_url?: string };
  if (!payload.signed_url) throw new Error("Object storage returned no signed URL");
  return payload.signed_url;
}
