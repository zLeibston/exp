# Terminal目录下的本机镜像与运行时存储

Docker 与 containerd 的持久数据统一存放在此目录；这是正在使用的数据目录，不是镜像导出备份。

- `containerd/`：镜像内容、镜像元数据、overlayfs快照。
- `docker/`：Docker管理数据、容器和构建缓存。
- `migration.json`：迁移记录与验证结果。
- `images-before-migration.json`：迁移前镜像清单，迁移后已核对一致。

系统配置：`/etc/docker/daemon.json` 的 `data-root`，以及 `/etc/containerd/config.toml` 的 `root`，均指向这里。服务仍由 systemd 管理；`/run/containerd` 保留为临时运行状态，不放入项目目录。

后续拉取和构建的新镜像会自动存储到这里。使用 `sudo docker image ls`、`sudo docker system df` 查看，用Docker命令管理镜像；底层数据目录保留root所有权。

当前绝对路径为 `/home/ubuntu/RSI/exp/tasks/terminal/env/`。`storage_relocation.json` 记录从旧 `tasks/env/` 迁入本目录的验证结果；`migration.json` 是更早从系统默认路径迁出的历史记录。
