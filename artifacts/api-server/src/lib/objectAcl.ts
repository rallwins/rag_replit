import { File } from "@google-cloud/storage";

const ACL_POLICY_METADATA_KEY = "aclPolicy";

export enum ObjectPermission {
  READ = "read",
  WRITE = "write",
}

export interface ObjectAclPolicy {
  owner: string;
  visibility: "public" | "private";
}

export async function setObjectAclPolicy(file: File, policy: ObjectAclPolicy): Promise<void> {
  const [exists] = await file.exists();
  if (!exists) throw new Error("Object not found");
  await file.setMetadata({
    metadata: { [ACL_POLICY_METADATA_KEY]: JSON.stringify(policy) },
  });
}

export async function getObjectAclPolicy(file: File): Promise<ObjectAclPolicy | null> {
  const [metadata] = await file.getMetadata();
  const value = metadata?.metadata?.[ACL_POLICY_METADATA_KEY];
  if (!value) return null;
  try {
    const parsed = JSON.parse(String(value)) as Partial<ObjectAclPolicy>;
    if (
      typeof parsed.owner === "string" &&
      (parsed.visibility === "private" || parsed.visibility === "public")
    ) {
      return parsed as ObjectAclPolicy;
    }
  } catch {
    return null;
  }
  return null;
}

export async function canAccessObject(
  file: File,
  userId: string | undefined,
  permission: ObjectPermission = ObjectPermission.READ,
): Promise<boolean> {
  const policy = await getObjectAclPolicy(file);
  if (!policy) return false;
  if (permission === ObjectPermission.READ && policy.visibility === "public") return true;
  return !!userId && policy.owner === userId;
}
