import type { NextFunction, Request, Response } from "express";
import * as oidc from "openid-client";
import {
  clearSession,
  getOidcConfig,
  getSession,
  getSessionId,
  updateSession,
  type AuthUser,
  type SessionData,
} from "../lib/auth";

declare global {
  namespace Express {
    interface User extends AuthUser {}
    interface Request {
      isAuthenticated(): this is AuthedRequest;
      user?: User;
    }
    interface AuthedRequest {
      user: User;
    }
  }
}

async function refreshSession(sid: string, session: SessionData): Promise<SessionData | null> {
  const now = Math.floor(Date.now() / 1000);
  if (!session.expires_at || now <= session.expires_at) return session;
  if (!session.refresh_token) return null;
  try {
    const tokens = await oidc.refreshTokenGrant(await getOidcConfig(), session.refresh_token);
    session.access_token = tokens.access_token;
    session.refresh_token = tokens.refresh_token ?? session.refresh_token;
    const expiresIn = tokens.expiresIn();
    if (expiresIn) session.expires_at = now + expiresIn;
    await updateSession(sid, session);
    return session;
  } catch {
    return null;
  }
}

export async function authMiddleware(req: Request, res: Response, next: NextFunction): Promise<void> {
  req.isAuthenticated = function (this: Request): this is Express.AuthedRequest {
    return this.user != null;
  };
  try {
    const sid = getSessionId(req);
    if (!sid) {
      next();
      return;
    }
    const session = await getSession(sid);
    if (!session?.user?.id) {
      await clearSession(res, sid);
      next();
      return;
    }
    const refreshed = await refreshSession(sid, session);
    if (!refreshed) {
      await clearSession(res, sid);
      next();
      return;
    }
    req.user = refreshed.user;
    next();
  } catch (error) {
    next(error);
  }
}
