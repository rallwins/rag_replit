import { Router, type IRouter } from "express";
import authRouter from "./auth";
import healthRouter from "./health";
import ragProxyRouter from "./ragProxy";
import storageRouter from "./storage";

const router: IRouter = Router();

router.use(healthRouter);
router.use(authRouter);
router.use(storageRouter);
router.use(ragProxyRouter);

export default router;
