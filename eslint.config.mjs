// 前端 JS 的静态检查配置（**开发期工具**，不参与运行时，也不进构建链）。
//
// 为什么需要它：前端是零构建的原生 JS，没有类型检查兜底，而这类低级错误
// 在浏览器里只有点到那个按钮才会暴露。实测踩过：
//   - 改 `contactRow` 时只改函数体、漏改签名，体里用 `reload` → ReferenceError
//     （注意：可选链 `reload?.()` 挡不住「根本没声明」，一样抛 ReferenceError）
//   - 对象字面量漏字段，导致拼出 `?page=NaN`
// `no-undef` 能可靠抓出前一类；其余几条规则抓的是同级别的低级手误。
//
// 用法：
//   npm install       # 只装开发依赖（node_modules 已被 .gitignore 排除）
//   npm run lint:js
//
// 环境是浏览器，所以用 globals.browser 提供 window / document / fetch 等。
import globals from "globals";

export default [
  {
    files: ["src/douyin_huohua_keeper/workbench/static/**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: {
        ...globals.browser,
        // common.js 提供的全局：两个页面都在主脚本**之前**加载它，
        // 普通脚本共享全局作用域，所以这些函数在 app.js / send.js 里直接可用。
        // ⚠️ 往 common.js 里加新函数后，记得同步登记到这里。
        huohuaReadToken: "readonly",
        huohuaSaveToken: "readonly",
        huohuaAbsorbTokenFromUrl: "readonly",
        huohuaRequest: "readonly",
        huohuaFlameSvg: "readonly",
        HUOHUA_TOKEN_KEY: "readonly",
        HUOHUA_LEGACY_TOKEN_KEY: "readonly",
        HUOHUA_FLAME_TEMPLATE: "readonly",
        // 跨文件共享的全局（app.js ↔ pages_tasks.js）：非模块脚本，靠全局作用域共享。
        // renderTasks / successStatsCard 在 pages_tasks.js 定义、被 app.js 引用；
        // state / api / h / clear / toast / errText / refresh / $$ 反之。
        renderTasks: "readonly",
        successStatsCard: "readonly",
        state: "readonly",
        api: "readonly",
        h: "readonly",
        clear: "readonly",
        toast: "readonly",
        errText: "readonly",
        refresh: "readonly",
        $$: "readonly",
      },
    },
    rules: {
      // 核心目的：用了没声明的变量 / 漏传的参数，直接报错
      "no-undef": "error",
      // 声明了没用到（漏改签名的常见伴生现象），提醒但不拦截
      "no-unused-vars": ["warn", { args: "after-used", caughtErrors: "none" }],
      // 同级别的低级手误
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-unreachable": "error",
      "no-cond-assign": "error",
      "no-constant-condition": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "valid-typeof": "error",
      "use-isnan": "error",
    },
  },
  {
    // common.js 是给两个页面共用的工具库：这里的函数本来就「不在本文件用」，
    // no-unused-vars 对它没有意义，关掉。
    files: ["src/douyin_huohua_keeper/workbench/static/common.js"],
    rules: {
      "no-unused-vars": "off",
    },
  },
  {
    // pages_tasks.js 把 app.js 里的 renderTasks / successStatsCard 拆出来：
    // 函数在本文件定义、被 app.js 调用，本文件内「未被使用」是预期，关掉 no-unused-vars。
    files: ["src/douyin_huohua_keeper/workbench/static/pages_tasks.js"],
    rules: {
      "no-unused-vars": "off",
    },
  },
];
