#!/usr/sbin/nft -f

# ----  只允许指定白名单 IP 访问指定端口（IPv4 + IPv6） ----

# 清空现有规则
flush ruleset

table inet filter {

    # 白名单 IPv4 集合
    set whitelist_v4 {
        type ipv4_addr
        flags interval
        elements = {1.1.1.1, 1.1.1.2}
    }

    # 白名单 IPv6 集合
    set whitelist_v6 {
        type ipv6_addr
        flags interval
        elements = {2001:db8::1, 2001:db8::2}  # 示例 IPv6
    }

    chain input {
        type filter hook input priority 0;

        # 1️⃣ 允许本地回环
        iif lo accept

        # 2️⃣ 已建立/相关连接允许通过
        ct state established,related accept

        # 3️⃣ 白名单 IPv4 允许访问指定端口
        ip saddr @whitelist_v4 tcp dport {21,22,23} accept

        # 4️⃣ 白名单 IPv6 允许访问指定端口
        ip6 saddr @whitelist_v6 tcp dport {21,22,23} accept

        # 5️⃣ 默认丢掉所有其他流量
        drop
    }
}
