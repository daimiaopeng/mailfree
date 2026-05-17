/**
 * 邮箱管理 API 模块
 * @module api/mailboxes
 */

import { getJwtPayload, isStrictAdmin, getMailboxAccess, logAuditEvent, errorResponse } from './helpers.js';
import { buildMockMailboxes, MOCK_DOMAINS } from './mock.js';
import { extractEmail, generateRandomId } from '../utils/common.js';
import { getCachedUserQuota, getCachedSystemStat } from '../utils/cache.js';
import {
  getOrCreateMailboxId,
  getMailboxIdByAddress,
  toggleMailboxPin,
  getTotalMailboxCount,
  assignMailboxToUser
} from '../db/index.js';
import { handleMailboxAdminApi } from './mailboxAdmin.js';

function clampText(value, max = 300) {
  return String(value || '').trim().slice(0, max);
}

function normalizeTags(value) {
  const list = Array.isArray(value)
    ? value
    : String(value || '').split(/[,，\s]+/);
  return Array.from(new Set(
    list.map(item => String(item || '').trim()).filter(Boolean).slice(0, 12)
  )).join(',');
}

function parseExpiresAtFromInput(value) {
  if (value === null || value === '') return null;
  if (!value) return undefined;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return undefined;
  return date.toISOString();
}

function parseTtlExpiresAt(value) {
  const hours = Number(value || 0);
  if (!Number.isFinite(hours) || hours <= 0) return undefined;
  const cappedHours = Math.min(hours, 24 * 365);
  return new Date(Date.now() + cappedHours * 60 * 60 * 1000).toISOString();
}

function buildMailboxMeta(input = {}) {
  const meta = {};
  if (Object.prototype.hasOwnProperty.call(input, 'note')) meta.note = clampText(input.note, 500);
  if (Object.prototype.hasOwnProperty.call(input, 'tags')) meta.tags = normalizeTags(input.tags);
  if (Object.prototype.hasOwnProperty.call(input, 'purpose')) meta.purpose = clampText(input.purpose, 120);

  const expiresAt = parseExpiresAtFromInput(input.expires_at ?? input.expiresAt);
  if (expiresAt !== undefined) meta.expires_at = expiresAt;

  const ttlExpiresAt = parseTtlExpiresAt(input.ttlHours ?? input.ttl_hours);
  if (ttlExpiresAt) meta.expires_at = ttlExpiresAt;
  return meta;
}

async function updateMailboxMeta(db, mailboxId, meta) {
  const fields = [];
  const params = [];
  for (const key of ['note', 'tags', 'purpose', 'expires_at']) {
    if (Object.prototype.hasOwnProperty.call(meta, key)) {
      fields.push(`${key} = ?`);
      params.push(meta[key]);
    }
  }
  if (!fields.length) return;
  params.push(Number(mailboxId));
  await db.prepare(`UPDATE mailboxes SET ${fields.join(', ')} WHERE id = ?`).bind(...params).run();
}

function mailboxInfoResponse(row, fallbackAddress = '') {
  return {
    id: row?.id ?? null,
    address: row?.address || fallbackAddress,
    is_favorite: !!row?.is_favorite,
    forward_to: row?.forward_to || null,
    can_login: !!row?.can_login,
    note: row?.note || '',
    tags: row?.tags || '',
    purpose: row?.purpose || '',
    expires_at: row?.expires_at || null,
    is_expired: !!(row?.expires_at && new Date(row.expires_at).getTime() <= Date.now())
  };
}

function addMailboxListFilters(url, whereConditions, bindParams) {
  const searchParam = url.searchParams.get('q');
  const domainParam = url.searchParams.get('domain');
  const loginParam = url.searchParams.get('login');
  const favoriteParam = url.searchParams.get('favorite');
  const forwardParam = url.searchParams.get('forward');
  const tagParam = url.searchParams.get('tag');
  const purposeParam = url.searchParams.get('purpose');
  const expiredParam = url.searchParams.get('expired');

  if (searchParam && searchParam.trim()) {
    const q = `%${searchParam.trim().toLowerCase()}%`;
    whereConditions.push('(m.address LIKE ? OR COALESCE(m.note, \'\') LIKE ? OR COALESCE(m.tags, \'\') LIKE ? OR COALESCE(m.purpose, \'\') LIKE ?)');
    bindParams.push(q, q, q, q);
  }
  if (domainParam) {
    whereConditions.push('m.domain = ?');
    bindParams.push(domainParam);
  }
  if (loginParam === 'true' || loginParam === '1' || loginParam === 'allowed') {
    whereConditions.push('m.can_login = 1');
  } else if (loginParam === 'false' || loginParam === '0' || loginParam === 'denied') {
    whereConditions.push('(m.can_login = 0 OR m.can_login IS NULL)');
  }
  if (favoriteParam === 'true' || favoriteParam === '1' || favoriteParam === 'favorite') {
    whereConditions.push('m.is_favorite = 1');
  } else if (favoriteParam === 'false' || favoriteParam === '0' || favoriteParam === 'not-favorite') {
    whereConditions.push('(m.is_favorite = 0 OR m.is_favorite IS NULL)');
  }
  if (forwardParam === 'true' || forwardParam === '1' || forwardParam === 'has-forward') {
    whereConditions.push("(m.forward_to IS NOT NULL AND m.forward_to != '')");
  } else if (forwardParam === 'false' || forwardParam === '0' || forwardParam === 'no-forward') {
    whereConditions.push("(m.forward_to IS NULL OR m.forward_to = '')");
  }
  if (tagParam && tagParam.trim()) {
    whereConditions.push("(',' || COALESCE(m.tags, '') || ',') LIKE ?");
    bindParams.push(`%,${tagParam.trim()},%`);
  }
  if (purposeParam && purposeParam.trim()) {
    whereConditions.push('COALESCE(m.purpose, \'\') LIKE ?');
    bindParams.push(`%${purposeParam.trim()}%`);
  }
  if (expiredParam === 'true' || expiredParam === '1') {
    whereConditions.push("m.expires_at IS NOT NULL AND datetime(m.expires_at) <= datetime('now')");
  } else if (expiredParam === 'false' || expiredParam === '0') {
    whereConditions.push("(m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now'))");
  }
}

export async function cleanupExpiredMailboxes(db, r2 = null, limit = 500) {
  const { results: expired } = await db.prepare(`
    SELECT id, address FROM mailboxes
    WHERE expires_at IS NOT NULL AND datetime(expires_at) <= datetime('now')
    ORDER BY datetime(expires_at) ASC
    LIMIT ?
  `).bind(Math.max(1, Math.min(1000, Number(limit || 500)))).all();
  const items = expired || [];
  if (!items.length) {
    return { success: true, deleted_mailboxes: 0, deleted_messages: 0, deleted_r2_objects: 0 };
  }

  const ids = items.map(row => Number(row.id)).filter(Boolean);
  const placeholders = ids.map(() => '?').join(',');
  const { results: objects } = await db.prepare(`
    SELECT r2_object_key FROM messages
    WHERE mailbox_id IN (${placeholders})
      AND r2_object_key IS NOT NULL
      AND r2_object_key != ''
  `).bind(...ids).all();

  const messageResult = await db.prepare(`DELETE FROM messages WHERE mailbox_id IN (${placeholders})`)
    .bind(...ids).run();
  const mailboxResult = await db.prepare(`DELETE FROM mailboxes WHERE id IN (${placeholders})`)
    .bind(...ids).run();

  let deletedR2Objects = 0;
  if (r2 && objects?.length) {
    for (const row of objects) {
      try {
        await r2.delete(row.r2_object_key);
        deletedR2Objects++;
      } catch (_) { }
    }
  }

  return {
    success: true,
    deleted_mailboxes: mailboxResult?.meta?.changes || ids.length,
    deleted_messages: messageResult?.meta?.changes || 0,
    deleted_r2_objects: deletedR2Objects
  };
}

function normalizeAnalyticsRange(value) {
  const allowed = new Set(['7d', '30d', '90d']);
  const range = String(value || '30d').toLowerCase();
  return allowed.has(range) ? range : '30d';
}

function buildAnalyticsDays(range) {
  const count = Number(range.replace('d', ''));
  const days = [];
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);
  for (let i = count - 1; i >= 0; i--) {
    const date = new Date(today);
    date.setUTCDate(today.getUTCDate() - i);
    days.push(date.toISOString().slice(0, 10));
  }
  return days;
}

function mapDailyRows(rows = []) {
  const map = new Map();
  for (const row of rows || []) {
    if (row?.day) map.set(String(row.day), Number(row.total || 0));
  }
  return map;
}

async function queryDailyCount(db, table, column, days) {
  const start = `${days[0]} 00:00:00`;
  const { results } = await db.prepare(`
    SELECT date(${column}) AS day, COUNT(*) AS total
    FROM ${table}
    WHERE datetime(${column}) >= datetime(?)
    GROUP BY date(${column})
    ORDER BY day ASC
  `).bind(start).all();
  return mapDailyRows(results);
}

async function buildAdminAnalytics(db, range) {
  const days = buildAnalyticsDays(range);
  const count = async (sql) => (await db.prepare(sql).first())?.total || 0;
  const [usersByDay, mailboxesByDay, messagesByDay, sentByDay] = await Promise.all([
    queryDailyCount(db, 'users', 'created_at', days),
    queryDailyCount(db, 'mailboxes', 'created_at', days),
    queryDailyCount(db, 'messages', 'received_at', days),
    queryDailyCount(db, 'sent_emails', 'created_at', days)
  ]);

  const trend = days.map(day => ({
    date: day,
    users: usersByDay.get(day) || 0,
    mailboxes: mailboxesByDay.get(day) || 0,
    messages: messagesByDay.get(day) || 0,
    sent_emails: sentByDay.get(day) || 0
  }));

  const { results: sentStatus } = await db.prepare(`
    SELECT COALESCE(NULLIF(status, ''), 'unknown') AS status, COUNT(*) AS total
    FROM sent_emails
    GROUP BY COALESCE(NULLIF(status, ''), 'unknown')
    ORDER BY total DESC
  `).all();
  const { results: domainDistribution } = await db.prepare(`
    SELECT COALESCE(NULLIF(domain, ''), 'unknown') AS domain, COUNT(*) AS total
    FROM mailboxes
    GROUP BY COALESCE(NULLIF(domain, ''), 'unknown')
    ORDER BY total DESC
    LIMIT 12
  `).all();
  const { results: topUsers } = await db.prepare(`
    SELECT u.id, u.username, COUNT(um.mailbox_id) AS mailbox_count
    FROM users u
    LEFT JOIN user_mailboxes um ON um.user_id = u.id
    GROUP BY u.id, u.username
    ORDER BY mailbox_count DESC, u.id ASC
    LIMIT 10
  `).all();

  return {
    ok: true,
    range,
    generated_at: new Date().toISOString(),
    totals: {
      users: await count('SELECT COUNT(*) AS total FROM users'),
      mailboxes: await count('SELECT COUNT(*) AS total FROM mailboxes'),
      messages: await count('SELECT COUNT(*) AS total FROM messages'),
      sent_emails: await count('SELECT COUNT(*) AS total FROM sent_emails'),
      expired_mailboxes: await count("SELECT COUNT(*) AS total FROM mailboxes WHERE expires_at IS NOT NULL AND datetime(expires_at) <= datetime('now')")
    },
    trend,
    sent_status: (sentStatus || []).map(row => ({ status: row.status, total: Number(row.total || 0) })),
    domain_distribution: (domainDistribution || []).map(row => ({ domain: row.domain, total: Number(row.total || 0) })),
    top_users: (topUsers || []).map(row => ({ id: row.id, username: row.username, mailbox_count: Number(row.mailbox_count || 0) }))
  };
}

function buildMockAnalytics(range) {
  const days = buildAnalyticsDays(range);
  const trend = days.map((day, index) => ({
    date: day,
    users: index % 5 === 0 ? 1 : 0,
    mailboxes: 2 + (index % 4),
    messages: 8 + ((index * 7) % 18),
    sent_emails: 1 + (index % 6)
  }));
  return {
    ok: true,
    mock: true,
    range,
    generated_at: new Date().toISOString(),
    totals: { users: 8, mailboxes: 24, messages: 168, sent_emails: 36, expired_mailboxes: 3 },
    trend,
    sent_status: [
      { status: 'delivered', total: 24 },
      { status: 'queued', total: 8 },
      { status: 'failed', total: 4 }
    ],
    domain_distribution: [
      { domain: 'example.com', total: 18 },
      { domain: 'demo.test', total: 6 }
    ],
    top_users: [
      { id: 1, username: 'guest', mailbox_count: 8 },
      { id: 2, username: 'operator', mailbox_count: 5 },
      { id: 3, username: 'tester', mailbox_count: 3 }
    ]
  };
}

/**
 * 处理邮箱管理相关 API
 * @param {Request} request - HTTP 请求
 * @param {object} db - 数据库连接
 * @param {Array<string>} mailDomains - 邮件域名列表
 * @param {URL} url - 请求 URL
 * @param {string} path - 请求路径
 * @param {object} options - 选项
 * @returns {Promise<Response|null>} 响应或 null（未匹配）
 */
export async function handleMailboxesApi(request, db, mailDomains, url, path, options) {
  const isMock = !!options.mockOnly;

  // 返回域名列表给前端
  if (path === '/api/domains' && request.method === 'GET') {
    if (isMock) return Response.json(MOCK_DOMAINS);
    const domains = Array.isArray(mailDomains) ? mailDomains : [(mailDomains || 'temp.example.com')];
    return Response.json(domains);
  }

  // 随机生成邮箱
  if (path === '/api/generate') {
    const lengthParam = Number(url.searchParams.get('length') || 0);
    const randomId = generateRandomId(lengthParam || undefined);
    const domains = isMock ? MOCK_DOMAINS : (Array.isArray(mailDomains) ? mailDomains : [(mailDomains || 'temp.example.com')]);
    const domainIdx = Math.max(0, Math.min(domains.length - 1, Number(url.searchParams.get('domainIndex') || 0)));
    const chosenDomain = domains[domainIdx] || domains[0];
    const email = `${randomId}@${chosenDomain}`;
    
    if (!isMock) {
      try {
        const payload = getJwtPayload(request, options);
        const meta = buildMailboxMeta(Object.fromEntries(url.searchParams.entries()));
        const responseExpires = meta.expires_at ? new Date(meta.expires_at).getTime() : Date.now() + 3600000;
        if (payload?.userId) {
          await assignMailboxToUser(db, { userId: payload.userId, address: email });
          const mailboxId = await getMailboxIdByAddress(db, email);
          if (mailboxId) await updateMailboxMeta(db, mailboxId, meta);
          await logAuditEvent(db, request, options, {
            action: 'mailbox.generate',
            targetType: 'mailbox',
            targetId: mailboxId,
            targetAddress: email,
            metadata: { domain: chosenDomain }
          });
          return Response.json({ email, expires: responseExpires });
        }
        const mailboxId = await getOrCreateMailboxId(db, email);
        await updateMailboxMeta(db, mailboxId, meta);
        await logAuditEvent(db, request, options, {
          action: 'mailbox.generate',
          targetType: 'mailbox',
          targetId: mailboxId,
          targetAddress: email,
          metadata: { domain: chosenDomain }
        });
        return Response.json({ email, expires: responseExpires });
      } catch (e) {
        return errorResponse(String(e?.message || '创建失败'), 400);
      }
    }
    return Response.json({ email, expires: Date.now() + 3600000 });
  }

  // 自定义创建邮箱
  if (path === '/api/create' && request.method === 'POST') {
    if (isMock) {
      try {
        const body = await request.json();
        const local = String(body.local || '').trim().toLowerCase();
        const valid = /^[a-z0-9._-]{1,64}$/i.test(local);
        if (!valid) return errorResponse('非法用户名', 400);
        const domains = MOCK_DOMAINS;
        const domainIdx = Math.max(0, Math.min(domains.length - 1, Number(body.domainIndex || 0)));
        const chosenDomain = domains[domainIdx] || domains[0];
        const email = `${local}@${chosenDomain}`;
        return Response.json({ email, expires: Date.now() + 3600000 });
      } catch (_) { return errorResponse('Bad Request', 400); }
    }
    
    try {
      const body = await request.json();
      const local = String(body.local || '').trim().toLowerCase();
      const valid = /^[a-z0-9._-]{1,64}$/i.test(local);
      if (!valid) return errorResponse('非法用户名', 400);
      const domains = Array.isArray(mailDomains) ? mailDomains : [(mailDomains || 'temp.example.com')];
      const domainIdx = Math.max(0, Math.min(domains.length - 1, Number(body.domainIndex || 0)));
      const chosenDomain = domains[domainIdx] || domains[0];
      const email = `${local}@${chosenDomain}`;
      
      try {
        const payload = getJwtPayload(request, options);
        const userId = payload?.userId;
        const meta = buildMailboxMeta(body);
        const responseExpires = meta.expires_at ? new Date(meta.expires_at).getTime() : Date.now() + 3600000;
        let mailboxId = null;
        if (userId) {
          await assignMailboxToUser(db, { userId, address: email });
          mailboxId = await getMailboxIdByAddress(db, email);
        } else {
          mailboxId = await getOrCreateMailboxId(db, email);
        }
        if (mailboxId) await updateMailboxMeta(db, mailboxId, meta);
        await logAuditEvent(db, request, options, {
          action: 'mailbox.create',
          targetType: 'mailbox',
          targetId: mailboxId,
          targetAddress: email,
          metadata: { custom: true, domain: chosenDomain }
        });
        return Response.json({ email, expires: responseExpires });
      } catch (e) {
        return errorResponse(String(e?.message || '创建失败'), 400);
      }
    } catch (_) { return errorResponse('Bad Request', 400); }
  }

  // 获取邮箱详细信息（转发、收藏等）
  if (path === '/api/mailbox/info' && request.method === 'GET') {
    const address = url.searchParams.get('address');
    if (!address) return errorResponse('缺少邮箱地址', 400);
    
    if (isMock) {
      return Response.json({
        id: 1,
        address,
        is_favorite: false,
        forward_to: null,
        can_login: false,
        note: '',
        tags: '',
        purpose: '',
        expires_at: null,
        is_expired: false
      });
    }
    
    try {
      const { results } = await db.prepare(
        'SELECT id, address, is_favorite, forward_to, can_login, note, tags, purpose, expires_at FROM mailboxes WHERE address = ? LIMIT 1'
      ).bind(address.toLowerCase()).all();
      
      if (!results || results.length === 0) {
        return Response.json(mailboxInfoResponse(null, address));
      }
      
      const row = results[0];
      const access = await getMailboxAccess(db, request, options, { mailboxId: row.id });
      if (!access.allowed) return errorResponse('Forbidden', 403);
      return Response.json(mailboxInfoResponse(row, address));
    } catch (e) {
      return errorResponse('查询失败', 500);
    }
  }

  // 更新邮箱备注、标签、用途和过期时间
  if (path === '/api/mailbox/info' && request.method === 'PATCH') {
    if (isMock) return errorResponse('演示模式不可修改', 403);
    try {
      const body = await request.json();
      const address = String(body.address || '').trim().toLowerCase();
      const mailboxId = Number(body.mailbox_id || body.mailboxId || 0);
      if (!address && !mailboxId) return errorResponse('缺少邮箱标识', 400);

      const access = await getMailboxAccess(db, request, options, { mailboxId: mailboxId || null, address });
      if (!access.exists) return errorResponse('Not Found', 404);
      if (!access.allowed) return errorResponse('Forbidden', 403);

      const meta = buildMailboxMeta(body);
      await updateMailboxMeta(db, access.mailbox.id, meta);
      const row = await db.prepare(
        'SELECT id, address, is_favorite, forward_to, can_login, note, tags, purpose, expires_at FROM mailboxes WHERE id = ? LIMIT 1'
      ).bind(access.mailbox.id).first();
      await logAuditEvent(db, request, options, {
        action: 'mailbox.metadata.update',
        targetType: 'mailbox',
        targetId: access.mailbox.id,
        targetAddress: row?.address || address,
        metadata: meta
      });
      return Response.json(mailboxInfoResponse(row, address));
    } catch (e) {
      return errorResponse('更新邮箱信息失败', 500);
    }
  }

  // 用户配额和邮箱统计
  if (path === '/api/user/quota' && request.method === 'GET') {
    const payload = getJwtPayload(request, options);
    const uid = Number(payload?.userId || 0);
    const role = payload?.role || '';
    
    if (isMock) {
      return Response.json({ limit: 999, used: 2, remaining: 997 });
    }
    
    if (isStrictAdmin(request, options)) {
      const totalMailboxes = await getCachedSystemStat(db, 'total_mailboxes', async () => {
        return await getTotalMailboxCount(db);
      });
      return Response.json({
        limit: -1,
        used: totalMailboxes,
        remaining: -1,
        note: '管理员无邮箱数量限制'
      });
    }
    
    if (!uid) return Response.json({ limit: 10, used: 0, remaining: 10 });
    
    const quota = await getCachedUserQuota(db, uid);
    return Response.json(quota);
  }

  // 获取用户的邮箱列表
  if (path === '/api/mailboxes' && request.method === 'GET') {
    if (isMock) {
      const searchParam = url.searchParams.get('q');
      const domainParam = url.searchParams.get('domain');
      const favoriteParam = url.searchParams.get('favorite');
      const forwardParam = url.searchParams.get('forward');
      let results = buildMockMailboxes(MOCK_DOMAINS);
      // 搜索过滤
      if (searchParam && searchParam.trim()) {
        const q = searchParam.trim().toLowerCase();
        results = results.filter(m => m.address.toLowerCase().includes(q));
      }
      if (domainParam) {
        results = results.filter(m => m.address.endsWith('@' + domainParam));
      }
      if (favoriteParam === 'true' || favoriteParam === '1') {
        results = results.filter(m => m.is_favorite);
      } else if (favoriteParam === 'false' || favoriteParam === '0') {
        results = results.filter(m => !m.is_favorite);
      }
      if (forwardParam === 'true' || forwardParam === '1') {
        results = results.filter(m => m.forward_to);
      } else if (forwardParam === 'false' || forwardParam === '0') {
        results = results.filter(m => !m.forward_to);
      }
      // 分页
      const pageParam = url.searchParams.get('page');
      const sizeParam = url.searchParams.get('size');
      const page = Math.max(1, Number(pageParam || 1));
      const size = Math.max(1, Math.min(500, Number(sizeParam || 20)));
      const total = results.length;
      const start = (page - 1) * size;
      const pageResult = results.slice(start, start + size);
      return Response.json({ list: pageResult, total });
    }

    const payload = getJwtPayload(request, options);
    const mailboxOnly = !!options.mailboxOnly;

    if (mailboxOnly && payload?.mailboxAddress) {
      try {
        const { results } = await db.prepare(`
          SELECT id, address, created_at, 0 AS is_pinned,
                 CASE WHEN (password_hash IS NULL OR password_hash = '') THEN 1 ELSE 0 END AS password_is_default,
                 COALESCE(can_login, 0) AS can_login,
                 forward_to, COALESCE(is_favorite, 0) AS is_favorite,
                 note, tags, purpose, expires_at
          FROM mailboxes
          WHERE address = ?
          LIMIT 1
        `).bind(payload.mailboxAddress).all();
        return Response.json({ list: results || [], total: results?.length || 0 });
      } catch (e) {
        return Response.json({ list: [], total: 0 });
      }
    }

    try {
      const strictAdmin = isStrictAdmin(request, options);
      let uid = Number(payload?.userId || 0);
      
      if (!uid && strictAdmin) {
        const { results } = await db.prepare('SELECT id FROM users WHERE username = ?')
          .bind(String(options?.adminName || 'admin').toLowerCase()).all();
        if (results && results.length) {
          uid = Number(results[0].id);
        } else {
          const uname = String(options?.adminName || 'admin').toLowerCase();
          await db.prepare("INSERT INTO users (username, role, can_send, mailbox_limit) VALUES (?, 'admin', 1, 9999)").bind(uname).run();
          const again = await db.prepare('SELECT id FROM users WHERE username = ?').bind(uname).all();
          uid = Number(again?.results?.[0]?.id || 0);
        }
      }

      if (!uid && !strictAdmin) return Response.json({ list: [], total: 0 });

      // 支持两种分页参数：page/size 或 limit/offset
      let limit, offset;
      const pageParam = url.searchParams.get('page');
      const sizeParam = url.searchParams.get('size');
      
      if (pageParam !== null || sizeParam !== null) {
        // 使用 page/size 分页
        const page = Math.max(1, Number(pageParam || 1));
        const size = Math.max(1, Math.min(500, Number(sizeParam || 20)));
        limit = size;
        offset = (page - 1) * size;
      } else {
        // 使用 limit/offset 分页
        limit = Math.max(1, Math.min(500, Number(url.searchParams.get('limit') || 100)));
        offset = Math.max(0, Number(url.searchParams.get('offset') || 0));
      }

      const bindParams = [];
      const whereConditions = [];
      
      // 严格管理员可以看到所有邮箱，普通用户只能看到自己关联的邮箱
      const useUserFilter = !strictAdmin && uid;
      if (useUserFilter) {
        whereConditions.push('um.user_id = ?');
        bindParams.push(uid);
      }
      
      addMailboxListFilters(url, whereConditions, bindParams);
      
      const whereClause = whereConditions.length > 0 ? 'WHERE ' + whereConditions.join(' AND ') : '';
      bindParams.push(limit, offset);
      
      // 构建计数查询的参数（不包含 limit 和 offset）
      const countBindParams = bindParams.slice(0, -2);
      
      // 严格管理员使用 LEFT JOIN 显示所有邮箱，同时保留自己的置顶状态
      // 普通用户使用 INNER JOIN 只显示自己关联的邮箱
      if (strictAdmin && uid) {
        // 严格管理员：显示所有邮箱，使用 LEFT JOIN 获取自己的置顶状态
        const adminBindParams = [uid, ...bindParams];
        const adminCountBindParams = [uid, ...countBindParams];
        
        // 获取总数
        const countResult = await db.prepare(`
          SELECT COUNT(*) as total
          FROM mailboxes m
          LEFT JOIN user_mailboxes um ON m.id = um.mailbox_id AND um.user_id = ?
          ${whereClause}
        `).bind(...adminCountBindParams).first();
        const total = countResult?.total || 0;
        
        const { results } = await db.prepare(`
          SELECT m.id, m.address, m.created_at, COALESCE(um.is_pinned, 0) AS is_pinned,
                 CASE WHEN (m.password_hash IS NULL OR m.password_hash = '') THEN 1 ELSE 0 END AS password_is_default,
                 COALESCE(m.can_login, 0) AS can_login,
                 m.forward_to, COALESCE(m.is_favorite, 0) AS is_favorite,
                 m.note, m.tags, m.purpose, m.expires_at
          FROM mailboxes m
          LEFT JOIN user_mailboxes um ON m.id = um.mailbox_id AND um.user_id = ?
          ${whereClause}
          ORDER BY COALESCE(um.is_pinned, 0) DESC, m.created_at DESC
          LIMIT ? OFFSET ?
        `).bind(...adminBindParams).all();
        return Response.json({ list: results || [], total });
      } else if (strictAdmin) {
        // 严格管理员但没有 uid（不应该发生，但作为兜底）
        // 获取总数
        const countResult = await db.prepare(`
          SELECT COUNT(*) as total
          FROM mailboxes m
          ${whereClause}
        `).bind(...countBindParams).first();
        const total = countResult?.total || 0;
        
        const { results } = await db.prepare(`
          SELECT m.id, m.address, m.created_at, 0 AS is_pinned,
                 CASE WHEN (m.password_hash IS NULL OR m.password_hash = '') THEN 1 ELSE 0 END AS password_is_default,
                 COALESCE(m.can_login, 0) AS can_login,
                 m.forward_to, COALESCE(m.is_favorite, 0) AS is_favorite,
                 m.note, m.tags, m.purpose, m.expires_at
          FROM mailboxes m
          ${whereClause}
          ORDER BY m.created_at DESC
          LIMIT ? OFFSET ?
        `).bind(...bindParams).all();
        return Response.json({ list: results || [], total });
      } else {
        // 普通用户：只显示自己关联的邮箱
        // 获取总数
        const countResult = await db.prepare(`
          SELECT COUNT(*) as total
          FROM user_mailboxes um
          JOIN mailboxes m ON m.id = um.mailbox_id
          ${whereClause}
        `).bind(...countBindParams).first();
        const total = countResult?.total || 0;
        
        const { results } = await db.prepare(`
          SELECT m.id, m.address, m.created_at, um.is_pinned,
                 CASE WHEN (m.password_hash IS NULL OR m.password_hash = '') THEN 1 ELSE 0 END AS password_is_default,
                 COALESCE(m.can_login, 0) AS can_login,
                 m.forward_to, COALESCE(m.is_favorite, 0) AS is_favorite,
                 m.note, m.tags, m.purpose, m.expires_at
          FROM user_mailboxes um
          JOIN mailboxes m ON m.id = um.mailbox_id
          ${whereClause}
          ORDER BY um.is_pinned DESC, m.created_at DESC
          LIMIT ? OFFSET ?
        `).bind(...bindParams).all();
        return Response.json({ list: results || [], total });
      }
    } catch (_) {
      return Response.json({ list: [], total: 0 });
    }
  }

  // 管理员健康检查：帮助自建站快速确认关键绑定和近期数据状态
  if (path === '/api/system/health' && request.method === 'GET') {
    if (isMock) return Response.json({ mock: true, ok: true });
    if (!isStrictAdmin(request, options)) return errorResponse('Forbidden', 403);
    try {
      const count = async (sql) => (await db.prepare(sql).first())?.total || 0;
      const domains = Array.isArray(mailDomains) ? mailDomains : [(mailDomains || 'temp.example.com')];
      const latestMessage = await db.prepare('SELECT received_at FROM messages ORDER BY received_at DESC LIMIT 1').first();
      const latestSent = await db.prepare('SELECT created_at, status FROM sent_emails ORDER BY created_at DESC LIMIT 1').first();
      return Response.json({
        ok: true,
        checked_at: new Date().toISOString(),
        db_bound: !!db,
        r2_bound: !!options.r2,
        resend_configured: !!options.resendApiKey,
        domains,
        counts: {
          users: await count('SELECT COUNT(*) AS total FROM users'),
          mailboxes: await count('SELECT COUNT(*) AS total FROM mailboxes'),
          messages: await count('SELECT COUNT(*) AS total FROM messages'),
          sent_emails: await count('SELECT COUNT(*) AS total FROM sent_emails'),
          expired_mailboxes: await count("SELECT COUNT(*) AS total FROM mailboxes WHERE expires_at IS NOT NULL AND datetime(expires_at) <= datetime('now')")
        },
        latest_message_at: latestMessage?.received_at || null,
        latest_sent_status: latestSent || null
      });
    } catch (e) {
      return errorResponse('健康检查失败', 500);
    }
  }

  // 管理员数据分析：运营趋势和分布图表的数据源
  if (path === '/api/admin/analytics' && request.method === 'GET') {
    const range = normalizeAnalyticsRange(url.searchParams.get('range'));
    if (isMock) return Response.json(buildMockAnalytics(range));
    if (!isStrictAdmin(request, options)) return errorResponse('Forbidden', 403);
    try {
      return Response.json(await buildAdminAnalytics(db, range));
    } catch (e) {
      return errorResponse('数据分析失败', 500);
    }
  }

  // 管理员查看审计日志
  if (path === '/api/audit/logs' && request.method === 'GET') {
    if (isMock) return Response.json({ list: [], total: 0 });
    if (!isStrictAdmin(request, options)) return errorResponse('Forbidden', 403);
    try {
      const limit = Math.max(1, Math.min(200, Number(url.searchParams.get('limit') || 50)));
      const offset = Math.max(0, Number(url.searchParams.get('offset') || 0));
      const action = String(url.searchParams.get('action') || '').trim();
      const where = action ? 'WHERE action = ?' : '';
      const binds = action ? [action] : [];
      const totalRow = await db.prepare(`SELECT COUNT(*) AS total FROM audit_logs ${where}`)
        .bind(...binds).first();
      const { results } = await db.prepare(`
        SELECT id, actor_role, actor_user_id, actor_username, action,
               target_type, target_id, target_address, metadata, ip, created_at
        FROM audit_logs
        ${where}
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ? OFFSET ?
      `).bind(...binds, limit, offset).all();
      return Response.json({ list: results || [], total: totalRow?.total || 0 });
    } catch (e) {
      return Response.json({ list: [], total: 0 });
    }
  }

  // 管理员清理已过期邮箱及其邮件
  if (path === '/api/maintenance/cleanup' && request.method === 'POST') {
    if (isMock) return errorResponse('演示模式不可清理', 403);
    if (!isStrictAdmin(request, options)) return errorResponse('Forbidden', 403);
    try {
      const result = await cleanupExpiredMailboxes(db, options.r2, Number(url.searchParams.get('limit') || 500));
      await logAuditEvent(db, request, options, {
        action: 'maintenance.cleanup_expired_mailboxes',
        targetType: 'mailbox',
        metadata: result
      });
      return Response.json(result);
    } catch (e) {
      return errorResponse('清理失败', 500);
    }
  }

  // 切换邮箱置顶状态
  if (path === '/api/mailboxes/pin' && request.method === 'POST') {
    if (isMock) return errorResponse('演示模式不可操作', 403);
    const address = url.searchParams.get('address');
    if (!address) return errorResponse('缺少 address 参数', 400);
    const payload = getJwtPayload(request, options);
    let uid = Number(payload?.userId || 0);
    
    if (!uid && isStrictAdmin(request, options)) {
      try {
        const { results } = await db.prepare('SELECT id FROM users WHERE username = ?')
          .bind(String(options?.adminName || 'admin').toLowerCase()).all();
        if (results && results.length) {
          uid = Number(results[0].id);
        } else {
          const uname = String(options?.adminName || 'admin').toLowerCase();
          await db.prepare("INSERT INTO users (username, role, can_send, mailbox_limit) VALUES (?, 'admin', 1, 9999)").bind(uname).run();
          const again = await db.prepare('SELECT id FROM users WHERE username = ?').bind(uname).all();
          uid = Number(again?.results?.[0]?.id || 0);
        }
      } catch (_) { uid = 0; }
    }
    if (!uid) return errorResponse('未登录', 401);
    try {
      if (!isStrictAdmin(request, options)) {
        const access = await getMailboxAccess(db, request, options, { address });
        if (!access.exists) return errorResponse('Not Found', 404);
        if (!access.allowed) return errorResponse('Forbidden', 403);
      }
      const result = await toggleMailboxPin(db, address, uid);
      await logAuditEvent(db, request, options, {
        action: 'mailbox.pin.toggle',
        targetType: 'mailbox',
        targetAddress: address,
        metadata: result
      });
      return Response.json({ success: true, ...result });
    } catch (e) {
      return errorResponse('操作失败: ' + e.message, 500);
    }
  }

  // 委托给管理员 API 处理剩余操作
  const adminResult = await handleMailboxAdminApi(request, db, url, path, options);
  if (adminResult) return adminResult;

  return null;
}
