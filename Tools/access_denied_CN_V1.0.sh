#!/bin/bash

# 🔹 改进点说明
# 1.自动导入 CN IP 集合
# nft -f /etc/nftables.conf 后立即执行 add element 导入
# 修改配置或重载规则不会让屏蔽失效
# 2.白名单 + 指定端口
# 白名单 IP 只允许访问 21/22
# 非指定端口自动走默认 drop
# 3.自动更新
# update.sh 会刷新 APNIC 数据并重新导入集合
# 每天 3 点执行

# ---- 屏蔽中国地区 IP 访问 ----

set -e

echo "===> 安装依赖（nftables + wget + cron）"
apt update -y
apt install -y nftables wget cron

echo "===> 启用 nftables 开机启动"
systemctl enable nftables
systemctl start nftables

# 工作目录
WORKDIR="/app/nft-cn-block"
mkdir -p $WORKDIR
cd $WORKDIR

# ------------------------------
# 下载 APNIC 数据并生成 IP 列表
# ------------------------------
echo "===> 下载 APNIC IP 数据"
wget -q https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest -O apnic.txt

echo "===> 生成中国 IPv4 列表"
grep '|CN|ipv4|' apnic.txt | awk -F'|' '{printf("%s/%d\n",$4,32-log($5)/log(2))}' > cn_ipv4.txt

echo "===> 生成中国 IPv6 列表"
grep '|CN|ipv6|' apnic.txt | awk -F'|' '{print $4 "/" $5}' > cn_ipv6.txt

# ------------------------------
# 写入 nftables 配置
# ------------------------------
cat > /etc/nftables.conf << 'EOF'
#!/usr/sbin/nft -f

flush ruleset

table inet filter {

    set cn_ipv4 {
        type ipv4_addr
        flags interval
        auto-merge
    }

    set cn_ipv6 {
        type ipv6_addr
        flags interval
        auto-merge
    }

    chain input {
        # 1️⃣ 允许本地回环
        iif lo accept

        # 2️⃣ 允许已建立连接
        ct state established,related accept

        # 3️⃣ 白名单 IP，只允许访问 21/22，非指定端口自动走默认 drop
        ip saddr {1.1.1.1, 2.2.2.2} tcp dport {21, 22} accept

        # 4️⃣ 中国 IP（非白名单）全部拒绝
        ip saddr @cn_ipv4 drop
        ip6 saddr @cn_ipv6 drop

        # 5️⃣ 海外 IP，只允许访问 21/22
        tcp dport {21, 22} accept

        # 6️⃣ ICMP（可选）
        ip protocol icmp accept
        ip6 nexthdr icmpv6 accept

        # 7️⃣ 默认拒绝（核心）
        drop
    }
}
EOF

echo "===> 应用 nftables 规则"
nft -f /etc/nftables.conf

# ------------------------------
# 导入 IPv4 / IPv6 CN IP
# ------------------------------
# 自动导入 CN IP 集合
# nft -f /etc/nftables.conf 后立即执行 add element 导入
# 修改配置或重载规则不会让屏蔽失效
echo "===> 导入 CN IP 到 nftables"
for ipfile in cn_ipv4.txt cn_ipv6.txt; do
    setname=$(basename $ipfile .txt)
    while read -r ip; do
        nft add element inet filter "$setname" { "$ip" }
    done < "$ipfile"
done

# ------------------------------
# 创建自动更新脚本
# ------------------------------
cat > "$WORKDIR/update.sh" << 'EOF'
#!/bin/bash
set -e
cd /app/nft-cn-block

echo "===> 更新 APNIC 数据"
wget -q https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest -O apnic.txt

echo "===> 重新生成 CN IPv4 / IPv6 列表"
grep '|CN|ipv4|' apnic.txt | awk -F'|' '{printf("%s/%d\n",$4,32-log($5)/log(2))}' > cn_ipv4.txt
grep '|CN|ipv6|' apnic.txt | awk -F'|' '{print $4 "/" $5}' > cn_ipv6.txt

echo "===> 清空旧 IP 集合"
nft flush set inet filter cn_ipv4
nft flush set inet filter cn_ipv6

echo "===> 导入 CN IP"
for ipfile in cn_ipv4.txt cn_ipv6.txt; do
    setname=$(basename $ipfile .txt)
    while read -r ip; do
        nft add element inet filter "$setname" { "$ip" }
    done < "$ipfile"
done

echo "===> 更新完成: $(date)"
EOF

chmod +x "$WORKDIR/update.sh"

# ------------------------------
# 添加定时任务
# ------------------------------
# 自动更新
# update.sh 会刷新 APNIC 数据并重新导入集合
# 每天 3 点执行
TMP_CRON=$(mktemp)
crontab -l 2>/dev/null > "$TMP_CRON" || true
if ! grep -Fq "$WORKDIR/update.sh" "$TMP_CRON"; then
    echo "0 3 * * * $WORKDIR/update.sh" >> "$TMP_CRON"
fi
crontab "$TMP_CRON"
rm -f "$TMP_CRON"

# ------------------------------
# 部署完成提示
# ------------------------------
# 提取允许的 TCP 端口
ALLOWED_PORTS=$(grep -oP 'tcp dport\s*{[^}]+}' /etc/nftables.conf | sed -E 's/tcp dport\s*{([^}]+)}/\1/' | tr -d ' ')
echo "===> 部署完成 ✅"
echo "-----------------------------------"
echo "✔ 已屏蔽 CN IPv4 + IPv6"
echo "✔ 数据目录: $WORKDIR"
echo "✔ 自动每日更新"
echo "✔ 已允许访问的端口: $ALLOWED_PORTS"
echo "-----------------------------------"
