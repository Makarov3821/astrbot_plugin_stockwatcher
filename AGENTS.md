# AstrBot 源码参考

- 当前目录的 `astrbot-temp` 是指向 `../astrbot_plugin_vikunja/astrbot-temp` 的软链接，复用已有 AstrBot 克隆，不另行复制。
- 每次开始查阅 AstrBot 源码前，先检查该仓库状态，并执行 `git -C astrbot-temp pull --ff-only` 更新。
- 若有本地修改、分支分叉或拉取失败，保留现状并明确说明；不要自动清理、覆盖或重置共享源码。必要时说明所参考的实际提交。
- 软链接仅用于本地开发，已经加入 `.gitignore`，不要把 AstrBot 源码纳入插件仓库。
