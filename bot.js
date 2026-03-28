import fs from "fs";
import readline from "readline";

const BASE_URL = "https://ilinkai.weixin.qq.com";

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

// ========== 自动重连配置（可调参数） ==========
// 测试时将数值改小，例如：
//   session_duration: 300, warning_before: 60, reminder_interval: 30,
//   force_before: 60, qrcode_scan_timeout: 120
const RECONNECT_CONFIG = {
  session_duration:    24 * 3600,  // 会话总时长（秒）
  warning_before:       2 * 3600,  // 提前多久发出警告（秒）
  reminder_interval:      30 * 60, // 用户回 N 后多久再问（秒）
  force_before:           30 * 60, // 最后多久强制重连（秒）
  qrcode_scan_timeout:       600,  // 等待用户扫码最长时间（秒）
};
// =============================================

// ========== 配置文件 ==========
const CONFIG_FILE = "config.json";
const DEFAULT_PROMPT = "你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些";

function maskKey(key) {
  if (key.length <= 10) return key;
  return key.slice(0, 5) + "*".repeat(key.length - 10) + key.slice(-5);
}

function rlQuestion(rl, q) {
  return new Promise(resolve => rl.question(q, resolve));
}

async function loadOrCreateConfig() {
  const sep = "=".repeat(60);
  const dash = "-".repeat(60);
  while (true) {
    if (!fs.existsSync(CONFIG_FILE)) {
      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      console.log(`\n${sep}`);
      console.log("  首次运行，需要配置 API 信息");
      console.log(sep);
      console.log();
      console.log("  !! 重要提示 !!");
      console.log("  当前版本仅支持 DusAPI");
      console.log("  注册地址：https://dusapi.com");
      console.log("  如需使用其他 AI 接口，请前往 GitHub 拉取源代码自行修改");
      console.log(dash);

      const apiKey = (await rlQuestion(rl, "\n请输入 API Key（留空使用默认值 your-api-key）: ")).trim() || "your-api-key";
      const baseUrl = (await rlQuestion(rl, "请输入 API 地址（留空默认 https://api.dusapi.com）: ")).trim() || "https://api.dusapi.com";
      const model = (await rlQuestion(rl, "请输入模型名称（留空默认 gpt-5）: ")).trim() || "gpt-5";
      const prompt = (await rlQuestion(rl, "请输入系统提示词（留空使用默认值）: ")).trim() || DEFAULT_PROMPT;
      rl.close();

      const cfg = { api_key: apiKey, base_url: baseUrl, model, prompt };
      fs.writeFileSync(CONFIG_FILE, JSON.stringify(cfg, null, 2), "utf-8");
      console.log(`\n配置已保存到 ${CONFIG_FILE}\n`);
      return cfg;
    } else {
      const cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf-8"));
      console.log(`\n${sep}`);
      console.log("  检测到配置文件，当前配置如下：");
      console.log(sep);
      console.log(`  API Key  : ${maskKey(cfg.api_key ?? "")}`);
      console.log(`  API 地址 : ${cfg.base_url ?? ""}`);
      console.log(`  模型     : ${cfg.model ?? ""}`);
      const p = cfg.prompt ?? "";
      console.log(`  提示词   : ${p.slice(0, 50)}${p.length > 50 ? "..." : ""}`);
      console.log(dash);

      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      const choice = (await rlQuestion(rl, "\n使用此配置继续？(直接回车或输入 Y 继续 / 输入 N 重新配置): ")).trim().toUpperCase();
      rl.close();

      if (choice === "N") {
        fs.unlinkSync(CONFIG_FILE);
        continue;
      }
      return cfg;
    }
  }
}
// ==============================

const COMMANDS_MSG = [
  "连接成功！",
  "可用指令：",
  "/help  /指令   - 查看全部指令列表",
  "/time          - 查询当前连接剩余时间",
  "/重新连接       - 立即触发重新连接（需确认）",
  "",
  "非指令输入即为 AI 对话"
].join("\n");

// 共享状态（模块级）
let botToken;
let botBaseUrl = BASE_URL;
let getUpdatesBuf = "";
const typingTicketCache = {};
let lastContact = { fromId: null, contextToken: null };
const welcomedUsers = new Set();
const manualReconnectPending = new Set();  // 存放 fromId，等待用户确认手动重连
let warningActive = false;
let reconnectInProgress = false;
let reconnectResolve = null;  // Y 回复时调用：reconnectResolve?.()
let loginTime;

function makeHeaders(token) {
  const uin = BigInt(Math.floor(Math.random() * 0xFFFFFFFF)).toString();
  return {
    "Content-Type": "application/json",
    "AuthorizationType": "ilink_bot_token",
    "X-WECHAT-UIN": Buffer.from(uin).toString("base64"),
    ...(token ? { "Authorization": `Bearer ${token}` } : {})
  };
}

async function apiPost(path, body, token, baseUrl) {
  const url = `${baseUrl ?? botBaseUrl}/${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: makeHeaders(token ?? botToken),
    body: JSON.stringify(body)
  });
  return res.json();
}

async function sendMsgSafe(toId, contextToken, text) {
  if (!toId || !contextToken) {
    console.log(`[重连通知] ${text}`);
    return;
  }
  try {
    const clientId = `openclaw-weixin-${Math.floor(Math.random() * 0xFFFFFFFF).toString(16).padStart(8, "0")}`;
    await apiPost("ilink/bot/sendmessage", {
      msg: {
        from_user_id: "",
        to_user_id: toId,
        client_id: clientId,
        message_type: 2,
        message_state: 2,
        context_token: contextToken,
        item_list: [{ type: 1, text_item: { text } }]
      },
      base_info: { channel_version: "1.0.2" }
    });
  } catch (e) {
    console.log(`[重连通知] 发送失败(${e?.message})，降级打印: ${text}`);
  }
}

async function doReconnect() {
  if (reconnectInProgress) return;
  reconnectInProgress = true;
  warningActive = false;
  reconnectResolve = null;

  console.log("[重连] 开始重连流程...");
  const { fromId, contextToken } = lastContact;

  // 获取新二维码（必须带 bot_type=3，使用动态 botBaseUrl）
  let qrcode, qrcodeUrl;
  try {
    const data = await fetch(`${botBaseUrl}/ilink/bot/get_bot_qrcode?bot_type=3`).then(r => r.json());
    qrcode = data.qrcode;
    qrcodeUrl = data.qrcode_img_content ?? qrcode;
  } catch (e) {
    console.log(`[重连] 获取二维码失败: ${e?.message}`);
    reconnectInProgress = false;
    loginTime = Date.now();
    return;
  }

  const qrMsg = `[重连] 请扫码完成新连接：${qrcodeUrl}`;
  console.log(qrMsg);
  await sendMsgSafe(fromId, contextToken, qrMsg);

  // 轮询扫码状态（带超时）
  const deadline = Date.now() + RECONNECT_CONFIG.qrcode_scan_timeout * 1000;
  let newToken = null, newBaseUrl = null;
  while (Date.now() < deadline) {
    try {
      const status = await fetch(
        `${botBaseUrl}/ilink/bot/get_qrcode_status?qrcode=${qrcode}`
      ).then(r => r.json());
      if (status.status === "confirmed") {
        newToken = status.bot_token;
        newBaseUrl = status.baseurl ?? botBaseUrl;
        break;
      }
    } catch {}
    await sleep(1000);
  }

  if (!newToken) {
    console.log("[重连] 扫码超时，重连未完成");
    await sendMsgSafe(fromId, contextToken, "[失败] 扫码超时，重连未完成，下次到期前会再次提醒");
    loginTime = Date.now();
    reconnectInProgress = false;
    return;
  }

  // 原子替换 token
  botToken = newToken;
  botBaseUrl = newBaseUrl;
  Object.keys(typingTicketCache).forEach(k => delete typingTicketCache[k]);
  console.log("[重连] 新连接已建立，token 已切换");
  await sendMsgSafe(fromId, contextToken, "[完成] 新连接已建立，已自动切换，继续使用");

  reconnectInProgress = false;
  loginTime = Date.now();
}

async function reconnectTimerLoop() {
  while (true) {
    // 等待到发警告的时间点
    const elapsed = (Date.now() - loginTime) / 1000;
    const firstWait = Math.max(0, RECONNECT_CONFIG.session_duration - RECONNECT_CONFIG.warning_before - elapsed);
    await sleep(firstWait * 1000);

    // 检查剩余时间
    let remaining = (loginTime + RECONNECT_CONFIG.session_duration * 1000 - Date.now()) / 1000;
    if (remaining <= RECONNECT_CONFIG.force_before) {
      const msg = "[自动] 连接即将到期，开始强制重新连接...";
      console.log(msg);
      await sendMsgSafe(lastContact.fromId, lastContact.contextToken, msg);
      await doReconnect();
      continue;
    }

    // 发初次警告
    const remainingH = (remaining / 3600).toFixed(1);
    const warnMsg = `[提醒] 连接还剩约 ${remainingH} 小时到期，是否现在重新连接？回复 Y 立即重连，N 稍后提醒`;
    console.log(warnMsg);
    await sendMsgSafe(lastContact.fromId, lastContact.contextToken, warnMsg);
    warningActive = true;

    // 询问循环
    while (true) {
      remaining = (loginTime + RECONNECT_CONFIG.session_duration * 1000 - Date.now()) / 1000;
      if (remaining <= RECONNECT_CONFIG.force_before) {
        const forceMsg = "[自动] 连接即将到期，开始强制重新连接...";
        console.log(forceMsg);
        await sendMsgSafe(lastContact.fromId, lastContact.contextToken, forceMsg);
        await doReconnect();
        break;
      }

      const waitSecs = Math.max(0, Math.min(RECONNECT_CONFIG.reminder_interval,
                                             remaining - RECONNECT_CONFIG.force_before));

      // 等待用户 Y 或超时，取先完成者
      let userReplied = false;
      await Promise.race([
        new Promise(r => { reconnectResolve = () => { userReplied = true; r(); }; }),
        sleep(waitSecs * 1000)
      ]);

      if (userReplied) {
        await doReconnect();
        break;
      }

      // 超时：重新评估剩余时间
      remaining = (loginTime + RECONNECT_CONFIG.session_duration * 1000 - Date.now()) / 1000;
      if (remaining <= RECONNECT_CONFIG.force_before) continue;

      const remainingM = Math.round(remaining / 60);
      const remindMsg = `[提醒] 连接还剩约 ${remainingM} 分钟，是否现在重新连接？回复 Y 立即重连，N 继续等待`;
      console.log(remindMsg);
      await sendMsgSafe(lastContact.fromId, lastContact.contextToken, remindMsg);
    }
  }
}

async function messageLoop() {
  console.log("开始监听消息...");
  while (true) {
    const result = await apiPost(
      "ilink/bot/getupdates",
      { get_updates_buf: getUpdatesBuf, base_info: { channel_version: "1.0.2" } }
    );
    getUpdatesBuf = result.get_updates_buf ?? getUpdatesBuf;

    for (const msg of result.msgs ?? []) {
      if (msg.message_type !== 1) continue;
      const text = msg.item_list?.[0]?.text_item?.text;
      const fromId = msg.from_user_id;
      const contextToken = msg.context_token;
      console.log(`收到消息: ${text}`);

      // 更新最近联系人
      lastContact = { fromId, contextToken };

      // 优先级 1：手动重连 Y/N 确认（/重新连接 发出后等待回复）
      if (manualReconnectPending.has(fromId) && ["Y", "N"].includes(text?.trim()?.toUpperCase())) {
        manualReconnectPending.delete(fromId);
        if (text.trim().toUpperCase() === "Y") {
          await sendMsgSafe(fromId, contextToken, "好的，正在重新连接...");
          await doReconnect();
        } else {
          await sendMsgSafe(fromId, contextToken, "已取消重新连接");
        }
        continue;
      }

      // 优先级 2：定时预警 Y/N 处理
      if (warningActive && ["Y", "N"].includes(text?.trim()?.toUpperCase())) {
        if (text.trim().toUpperCase() === "Y") {
          reconnectResolve?.();
          await sendMsgSafe(fromId, contextToken, "好的，正在重新连接...");
        } else {
          await sendMsgSafe(fromId, contextToken, "好的，稍后再提醒您");
        }
        continue;
      }

      // 优先级 3：首次交互，发送指令列表
      if (!welcomedUsers.has(fromId)) {
        welcomedUsers.add(fromId);
        await sendMsgSafe(fromId, contextToken, COMMANDS_MSG);
        continue;
      }

      // /help  /指令 — 返回指令列表
      if (["/help", "/指令"].includes(text?.trim())) {
        await sendMsgSafe(fromId, contextToken, COMMANDS_MSG);
        continue;
      }

      // /time 指令
      if (text?.trim() === "/time") {
        const rem = Math.max(0, (loginTime + RECONNECT_CONFIG.session_duration * 1000 - Date.now()) / 1000);
        const h = Math.floor(rem / 3600);
        const m = Math.floor((rem % 3600) / 60);
        const s = Math.floor(rem % 60);
        const ts = h > 0 ? `${h} 小时 ${m} 分钟` : `${m} 分钟 ${s} 秒`;
        await sendMsgSafe(fromId, contextToken, `当前连接剩余时间：${ts}`);
        continue;
      }

      // /重新连接 — 手动触发重连，等待 Y/N 确认
      if (text?.trim() === "/重新连接") {
        if (reconnectInProgress) {
          await sendMsgSafe(fromId, contextToken, "重连正在进行中，请稍候...");
        } else {
          manualReconnectPending.add(fromId);
          await sendMsgSafe(fromId, contextToken, "确认要立即重新连接吗？\n回复 Y 确认重连 / N 取消");
        }
        continue;
      }

      // getconfig 获取 typing_ticket（每个用户首次调用，缓存复用）
      if (!typingTicketCache[fromId]) {
        const cfg = await apiPost("ilink/bot/getconfig", {
          ilink_user_id: fromId,
          context_token: contextToken,
          base_info: { channel_version: "1.0.2" }
        });
        typingTicketCache[fromId] = cfg.typing_ticket ?? "";
      }
      const typingTicket = typingTicketCache[fromId];

      // sendtyping status=1：显示"正在输入"
      if (typingTicket) {
        await apiPost("ilink/bot/sendtyping", {
          ilink_user_id: fromId,
          typing_ticket: typingTicket,
          status: 1
        });
      }

      // 回复内容（替换为你的 AI 调用）
      const reply = "你好";

      // sendmessage：补全 SDK 所需字段
      const clientId = `openclaw-weixin-${Math.floor(Math.random() * 0xFFFFFFFF).toString(16).padStart(8, "0")}`;
      await apiPost("ilink/bot/sendmessage", {
        msg: {
          from_user_id: "",
          to_user_id: fromId,
          client_id: clientId,
          message_type: 2,
          message_state: 2,
          context_token: contextToken,
          item_list: [{ type: 1, text_item: { text: reply } }]
        },
        base_info: { channel_version: "1.0.2" }
      });
      console.log(`已回复: ${reply}`);

      // sendtyping status=2：取消"正在输入"
      if (typingTicket) {
        await apiPost("ilink/bot/sendtyping", {
          ilink_user_id: fromId,
          typing_ticket: typingTicket,
          status: 2
        });
      }
    }
  }
}

// ── 启动流程 ──

console.log(`
╔══════════════════════════════════════════════════════════╗
║          微信 ClawBot  ·  WeChat iLink Bot               ║
║  Copyright (c) 2026 SiverKing. All rights reserved.     ║
║  GitHub : https://github.com/SiverKing/weixin-ClawBot-API║
╚══════════════════════════════════════════════════════════╝`);

// 0. 加载配置
const botConfig = await loadOrCreateConfig();

// 1. 获取二维码
const { qrcode, qrcode_img_content } = await fetch(
  `${BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3`
).then(r => r.json());

if (qrcode_img_content) {
  const content = String(qrcode_img_content);
  if (content.startsWith("data:image/")) {
    const [header, b64] = content.split(",");
    const ext = header.match(/data:image\/(\w+)/)?.[1] ?? "png";
    fs.writeFileSync(`qrcode.${ext}`, Buffer.from(b64, "base64"));
    console.log(`二维码已保存到 qrcode.${ext}`);
  } else if (content.startsWith("http")) {
    console.log("二维码图片地址:", content);
    console.log("请将图片地址发送给文件传输助手，然后用手机端微信打开链接进行连接！！！");
  } else if (content.startsWith("<svg")) {
    fs.writeFileSync("qrcode.svg", content);
    console.log("二维码已保存到 qrcode.svg，用浏览器打开");
  } else {
    fs.writeFileSync("qrcode.png", Buffer.from(content, "base64"));
    console.log("二维码已保存到 qrcode.png");
  }
}

// 2. 等待扫码
console.log("等待扫码...");
while (true) {
  const status = await fetch(
    `${BASE_URL}/ilink/bot/get_qrcode_status?qrcode=${qrcode}`
  ).then(r => r.json());

  if (status.status === "confirmed") {
    botToken = status.bot_token;
    botBaseUrl = status.baseurl ?? BASE_URL;
    console.log("登录成功！");
    console.log("=".repeat(40));
    console.log(COMMANDS_MSG);
    console.log("=".repeat(40));
    break;
  }
  await sleep(1000);
}

// 3. 记录登录时间，并发启动消息循环和定时器循环
loginTime = Date.now();
await Promise.all([messageLoop(), reconnectTimerLoop()]);
