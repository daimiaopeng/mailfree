/**
 * EML 验证码提取测试
 *
 * 验证 extractVerificationCode 能否从真实邮件中正确提取验证码。
 * 预期值通过手动阅读邮件内容确认。
 */

import PostalMime from 'postal-mime';
import { readFileSync } from 'fs';

const SRC_BASE = new URL('../../src/email/parser.js', import.meta.url);
const EML_DIR = new URL('../../eml/', import.meta.url);

const { extractVerificationCode } = await import(SRC_BASE.href);

const TESTS = [
  {
    file: '032533-c820179e-3469-4a5e-90db-0be339ab9674.eml',
    expected: '739513',
    label: 'ChatGPT 验证码 (仅 HTML，无纯文本)',
  },
  {
    file: '094032-1a82fab7-1e5e-4cd7-b7bd-668ba9400196.eml',
    expected: '538388',
    label: 'Cursor 登录验证码 (中英混合)',
  },
  {
    file: '043204-44a2cf22-df52-45ac-9925-890035594df7.eml',
    expected: '611407',
    label: '宝可梦转发邮件验证码',
  },
  {
    file: '124005-ab0a5fee-d36f-45a5-9b04-e93bbc94db0a.eml',
    expected: '769683',
    label: 'Cursor 英文登录验证码',
  },
];

let passed = 0;
let failed = 0;

for (const { file, expected, label } of TESTS) {
  const path = new URL(file, EML_DIR).pathname;
  const raw = readFileSync(path, 'utf-8');
  const email = await PostalMime.parse(raw);

  const code = extractVerificationCode({
    subject: email.subject || '',
    text: email.text || '',
    html: email.html || '',
  });

  if (code === expected) {
    console.log(`✓ PASS  ${label}`);
    console.log(`        预期: ${expected} | 提取: ${code}`);
    passed++;
  } else {
    console.log(`✗ FAIL  ${label}`);
    console.log(`        预期: ${expected} | 提取: ${code || '(空)'}`);
    console.log(`        Subject: ${email.subject || '(empty)'}`);
    console.log(`        Text preview: ${(email.text || '').substring(0, 100)}`);
    failed++;
  }
  console.log();
}

console.log(`\n总计: ${passed} 通过, ${failed} 失败, ${TESTS.length} 测试`);
process.exit(failed > 0 ? 1 : 0);
