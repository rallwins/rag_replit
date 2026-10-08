import { GetCurrentAuthUserResponse } from "@workspace/api-zod";
import { db, usersTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { Router, type IRouter, type Request, type Response } from "express";
import * as oidc from "openid-client";
import {
  clearSession,
  createSession,
  getOidcConfig,
  getSessionId,
  ISSUER_URL,
  SESSION_COOKIE,
  SESSION_TTL_MS,
  type AuthUser,
  type SessionData,
} from "../lib/auth";

const router: IRouter = Router();
const OIDC_COOKIE_TTL_MS = 10 * 60 * 1000;

function forwarded(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value?.split(",")[0]?.trim();
}

function getOrigin(req: Request): string {
  const protocol = forwarded(req.headers["x-forwarded-proto"]) ?? "https";
  const host = forwarded(req.headers["x-forwarded-host"]) ?? req.headers.host ?? "localhost";
  return `${protocol}://${host}`;
}

function safeReturnTo(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return "/";
  return value;
}

function setSecureCookie(res: Response, name: string, value: string, maxAge: number): void {
  res.cookie(name, value, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge,
  });
}

async function upsertUser(claims: Record<string, unknown>): Promise<AuthUser> {
  const user = {
    id: String(claims.sub),
    email: typeof claims.email === "string" ? claims.email : null,
    firstName:
      typeof claims.first_name === "string"
        ? claims.first_name
        : typeof claims.given_name === "string"
          ? claims.given_name
          : null,
    lastName:
      typeof claims.last_name === "string"
        ? claims.last_name
        : typeof claims.family_name === "string"
          ? claims.family_name
          : null,
    profileImageUrl:
      typeof claims.profile_image_url === "string"
        ? claims.profile_image_url
        : typeof claims.picture === "string"
          ? claims.picture
          : null,
  };
  const [saved] = await db
    .insert(usersTable)
    .values(user)
    .onConflictDoUpdate({
      target: usersTable.id,
      set: { ...user, updatedAt: new Date() },
    })
    .returning();
  return {
    id: saved.id,
    email: saved.email,
    firstName: saved.firstName,
    lastName: saved.lastName,
    profileImageUrl: saved.profileImageUrl,
  };
}

router.get("/auth/user", (req: Request, res: Response): void => {
  res.json(GetCurrentAuthUserResponse.parse({ user: req.isAuthenticated() ? req.user : null }));
});

router.get("/login", async (req: Request, res: Response): Promise<void> => {
  const config = await getOidcConfig();
  const callbackUrl = `${getOrigin(req)}/api/callback`;
  const state = oidc.randomState();
  const nonce = oidc.randomNonce();
  const verifier = oidc.randomPKCECodeVerifier();
  const challenge = await oidc.calculatePKCECodeChallenge(verifier);
  const redirect = oidc.buildAuthorizationUrl(config, {
    redirect_uri: callbackUrl,
    scope: "openid email profile offline_access",
    code_challenge: challenge,
    code_challenge_method: "S256",
    state,
    nonce,
  });
  const returnTo = safeReturnTo(req.query.returnTo);
  setSecureCookie(res, "code_verifier", verifier, OIDC_COOKIE_TTL_MS);
  setSecureCookie(res, "nonce", nonce, OIDC_COOKIE_TTL_MS);
  setSecureCookie(res, "state", state, OIDC_COOKIE_TTL_MS);
  setSecureCookie(res, "return_to", returnTo, OIDC_COOKIE_TTL_MS);
  res.redirect(redirect.href);
});

router.get("/callback", async (req: Request, res: Response): Promise<void> => {
  const verifier = req.cookies?.code_verifier;
  const nonce = req.cookies?.nonce;
  const expectedState = req.cookies?.state;
  if (!verifier || !expectedState) {
    res.redirect("/api/login");
    return;
  }
  const callbackUrl = `${getOrigin(req)}/api/callback`;
  const currentUrl = new URL(`${callbackUrl}?${new URL(req.url, `http://${req.headers.host}`).searchParams}`);
  let tokens: oidc.TokenEndpointResponse & oidc.TokenEndpointResponseHelpers;
  try {
    tokens = await oidc.authorizationCodeGrant(await getOidcConfig(), currentUrl, {
      pkceCodeVerifier: verifier,
      expectedNonce: nonce,
      expectedState,
      idTokenExpected: true,
    });
  } catch (error) {
    req.log.warn({ errorName: error instanceof Error ? error.name : "UnknownError" }, "OIDC callback validation failed");
    res.redirect("/api/login");
    return;
  }
  const claims = tokens.claims();
  if (!claims?.sub) {
    res.redirect("/api/login");
    return;
  }
  const user = await upsertUser(claims as unknown as Record<string, unknown>);
  const now = Math.floor(Date.now() / 1000);
  const session: SessionData = {
    user,
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token,
    expires_at: tokens.expiresIn() ? now + tokens.expiresIn()! : claims.exp,
  };
  const sid = await createSession(session);
  for (const name of ["code_verifier", "nonce", "state", "return_to"]) {
    res.clearCookie(name, { path: "/" });
  }
  setSecureCookie(res, SESSION_COOKIE, sid, SESSION_TTL_MS);
  res.redirect(safeReturnTo(req.cookies?.return_to));
});

router.get("/logout", async (req: Request, res: Response): Promise<void> => {
  const config = await getOidcConfig();
  const sid = getSessionId(req);
  const returnTo = safeReturnTo(req.query.returnTo);
  const redirectUri = new URL(returnTo, `${getOrigin(req)}/`).href;
  await clearSession(res, sid);
  const endSessionUrl = oidc.buildEndSessionUrl(config, {
    client_id: process.env.REPL_ID!,
    post_logout_redirect_uri: redirectUri,
  });
  res.redirect(endSessionUrl.href);
});

export default router;
