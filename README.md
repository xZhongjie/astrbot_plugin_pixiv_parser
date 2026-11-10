# astrbot_plugin_pixiv_parser

Pixiv 解析与关注新作自动推送。

## 功能

- `/pixiv 12345678`：按 pid 解析作品
- `/pixiv https://www.pixiv.net/artworks/12345678`：按链接解析
- 群消息里直接发 Pixiv 链接也会自动解析
- `/pixiv_push`：开启/关闭当前会话的新作推送
- `/pixiv_status`：查看 Cookie、推送会话、已记录作品数

## 新作推送原理

使用 Pixiv 的 `follow_latest` 接口拉取**当前登录账号关注列表**的最新作品：

1. 首次轮询只记录已有作品作为基线，不推送历史
2. 之后每次发现新 pid 就推送
3. 已推送记录保存在插件数据目录 `state.json`

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `cookie` | 空 | `PHPSESSID=...`，必须 |
| `proxy` | 空 | HTTP 代理 |
| `push_enabled` | false | 启用自动推送 |
| `poll_interval_minutes` | 30 | 轮询间隔 |
| `push_limit` | 3 | 单次最多推送数量 |
| `max_images` | 4 | 每个作品最多发几张图 |

## 使用步骤

1. 浏览器登录 pixiv.net，F12 → Application → Cookies → 复制 `PHPSESSID`
2. 填到插件配置 `cookie`
3. 打开 `push_enabled`
4. 在要接收推送的群里发 `/pixiv_push`
5. 重载插件

## 注意

- 解析和推送都依赖登录 Cookie，Cookie 失效会解析失败
- 访问 Pixiv 需要代理时，把 `proxy` 填上
- Pixiv 有访问频率限制，轮询间隔不建议低于 10 分钟
