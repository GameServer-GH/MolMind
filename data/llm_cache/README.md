# data/llm_cache

机制 LLM 响应磁盘缓存（按 prompt SHA256 分文件）。  
若已跑过机制生成，可将缓存打进镜像，无 API Key 时仍能命中缓存得到相同 Markdown。

密钥**不会**写入此目录。
