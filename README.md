# AstrBot 综合插件

<p align="center"><img src="https://raw.githubusercontent.com/yiyinfaith/astrbot_plugin_omni/main/logo.png" alt="综合插件 Logo" width="160" height="160" /></p>

把零散的娱乐与工具小功能集中到一个插件里。每个功能在配置页有自己的分组，开关和设置放在一起；关掉的功能不响应指令，对应的函数工具也不会交给 LLM。

## 指令

| 功能 | 用法 | 说明 |
| --- | --- | --- |
| 左右脑互搏 | `/左右脑互搏 [回合数]` | 默认 3 回合，需要已配置对话模型 |
| 看看腿 | `/看看腿` | 随机图片，默认关闭 |
| 历史上的今天 | `/历史上的今天` | 图片或文字，可在配置中切换 |
| Emoji 合成 | `/合成😀😭` | 合成两个 Emoji，可不加空格 |
| 点阵字 | `/点阵字 <内容> <填充字>` | 如 `/点阵字 你好 好` |
| 今日人品 | `/今日人品` | 每人每天固定 |
| 浅草寺抽签 | `/抽签`、`/转运`、`/解签` | 每天一签，转运次数可配置；解签需要对话模型 |
| 卢恩占卜 | `/卢恩占卜 单符\|三符\|五符\|九符\|符文表 [问题]` | 抽符后交给 LLM 解读 |
| 梗图猜词 | `/梗图`、`/答案 <答案>` | 只发 `/答案` 查看正确答案；需要柠柚 API Key |
| 随机图片 | `/随机图片 [标签]` | 来自 Safebooru；不带标签完全随机，标签支持中文 |
| 图片标签识别 | `/tag` | 带图、引用图片，或发指令后再发图 |
| 二维码 | `/二维码 <内容>`、`/二维码`（或 `/扫码`）+ 图片 | 带内容生成，带图或引用图片识别成链接 |
| 天气 | `/天气 [城市]` | 实况、未来 3 天预报和生活指数 |
| 翻译 | `/翻译 <目标语种> <内容>` | 百度翻译，28 种语言；只发 `/翻译` 查看语种 |
| IP / 域名 | `/查IP <IP或域名>`、`/查域名 <域名>` | 归属地与运营商、Whois 注册信息 |
| B 站查询 | `/B站查询 <UID>` | 基于 AICU 生成用户评论画像图 |
| 点赞 | `赞我`、`赞@某人`、`/订阅点赞` | 仅 QQ（aiocqhttp）；订阅后每天首次有人点赞时自动给你点赞，再发一次 `/订阅点赞` 取消 |
| 对话回退 | `/回退 [轮数]`、`/取消回退` | 不满意回复时删掉最近几轮对话（默认 1 轮，最多 10 轮）重新提问；下一次对话前可恢复。英文写法 `/rewind x`、`/rewind cancel` |
| 执行器与指令查询 | `/指令查询帮助`、`/刷新指令缓存`、`/执行器状态`、`/测试bot身份` | 共用「LLM 指令工具」配置组；LLM 可查询当前配置下的指令、插件状态并按权限执行指令 |

## 函数工具

| 工具 | 说明 |
| --- | --- |
| `calculate` | 精确计算算式（加减乘除、乘方、比较大小等），避免 LLM 心算出错 |
| `get_weather` | 查询城市天气 |
| `explain_fortune` | 解读用户今天抽到的签 |
| `plot_2d` | 二维图像：函数（可多条对比）、隐式方程、极坐标、参数方程、矢量场 |
| `plot_3d` | 三维图像：曲面（可多个对比）、隐式曲面、球坐标、空间参数曲线 |
| `plot_vectors` | 向量示意：二维/三维向量、加法、数乘、基变换 |
| `execute_command` | 按当前会话权限执行 AstrBot 指令 |
| `list_executable_commands` | 列出当前可由 LLM 执行的指令 |
| `search_command` | 搜索当前 Bot 配置中生效的指令 |
| `get_command_detail` | 查询一条指令的详细用法 |
| `get_plugin_status` | 查询当前 Bot 与配置下的插件状态 |
| `list_plugin_commands` | 列出插件提供的生效指令 |

开启「Markdown 转图片」后，LLM 会把表格、代码块等复杂格式和普通文字打不出来的数学公式写进 `<md></md>`，发送时自动渲染成图片（公式由 MathJax 排版）。

开启「上下文与群聊优化」后，可按需压缩旧工具结果、提供当前群身份查询工具、关闭指定群聊的 @Bot 或引用 Bot 唤醒，并在空闲时自动清理 AstrBot 的临时缓存。

## 配置

在 **AstrBot WebUI → 插件 → 综合插件 → 配置** 中操作。需要 Key 的功能：

**心知天气（天气）**
1. 打开 [心知天气](https://www.seniverse.com) 注册并登录。
2. 进入「控制台 → 产品管理」，开通 **免费版**（天气实况、3 天预报、生活指数）。
3. 在免费版的密钥信息中复制 **私钥**，填入「天气 → 心知天气 API 私钥」。

**百度翻译**
1. 打开 [百度翻译开放平台](https://fanyi-api.baidu.com)，登录后开通 **通用文本翻译**（有免费额度）。
2. 进入「管理控制台 → 开发者信息」，复制 **APP ID** 和 **密钥**。
3. 分别填入「百度翻译 → APP ID / 密钥」。

**柠柚 API（梗图猜词、历史上的今天）**：在 [柠柚 API](https://api.nycnm.cn) 注册，于用户中心获取 API Key，填入「柠柚 API → 柠柚 API Key」。未填时历史上的今天改用免费文字接口。

其余可选项：B 站查询的 AICU Cookie（不填时设备信息不可用）、图片标签识别的 Gradio 地址与模型（默认 `smilingwolf-wd-tagger.hf.space`，可换成自建的同款 Space）。

## 运行依赖

- B 站查询和 Markdown 转图片使用 Playwright Chromium，缺失时首次使用会自动执行 `playwright install chromium`。
- 二维码识别优先用 pyzbar（Linux 需 `apt install libzbar0`），不可用时自动改用 OpenCV（如已安装）。
- 数学图像里有中文时需要系统中文字体，如 `apt install fonts-noto-cjk`。

## 交流与反馈

使用问题、Bug 反馈和功能建议欢迎加群交流：

| 吟枢苑✨吟窝（插件反馈群） |
| :---: |
| 群号：1059351824 |
| <img src="https://raw.githubusercontent.com/yiyinfaith/astrbot_plugin_omni/main/assets/qq-group-chat.jpg" alt="插件反馈群二维码" width="240" /> |

## 参考与许可

项目：[yiyinfaith/astrbot_plugin_omni](https://github.com/yiyinfaith/astrbot_plugin_omni)。

各功能参考以下插件的思路，感谢原作者：
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
[zanwo](https://github.com/Futureppo/astrbot_plugin_zanwo)、
[llm_amnesia](https://github.com/SinkAbyss/astrbot_plugin_llm_amnesia)、
[llm_executor](https://github.com/TenmaGabriel0721/astrbot_plugin_llm_executor)、
[command_query](https://github.com/TenmaGabriel0721/astrbot_plugin_command_query)、
[AstrNa](https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_AstrNa)

遵守本仓库许可证及各上游服务的使用要求。
