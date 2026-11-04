# AstrBot 综合插件

把零散的娱乐与工具小功能集中到一个插件里，每个功能在配置页顶部都有独立开关，关掉的功能不响应指令，对应的函数工具也不会交给 LLM。

## 功能与命令

| 功能 | 用法 | 说明 |
| --- | --- | --- |
| 左右脑互搏 | `/左右脑互搏 [回合数]` | 默认 3 回合，需要已配置对话模型 |
| 看看腿 | 消息含`看看腿` | 随机图片，接口可在「娱乐小功能设置」更换 |
| 历史上的今天 | 消息含`历史上的今天` | 图片或文字；未填柠柚 Key 时用免费文字接口 |
| Emoji 合成 | `合成 😀😭` | 两个 Emoji 合成一张图 |
| 点阵字 | `点阵字 你好 好` | 用填充字拼出文字点阵，本地生成 |
| 今日人品 | `/jrrp` | 每人每天固定 |
| 浅草寺抽签 | `/抽签`、`/转运`、`/解签`、`/抽签帮助` | 解签需要对话模型；LLM 也可调用 `explain_fortune` |
| 卢恩占卜 | `/卢恩占卜 单符\|三符\|五符\|九符\|符文表 [问题]` | 抽符后交给 LLM 解读 |
| 梗图猜词 | `/梗图`、`/答案 <答案>`、`/提示`、`/梗图帮助` | 需要柠柚 API Key，每人独立记题 |
| Safebooru | `/safebooru <标签>`、`/safebooru_random` | 标签支持中文，同一标签依次轮换图片 |
| 图片标签识别 | `/tag` | 带图、引用图片，或发指令后再发图 |
| 二维码 | `/qr <链接或文字>`、`/qr_decode`（`/二维码转链接`、`/扫码`） | 识别同样支持带图、引用或随后发图 |
| 天气 | `/weather current\|forecast\|help [城市]` | 心知天气；LLM 可调用 `get_current_weather`、`get_forecast_weather` |
| 百度翻译 | `/fy <目标语种> <内容>`、`/fy help` | 支持 28 种语言 |
| IP / Whois | `/ip <IP或域名>`、`/whois <域名>` | |
| B 站评论查询 | `/uid <UID>` | 基于 AICU，生成用户评论画像图 |
| 数学绘图 | `/plot`、`/plot3d`、`/polar`、`/parametric`、`/vector` 等 | 发送 `/plot` 等不带参数可看用法；LLM 可调用 16 个 `plot_*` 工具 |
| 四则运算 | 函数工具 `FFRadd` 等 | 让 LLM 精确计算加减乘除和比大小 |
| Markdown 转图片 | 自动 | LLM 把复杂格式写进 `<md></md>`，发送时渲染成图片 |
| 点赞 | `赞我`、`赞@某人`、`/订阅点赞`、`/取消订阅点赞`、`/订阅点赞列表`、`/谁赞了bot` | 仅 QQ（aiocqhttp）；订阅用户每天首次有人点赞时自动点赞 |

数学绘图完整指令：`/plot`、`/plot3d`、`/plot3dm`、`/polar`、`/parametric`、`/parametric3d`、`/spherical`、`/implicit3d`、`/vector`、`/vector3d`、`/vector2d`、`/vector_add`、`/vector_scale`、`/basis`、`/plot_status`。

## 配置

在 **AstrBot WebUI → 插件 → 综合插件 → 配置** 中操作。需要 Key 的功能：

**心知天气（天气）**
1. 打开 [心知天气](https://www.seniverse.com) 注册并登录。
2. 进入「控制台 → 产品管理」，开通 **免费版**（天气实况、3 天预报、生活指数）。
3. 在免费版的密钥信息中复制 **私钥**，填入「天气设置 → 心知天气 API 私钥」。

**百度翻译（/fy）**
1. 打开 [百度翻译开放平台](https://fanyi-api.baidu.com)，登录后开通 **通用文本翻译**（有免费额度）。
2. 进入「管理控制台 → 开发者信息」，复制 **APP ID** 和 **密钥**。
3. 分别填入「百度翻译设置 → APP ID / 密钥」。

**柠柚 API（梗图猜词、历史上的今天）**：在 [柠柚 API](https://api.nycnm.cn) 注册，于用户中心获取 API Key，填入「柠柚 API → 柠柚 API Key」。

其余可选项：AICU 的 Cookie（不填时设备信息不可用）、图片标签识别的 Gradio 地址与模型（默认使用 `smilingwolf-wd-tagger.hf.space`，可换成自建的同款 Space）。

## 运行依赖

- B 站评论查询和 Markdown 转图片使用 Playwright Chromium，缺失时首次使用会自动执行 `playwright install chromium`。
- 二维码识别优先用 pyzbar（Linux 需 `apt install libzbar0`），不可用时自动改用 OpenCV（如已安装）。
- 数学绘图图中有中文时需要系统中文字体，如 `apt install fonts-noto-cjk`。

## 交流与反馈

使用问题、Bug 反馈和功能建议欢迎加群交流：

| 吟枢苑✨插件交流群 | 吟枢苑✨吟窝（娱乐群） |
| :---: | :---: |
| 群号：529924533 | 群号：1059351824 |
| <img src="https://raw.githubusercontent.com/yiyinfaith/astrbot_plugin_omni/main/assets/qq-group-plugin.jpg" alt="插件交流群二维码" width="240" /> | <img src="https://raw.githubusercontent.com/yiyinfaith/astrbot_plugin_omni/main/assets/qq-group-chat.jpg" alt="娱乐群二维码" width="240" /> |

## 参考与许可

项目：[yiyinfaith/astrbot_plugin_omni](https://github.com/yiyinfaith/astrbot_plugin_omni)。各功能整合自以下插件，感谢原作者：
[meanig](https://github.com/ttq7/meanig)、
[primermath](https://github.com/tenno1174/astrbot_plugin_primermath)、
[runes](https://github.com/yuweitk/astrbot_plugin_runes)、
[aicu](https://github.com/Huahuatgc/astrbot_plugin_aicu)、
[gengtu](https://github.com/ningyou8023/astrbot_plugin_gengtu)、
[ipquery](https://github.com/TianLang-Hacker/astrbot_plugin_ipquery)、
[markdown2img](https://github.com/t0saki/astrbot_plugin_markdown2img)、
[math_plotter](https://github.com/D1ff1culTT/astrbot_plugin_math_plotter)、
[qrcode](https://github.com/Soffd/astrbot_plugin_qrcode)、
[sensoji_pro](https://github.com/xiamuceer-j/astrbot_plugin_sensoji_pro)、
[tagger](https://github.com/yudengghost/astrbot_plugin_tagger)、
[weather](https://github.com/Last-emo-boy/astrbot_plugin_weather)、
[jrrp](https://github.com/exusiaiwei/jrrp)、
[safebooru](https://github.com/Last-emo-boy/astrbot_plugin_safebooru)、
[Translation](https://github.com/zengweis/astrbot_plugin_Translation)、
[zanwo](https://github.com/Futureppo/astrbot_plugin_zanwo)。
遵守本仓库许可证及各上游服务的使用要求。
