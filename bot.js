import fs from "fs";

const BASE_URL = "https://ilinkai.weixin.qq.com";
const BOT_TOKEN_FILE = "bot_token.txt";

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

function makeHeaders(token) {
  const uin = BigInt(Math.floor(Math.random() * 0xFFFFFFFF)).toString();
  return {
    "Content-Type": "application/json",
    "AuthorizationType": "ilink_bot_token",
    "X-WECHAT-UIN": Buffer.from(uin).toString("base64"),
    ...(token ? { "Authorization": `Bearer ${token}` } : {})
  };
}

async function apiPost(path, body, token) {
  const res = await fetch(`${BASE_URL}/${path}`, {
    method: "POST",
    headers: makeHeaders(token),
    body: JSON.stringify(body)
  });
  return res.json();
}

let botToken = '';
try{
  botToken = fs.readFileSync(BOT_TOKEN_FILE, "utf8").trim();
}catch(err){}

while(true){
  if(!botToken){
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
    if(!botToken){
      console.log("等待扫码...");
      while (true) {
        const status = await fetch(
          `${BASE_URL}/ilink/bot/get_qrcode_status?qrcode=${qrcode}`
        ).then(r => r.json());

        if (status.status === "confirmed") {
          botToken = status.bot_token;
          console.log("登录成功！");
          fs.writeFileSync(BOT_TOKEN_FILE, botToken);
          break;
        }
        await sleep(1000);
      }
    }
  }
  if(!botToken)
    continue;
  while(botToken){
    // 3. 长轮询收消息
    let getUpdatesBuf = "";
    const typingTicketCache = {};  // 按用户缓存 typing_ticket
    console.log("开始监听消息...");
    while (true) {
      const { msgs, get_updates_buf, errcode } = await apiPost(
        "ilink/bot/getupdates",
        { get_updates_buf: getUpdatesBuf, base_info: { channel_version: "1.0.2" } },
        botToken
      );
      if(errcode == -14){
        console.log("登录过期，重新登录...");
        botToken = null;
        break;
      }
      getUpdatesBuf = get_updates_buf ?? getUpdatesBuf;

      for (const msg of msgs ?? []) {
        if (msg.message_type !== 1) continue;
        const text = msg.item_list?.[0]?.text_item?.text;
        const fromId = msg.from_user_id;
        const contextToken = msg.context_token;
        console.log(`收到消息: ${text}`);

        // getconfig 获取 typing_ticket（每个用户首次调用，缓存复用）
        if (!typingTicketCache[fromId]) {
          const cfg = await apiPost("ilink/bot/getconfig", {
            ilink_user_id: fromId,
            context_token: contextToken,
            base_info: { channel_version: "1.0.2" }
          }, botToken);
          typingTicketCache[fromId] = cfg.typing_ticket ?? "";
        }
        const typingTicket = typingTicketCache[fromId];

        // sendtyping status=1：显示"正在输入"
        if (typingTicket) {
          await apiPost("ilink/bot/sendtyping", {
            ilink_user_id: fromId,
            typing_ticket: typingTicket,
            status: 1
          }, botToken);
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
        }, botToken);
        console.log(`已回复: ${reply}`);

        // sendtyping status=2：取消"正在输入"
        if (typingTicket) {
          await apiPost("ilink/bot/sendtyping", {
            ilink_user_id: fromId,
            typing_ticket: typingTicket,
            status: 2
          }, botToken);
        }
      }
    }
  }
}
