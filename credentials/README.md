# 本地 X 凭证

把 X 浏览器 Cookie 写入同目录下的 `x_twitter_cookies.txt`，只保留一行，例如：

```text
auth_token=你的auth_token; ct0=你的ct0
```

这个文件已加入 `.gitignore`，不会被提交到 GitHub。程序会优先读取环境变量
`TWITTER_COOKIES`；只有环境变量为空时才读取这里的文件。

如果要使用第二个 X 账号，在 `.env` 里新增一行 `TWITTER_COOKIES_2=...`。
第一个账号使用 `TWITTER_COOKIES`，第三个可以使用 `TWITTER_COOKIES_3`，依此类推。

不要把 Cookie 发到聊天窗口。Cookie 失效后，直接在本地替换这一行即可。
