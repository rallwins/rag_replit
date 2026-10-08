import express, { type Express } from "express";
import cookieParser from "cookie-parser";
import pinoHttp from "pino-http";
import router from "./routes";
import { authMiddleware } from "./middlewares/authMiddleware";
import { logger } from "./lib/logger";

const app: Express = express();
app.set("trust proxy", 1);

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(cookieParser());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(authMiddleware);

app.use((req, res, next) => {
  if (["GET", "HEAD", "OPTIONS"].includes(req.method)) {
    next();
    return;
  }
  const origin = req.header("origin");
  if (!origin) {
    next();
    return;
  }
  const forwardedProto = req.header("x-forwarded-proto")?.split(",")[0]?.trim() ?? "https";
  const forwardedHost = req.header("x-forwarded-host")?.split(",")[0]?.trim() ?? req.header("host");
  if (forwardedHost && origin === `${forwardedProto}://${forwardedHost}`) {
    next();
    return;
  }
  res.status(403).json({
    type: "about:blank",
    title: "Cross-origin request denied",
    status: 403,
    code: "csrf_origin_rejected",
    request_id: String(req.id ?? ""),
  });
});

app.use("/api", router);

export default app;
