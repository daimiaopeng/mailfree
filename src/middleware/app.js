/**
 * Hono 应用中间件
 * @module middleware/app
 */

import { verifyJwtWithCache, checkRootAdminOverride } from './auth.js';

export function authMiddleware() {
  return async (c, next) => {
    const token = c.env.JWT_TOKEN || c.env.JWT_SECRET || '';
    const root = checkRootAdminOverride(c.req.raw, token);
    if (root) { c.set('authPayload', root); return next(); }
    const payload = await verifyJwtWithCache(token, c.req.header('Cookie') || '');
    if (!payload) return c.text('Unauthorized', 401);
    c.set('authPayload', payload);
    return next();
  };
}

export function rateLimiter({ windowMs = 60_000, max = 100 } = {}) {
  const store = new Map();
  return async (c, next) => {
    const ip = c.req.header('CF-Connecting-IP') || c.req.header('X-Forwarded-For') || 'unknown';
    const key = `${ip}:${c.req.path}`;
    const now = Date.now();
    let e = store.get(key);
    if (!e || e.resetAt < now) { store.set(key, { count: 1, resetAt: now + windowMs }); return next(); }
    if (++e.count > max) return c.text('Too Many Requests', 429);
    return next();
  };
}