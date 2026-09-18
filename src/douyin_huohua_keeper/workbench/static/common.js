/* common.js —— app.js 与 send.js 共用的底层工具。
 *
 * 为什么单独抽出来：两个页面各自实现了一遍「令牌存取、带令牌的请求、
 * HTML 转义、火焰 SVG」，而且实现还不一致 —— 群发页的火焰渐变 id 是写死的
 * `fireGrad`，同一页出现多个火焰时后面的会引用到第一个的渐变（实测踩过）。
 *
 * 零构建原则不变：这就是一个普通的 <script>，没有模块、没有打包。
 * 两个页面都在自己的主脚本**之前**加载它。
 */

"use strict";

/* ---------- 令牌 ----------
 * 两个页面共用同一个 key（在主站输过令牌，打开群发页不用再输一次）。
 * huohua_token 是历史 key，仍兼容读取。
 * 另外支持从 URL 的 ?token= 吸收 —— 手机上直接开带令牌的链接就能用。 */

const HUOHUA_TOKEN_KEY = "huohua.token";
const HUOHUA_LEGACY_TOKEN_KEY = "huohua_token";

function huohuaReadToken() {
  try {
    return (
      localStorage.getItem(HUOHUA_TOKEN_KEY) ||
      localStorage.getItem(HUOHUA_LEGACY_TOKEN_KEY) ||
      ""
    );
  } catch (e) {
    return "";
  }
}

function huohuaSaveToken(value) {
  try {
    if (value) {
      localStorage.setItem(HUOHUA_TOKEN_KEY, value);
      localStorage.removeItem(HUOHUA_LEGACY_TOKEN_KEY);
    } else {
      localStorage.removeItem(HUOHUA_TOKEN_KEY);
      localStorage.removeItem(HUOHUA_LEGACY_TOKEN_KEY);
    }
  } catch (e) { /* 隐私模式，忽略 */ }
}

/* 从 ?token= 吸收令牌。只在页面加载时调一次。 */
function huohuaAbsorbTokenFromUrl() {
  let fromUrl = null;
  try {
    fromUrl = new URLSearchParams(location.search).get("token");
  } catch (e) {
    fromUrl = null;
  }
  if (fromUrl) huohuaSaveToken(fromUrl);
}

/* ---------- 基础请求 ----------
 * 统一规则：带令牌头 → 401 抛 { auth: true } → 403 / 其它抛 { detail } → 204 返回 {}。
 * 网络失败抛 { detail: 人话 }。
 * 两个页面在它上面包一层，各自处理自己的令牌弹窗。 */

async function huohuaRequest(method, path, body, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  const token = huohuaReadToken();
  if (token) headers["X-Huohua-Token"] = token;
  const hasBody = body !== undefined;
  if (hasBody) headers["Content-Type"] = "application/json";

  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers,
      body: hasBody ? JSON.stringify(body) : undefined,
    });
  } catch (netErr) {
    throw { detail: `无法连接到服务：${netErr.message}。确认服务在跑、网络通。` };
  }

  // 204 之类没有 body
  if (resp.status === 204) return {};

  let payload;
  const text = await resp.text();
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (e) {
    payload = { detail: text.slice(0, 400) || `HTTP ${resp.status}` };
  }

  if (!resp.ok) {
    if (resp.status === 401) {
      throw { detail: "令牌不正确或已失效。请重新输入访问令牌。", auth: true };
    }
    if (resp.status === 403) {
      throw { detail: payload.message || payload.detail || "IP 不在允许列表内。" };
    }
    throw payload;
  }
  return payload;
}

/* ---------- 工具 ---------- */

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* ---------- 火花图标（自绘 SVG，橙色渐变）----------
 * 同一页面上有多个火焰时，渐变 id 不能重复（否则后面的会引用到第一个的渐变），
 * 所以每次调用都换一个唯一 id。
 * ⚠️ 群发页以前用的是写死的 `id="fireGrad"`，一页多个火焰就会撞 id。 */

const HUOHUA_FLAME_TEMPLATE =
  '<svg viewBox="0 0 24 24" aria-hidden="true">' +
  '<defs><linearGradient id="__FID__" x1="0" y1="0" x2="0" y2="1">' +
  '<stop offset="0" stop-color="var(--fire-1)"/>' +
  '<stop offset=".55" stop-color="var(--fire-2)"/>' +
  '<stop offset="1" stop-color="var(--fire-3)"/></linearGradient></defs>' +
  '<path fill="url(#__FID__)" d="M12.2 1.8c.3 2.6-.8 4.4-2.1 5.9C8.8 9.2 7.2 10.6 6.4 12a6.6 6.6 0 0 0-.9 3.3A6.5 6.5 0 0 0 12 22a6.5 6.5 0 0 0 6.5-6.6c0-2.2-1-4.2-2.2-6-.4 1.4-1.2 2.4-2.2 2.2-1.2-.2-1.3-1.7-.9-3.6.4-2 .2-4.4-1-6.2z"/>' +
  '<path fill="#fff" opacity=".55" d="M9.2 14.2c-.6 1-.8 1.8-.6 2.9.3 1.6 1.7 2.7 3.2 2.7-1.4-.8-2-2-2-3.3 0-.9.2-1.6-.6-2.3z"/></svg>';

let huohuaFlameSeq = 0;

function huohuaFlameSvg() {
  return HUOHUA_FLAME_TEMPLATE.replace(/__FID__/g, `fireGrad${++huohuaFlameSeq}`);
}
