# 密钥约定

本目录只保存 KMS / Secret 引用说明。生产环境通过 systemd `LoadCredential=` 注入，文件位于 `/etc/aitrace/credentials/`，不得提交任何真实密钥。`qoder_access_token` 不属于本目录，任何情况下不得写入磁盘。
